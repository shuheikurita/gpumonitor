#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import threading
import time
import tomllib
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data" / "history.json"

def load_config() -> dict:
    config_path = BASE_DIR / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"config.toml not found. Copy config.toml.example to config.toml and edit it.")
    with open(config_path, "rb") as f:
        return tomllib.load(f)

cfg = load_config()

_ssh_key_raw = cfg["ssh"]["key"]
SSH_KEY  = str(BASE_DIR / _ssh_key_raw) if not Path(_ssh_key_raw).is_absolute() else _ssh_key_raw
SSH_USER = cfg["ssh"]["user"]
HISTORY_HOURS    = cfg["app"].get("history_hours", 24)
COLLECT_INTERVAL = cfg["app"].get("collect_interval", 60)
PORT             = cfg["app"].get("port", 8080)
SERVERS          = cfg.get("servers", [])

app = Flask(__name__, static_folder=str(BASE_DIR / "static"))
data_lock = threading.Lock()


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    tmp = str(DATA_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, DATA_FILE)


def read_server_list() -> list[dict]:
    return SERVERS


def ssh_run(ip: str, command: str, timeout: int = 30) -> str | None:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", SSH_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes",
                "-o", "LogLevel=ERROR",
                f"{SSH_USER}@{ip}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        log.warning(f"SSH {ip} exit {result.returncode}: {result.stderr.strip()[:200]}")
        return None
    except subprocess.TimeoutExpired:
        log.warning(f"SSH timeout: {ip}")
        return None
    except Exception as e:
        log.error(f"SSH error {ip}: {e}")
        return None


def parse_sys(section: str) -> dict | None:
    """Parse two /proc/stat cpu lines (1s apart) + /proc/meminfo into usage percentages."""
    cpu_samples = []
    mem = {}
    for line in section.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            try:
                cpu_samples.append([int(x) for x in parts[1:]])
            except ValueError:
                pass
        elif parts[0] == "MemTotal:":
            mem["total"] = int(parts[1])  # kB
        elif parts[0] == "MemAvailable:":
            mem["available"] = int(parts[1])
    if len(cpu_samples) < 2 or "total" not in mem or "available" not in mem or mem["total"] <= 0:
        return None

    def busy_total(f: list[int]) -> tuple[int, int]:
        total = sum(f)
        idle = f[3] + (f[4] if len(f) > 4 else 0)  # idle + iowait
        return total - idle, total

    b1, t1 = busy_total(cpu_samples[0])
    b2, t2 = busy_total(cpu_samples[-1])
    dt = t2 - t1
    used_kb = mem["total"] - mem["available"]
    return {
        "cpu_percent": round((b2 - b1) / dt * 100, 1) if dt > 0 else 0.0,
        "mem_percent": round(used_kb / mem["total"] * 100, 1),
        "mem_total": mem["total"] // 1024,  # MiB
        "mem_used": used_kb // 1024,        # MiB
    }


def collect_server(server: dict) -> tuple[str, list | None, dict | None]:
    ip = server["ip"]
    name = server["name"]
    has_gpu = server.get("gpu", True)

    # Single call: GPU info + process info + PID owners + CPU/RAM info separated
    # by sentinels. Everything happens in ONE ssh connection per cycle: on a
    # host whose auth path is stalled (e.g. NFS home), every extra connection
    # attempt can leave a stuck sshd child occupying a MaxStartups slot.
    gpu_cmd = (
        "nvidia-smi --query-gpu=index,uuid,memory.total,memory.used,name,utilization.gpu"
        " --format=csv,noheader,nounits 2>/dev/null;"
        " echo '===PROCS===';"
        " nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory"
        " --format=csv,noheader,nounits 2>/dev/null;"
        " echo '===USERS===';"
        " pids=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null | paste -sd, -);"
        " if [ -n \"$pids\" ]; then ps -o pid=,user= -p \"$pids\" 2>/dev/null; fi;"
        if has_gpu
        else "echo '===PROCS==='; echo '===USERS===';"
    )
    cmd = (
        gpu_cmd
        + " echo '===SYS===';"
        " head -1 /proc/stat;"
        " grep -E 'MemTotal|MemAvailable' /proc/meminfo;"
        " sleep 1;"
        " head -1 /proc/stat"
    )
    output = ssh_run(ip, cmd)
    if output is None:
        log.warning(f"{name} ({ip}): unreachable")
        return name, None, None

    sections = output.split("===PROCS===")
    gpu_section = sections[0].strip()
    rest = sections[1] if len(sections) > 1 else ""
    rest_sections = rest.split("===USERS===")
    proc_section = rest_sections[0].strip()
    rest2 = rest_sections[1] if len(rest_sections) > 1 else ""
    rest2_sections = rest2.split("===SYS===")
    users_section = rest2_sections[0].strip()
    sys_info = parse_sys(rest2_sections[1]) if len(rest2_sections) > 1 else None

    # Parse GPUs: index, uuid, total_MiB, used_MiB
    gpus: dict[str, dict] = {}
    for line in gpu_section.splitlines():
        p = [x.strip() for x in line.split(",", 5)]
        if len(p) == 6:
            try:
                gpus[p[1]] = {
                    "index": int(p[0]),
                    "memory_total": int(p[2]),
                    "memory_used": int(p[3]),
                    "name": p[4],
                    "gpu_util": int(p[5]),
                    "processes": [],
                }
            except ValueError:
                pass

    # Parse processes: pid, gpu_uuid, used_MiB
    procs = []
    for line in proc_section.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 3:
            try:
                procs.append({"pid": p[0], "uuid": p[1], "memory": int(p[2])})
            except ValueError:
                pass

    # PID -> username (resolved remotely within the same ssh call)
    pid_to_user: dict[str, str] = {}
    for line in users_section.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            pid_to_user[parts[0]] = parts[1]

    # Assign users to GPU processes, merging same user on same GPU
    for proc in procs:
        uuid = proc["uuid"]
        if uuid not in gpus:
            continue
        user = pid_to_user.get(proc["pid"], "unknown")
        existing = next(
            (x for x in gpus[uuid]["processes"] if x["user"] == user), None
        )
        if existing:
            existing["memory"] += proc["memory"]
        else:
            gpus[uuid]["processes"].append({"user": user, "memory": proc["memory"]})

    gpu_list = sorted(gpus.values(), key=lambda g: g["index"])
    if has_gpu:
        log.info(f"{name}: {len(gpu_list)} GPU(s) collected")
    else:
        log.info(f"{name}: sys info collected (no GPU)")
    return name, gpu_list, sys_info


# Per-server failure backoff. A node whose home filesystem (e.g. NFS) is
# stalled still accepts TCP but hangs in auth reading authorized_keys; each
# attempt leaves a D-state sshd child that keeps a MaxStartups slot occupied,
# so retrying every cycle can lock everyone out of ssh on the sick node.
# Backing off caps how fast stuck slots pile up.
BACKOFF_BASE = 120   # seconds after first failure
BACKOFF_MAX  = 1800
_fail_state: dict[str, dict] = {}  # name -> {"fails": int, "next_try": float}


def collect_all() -> None:
    log.info("Collection cycle start")
    servers = read_server_list()
    now = int(time.time())
    cutoff = now - HISTORY_HOURS * 3600

    with data_lock:
        data = load_data()

    for server in servers:
        name = server["name"]
        st = _fail_state.get(name)
        if st and time.time() < st["next_try"]:
            log.info(
                f"{name}: skipped (backoff after {st['fails']} failure(s),"
                f" retry in {int(st['next_try'] - time.time())}s)"
            )
            continue

        name, gpu_list, sys_info = collect_server(server)

        if gpu_list is None:
            fails = (st["fails"] if st else 0) + 1
            delay = min(BACKOFF_BASE * 2 ** (fails - 1), BACKOFF_MAX)
            _fail_state[name] = {"fails": fails, "next_try": time.time() + delay}
            log.warning(f"{name}: collection failed ({fails}x), backing off {delay}s")
        elif st:
            del _fail_state[name]
            log.info(f"{name}: recovered after {st['fails']} failure(s)")

        if name not in data:
            data[name] = {"ip": server["ip"], "history": []}
        data[name]["gpu"] = server.get("gpu", True)
        data[name]["history"] = [
            h for h in data[name]["history"] if h["timestamp"] > cutoff
        ]
        if gpu_list is not None:
            entry = {"timestamp": now, "gpus": gpu_list}
            if sys_info is not None:
                entry["sys"] = sys_info
            data[name]["history"].append(entry)

    with data_lock:
        save_data(data)
    log.info("Collection cycle done")


def scheduler_loop() -> None:
    while True:
        try:
            collect_all()
        except Exception as e:
            log.error(f"Collection failed: {e}")
        time.sleep(COLLECT_INTERVAL)


@app.route("/api/data")
def api_data():
    with data_lock:
        data = load_data()
    servers = read_server_list()
    ordered = {s["name"]: data[s["name"]] for s in servers if s["name"] in data}
    return jsonify(ordered)


@app.route("/api/servers")
def api_servers():
    return jsonify(read_server_list())



@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    DATA_FILE.parent.mkdir(exist_ok=True)
    try:
        os.chmod(SSH_KEY, 0o600)
    except Exception:
        pass
    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
