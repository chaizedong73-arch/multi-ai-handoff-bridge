"""seekdb（嵌入式 pyseekdb）封装：跨 agent 语料写入、三路混合检索、文件反查、会话列举。

pyseekdb API 用法严格参照 RecallLoop（qoder/app/store.py）与仓库 minimax/ctxsdd/obstore.py 实测签名：
  Client(path=) / Schema(vector_index=HNSWConfiguration(dimension, distance="cosine"))
  col.upsert(ids, embeddings, metadatas, documents) / col.add(...)
  col.query(query_embeddings=[vec], n_results=k, where={...})
  col.delete(where={"field": ...}) / col.get(limit=, include=[...]) / col.count()

语料三类记录 → 三类可检索块（metadata.kind 区分）：
  message  公开对话（user/assistant），带 role/turn/srcFile:srcLine 引用
  change   结构化文件操作，带 action/status/normalizedPath 引用
  session  会话摘要（owner/title/model/cwd/state/计数）

检索三路融合（同 RecallLoop）：vector_sim = 1 - cosine_distance；keyword = 命中数/查询词数；
score = (1-w)*vector + w*keyword，w = KEYWORD_WEIGHT（默认 0.4）。结构化过滤（owner/kind/sessionId）
用单字段 where 下推 + 代码侧兜底。每条结果携带 citation 回查原始日志位置。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import config
import embedder

try:
    import numpy as _np
except Exception:  # numpy 随 pyseekdb 安装；缺失时退化为 ANN 候选路径
    _np = None

_lock = threading.RLock()  # 嵌入式 seekdb 客户端非线程安全：所有集合操作必须串行
_client = None
_collection = None

# 全量扫描内存缓存：hybrid_search / find_by_file / list_sessions 共用，导入或重建后失效。
_SCAN_MAX = 200000  # 超过此规模退回 ANN 候选路径，避免全量载入
_scan_cache = {"valid": False, "ids": [], "docs": [], "metas": [], "mat": None, "count": 0}

_STOP = set(
    """的 了 和 是 在 我 有 也 就 不 人 都 一 上 与 及 或 等 被 把 对 为 以 而 之 其 此 该 中 什么 如何 怎么 哪些 一个 可以 这个 那个 我们 你们 他们
a an the is are was were be been of to in on for and or not with as at by from it this that these those what how why which""".split()
)
_WORD_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")
_EMBED_BATCH = 256


# ---------------- seekdb 客户端 ----------------


def _get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection
    with _lock:
        if _collection is not None:
            return _collection
        import pyseekdb
        from pyseekdb import HNSWConfiguration, Schema

        config.ensure_dirs()
        db_path = Path(config.SEEKDB_PATH)
        db_path.mkdir(parents=True, exist_ok=True)
        _client = pyseekdb.Client(path=str(db_path))
        schema = Schema(vector_index=HNSWConfiguration(dimension=config.EMBEDDING_DIM, distance="cosine"))
        try:
            _collection = _client.get_collection(config.COLLECTION_NAME)
        except Exception:
            _client.create_collection(name=config.COLLECTION_NAME, schema=schema)
            _collection = _client.get_collection(config.COLLECTION_NAME)
    return _collection


def _write(col, ids, docs, embs, metas) -> None:
    if hasattr(col, "upsert"):
        col.upsert(ids=ids, documents=docs, embeddings=embs, metadatas=metas)
    else:
        col.add(ids=ids, documents=docs, embeddings=embs, metadatas=metas)


def reset_collection() -> None:
    """删除并重建集合（全量重导入前清库）。"""
    global _collection
    with _lock:
        col = _get_collection()
        try:
            _client.delete_collection(config.COLLECTION_NAME)
        except Exception:
            pass
        from pyseekdb import HNSWConfiguration, Schema

        schema = Schema(vector_index=HNSWConfiguration(dimension=config.EMBEDDING_DIM, distance="cosine"))
        _client.create_collection(name=config.COLLECTION_NAME, schema=schema)
        _collection = _client.get_collection(config.COLLECTION_NAME)
        _scan_cache["valid"] = False


# ---------------- 语料 → 可检索块 ----------------


def _digest(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _meta_scalars(m: Dict) -> Dict:
    """pyseekdb metadata 仅接受标量；统一转 str/int/float/bool，None→""。"""
    out = {}
    for k, v in m.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def build_doc(rec: Dict) -> Optional[Dict]:
    """把一条语料记录映射为 {id, text, metadata}；不可检索的记录返回 None。"""
    t = rec.get("type")
    owner = rec.get("owner", "")
    skey = rec.get("sessionKey") or rec.get("key") or ""
    if t == "message":
        text = rec.get("text") or ""
        if not text.strip():
            return None
        role = rec.get("role", "")
        doc_text = f"[{owner}/{role}] {text}"
        did = "msg_" + _digest("msg", skey, rec.get("srcLine"), rec.get("turn"), role, rec.get("at"), text[:64])
        meta = {
            "kind": "message", "owner": owner, "sessionId": rec.get("sessionId", ""), "sessionKey": skey,
            "sessionTitle": rec.get("sessionTitle", ""), "role": role, "turn": rec.get("turn") or "",
            "at": int(rec.get("at") or 0), "srcFile": rec.get("srcFile", ""), "srcLine": str(rec.get("srcLine") or ""),
            "truncated": bool(rec.get("truncated")), "cwd": rec.get("cwd", ""),
            "taskId": rec.get("taskId", ""), "dependsOn": rec.get("dependsOn", ""), "taskDone": bool(rec.get("taskDone")),
        }
    elif t == "change":
        action = rec.get("action", "")
        pathv = rec.get("path", "")
        status = rec.get("status", "")
        doc_text = f"{owner} {action} {status} {pathv}"
        did = "chg_" + _digest("chg", skey, rec.get("srcLine"), action, rec.get("normalizedPath") or pathv, rec.get("at"))
        meta = {
            "kind": "change", "owner": owner, "sessionId": rec.get("sessionId", ""), "sessionKey": skey,
            "sessionTitle": rec.get("sessionTitle", ""), "action": action, "path": pathv,
            "normalizedPath": rec.get("normalizedPath", ""), "status": status,
            "resultObserved": bool(rec.get("resultObserved")), "turn": rec.get("turn") or "",
            "at": int(rec.get("at") or 0), "srcFile": rec.get("srcFile", ""), "srcLine": str(rec.get("srcLine") or ""),
            "cwd": rec.get("cwd", ""), "taskId": rec.get("taskId", ""),
        }
    elif t == "session":
        title = rec.get("title", "")
        doc_text = f"{owner} 会话《{title}》 模型 {rec.get('model','')} 目录 {rec.get('cwd','')} 状态 {rec.get('state','')}"
        did = "ses_" + _digest("ses", skey)
        meta = {
            "kind": "session", "owner": owner, "sessionId": rec.get("id", ""), "sessionKey": skey,
            "sessionTitle": title, "model": rec.get("model", ""), "cwd": rec.get("cwd", ""),
            "state": rec.get("state", ""), "msgCount": int(rec.get("msgCount") or 0),
            "changeCount": int(rec.get("changeCount") or 0), "mtime": int(rec.get("mtime") or 0),
            "coverageComplete": bool(rec.get("coverageComplete")), "logFile": rec.get("logFile", ""),
            "taskIds": rec.get("taskIds", ""),
        }
    else:
        return None
    return {"id": did, "text": doc_text, "metadata": _meta_scalars(meta)}


def import_corpus(jsonl_path: Optional[str] = None, reset: bool = True) -> Dict:
    """读取语料 JSONL → 构建块 → 批量 embed → upsert 写入 seekdb。返回统计。"""
    p = Path(jsonl_path or config.CORPUS_PATH)
    if not p.exists():
        raise FileNotFoundError(f"语料文件不存在：{p}（先运行 Node ingest：node export-corpus.mjs）")
    with _lock:
        col = _get_collection()
        if reset:
            reset_collection()
            col = _collection
        ids: List[str] = []
        docs: List[str] = []
        metas: List[Dict] = []
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
                d = build_doc(rec)
                if d is None:
                    skipped += 1
                    continue
                ids.append(d["id"])
                docs.append(d["text"])
                metas.append(d["metadata"])
                by_kind[d["metadata"]["kind"]] = by_kind.get(d["metadata"]["kind"], 0) + 1
        # 批量 embed + 写入
        written = 0
        for i in range(0, len(ids), _EMBED_BATCH):
            b_ids, b_docs, b_metas = ids[i : i + _EMBED_BATCH], docs[i : i + _EMBED_BATCH], metas[i : i + _EMBED_BATCH]
            embs = embedder.embed(b_docs)
            _write(col, b_ids, b_docs, embs, b_metas)
            written += len(b_ids)
        _scan_cache["valid"] = False
        try:
            if hasattr(col, "refresh_index"):
                col.refresh_index()
        except Exception:
            pass
        try:
            count = col.count()
        except Exception:
            count = written
    return {
        "ok": True, "corpus": str(p), "written": written, "skipped": skipped,
        "byKind": by_kind, "collectionCount": count, "importedAt": int(time.time()),
        "seekdbPath": str(config.SEEKDB_PATH), "embeddingProvider": config.EMBEDDING_PROVIDER,
    }


# ---------------- 检索 ----------------


def _keywords(text: str) -> List[str]:
    toks = [t for t in _WORD_RE.findall(text.lower()) if t not in _STOP and len(t) > 0]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _keyword_score(qkws: List[str], doc_text: str) -> float:
    if not qkws:
        return 0.0
    low = doc_text.lower()
    return sum(1 for k in qkws if k in low) / len(qkws)


def _citation(meta: Dict) -> Dict:
    return {
        "owner": meta.get("owner", ""), "kind": meta.get("kind", ""),
        "sessionKey": meta.get("sessionKey", ""), "sessionTitle": meta.get("sessionTitle", ""),
        "turn": meta.get("turn", ""), "srcFile": meta.get("srcFile", "") or meta.get("logFile", ""),
        "srcLine": meta.get("srcLine", ""), "at": meta.get("at") or meta.get("mtime") or 0,
    }


def _match_filters(meta: Dict, owner: Optional[str], kind: Optional[str], sessionId: Optional[str]) -> bool:
    if owner and meta.get("owner") != owner:
        return False
    if kind and meta.get("kind") != kind:
        return False
    if sessionId and meta.get("sessionId") != sessionId and meta.get("sessionKey") != sessionId:
        return False
    return True


def hybrid_search(
    query: str,
    owner: Optional[str] = None,
    kind: Optional[str] = None,
    sessionId: Optional[str] = None,
    top_k: Optional[int] = None,
    after: Optional[float] = None,
):
    """三路混合检索：稠密向量 + 稀疏关键词 + 结构化(owner/kind/sessionId)过滤。

    after（0.7.2）：unix 秒时间窗下限，只返回 at ≥ after 的块——供交接简报的
    「近期窗口 facet」使用，保证最近小会话不被历史高分条目整体挤出候选池。
    默认 None = 历史行为完全不变。

    默认走全量混合（_hybrid_full）：载入全部块的向量与文本，对每条同时算 cosine 与关键词命中，
    加权融合 score=(1-w)*vec+w*kw。相比只对 ANN 候选重排，能召回向量排名靠后但关键词强匹配的块。
    集合过大或无 numpy/embeddings 时退回 ANN 候选路径（_hybrid_ann）。返回 (results, log)。
    """
    k = top_k or config.SEARCH_TOP_K
    qvec = embedder.embed([query])[0]
    filters = {"owner": owner, "kind": kind, "sessionId": sessionId, "after": after}
    has_data = _ensure_scan()
    if not _scan_cache["count"]:
        return [], {"query": query, "filters": filters, "mode": "empty", "candidates": 0, "results": []}
    if has_data and _scan_cache["mat"] is not None:
        return _hybrid_full(query, qvec, owner, kind, sessionId, k, after)
    return _hybrid_ann(query, qvec, owner, kind, sessionId, k)


def _hybrid_full(query, qvec, owner, kind, sessionId, k, after=None):
    return _fuse_full(_scan_cache["ids"], _scan_cache["docs"], _scan_cache["metas"], _scan_cache["mat"],
                      qvec, query, owner, kind, sessionId, k, mode="full-hybrid", after=after)


def _fuse_full(ids, docs, metas, mat, qvec, query, owner, kind, sessionId, k, mode="full-hybrid", after=None):
    """全量融合打分：dense cosine + keyword 命中，score=(1-w)*vec+w*kw。

    seekdb 版（_hybrid_full）与 light 版（store_light.hybrid_search）共用同一实现——
    打分/过滤/citation 是一个事实，只存这一处。after=unix 秒时间窗下限（可选）。"""
    qv = _np.array(qvec, dtype=_np.float32)
    qn = float(_np.linalg.norm(qv)) or 1.0
    dense = mat @ (qv / qn)  # cosine 相似度（行已归一化）
    qkws = _keywords(query)
    w = config.KEYWORD_WEIGHT
    scored = []
    for i, meta in enumerate(metas):
        if not meta or not _match_filters(meta, owner, kind, sessionId):
            continue
        if after and float(meta.get("at") or meta.get("mtime") or 0) < after:
            continue
        text = docs[i] if i < len(docs) else ""
        vec_sim = float(dense[i])
        kw = _keyword_score(qkws, text)
        scored.append(((1 - w) * vec_sim + w * kw, vec_sim, kw, i))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, vec_sim, kw, i in scored[:k]:
        meta = metas[i] or {}
        results.append({
            "id": ids[i] if i < len(ids) else "", "text": docs[i] if i < len(docs) else "",
            "score": round(score, 4),
            "signals": {"vector": round(vec_sim, 4), "keyword": round(kw, 4)},
            "distance": round(1.0 - vec_sim, 4),
            "meta": {kk: meta.get(kk) for kk in ("kind", "owner", "role", "action", "status", "path", "sessionKey", "sessionTitle", "turn", "at")},
            "citation": _citation(meta),
        })
    log = {
        "query": query, "filters": {"owner": owner, "kind": kind, "sessionId": sessionId},
        "mode": mode, "scanned": len(metas), "candidates": len(scored),
        "results": [{"id": r["id"], "kind": r["meta"].get("kind"), "owner": r["meta"].get("owner"),
                     "score": r["score"], "vector": r["signals"]["vector"], "keyword": r["signals"]["keyword"],
                     "srcFile": r["citation"]["srcFile"], "srcLine": r["citation"]["srcLine"]} for r in results],
    }
    return results, log


def _hybrid_ann(query, qvec, owner, kind, sessionId, k):
    """退回路径：col.query ANN 候选 + 代码侧重排（RecallLoop 原始做法）。"""
    col = _get_collection()
    filters = {"owner": owner, "kind": kind, "sessionId": sessionId}
    with _lock:
        try:
            count = col.count()
        except Exception:
            count = 0
        if not count:
            return [], {"query": query, "filters": filters, "mode": "ann", "candidates": 0, "results": []}
        n_cand = max(k * 4, min(config.VECTOR_CANDIDATES, count))
        where = None
        if sessionId:
            where = {"sessionId": sessionId}
        elif kind:
            where = {"kind": kind}
        elif owner:
            where = {"owner": owner}
        try:
            res = col.query(query_embeddings=[qvec], n_results=n_cand, where=where) if where else col.query(query_embeddings=[qvec], n_results=n_cand)
        except Exception:
            res = col.query(query_embeddings=[qvec], n_results=n_cand)

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    qkws = _keywords(query)
    w = config.KEYWORD_WEIGHT
    scored = []
    for i, cid in enumerate(ids):
        text = docs[i] if i < len(docs) else ""
        meta = metas[i] if i < len(metas) else {}
        dist = dists[i] if i < len(dists) else None
        if not _match_filters(meta, owner, kind, sessionId):
            continue
        vec_sim = (1.0 - float(dist)) if dist is not None else 0.0
        kw = _keyword_score(qkws, text)
        score = (1 - w) * vec_sim + w * kw
        scored.append({
            "id": cid, "text": text, "score": round(score, 4),
            "signals": {"vector": round(vec_sim, 4), "keyword": round(kw, 4)},
            "distance": round(float(dist), 4) if dist is not None else None,
            "meta": {kk: meta.get(kk) for kk in ("kind", "owner", "role", "action", "status", "path", "sessionKey", "sessionTitle", "turn", "at")},
            "citation": _citation(meta),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:k]
    log = {
        "query": query, "filters": filters, "mode": "ann",
        "candidates": len(scored), "wherePushdown": bool(where),
        "results": [{"id": r["id"], "kind": r["meta"].get("kind"), "owner": r["meta"].get("owner"),
                     "score": r["score"], "vector": r["signals"]["vector"], "keyword": r["signals"]["keyword"],
                     "srcFile": r["citation"]["srcFile"], "srcLine": r["citation"]["srcLine"]} for r in results],
    }
    return results, log


def _ensure_scan(force: bool = False) -> bool:
    """载入全量 ids/docs/metas/embeddings 到内存缓存。返回是否具备全量混合检索条件（有 numpy+embeddings）。"""
    col = _get_collection()
    with _lock:
        try:
            count = col.count()
        except Exception:
            count = 0
        if not count:
            _scan_cache.update(valid=True, ids=[], docs=[], metas=[], mat=None, count=0)
            return False
        if _np is None or count > _SCAN_MAX:
            _scan_cache["valid"] = False
            _scan_cache["count"] = count
            return False
        if _scan_cache["valid"] and not force and _scan_cache["count"] == count and _scan_cache["mat"] is not None:
            return True
        try:
            res = col.get(limit=count, include=["documents", "metadatas", "embeddings"])
        except Exception:
            res = col.get(limit=count, include=["documents", "metadatas"])
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        embs = res.get("embeddings") or []
        mat = None
        if embs and len(embs) == len(ids):
            arr = _np.array([e if e is not None else [0.0] * config.EMBEDDING_DIM for e in embs], dtype=_np.float32)
            norms = _np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            mat = arr / norms
        _scan_cache.update(valid=True, ids=ids, docs=docs, metas=metas, mat=mat, count=count)
        return mat is not None


def _scan_all(include: Optional[List[str]] = None) -> Dict:
    """全量 ids/documents/metadatas（走内存缓存）。"""
    _ensure_scan()
    return {"ids": _scan_cache["ids"], "documents": _scan_cache["docs"], "metadatas": _scan_cache["metas"]}


def normalize_path(value: str) -> str:
    """与 v1 normalizePath 对齐：反斜杠转正斜杠、去 UNC 前缀、转小写。"""
    if not value:
        return ""
    s = str(value).replace("\\", "/")
    s = re.sub(r"^//[?.]/UNC/", "//", s, flags=re.IGNORECASE)
    s = re.sub(r"^//[?.]/", "", s)
    return s.lower()


def find_by_file(file_path: str, limit: int = 200) -> Dict:
    """文件反查（v1 --file 的 seekdb 版）：扫描 change 块，按 normalizedPath 精确/后缀匹配。
    返回带 citation 的操作记录，按时间排序。"""
    target = normalize_path(file_path)
    res = _scan_all(["documents", "metadatas"])
    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    matches = []
    for i, meta in enumerate(metas):
        if not meta or meta.get("kind") != "change":
            continue
        np = normalize_path(meta.get("normalizedPath") or meta.get("path") or "")
        if not np:
            continue
        exact = np == target
        suffix = (not exact) and (np.endswith("/" + target) or target.endswith("/" + np) or np.endswith(target) or target.endswith(np))
        if exact or suffix:
            matches.append({
                "id": ids[i] if i < len(ids) else "", "text": docs[i] if i < len(docs) else "",
                "exact": exact, "action": meta.get("action", ""), "status": meta.get("status", ""),
                "path": meta.get("path", ""), "normalizedPath": meta.get("normalizedPath", ""),
                "citation": _citation(meta),
            })
    matches.sort(key=lambda x: (not x["exact"], x["citation"].get("at") or 0))
    return {"file": file_path, "normalized": target, "matchCount": len(matches), "matches": matches[:limit]}


def list_sessions() -> List[Dict]:
    """列举全部会话摘要块（kind=session），按 mtime 倒序。"""
    res = _scan_all(["metadatas"])
    metas = res.get("metadatas") or []
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
    col = _get_collection()
    with _lock:
        try:
            count = col.count()
        except Exception:
            count = 0
    by_kind = {"message": 0, "change": 0, "session": 0}
    by_owner: Dict[str, int] = {}
    if count:
        res = _scan_all(["metadatas"])
        for meta in res.get("metadatas") or []:
            if not meta:
                continue
            kk = meta.get("kind")
            if kk in by_kind:
                by_kind[kk] += 1
            ow = meta.get("owner", "?")
            by_owner[ow] = by_owner.get(ow, 0) + 1
    return {"collection": config.COLLECTION_NAME, "count": count, "byKind": by_kind, "byOwner": by_owner,
            "seekdbPath": str(config.SEEKDB_PATH)}


# ---------------- 后端分发（0.7.0）：MULTI_AI_STORE=light 时数据面整体换零 torch 实现 ----------------
# 共享层（本文件，两后端同一事实）：build_doc / _meta_scalars / _keywords / _keyword_score /
# _citation / _match_filters / normalize_path / _fuse_full。
# 数据层（向量与块的存取+检索）切换：seekdb = 本文件 pyseekdb 实现；light = store_light
# （docs.jsonl + emb.npy + numpy，绝不 import torch/pyseekdb）。store_light 末尾才 import，
# 保证它内部 lazy `import store` 拿到的是完整模块。
if (getattr(config, "STORE_PROVIDER", "seekdb") or "seekdb").strip().lower() == "light":
    import store_light as _light_impl  # noqa: E402

    import_corpus = _light_impl.import_corpus
    reset_collection = _light_impl.reset_collection
    hybrid_search = _light_impl.hybrid_search
    find_by_file = _light_impl.find_by_file
    list_sessions = _light_impl.list_sessions
    stats = _light_impl.stats
    _scan_all = _light_impl._scan_all
