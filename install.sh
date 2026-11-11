#!/usr/bin/env bash
# 多AI交接桥 · 安装脚本（在 WSL 内、仓库根目录运行：  bash install.sh）
# 依赖：WSL2 内的 python3、node（采集器用）；Windows 侧仅 start-bridge.cmd 需要 wsl.exe。
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "[1/4] 创建 Python 虚拟环境（$HOME/handoff-bridge-venv）"
python3 -m venv "$HOME/handoff-bridge-venv"
"$HOME/handoff-bridge-venv/bin/pip" -q install -r "$REPO_DIR/requirements.txt" \
  || { echo "pip 安装失败：网络受限时先 export HTTPS_PROXY=http://127.0.0.1:<端口> 后重试"; exit 1; }
echo "[2/4] 写入环境文件 $HOME/handoff-bridge.env"
cat > "$HOME/handoff-bridge.env" <<ENV
HB_BACKEND_DIR="$REPO_DIR/backend"
HB_VENV_PY="$HOME/handoff-bridge-venv/bin/python"
ENV
echo "[3/4] 下载嵌入模型（int8，约 112MB；--fp32 可换高精度版）"
bash "$REPO_DIR/download-model.sh" "$@"
echo "[4/4] 自检"
( cd "$REPO_DIR/backend" && "$HOME/handoff-bridge-venv/bin/python" -c "import config, store, embedder; print('import OK · store =', config.STORE_PROVIDER, '· embed =', config.EMBEDDING_PROVIDER)" )
cat <<'NEXT'

安装完成。下一步：
  1) 采集本机 agent 公开会话（内置 DeepSeek Harness / WorkBuddy / Codex CLI 三类）：
       node ingest/export-corpus.mjs --out ingest/out/corpus.jsonl
  2) 导入检索库（首次全量）：
       "$HOME/handoff-bridge-venv/bin/python" backend/importer.py
  3) 启动控制台（可选，人工检查用）：
       Windows 侧双击 start-bridge.cmd  ->  http://localhost:8765
  4) 让 AI 直接调用（推荐，免启动）：见 README「MCP 接入」一节
NEXT
