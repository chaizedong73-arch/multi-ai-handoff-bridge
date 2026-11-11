"""Embedding 抽象：真实语义向量优先，纯本地哈希兜底，API 可选。

provider（config.EMBEDDING_PROVIDER）：
- local/auto/st（默认）：用 sentence-transformers 多语模型（默认 paraphrase-multilingual-
  MiniLM-L12-v2，384 维，中文可用）产生真实语义向量；模型不可用时退回 _local_embed 哈希。
- onnx：int8 量化 ONNX + onnxruntime CPU 推理（② 内存足迹）。运行时只 import
  tokenizers+onnxruntime+numpy，绝不 import torch，从而绕开 torch 常驻的 ~1240MB。
  语义与 ST 同模型一致（int8 动态量化 cosine≥0.975）；载模型失败时退回 _st_embed。
- api：OpenAI 兼容 /embeddings（需 EMBEDDING_API_BASE，Key 仅走环境）。
- hash：强制旧的确定性哈希（离线/测试用，无真实语义）。

维度统一 = config.EMBEDDING_DIM（384），与集合 HNSW 配置对齐；不足补零、超出截断后归一化。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import List

import config

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_ST_MODEL = None
_ST_FAILED = False

_ONNX_SESSION = None
_ONNX_TOK = None
_ONNX_FAILED = False


def _features(text: str) -> List[str]:
    low = text.lower()
    feats: List[str] = _TOKEN_RE.findall(low)
    for run in _CJK_RE.findall(low):
        for i in range(len(run) - 1):
            feats.append(run[i : i + 2])
    return feats


def _hash_embed_one(text: str, dim: int) -> List[float]:
    v = [0.0] * dim
    for f in _features(text):
        h = int(hashlib.md5(f.encode("utf-8")).hexdigest(), 16)
        bucket = h % dim
        sign = 1.0 if (h >> 64) & 1 else -1.0
        v[bucket] += sign
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _local_embed(texts: List[str], dim: int) -> List[List[float]]:
    return [_hash_embed_one(t, dim) for t in texts]


def _get_st_model():
    """惰性加载 sentence-transformers 多语模型；失败则标记，后续直接走哈希兜底。"""
    global _ST_MODEL, _ST_FAILED
    if _ST_MODEL is not None:
        return _ST_MODEL
    if _ST_FAILED:
        return None
    try:
        from sentence_transformers import SentenceTransformer

        name = config.EMBEDDING_MODEL or "paraphrase-multilingual-MiniLM-L12-v2"
        _ST_MODEL = SentenceTransformer(name)
        return _ST_MODEL
    except Exception:
        _ST_FAILED = True
        _ST_MODEL = None
        return None


def _st_embed(texts: List[str], dim: int) -> List[List[float]]:
    model = _get_st_model()
    if model is None:
        return _local_embed(texts, dim)
    vecs = model.encode(
        texts, normalize_embeddings=True, batch_size=64,
        show_progress_bar=False, convert_to_numpy=True,
    )
    out: List[List[float]] = []
    for v in vecs:
        v = [float(x) for x in v]
        if len(v) >= dim:
            v = v[:dim]
        else:
            v = v + [0.0] * (dim - len(v))
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


def _get_onnx():
    """惰性加载 int8-ONNX 会话 + tokenizer（进程内单例）。

    关键：SessionOptions 把 intra/inter-op 线程钉到 1，否则默认 16 线程会让
    CPU arena 峰值从 ~666MB 涨回 ~1676MB（见 onnx_measure.py）。失败标记后
    不再重试，调用方退回 _st_embed。
    """
    global _ONNX_SESSION, _ONNX_TOK, _ONNX_FAILED
    if _ONNX_SESSION is not None:
        return _ONNX_SESSION, _ONNX_TOK
    if _ONNX_FAILED:
        return None, None
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = config.ONNX_INTRA_THREADS
        so.inter_op_num_threads = config.ONNX_INTER_THREADS
        sess = ort.InferenceSession(
            config.EMBEDDING_ONNX_PATH, sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        tok = Tokenizer.from_file(config.EMBEDDING_TOKENIZER_PATH)
        tok.enable_truncation(max_length=config.ONNX_MAX_SEQ)
        _ONNX_SESSION = sess
        _ONNX_TOK = tok
        return sess, tok
    except Exception:
        _ONNX_FAILED = True
        _ONNX_SESSION = None
        _ONNX_TOK = None
        return None, None


def _onnx_embed_batch(session, tok, batch, dim):
    """一批文本 -> 归一化向量。复刻 ST：按批内最长右填充，mean-pool over
    attention mask，再 L2 归一化；token_type_ids 全 0。"""
    import numpy as np

    encs = [tok.encode(t) for t in batch]
    maxlen = max(len(e.ids) for e in encs)
    maxlen = max(maxlen, 2)
    ids, mask, tt = [], [], []
    for e in encs:
        pad = maxlen - len(e.ids)
        ids.append(e.ids + [0] * pad)
        mask.append([1] * len(e.ids) + [0] * pad)
        tt.append([0] * len(e.ids) + [0] * pad)
    out = session.run(["last_hidden_state"], {
        "input_ids": np.array(ids, dtype="int64"),
        "attention_mask": np.array(mask, dtype="int64"),
        "token_type_ids": np.array(tt, dtype="int64"),
    })[0]  # [b,s,384]
    m = np.array(mask, dtype="float32")[:, :, None]
    pooled = (out * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
    norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
    vecs = pooled / norms
    result = []
    for v in vecs:
        v = [float(x) for x in v]
        if len(v) >= dim:
            v = v[:dim]
        else:
            v = v + [0.0] * (dim - len(v))
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        result.append([x / norm for x in v])
    return result


def _onnx_embed(texts: List[str], dim: int) -> List[List[float]]:
    sess, tok = _get_onnx()
    if sess is None:
        if getattr(config, "EMBEDDING_ONNX_STRICT", False):
            return _local_embed(texts, dim)  # light 后端红线：宁可降质，不碰 torch
        return _st_embed(texts, dim)
    batch = max(1, config.ONNX_BATCH)
    out: List[List[float]] = []
    for i in range(0, len(texts), batch):
        out.extend(_onnx_embed_batch(sess, tok, texts[i : i + batch], dim))
    return out


def _api_embed(texts: List[str], dim: int) -> List[List[float]]:
    import httpx

    base = config.EMBEDDING_API_BASE.rstrip("/")
    url = f"{base}/embeddings" if base else "https://api.openai.com/v1/embeddings"
    headers = {"Content-Type": "application/json"}
    if config.EMBEDDING_API_KEY:
        headers["Authorization"] = f"Bearer {config.EMBEDDING_API_KEY}"
    payload = {"model": config.EMBEDDING_MODEL or "text-embedding-3-small", "input": texts}
    with httpx.Client(timeout=30) as client:
        r = client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()["data"]
    vecs = [[float(x) for x in d["embedding"]] for d in data]
    out = []
    for v in vecs:
        if len(v) >= dim:
            v = v[:dim]
        else:
            v = v + [0.0] * (dim - len(v))
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


def embed(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    dim = config.EMBEDDING_DIM
    provider = (config.EMBEDDING_PROVIDER or "local").lower()
    if provider == "hash":
        return _local_embed(texts, dim)
    if provider == "onnx":
        return _onnx_embed(texts, dim)
    if provider == "api" and config.EMBEDDING_API_BASE:
        try:
            return _api_embed(texts, dim)
        except Exception:
            return _st_embed(texts, dim)
    return _st_embed(texts, dim)

