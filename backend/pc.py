"""多AI协同 v2 · PowerContext 共享记忆 + 原生交接客户端（HTTP）。

端点与字段严格依据 src/powercontext/http/_generated/schema.py，并在本地 :8000 实测通过：
  POST /v1/scopes                         {title, summary, idempotency_key}      -> {scope_id,...}
  POST /v1/sources/content                {scope_id, source_id, content, metadata?} -> {status:"accepted", position, source:{name,source_id}}
  POST /v1/memory/remember                {scope_id, kind, text, reason?}         -> {memory:{...}, entry:{citation:{memory_ref, entry_id, entry_version_id}}}
  POST /v1/memory/search                  {scope_id, query, limit?, mode?}        -> {memory, mode, hits:[{citation, text, score, matched_by}]}
  POST /v1/context/prepare                {scope_id, query, max_bytes?}           -> {status:"ready"|"empty", content}
  POST /v1/scopes/{scope_id}/artifacts    {family:"handoff", content:HandoffContent} -> {scope_id, family, artifact_id, revision, sources, artifacts}
  POST /v1/handoff/continue               {scope_id, selection:"latest"|"prepared"|"exact", prepared?, revision?} -> {status:"resolved", content, evidence_checks}
  GET  /v1/scopes/{scope_id}/artifacts/handoff/handoff  -> {scope_id, family, artifact_id, revision, content}

安全：Token 仅从环境变量读取，绝不写入 URL/日志；scope_id 由应用决定并持久化。
成功调用向 DATA_DIR/pc_log.jsonl 追加一行证据（不含 Token）。

注意（本地实测约束）：
- memory/search 的 mode="hybrid" 会返回 422 capability_not_supported；仅用 auto / fts。
- 服务端交接 GENERATION（/v1/handoff/prepare|finalize|commit）需要 LLM 后端，本地缺失返回
  503 handoff_generation_unavailable；因此走「客户端自撰交接」路径：客户端组装 HandoffContent
  后 POST /v1/scopes/{id}/artifacts {family:"handoff"}，接收方 POST /v1/handoff/continue 读回。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Dict, List, Optional

import config

_SCOPE_FILE = config.DATA_DIR / "scope.json"
_PC_LOG = config.DATA_DIR / "pc_log.jsonl"
_IDEMPOTENCY_KEY = "multi-ai-v2-shared-memory"

_cached_scope: Optional[str] = None


class PowerContextUnavailable(Exception):
    """PowerContext Server 未启动或不可达，或返回错误。"""


# ---------------- 基础 HTTP ----------------


def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if config.POWERCONTEXT_TOKEN:
        h["Authorization"] = f"Bearer {config.POWERCONTEXT_TOKEN}"
    return h


def _log(endpoint: str, req_summary: Dict, resp_summary: Dict) -> None:
    config.ensure_dirs()
    line = {
        "ts": int(time.time()),
        "endpoint": endpoint,
        "request": req_summary,
        "response": resp_summary,
    }
    with open(_PC_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _request(method: str, endpoint: str, payload: Optional[Dict] = None,
             extra_headers: Optional[Dict] = None):
    """底层请求，返回 httpx.Response（不抛非连接类错误），供上层按状态码分流。"""
    import httpx

    url = f"{config.POWERCONTEXT_URL}{endpoint}"
    headers = _headers()
    if extra_headers:
        headers.update(extra_headers)
    try:
        with httpx.Client(timeout=config.POWERCONTEXT_TIMEOUT) as client:
            return client.request(method, url, json=payload, headers=headers)
    except Exception as e:
        raise PowerContextUnavailable(f"无法连接 PowerContext（{url}）：{e}") from e


def _post(endpoint: str, payload: Dict) -> Dict:
    r = _request("POST", endpoint, payload)
    if r.status_code >= 400:
        raise PowerContextUnavailable(f"{endpoint} 返回 {r.status_code}：{r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return {}


def _get(endpoint: str) -> Dict:
    r = _request("GET", endpoint)
    if r.status_code >= 400:
        raise PowerContextUnavailable(f"{endpoint} 返回 {r.status_code}：{r.text[:200]}")
    try:
        return r.json()
    except Exception:
        return {}


# ---------------- Scope ----------------


def ensure_scope() -> str:
    """返回稳定 scope_id：环境变量 > DATA_DIR/scope.json 缓存 > 调 /v1/scopes 创建。

    scope 由应用决定并持久化，绝不取自模型输出。
    """
    global _cached_scope
    if config.POWERCONTEXT_SCOPE_ID:
        _cached_scope = config.POWERCONTEXT_SCOPE_ID
        return _cached_scope
    if _cached_scope:
        return _cached_scope
    config.ensure_dirs()
    if _SCOPE_FILE.exists():
        try:
            _cached_scope = json.loads(_SCOPE_FILE.read_text(encoding="utf-8")).get("scope_id")
            if _cached_scope:
                return _cached_scope
        except Exception:
            pass
    resp = _post(
        "/v1/scopes",
        {
            "title": "多AI协同 v2 共享记忆",
            "summary": "跨 agent（DSH / WorkBuddy / Codex）共享记忆与交接 Scope：检索证据、决策约束与工作交接。",
            "idempotency_key": _IDEMPOTENCY_KEY,
        },
    )
    scope_id = resp.get("scope_id", "")
    if not scope_id:
        raise PowerContextUnavailable(f"/v1/scopes 未返回 scope_id：{resp}")
    _SCOPE_FILE.write_text(json.dumps({"scope_id": scope_id}, ensure_ascii=False), encoding="utf-8")
    _cached_scope = scope_id
    print(f"[pc] created scope_id={scope_id}")
    _log("/v1/scopes", {"idempotency_key": _IDEMPOTENCY_KEY}, {"scope_id": scope_id})
    return scope_id


# ---------------- 写入 ----------------


def capture_source(source_id: str, content: str, metadata: Optional[Dict] = None) -> Dict:
    """留底一段来源内容（如检索命中的语料证据）。返回含 source.name/source_id，可用于交接引用。"""
    scope_id = ensure_scope()
    payload = {"scope_id": scope_id, "source_id": source_id, "content": content}
    if metadata:
        payload["metadata"] = metadata
    resp = _post("/v1/sources/content", payload)
    src = resp.get("source") or {}
    _log(
        "/v1/sources/content",
        {"scope_id": scope_id, "source_id": source_id, "content_len": len(content)},
        {"status": resp.get("status"), "position": resp.get("position")},
    )
    return {
        "resp": resp,
        "source_ref": {"name": src.get("name") or "content", "source_id": src.get("source_id") or source_id},
    }


def remember(text: str, kind: str = "working_note", reason: Optional[str] = None) -> Dict:
    """写入一条共享记忆。kind 为自由字符串（≤128），推荐 fact/preference/decision/constraint/working_note。

    返回 {resp, citation}，citation 可直接用于交接证据（HandoffCitation kind="memory"）。
    """
    scope_id = ensure_scope()
    payload = {"scope_id": scope_id, "kind": kind, "text": text}
    if reason:
        payload["reason"] = reason
    resp = _post("/v1/memory/remember", payload)
    entry = resp.get("entry") or {}
    citation = entry.get("citation") or {}
    _log(
        "/v1/memory/remember",
        {"scope_id": scope_id, "kind": kind, "text": text[:120]},
        {"status": resp.get("status") or "ok", "entry_id": citation.get("entry_id")},
    )
    return {"resp": resp, "citation": citation}


def memory_citation(citation: Dict) -> Optional[Dict]:
    """把 remember 返回的 entry.citation 规整成 HandoffCitation(kind=memory)。"""
    mem_ref = citation.get("memory_ref")
    entry_id = citation.get("entry_id")
    ev_id = citation.get("entry_version_id")
    if not (mem_ref and entry_id and ev_id):
        return None
    return {"kind": "memory", "memory_citation": {
        "memory_ref": {
            "family": mem_ref.get("family"),
            "artifact_id": mem_ref.get("artifact_id"),
            "revision": mem_ref.get("revision"),
        },
        "entry_id": entry_id,
        "entry_version_id": ev_id,
    }}


def source_citation(source_id: str, name: str = "content") -> Dict:
    """构成交接里的来源引用（HandoffCitation kind=source）。"""
    return {"kind": "source", "source_ref": {"name": name, "source_id": source_id}}


# ---------------- 读取 ----------------


def search(query: str, limit: int = 10, mode: str = "auto") -> Dict:
    """调 /v1/memory/search。mode 仅 auto / fts（hybrid 在本地不可用会 422）。

    返回 {mode, hits:[{text, score, matched_by, citation}]}。
    """
    scope_id = ensure_scope()
    payload = {"scope_id": scope_id, "query": query, "limit": limit, "mode": mode}
    resp = _post("/v1/memory/search", payload)
    hits = resp.get("hits", []) or []
    out = [
        {
            "text": h.get("text", ""),
            "score": h.get("score", 0),
            "matched_by": h.get("matched_by", ""),
            "citation": h.get("citation"),
        }
        for h in hits
    ]
    _log(
        "/v1/memory/search",
        {"scope_id": scope_id, "query": query[:60], "limit": limit, "mode": mode},
        {"mode": resp.get("mode"), "hits": len(out)},
    )
    return {"mode": resp.get("mode", mode), "hits": out}


def prepare_context(query: str, max_bytes: int = 4000) -> Dict:
    """调 /v1/context/prepare 取回历史上下文；status==empty 时返回空，不伪造历史。"""
    scope_id = ensure_scope()
    resp = _post(
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": query, "max_bytes": max_bytes},
    )
    status = resp.get("status", "empty")
    content = resp.get("content") or ""
    _log(
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": query[:60], "max_bytes": max_bytes},
        {"status": status, "content_bytes": resp.get("content_bytes", len(content))},
    )
    return {"status": status, "content": content}


# ---------------- 原生交接（客户端自撰） ----------------

_HANDOFF_SCHEMA = "powercontext.handoff.v1"


def create_handoff(
    objective: str,
    state: List[Dict],
    disposition: str = "continuable",
    next_action: Optional[Dict] = None,
    omissions: Optional[List[Dict]] = None,
) -> Dict:
    """客户端自撰交接（upsert）：POST /v1/scopes/{scope}/artifacts {family:"handoff"}。

    handoff 是 scope 内的 singleton artifact：首次 POST 创建（201），已存在时返回 409
    artifact_already_exists，此时改用 Replace Artifact（PUT .../artifacts/handoff/handoff）
    提交为下一个 revision。

    state: [{text, citations:[HandoffCitation]}] 1-64 项，每项 citations 1-32 个。
    next_action: {text, citations} 或 None。
    omissions: [{text, citation}] 0-64 项（HandoffOmission，citation 可为 null）。
    disposition: "continuable" | "blocked" | "complete"。
    返回 {resp, revision, replaced}。
    """
    scope_id = ensure_scope()
    content = {
        "schema": _HANDOFF_SCHEMA,
        "objective": objective,
        "state": state,
        "disposition": disposition,
        "next_action": next_action,
        "omissions": omissions or [],
    }
    r = _request("POST", f"/v1/scopes/{scope_id}/artifacts", {"family": "handoff", "content": content})
    if r.status_code == 409:
        return _replace_handoff(scope_id, content, disposition, state)
    if r.status_code >= 400:
        raise PowerContextUnavailable(
            f"/v1/scopes/{scope_id}/artifacts 返回 {r.status_code}：{r.text[:200]}")
    resp = r.json()
    revision = resp.get("revision")
    _log(
        f"/v1/scopes/{scope_id}/artifacts",
        {"family": "handoff", "disposition": disposition, "state_items": len(state)},
        {"artifact_id": resp.get("artifact_id"), "revision": revision, "op": "create",
         "sources": len(resp.get("sources") or []), "artifacts": len(resp.get("artifacts") or [])},
    )
    return {"resp": resp, "revision": revision, "replaced": False}


def _replace_handoff(scope_id: str, content: Dict, disposition: str, state: List[Dict]) -> Dict:
    """Replace Artifact：PUT /v1/scopes/{scope}/artifacts/handoff/handoff {content}。

    乐观并发：先 GET 取当前 ETag，带 If-Match 提交（服务端要求则满足 428/412 前置条件）。
    """
    endpoint = f"/v1/scopes/{scope_id}/artifacts/handoff/handoff"
    g = _request("GET", endpoint)
    etag = g.headers.get("ETag") if g.status_code < 400 else None
    extra = {"If-Match": etag} if etag else None
    rp = _request("PUT", endpoint, {"content": content}, extra_headers=extra)
    if rp.status_code >= 400:
        raise PowerContextUnavailable(f"{endpoint} PUT 返回 {rp.status_code}：{rp.text[:200]}")
    resp = rp.json()
    revision = resp.get("revision")
    _log(
        endpoint,
        {"method": "PUT", "disposition": disposition, "state_items": len(state), "if_match": bool(etag)},
        {"artifact_id": resp.get("artifact_id"), "revision": revision, "op": "replace"},
    )
    return {"resp": resp, "revision": revision, "replaced": True}


def read_handoff(selection: str = "latest", revision: Optional[int] = None) -> Dict:
    """接收方读回交接：POST /v1/handoff/continue {scope_id, selection}。

    返回 {status, selection, content, evidence_checks}。
    evidence_checks: [{claim:"state"|"next_action", state_index, status:"available"|"unavailable", unavailable_evidence}]。
    """
    scope_id = ensure_scope()
    payload = {"scope_id": scope_id, "selection": selection}
    if selection == "exact" and revision is not None:
        payload["revision"] = revision
    resp = _post("/v1/handoff/continue", payload)
    checks = resp.get("evidence_checks", []) or []
    available = sum(1 for c in checks if c.get("status") == "available")
    _log(
        "/v1/handoff/continue",
        {"scope_id": scope_id, "selection": selection},
        {"status": resp.get("status"), "evidence_available": f"{available}/{len(checks)}"},
    )
    return {
        "status": resp.get("status"),
        "selection": resp.get("selection", selection),
        "content": resp.get("content") or {},
        "evidence_checks": checks,
    }


def get_handoff_artifact() -> Dict:
    """直接读取当前交接工件（不经 continue）：GET /v1/scopes/{scope}/artifacts/handoff/handoff。"""
    scope_id = ensure_scope()
    return _get(f"/v1/scopes/{scope_id}/artifacts/handoff/handoff")


# ---------------- 便捷封装 ----------------


def handoff_citation_from_hit(hit: Dict) -> Optional[Dict]:
    """把 search() 命中的 citation 规整成 HandoffCitation(kind=memory)。"""
    return memory_citation(hit.get("citation") or {})


def is_available() -> bool:
    try:
        ensure_scope()
        return True
    except Exception:
        return False


def new_source_id(prefix: str = "src") -> str:
    """全局唯一 source_id：避免同秒多源触发 409 source_conflict。"""
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
