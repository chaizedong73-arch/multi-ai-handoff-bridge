#!/usr/bin/env python3
"""② 构建：把本地缓存的 paraphrase-multilingual-MiniLM-L12-v2 导出成 ONNX 并动态 int8 量化。

一次性离线构建（torch 只在这一步用来导出，运行时不需要 torch）。产物落 WSL 原生目录
（config.DATA_DIR/onnx），避开 /mnt/c。导出后用 onnxruntime.quantization 做 weight-only
动态 int8 量化（BERT 的 MatMul/Gemm 权重 → int8），运行时峰值内存大幅下降。

不联网：模型从 HF 本地缓存快照读，local_files_only=True。
"""
from __future__ import annotations
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
OPSET = 17


def _snapshot_dir() -> str:
    # models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2
    base = os.path.expanduser("~/.cache/huggingface/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/snapshots")
    snaps = glob.glob(os.path.join(base, "*"))
    if not snaps:
        raise SystemExit(f"未找到本地模型快照：{base}")
    return snaps[0]


def main() -> int:
    import torch
    from transformers import AutoModel
    snap = _snapshot_dir()
    outdir = os.path.join(os.path.expanduser("~/multi-ai-data"), "onnx")
    os.makedirs(outdir, exist_ok=True)
    fp32 = os.path.join(outdir, "model.onnx")
    print(f"[onnx_build] snapshot={snap}")
    print(f"[onnx_build] outdir={outdir}")

    # 1) 加载 BertModel（transformers），禁用 return_dict 以便 ONNX 直接拿 last_hidden_state
    torch.set_grad_enabled(False)
    model = AutoModel.from_pretrained(snap, local_files_only=True)
    model.eval()
    try:
        model.config.use_cache = False
    except Exception:
        pass

    # 2) 动态轴导出：batch 与 seq 可变；输出 last_hidden_state [b,s,384]
    ids = torch.ones(1, 8, dtype=torch.long)
    mask = torch.ones(1, 8, dtype=torch.long)
    type_ids = torch.zeros(1, 8, dtype=torch.long)
    dynamic = {
        "input_ids": {0: "batch", 1: "seq"},
        "attention_mask": {0: "batch", 1: "seq"},
        "token_type_ids": {0: "batch", 1: "seq"},
        "last_hidden_state": {0: "batch", 1: "seq"},
    }

    class Wrapper(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask, token_type_ids):
            out = self.m(input_ids=input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)
            return out.last_hidden_state

    wrapper = Wrapper(model)
    torch.onnx.export(
        wrapper, (ids, mask, type_ids), fp32,
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["last_hidden_state"],
        dynamic_axes=dynamic, opset_version=OPSET, do_constant_folding=True,
    )
    sz = round(os.path.getsize(fp32) / 1024 / 1024, 1)
    print(f"[onnx_build] fp32 onnx written={fp32} size={sz}MB")

    # 3) 动态 int8 量化（weight-only）
    from onnxruntime.quantization import QuantType, quantize_dynamic
    int8 = os.path.join(outdir, "model.int8.onnx")
    quantize_dynamic(fp32, int8, weight_type=QuantType.QInt8)
    sz8 = round(os.path.getsize(int8) / 1024 / 1024, 1)
    print(f"[onnx_build] int8 onnx written={int8} size={sz8}MB")

    meta = {"snapshot": snap, "opset": OPSET, "fp32_mb": sz, "int8_mb": sz8,
            "fp32_path": fp32, "int8_path": int8}
    with open(os.path.join(outdir, "build-meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[onnx_build] DONE {json.dumps(meta, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
