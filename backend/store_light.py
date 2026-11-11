#!/usr/bin/env python3
"""light store：零 torch 的检索后端（0.7.0 内存优化）。

为什么存在：pyseekdb 一旦 import 就拉进 torch（~569MB 地板）+ 集合打开时加载
关不掉的默认嵌入 EF（→885MB）——0.4.4 实测结论（vault 学习清单 Part C）。
而本项目检索实际走的是「全量扫描 + numpy 融合」（store._hybrid_full），seekdb 的
ANN 索引从未成为必选项。故数据面换成：docs.jsonl（原文+元数据）+ emb.npy
（归一化 float32 矩阵）+ numpy 暴力融合；嵌入用 int8-ONNX（0.4.4 已证语义等价）。
全链只 import numpy / onnxruntime / tokenizers，绝不 import torch / pyseekdb /
sentence_transformers（onnx 加载失败时退哈希向量，config.EMBEDDING_ONNX_STRICT）。

与 seekdb 版（store.py 本体）的契约：
- 公开函数签名一致：import_corpus / reset_collection / hybrid_search / find_by_file /
  list_sessions / stats / _scan_all，调用方（main / dispatch / catchup /
  handoff_brief / bridge_api）零改动（store.py 底部按 MULTI_AI_STORE 重绑定）。
- 语义对齐：内容哈希 id upsert（重复导入=覆盖不重复，配合 catchup 水位线
  「至少一次+幂等」）；keyword / citation / 过滤 / 融合打分直接复用 store.py 的
  同一实现（lazy import，一个事实只存一处）。

落盘（DATA_DIR/light/）：docs.jsonl + emb.npy，行序=矩阵行序，tmp+os.replace 原子写。
进程缓存按文件 (size,mtime) 失效：CLI 懒补新事件后，常驻控制台下一次查询自动看到新数据。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
import embedder

try:
    import numpy as np
except Exception:  # numpy 缺失时 light 后端不可用（venv 必有；守卫仅为防裸解释器）
    np = None

_lock = threading.RLock()
_cache: Dict = {"key": None, "ids": [], "docs": [], "metas": [], "mat": None, "count": 0}
_EMBED_BATCH = 256


def _dir() -> Path:
    return Path(config.DATA_DIR) / "light"


def _docs_path() -> Path:
    return _dir() / "docs.jsonl"


def _emb_path() -> Path:
    return _dir() / "emb.npy"


def _file_key() -> Tuple:
    def k(p: Path):
        try:
            st = p.stat()
            return (st.st_size, int(st.st_mtime))
        except Exception:
            return None
    return (k(_docs_path()), k(_emb_path()))


def _ensure_loaded(force: bool = False) -> None:
    """载入 docs+矩阵到进程缓存；文件 (size,mtime) 变化即重载。"""
    key = _file_key()
    if not force and _cache["key"] == key:
        return
    dp, ep = _docs_path(), _emb_path()
    if np is None or not dp.exists():
        _cache.update(key=key, ids=[], docs=[], metas=[], mat=None, count=0)
        return
    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict] = []
    with open(dp, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            ids.append(o.get("id", ""))
            docs.append(o.get("text", ""))
            metas.append(o.get("meta") or {})
    mat = None
    if ep.exists():
        arr = np.load(ep, mmap_mode="r")
        if arr.shape[0] == len(ids) and arr.shape[0] > 0:
            mat = np.asarray(arr, dtype=np.float32)  # mmap 常驻，检索时零拷贝读
        else:  # 矩阵与行数不一致 = 损坏，宁可空结果也不错检索
            mat = None
    _cache.update(key=key, ids=ids, docs=docs, metas=metas, mat=mat, count=len(ids))


def _persist(rows: List[Tuple[str, str, Dict]]) -> None:
    """原子写 docs.jsonl + emb.npy。rows=[(id, text, meta, emb)]，emb 已是向量或 None→零向量。"""
    if np is None:
        raise RuntimeError("light store 需要 numpy（venv 应自带）")
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    dp, ep = _docs_path(), _emb_path()
    tmp_d, tmp_e = str(dp) + ".tmp", str(ep) + ".tmp"
    with open(tmp_d, "w", encoding="utf-8") as f:
        for _id, text, meta, _emb in rows:
            f.write(json.dumps({"id": _id, "text": text, "meta": meta}, ensure_ascii=False) + "\n")
    mat = np.zeros((max(1, len(rows)), config.EMBEDDING_DIM), dtype=np.float32)
    for i, (_id, _text, _meta, emb) in enumerate(rows):
        v = np.asarray(emb if emb is not None else 0.0, dtype=np.float32)[: config.EMBEDDING_DIM]
        n = float(np.linalg.norm(v)) or 1.0
        mat[i, : len(v)] = v / n
    if not rows:
        mat = np.zeros((0, config.EMBEDDING_DIM), dtype=np.float32)
    with open(tmp_e, "wb") as f:
        np.save(f, mat)
    os.replace(tmp_d, dp)
    os.replace(tmp_e, ep)


# ---------------- 导入（对应 seekdb 版 import_corpus / reset_collection） ----------------


def reset_collection() -> None:
    """清空 light 存储（全量重导入前）。"""
    with _lock:
        for p in (_docs_path(), _emb_path()):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        _cache.update(key=None, ids=[], docs=[], metas=[], mat=None, count=0)


def import_corpus(jsonl_path: Optional[str] = None, reset: bool = True) -> Dict:
    """读语料 JSONL → store.build_doc → 按 id upsert → 只嵌新增/变化行 → 原子落盘。

    reset=True：清空后全量导入（catchup --full / 首灌）。
    reset=False：增量 upsert（catchup 增量懒补）；同 id 文本或元数据变化才重嵌，
    未变化的行保留旧向量（增量只省嵌入计算，与 seekdb upsert 覆盖语义一致）。
    """
    import store as _s

    p = Path(jsonl_path or config.CORPUS_PATH)
    if not p.exists():
        raise FileNotFoundError(f"语料文件不存在：{p}（先运行 Node ingest：node export-corpus.mjs）")
    with _lock:
        _ensure_loaded(force=not reset)
        order: List[str] = []
        rows: Dict[str, List] = {}
        if not reset:
            for i, _id in enumerate(_cache["ids"]):
                emb = None
                if _cache["mat"] is not None:
                    emb = np.array(_cache["mat"][i], dtype=np.float32)  # 拷贝，脱离旧 mmap
                rows[_id] = [_cache["docs"][i], _cache["metas"][i], emb]
                order.append(_id)
        ids, docs, metas = [], [], []
        skipped = 0
        by_kind = {"message": 0, "change": 0, "session": 0}
        with open(p, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    skipped += 1
                    continue
                doc = _s.build_doc(rec)
                if doc is None:
                    skipped += 1
                    continue
                ids.append(doc["id"])
                docs.append(doc["text"])
                metas.append(doc["metadata"])
                by_kind[doc["metadata"]["kind"]] = by_kind.get(doc["metadata"]["kind"], 0) + 1
        changed = 0
        for _id, text, meta in zip(ids, docs, metas):
            old = rows.get(_id)
            if old is None:
                rows[_id] = [text, meta, None]
                order.append(_id)
            elif old[0] != text or old[1] != meta:
                old[0], old[1], old[2] = text, meta, None
                changed += 1
        need = [j for j, _id in enumerate(order) if rows[_id][2] is None]
        for s in range(0, len(need), _EMBED_BATCH):
            sel = need[s : s + _EMBED_BATCH]
            embs = embedder.embed([rows[order[j]][0] for j in sel])
            for j, emb in zip(sel, embs):
                rows[order[j]][2] = emb
        _persist([( _id, rows[_id][0], rows[_id][1], rows[_id][2]) for _id in order])
        _ensure_loaded(force=True)
        written = len(ids)
    return {
        "ok": True, "corpus": str(p), "written": written, "skipped": skipped,
        "byKind": by_kind, "collectionCount": len(order), "updated": changed,
        "importedAt": int(time.time()),
        "storePath": str(_dir()), "embeddingProvider": config.EMBEDDING_PROVIDER,
        "storeProvider": "light",
    }


# ---------------- 检索（对应 seekdb 版 hybrid_search / find_by_file / list_sessions） ----------------


def hybrid_search(
    query: str,
    owner: Optional[str] = None,
    kind: Optional[str] = None,
    sessionId: Optional[str] = None,
    top_k: Optional[int] = None,
    after: Optional[float] = None,
):
    """与 seekdb 版同语义的全量融合检索：复用 store._fuse_full 同一份打分实现。

    after（0.7.2）：unix 秒时间窗下限，只返回 at ≥ after 的块；None = 全量（历史行为）。"""
    import store as _s

    k = top_k or config.SEARCH_TOP_K
    qvec = embedder.embed([query])[0]
    with _lock:
        _ensure_loaded()
        if not _cache["count"] or _cache["mat"] is None:
            return [], {"query": query, "filters": {"owner": owner, "kind": kind, "sessionId": sessionId, "after": after},
                        "mode": "empty", "candidates": 0, "results": []}
        return _s._fuse_full(
            _cache["ids"], _cache["docs"], _cache["metas"], _cache["mat"],
            qvec, query, owner, kind, sessionId, k, mode="light-full-hybrid", after=after)


def _scan_all(include: Optional[List[str]] = None) -> Dict:
    """全量 ids/documents/metadatas（进程缓存，handoff_brief._corpus_sessions 依赖）。"""
    with _lock:
        _ensure_loaded()
        return {"ids": list(_cache["ids"]), "documents": list(_cache["docs"]),
                "metadatas": list(_cache["metas"])}


def find_by_file(file_path: str, limit: int = 200) -> Dict:
    """文件反查：扫 change 块按 normalizedPath 精确/后缀匹配（复用 store 同一逻辑）。"""
    import store as _s

    target = _s.normalize_path(file_path)
    with _lock:
        _ensure_loaded()
        ids, docs, metas = list(_cache["ids"]), list(_cache["docs"]), list(_cache["metas"])
    matches = []
    for i, meta in enumerate(metas):
        if not meta or meta.get("kind") != "change":
            continue
        np_ = _s.normalize_path(meta.get("normalizedPath") or meta.get("path") or "")
        if not np_:
            continue
        exact = np_ == target
        suffix = (not exact) and (np_.endswith("/" + target) or target.endswith("/" + np_)
                                  or np_.endswith(target) or target.endswith(np_))
        if exact or suffix:
            matches.append({
                "id": ids[i] if i < len(ids) else "", "text": docs[i] if i < len(docs) else "",
                "exact": exact, "action": meta.get("action", ""), "status": meta.get("status", ""),
                "path": meta.get("path", ""), "normalizedPath": meta.get("normalizedPath", ""),
                "citation": _s._citation(meta),
            })
    matches.sort(key=lambda x: (not x["exact"], x["citation"].get("at") or 0))
    return {"file": file_path, "normalized": target, "matchCount": len(matches), "matches": matches[:limit]}


def list_sessions() -> List[Dict]:
    """全部会话摘要块（kind=session），按 mtime 倒序。"""
    with _lock:
        _ensure_loaded()
        metas = list(_cache["metas"])
    out = []
    for meta in metas:
        if meta and meta.get("kind") == "session":
            out.append({
                "sessionKey": meta.get("sessionKey", ""), "sessionId": meta.get("sessionId", ""),
                "owner": meta.get("owner", ""), "title": meta.get("sessionTitle", ""),
                "model": meta.get("model", ""), "cwd": meta.get("cwd", ""), "state": meta.get("state", ""),
                "msgCount": meta.get("msgCount", 0), "changeCount": meta.get("changeCount", 0),
                "mtime": meta.get("mtime", 0), "coverageComplete": meta.get("coverageComplete", False),
            })
    out.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return out


def stats() -> Dict:
    """集合统计。与 seekdb 版字段兼容（count/byKind/byOwner），额外带 storeProvider/storePath。"""
    with _lock:
        _ensure_loaded()
        metas = list(_cache["metas"])
        count = _cache["count"]
    by_kind = {"message": 0, "change": 0, "session": 0}
    by_owner: Dict[str, int] = {}
    for meta in metas:
        if not meta:
            continue
        kk = meta.get("kind")
        if kk in by_kind:
            by_kind[kk] += 1
        ow = meta.get("owner", "?")
        by_owner[ow] = by_owner.get(ow, 0) + 1
    return {"collection": config.COLLECTION_NAME, "count": count, "byKind": by_kind,
            "byOwner": by_owner, "storeProvider": "light", "storePath": str(_dir())}
