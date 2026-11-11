"""多AI协同 v2 — FastAPI 入口。

三层职责：
- seekdb（store.py）：跨 agent 语料的三路混合检索 + 按文件反查，带 file:line 引用。
- PowerContext（pc.py）：跨 agent 共享记忆（L1 状态层）+ 原生交接（L2 交接层）。
- dispatch.py：父进程派工闭环（拆任务→捞证据→判质量→换策略重捞→交叉核验→整合交接）。

无 Key 全流程可用；seekdb / PowerContext 未就绪时相关接口优雅降级（返回 reason，不白屏），
前端据此进入演示模式。启动时后台预热 pyseekdb（首查需载入模型，约 20-30s），避免首个请求卡死。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bridge_api  # 先于 config：桥内 setdefault 生产集合 env（multi_ai_events）
import config
import pc

app = FastAPI(title="多AI协同 v2 · 交接桥控制台", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_RETRIEVAL_LOG = config.DATA_DIR / "retrieval_log.jsonl"
_READY = {"seekdb": False, "powercontext": False, "count": 0}


def _log_retrieval(record: Dict) -> None:
    config.ensure_dirs()
    record = {"ts": int(time.time()), **record}
    with open(_RETRIEVAL_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _warmup() -> None:
    """启动预热刻意保持轻量：只探测 PowerContext 可用性。

    重资产（pyseekdb/torch/嵌入模型）不再启动时拉起——由桥控制台「③检索」环节
    显式加载（bridge_api.stage3_load），是否驻留如实展示。避免服务一启动就吃 ~1.3GB。
    """
    try:
        _READY["powercontext"] = pc.is_available()
    except Exception:
        _READY["powercontext"] = False
    print(f"[warmup] light ready={_READY}（seekdb 集合按需显式加载）")


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_warmup, daemon=True).start()


# ---------------- 请求模型 ----------------


class SearchIn(BaseModel):
    query: str
    owner: Optional[str] = None
    kind: Optional[str] = None
    sessionId: Optional[str] = None
    top_k: Optional[int] = None


class FindFileIn(BaseModel):
    path: str
    limit: Optional[int] = None


class MemorySearchIn(BaseModel):
    query: str
    limit: Optional[int] = 10


class DispatchIn(BaseModel):
    goal: str
    num_agents: Optional[int] = 2
    max_rounds: Optional[int] = 2
    mode: Optional[str] = "continue-original-session"


# ---------------- 路由 ----------------


@app.get("/api/health")
def health():
    return {"status": "ok", "ready": _READY}


@app.get("/api/stats")
def stats():
    import store
    if not _READY["seekdb"]:
        try:
            store.stats()
            _READY["seekdb"] = True
        except Exception as e:
            return JSONResponse(status_code=503, content={"available": False, "reason": str(e)})
    try:
        st = store.stats()
        st["available"] = True
        return st
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e)})


@app.get("/api/sessions")
def sessions():
    import store
    try:
        return {"available": True, "sessions": store.list_sessions()}
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e), "sessions": []})


@app.post("/api/search")
def search(req: SearchIn):
    import store
    try:
        results, log_record = store.hybrid_search(
            req.query, owner=req.owner, kind=req.kind, sessionId=req.sessionId, top_k=req.top_k)
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e), "results": []})
    _log_retrieval(log_record)
    return {
        "available": True,
        "mode": log_record.get("mode"),
        "results": [
            {
                "id": r["id"], "text": r["text"], "score": r["score"], "signals": r["signals"],
                "meta": r.get("meta"), "citation": r.get("citation"),
            }
            for r in results
        ],
        "engine": "seekdb 向量+关键词+结构化 三路混合（全量重排）",
    }


@app.post("/api/find_file")
def find_file(req: FindFileIn):
    import store
    try:
        return {"available": True, **store.find_by_file(req.path, limit=req.limit or 200)}
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e)})


@app.get("/api/memory/status")
def memory_status():
    avail = _READY.get("powercontext")
    if avail is None:
        try:
            avail = pc.is_available()
            _READY["powercontext"] = avail
        except Exception:
            avail = False
    out = {"available": bool(avail)}
    if avail:
        try:
            out["scope_id"] = pc.ensure_scope()
        except Exception as e:
            out["available"] = False
            out["reason"] = str(e)
    return out


@app.post("/api/memory/search")
def memory_search(req: MemorySearchIn):
    try:
        return {"available": True, **pc.search(req.query, limit=req.limit or 10, mode="auto")}
    except pc.PowerContextUnavailable as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e), "hits": []})
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e), "hits": []})


@app.get("/api/memory/context")
def memory_context(query: str = "多AI协同 共享记忆 交接 进度"):
    try:
        return {"available": True, **pc.prepare_context(query, max_bytes=4000)}
    except pc.PowerContextUnavailable as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e), "status": "unavailable"})
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e), "status": "unavailable"})


@app.get("/api/handoff")
def handoff():
    """读回当前交接（原生 handoff/continue，latest）+ 证据核验结果。"""
    try:
        r = pc.read_handoff(selection="latest")
        return {"available": True, **r}
    except pc.PowerContextUnavailable as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e)})
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e)})


@app.post("/api/dispatch")
def run_dispatch(req: DispatchIn):
    """跑完整派工闭环。耗时（含检索多轮），前端需给足超时与加载态。"""
    import dispatch
    try:
        rec = dispatch.run_closed_loop(
            req.goal, num_agents=req.num_agents or 2,
            max_rounds=req.max_rounds or 2, mode=req.mode or "continue-original-session")
        return {"available": True, **rec}
    except pc.PowerContextUnavailable as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": f"PowerContext 不可用：{e}"})
    except Exception as e:
        return JSONResponse(status_code=503, content={"available": False, "reason": str(e)})


# 桥控制台 API（管线四环节检查 + 简报运行器 + 台账/评测/服务生命周期）
app.include_router(bridge_api.router)

# 前端单页（挂在最后，避免覆盖 /api）
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
