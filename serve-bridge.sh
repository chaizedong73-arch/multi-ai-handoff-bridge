#!/usr/bin/env bash
# Handoff Bridge 引擎启动器（被 start-bridge.cmd / 手动调用；install.sh 会写好环境文件）
set -e
[ -f "$HOME/handoff-bridge.env" ] && . "$HOME/handoff-bridge.env"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${HB_BACKEND_DIR:-$SELF_DIR/backend}"
VENV_PY="${HB_VENV_PY:-$HOME/handoff-bridge-venv/bin/python}"
export MULTI_AI_COLLECTION="${MULTI_AI_COLLECTION:-multi_ai_events}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
cd "$BACKEND_DIR"
exec "$VENV_PY" -m uvicorn main:app --host 0.0.0.0 --port "${BRIDGE_PORT:-8765}"
