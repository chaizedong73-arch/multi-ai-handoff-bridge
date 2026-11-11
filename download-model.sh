#!/usr/bin/env bash
# 下载嵌入模型到数据目录（默认 int8 约 112MB；bash download-model.sh --fp32 换高精度 fp32 约 470MB，
# fp32 与原版 sentence-transformers 语义逐位一致、评测高约 1 分，需同时设
# EMBEDDING_ONNX_PATH=<数据目录>/onnx/model.onnx）。
set -e
BASE="https://github.com/chaizedong73-arch/multi-ai-handoff-bridge/releases/download/v0.7.2"
DEST="${MULTI_AI_ONNX_DIR:-$HOME/multi-ai-data/onnx}"
mkdir -p "$DEST"
fetch() { [ -f "$DEST/$2" ] || { echo "下载 $2"; curl -fL --retry 3 -o "$DEST/$2" "$BASE/$2"; }; }
fetch tokenizer.json tokenizer.json
fetch model.int8.onnx model.int8.onnx
if [ "$1" = "--fp32" ]; then
  fetch model.onnx model.onnx
  fetch model.onnx.data model.onnx.data
fi
echo "模型目录：$DEST"; ls -la "$DEST"
