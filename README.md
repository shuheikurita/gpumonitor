# GPU Monitor

A lightweight GPU monitoring dashboard that SSHes into a list of servers every minute, collects VRAM usage via `nvidia-smi`, and displays a live web UI with 24-hour history.

## Setup

**Requirements:** Python 3.11+, `uv`, SSH key with access to target servers

```bash
# Install dependencies
uv sync

# Configure
cp config.toml.example config.toml
# Edit config.toml: set SSH key path, username, port, and server list

# Run
uv run python app.py
```

Then open `http://<host>:<port>/` in your browser.

## Configuration

`config.toml` (not committed — copy from `config.toml.example`):

```toml
[app]
port = 8080
history_hours = 24
collect_interval = 60  # seconds

[ssh]
key = "id_rsa"          # relative to project root, or absolute path
user = "your_username"

[[servers]]
ip   = "192.168.1.10"
name = "gpu-node-01"
```

## Data

Collected data is stored in `data/history.json` (excluded from git). History older than `history_hours` is pruned automatically.
