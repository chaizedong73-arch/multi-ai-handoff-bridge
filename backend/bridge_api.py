"""交接桥控制台 · 管线检查端点（v2 backend 附加层，由 main.py 挂载）。

定位：把 ①采集 ②归一化 ③检索 ④简报 四个环节做成「可检查」——
每个环节都有真实状态、可展开的原始材料、明确的检查动作与耗时/内存实测。

边界（与项目主页红线一致）：
- 全部只读探测；唯一写入是 ④ 简报产物落 DATA_DIR/briefs/ 与服务 PID 文件。
- events.jsonl 是 v1 实时监控的只读产出，绝不追加/改写。
- 重资产（pyseekdb/torch/ST 嵌入模型）惰性加载：是否已驻留如实报告（/proc RSS 实测），
  启动时不偷偷拉起；服务整体启停在「服务」面板（SIGTERM，宿主侧经 reg.cmd 登记）。

注意：本模块必须在 import config 之前设置集合环境变量——控制台面向生产链路
（v1 events.jsonl → events-corpus → multi_ai_events 集合），不是早期 demo 语料。
"""
from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

os.environ.setdefault("MULTI_AI_COLLECTION", "multi_ai_events")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# EMBEDDING_PROVIDER 不在此 setdefault：由 config 按 MULTI_AI_STORE 联动
# （light ⇒ onnx 零 torch；seekdb ⇒ local/ST，历史行为不变）。显式 env 一律尊重。

import config  # noqa: E402
from fastapi import APIRouter  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

router = APIRouter()

# v1 只读事件流（① 采集源，绝不写）
EVENTS_PATH = Path(os.environ.get("MULTI_AI_EVENTS", str(Path.home() / "multi-ai-data" / "events.jsonl")))  # ① 采集源（可选；未配置时该环节显示不可用）
# MCP 轻壳的跨 agent 调用台账与简报副本（只读）
LEDGER_PATH = Path(os.environ.get("HB_LEDGER", str(config.DATA_DIR / "call-ledger.jsonl")))
MCP_BRIEFS_DIR = Path(os.environ.get("HB_BRIEF_DIR", str(config.DATA_DIR / "briefs-mcp")))
BACKEND_DIR = Path(__file__).resolve().parent
BRIEFS_DIR = config.DATA_DIR / "briefs"

_STATE: Dict = {
    "started_at": time.time(),
    "collection_open": False,   # seekdb 集合已打开（= pyseekdb/torch 已驻留）
    "st_model_loaded": False,   # ST 嵌入模型已加载
    "last_stage3_load": None,   # ③ 加载实测 {load_ms, rss_before_mb, rss_after_mb}
    "last_eval": None,
}
_events_cache: Optional[Dict] = None
_events_lock = threading.Lock()
_JOBS: Dict[str, Dict] = {}
_job_lock = threading.Lock()
_current_job = {"id": None}


# ---------------- 小工具 ----------------

def _rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


def _proc() -> Dict:
    return {
        "pid": os.getpid(),
        "rss_mb": _rss_mb(),
        "uptime_s": round(time.time() - _STATE["started_at"]),
        "collection_open": _STATE["collection_open"],
        "st_model_loaded": _STATE["st_model_loaded"],
        "collection": config.COLLECTION_NAME,
        "seekdbPath": str(config.SEEKDB_PATH),
        "storeProvider": getattr(config, "STORE_PROVIDER", "seekdb"),
        "embeddingProvider": getattr(config, "EMBEDDING_PROVIDER", "local"),
        "python": os.path.basename(os.environ.get("VIRTUAL_ENV", "")) or "recallloop-venv",
    }


def _ensure_pidfiles() -> None:
    """写服务 PID 文件：stop-bridge.cmd 按它发 SIGTERM。已启动则不覆盖。"""
    try:
        config.ensure_dirs()
        pid_file = config.DATA_DIR / "bridge-console.pid"
        old = pid_file.read_text(encoding="ascii").strip() if pid_file.exists() else ""
        if old and old != str(os.getpid()) and Path(f"/proc/{old}").exists():
            return  # 已有实例在跑，不动它
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        (config.DATA_DIR / "bridge-console.json").write_text(json.dumps(
            {"pid": os.getpid(), "started_at": int(_STATE["started_at"]),
             "collection": config.COLLECTION_NAME, "port": os.environ.get("BRIDGE_PORT", "8765")},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _stat_file(p: Path) -> Dict:
    try:
        st = p.stat()
        return {"path": str(p), "exists": True, "bytes": st.st_size,
                "mtime": int(st.st_mtime), "mtimeIso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
    except Exception:
        return {"path": str(p), "exists": False}


def _read_json(p: Path) -> Optional[Dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------- ① 采集：events.jsonl 只读检查 ----------------

def _scan_events_meta() -> None:
    """全量扫一遍 events.jsonl：行数 + 按 source 计数。/mnt/c 较慢，放后台线程，按 (size,mtime) 缓存。"""
    global _events_cache
    try:
        st = EVENTS_PATH.stat()
        key = (st.st_size, st.st_mtime)
        with _events_lock:
            if _events_cache and _events_cache.get("key") == key:
                return
        lines, by_owner, by_kind = 0, {}, {}
        with open(EVENTS_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                lines += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                o = obj.get("source") or "?"
                k = obj.get("kind") or "?"
                by_owner[o] = by_owner.get(o, 0) + 1
                by_kind[k] = by_kind.get(k, 0) + 1
        with _events_lock:
            _events_cache = {"key": key, "lines": lines, "byOwner": by_owner, "byKind": by_kind,
                             "scannedAt": int(time.time())}
    except Exception as e:
        with _events_lock:
            _events_cache = {"key": None, "error": str(e)}


@router.get("/api/bridge/events/inspect")
def events_inspect(start: int = 1, limit: int = 20):
    """按行号读 events.jsonl 原始行（只读）。④ 简报里的 events.jsonl:行号 在这里回查。"""
    if not EVENTS_PATH.exists():
        return JSONResponse(status_code=404, content={"error": f"找不到 {EVENTS_PATH}"})
    start = max(1, start)
    limit = min(200, max(1, limit))
    rows: List[Dict] = []
    with open(EVENTS_PATH, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i < start:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                obj = {}
            summary = str(obj.get("summary") or obj.get("result") or "")
            if len(summary) > 400:
                summary = summary[:400] + "…[截断]"
            rows.append({"n": i, "at": obj.get("at"), "source": obj.get("source"),
                         "kind": obj.get("kind"), "sessionName": obj.get("sessionName"),
                         "summary": summary})
            if len(rows) >= limit:
                break
    with _events_lock:
        total = (_events_cache or {}).get("lines")
    return {"path": str(EVENTS_PATH), "start": start, "rows": rows, "totalLines": total}


# ---------------- ② 归一化：水位线 + 归一化产物 ----------------

@router.get("/api/bridge/corpus/inspect")
def corpus_inspect(start: int = 1, limit: int = 20):
    """读归一化产物 events-corpus.jsonl（含 redact 掩码的语料，只读）。"""
    p = config.DATA_DIR / "events-corpus.jsonl"
    if not p.exists():
        return JSONResponse(status_code=404, content={"error": f"找不到 {p}"})
    start = max(1, start)
    limit = min(200, max(1, limit))
    rows: List[Dict] = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i < start:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                obj = {}
            text = str(obj.get("text") or "")
            if len(text) > 400:
                text = text[:400] + "…[截断]"
            rows.append({"n": i, "type": obj.get("type"), "owner": obj.get("owner"),
                         "text": text})
            if len(rows) >= limit:
                break
    return {"path": str(p), "start": start, "rows": rows}


@router.post("/api/bridge/catchup")
def run_catchup():
    """跑 ①→② 增量懒补（同进程）。无新行时 early-return，不碰模型——耗时如实报。"""
    t0 = time.time()
    rss_before = _rss_mb()
    try:
        import catchup
        res = catchup.catch_up(force_full=False)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
    return {"ok": True, "catchup": res, "duration_ms": round((time.time() - t0) * 1000),
            "rss_before_mb": rss_before, "rss_after_mb": _rss_mb(),
            "note": "无新行时 early-return 不加载模型；增量只省嵌入计算，不降已驻留内存"}


# ---------------- ③ 检索：显式加载 + 状态 ----------------

@router.post("/api/bridge/stage3/load")
def stage3_load():
    """显式打开 seekdb 集合并预热嵌入模型（此后 pyseekdb/torch/ST 驻留，RSS 如实报）。"""
    if _STATE["collection_open"] and _STATE["st_model_loaded"]:
        import store
        return {"ok": True, "alreadyLoaded": True, "stats": store.stats(),
                "load": _STATE["last_stage3_load"]}
    rss_before = _rss_mb()
    t0 = time.time()
    try:
        import store
        import embedder
        st = store.stats()
        embedder.embed(["预热 warmup"])
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}",
                                                      "reason": "加载失败——检查 ~/recallloop-venv 与 ~/multi-ai-data/seekdb"})
    _STATE["collection_open"] = True
    _STATE["st_model_loaded"] = True
    rec = {"load_ms": round((time.time() - t0) * 1000),
           "rss_before_mb": rss_before, "rss_after_mb": _rss_mb()}
    _STATE["last_stage3_load"] = rec
    return {"ok": True, "alreadyLoaded": False, "stats": st, "load": rec}


def _stage3_view() -> Dict:
    light = (getattr(config, "STORE_PROVIDER", "seekdb") or "seekdb").lower() == "light"
    return {"open": _STATE["collection_open"], "st_model_loaded": _STATE["st_model_loaded"],
            "collection": config.COLLECTION_NAME, "seekdbPath": str(config.SEEKDB_PATH),
            "storeProvider": getattr(config, "STORE_PROVIDER", "seekdb"),
            "embeddingProvider": getattr(config, "EMBEDDING_PROVIDER", "local"),
            "lastLoad": _STATE["last_stage3_load"],
            "stats": None,  # 按需经 /api/stats 或 stage3/load 取，避免 overview 意外拉起重资产
            "hint": ("集合未打开时此环节不可检索；点「加载」显式拉起（light 轻量秒级，零 torch）"
                     if light else
                     "集合未打开时此环节不可检索；点「加载 seekdb 集合」显式拉起（约 20-40s）")}


# ---------------- ④ 简报：异步运行器（分阶段可观察） ----------------

class BriefRunIn(BaseModel):
    intent: str
    topic: str
    src: Optional[str] = None
    dst: Optional[str] = None
    catchup: bool = True


def _phase(job: Dict, name: str) -> None:
    job["phase"] = name
    job["phases"].append({"name": name, "at": round(time.time() - job["startedAt"], 3)})


def _brief_worker(jid: str, req: Dict) -> None:
    job = _JOBS[jid]
    timing: Dict = {}
    try:
        t0 = time.time()
        if req.get("catchup"):
            _phase(job, "catchup(懒补)")
            import catchup
            job["catchup"] = catchup.catch_up(force_full=False)
        timing["catchup_ms"] = round((time.time() - t0) * 1000)

        _phase(job, "warmup(载入模型)")
        t1 = time.time()
        import store  # noqa: F401
        import embedder
        rss_before = _rss_mb()
        store.stats()
        embedder.embed(["warmup 预热"])
        _STATE["collection_open"] = True
        _STATE["st_model_loaded"] = True
        timing["warmup_ms"] = round((time.time() - t1) * 1000)
        timing["rss_after_warmup_mb"] = _rss_mb()

        _phase(job, "build(检索+组装)")
        t2 = time.time()
        import handoff_brief
        md = handoff_brief.build_brief(req["intent"], req["topic"], req.get("src"), req.get("dst"))
        BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        out = BRIEFS_DIR / f"console-{time.strftime('%Y%m%d-%H%M%S')}.md"
        out.write_text(md, encoding="utf-8")
        timing["build_ms"] = round((time.time() - t2) * 1000)
        timing["total_ms"] = round((time.time() - t0) * 1000)
        job["result"] = {"path": str(out), "bytes": len(md.encode("utf-8")),
                         "markdown": md, "timing": timing}
        _phase(job, "done")
    except Exception as e:
        job["phase"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


@router.post("/api/bridge/brief/run")
def brief_run(req: BriefRunIn):
    with _job_lock:
        cur = _current_job["id"]
        if cur and _JOBS[cur]["phase"] not in ("done", "error"):
            return JSONResponse(status_code=409, content={"error": "已有简报任务在跑", "jobId": cur})
        jid = f"job-{time.strftime('%Y%m%d-%H%M%S')}"
        _JOBS[jid] = {"id": jid, "phase": "queued", "startedAt": time.time(),
                      "phases": [{"name": "queued", "at": 0.0}], "error": None, "result": None}
        _current_job["id"] = jid
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    threading.Thread(target=_brief_worker, args=(jid, data), daemon=True).start()
    return {"ok": True, "jobId": jid}


@router.get("/api/bridge/brief/status")
def brief_status(job_id: Optional[str] = None):
    jid = job_id or _current_job["id"]
    if not jid or jid not in _JOBS:
        return {"active": False, "job": None}
    return {"active": _JOBS[jid]["phase"] not in ("done", "error"), "job": _JOBS[jid]}


@router.get("/api/bridge/briefs")
def list_briefs():
    out: List[Dict] = []
    for d, src in ((BRIEFS_DIR, "console"), (MCP_BRIEFS_DIR, "mcp")):
        try:
            for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                st = p.stat()
                out.append({"name": p.name, "source": src, "bytes": st.st_size,
                            "mtimeIso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                            "path": str(p)})
        except Exception:
            pass
    return {"briefs": out[:60]}


@router.get("/api/bridge/brief/file")
def brief_file(path: str):
    """读一份简报原文。只允许 briefs 两个目录内，防目录穿越。"""
    p = Path(path).resolve()
    allowed = [str(BRIEFS_DIR.resolve()), str(MCP_BRIEFS_DIR.resolve())]
    if not any(str(p).startswith(a + os.sep) or str(p) == a for a in allowed):
        return JSONResponse(status_code=403, content={"error": "只允许读 briefs 目录内的文件"})
    if not p.exists():
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    return {"path": str(p), "markdown": p.read_text(encoding="utf-8", errors="replace")}


# ---------------- 台账 / 评测 / 检索日志 ----------------

@router.get("/api/bridge/ledger")
def ledger(limit: int = 50):
    rows: List[Dict] = []
    try:
        with open(LEDGER_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return {"path": str(LEDGER_PATH), "count": len(rows), "rows": rows[-limit:][::-1]}


_SUITE_TABLE = re.compile(
    r"^(\S+)\s+([\d.]+)\s+(\d+)%\s+(\d+)%\s+(\d+)%\s+(\d+)%\s+(\d+)%", re.M)


def _parse_suite(txt: str) -> Dict:
    cases = []
    for m in _SUITE_TABLE.finditer(txt):
        if m.group(1) == "case":
            continue
        cases.append({"case": m.group(1), "total": float(m.group(2)),
                      "open": int(m.group(3)), "fact": int(m.group(4)),
                      "noise": int(m.group(5)), "path": int(m.group(6)),
                      "struct": int(m.group(7))})
    avg = re.search(r"平均总分:\s*([\d.]+)", txt)
    return {"cases": cases, "avg": float(avg.group(1)) if avg else None}


@router.get("/api/bridge/eval")
def eval_report():
    """读评测报告（机械打分，答案 key 固定）。当前文件与 ST 基线备份都展示，不粉饰。"""
    out = []
    for f, label in (("_eval_suite_report_st.bak.txt", "ST 生产基线（备份）"),
                     ("_eval_suite_report_st_20261111.txt", "ST 同语料基线（2026-11-11 重跑）"),
                     ("_eval_suite_report.txt", "最近一次跑")):
        p = BACKEND_DIR / f
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
            out.append({"file": f, "label": label, "mtimeIso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
                "parsed": _parse_suite(txt), "raw": txt})
        except Exception:
            out.append({"file": f, "label": label, "missing": True})
    return {"reports": out,
            "weights": "④未解决线头召回 34 + 事实召回 26 + 无噪声 20 + 路径洁净 12 + 结构 8"}


@router.get("/api/bridge/retrieval_log")
def retrieval_log(limit: int = 10):
    p = config.DATA_DIR / "retrieval_log.jsonl"
    rows: List[Dict] = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return {"count": len(rows), "rows": rows[-limit:][::-1]}


# ---------------- 总览 + 服务生命周期 ----------------

@router.get("/api/bridge/overview")
def overview():
    _ensure_pidfiles()
    # ① events 元数据：没缓存就后台扫（/mnt/c 慢，不阻塞总览）
    with _events_lock:
        ev = dict(_events_cache) if _events_cache else None
    if ev is None or ("error" not in ev and "lines" not in ev):
        ev = ev or {}
        ev.setdefault("counting", True)
        threading.Thread(target=_scan_events_meta, daemon=True).start()
    wm = _read_json(config.DATA_DIR / "ingest-watermark.json") or {}
    stage1_lines = (ev or {}).get("lines")
    briefs = list_briefs()["briefs"]
    return {
        "server": _proc(),
        "stage1": {"file": _stat_file(EVENTS_PATH), "meta": ev,
                   "note": "v1 实时监控的只读产出；采集只读、真实聊天留本机"},
        "stage2": {"watermark": wm,
                   "newLinesSinceWatermark": (stage1_lines - wm.get("last_line", 0))
                   if isinstance(stage1_lines, int) and wm.get("last_line") else None,
                   "corpus": _stat_file(config.DATA_DIR / "events-corpus.jsonl"),
                   "delta": _stat_file(config.DATA_DIR / "events-corpus-delta.jsonl"),
                   "meta": _read_json(config.DATA_DIR / "events-corpus-meta.json"),
                   "note": "①→② 归一化 + redact 掩码；经增量懒补进集合"},
        "stage3": _stage3_view(),
        "stage4": {"briefs": briefs[:8], "briefsCount": len(briefs)},
        "ledger": {"file": _stat_file(LEDGER_PATH),
                   "note": "跨 agent 调用台账（MCP 轻壳落痕，桥侧证据）"},
    }


@router.post("/api/bridge/shutdown")
def shutdown():
    """优雅关闭：SIGTERM → uvicorn 收尾退出 → reg.cmd 自动注销。"""
    def _bye():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)
    threading.Timer(0.4, _bye).start()
    return {"ok": True, "message": "服务将在约 0.4s 后退出（SIGTERM）。再次启动：双击 start-bridge.cmd"}
