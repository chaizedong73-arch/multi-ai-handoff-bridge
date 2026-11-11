#!/usr/bin/env python3
"""① 增量懒补：按行偏移水位线，把 events.jsonl 的新事件补进 seekdb，跑完即退。

为什么「懒补」而不常驻：v1 监控已经常驻产出 events.jsonl（便宜、无模型）。真正贵的是
「嵌入」——1.4GB 模型 + 全量重嵌。把两者解耦：数据持续产生（不嵌），只在有人接手时
（handoff_brief --catchup / MCP worker）按需冷启动补一次增量，跑完立刻退出、内存被回收。

三个关键设计（也是「你要能讲清的」料）：
- 水位线用「行偏移」不用时间戳：events.jsonl 是 append-only 不可变日志，行号单调递增，
  记「已处理到第几行」最稳；时间窗口会漏（相对窗跨界）、会重（乱序/迟到事件）。
- 至少一次 + 幂等 = 有效恰好一次：水位线在「导入成功之后」才落盘。若导入中途崩溃，
  下次会重跑同一段——但 store 用内容哈希 id 做 upsert，重复导入是覆盖、不产生副本。
  所以宁可重复处理，也不漏处理。
- 聚合记录陷阱：session 是可变汇总，增量喂会残缺覆盖完整（见 events_to_corpus 文件头）。
  故 delta corpus 里 message/change 只含新行，session 永远全量重算。

不常驻、不联网、不调外部 LLM。early-return（无新行）时不 import store、不加载模型。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Dict, Optional

os.environ.setdefault("MULTI_AI_COLLECTION", "multi_ai_events")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# EMBEDDING_PROVIDER 由 config 按 MULTI_AI_STORE 联动（0.7.0），不在此写死

import config  # noqa: E402
import events_to_corpus as e2c  # noqa: E402

DEFAULT_EVENTS = e2c.DEFAULT_EVENTS


def _watermark_path() -> str:
    return os.path.join(str(config.DATA_DIR), "ingest-watermark.json")


def read_watermark() -> Dict:
    p = _watermark_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            w = json.load(f)
        if isinstance(w, dict):
            return w
    except Exception:
        pass
    return {"last_line": 0, "events": "", "updated_at": 0}


def write_watermark(last_line: int, events: str) -> None:
    p = _watermark_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_line": int(last_line), "events": events,
                   "updated_at": int(time.time())}, f, ensure_ascii=False)
    os.replace(tmp, p)  # 原子替换：崩溃只会留下旧水位线，不会写坏


def count_lines(path: str) -> int:
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in f:
            n += 1
    return n


def catch_up(events: Optional[str] = None, force_full: bool = False) -> Dict:
    """把 events 相对水位线的新增补进 seekdb。返回统计（changed=False 表示无新行、未加载模型）。

    force_full=True 时忽略水位线做全量重导入（reset=True）——首灌或日志被重置时用。
    纯函数：不打印，是否输出由调用方决定。
    """
    events = events or os.environ.get("MULTI_AI_EVENTS", DEFAULT_EVENTS)
    if not os.path.exists(events):
        return {"ok": False, "error": f"事件流不存在：{events}", "changed": False}

    wm = read_watermark()
    last_line = int(wm.get("last_line") or 0)
    wm_events = wm.get("events") or ""
    cur_lines = count_lines(events)

    # 日志被重置/轮转/换源（行数变少或来源变了）→ 水位线失效，必须全量重灌
    rotated = (wm_events and wm_events != events) or (cur_lines < last_line)
    if force_full or rotated or last_line == 0:
        mode = "full"
        after_line = 0
        reset = True
    else:
        mode = "incremental"
        after_line = last_line
        reset = False

    # early-return：增量模式下没有新行 → 不 import store、不加载模型、直接退
    if mode == "incremental" and cur_lines <= last_line:
        return {"ok": True, "changed": False, "mode": mode,
                "last_line": last_line, "cur_lines": cur_lines,
                "notice": "无新事件，未加载嵌入模型（early-return）"}

    # 有增量（或全量）：此刻才 import store（触发 embedder），归一化 delta → upsert
    import store  # noqa: E402  延迟导入，保证 early-return 路径零模型开销

    delta_out = os.path.join(str(config.DATA_DIR), "events-corpus-delta.jsonl")
    norm = e2c.normalize(events, delta_out, after_line=after_line)
    imp = store.import_corpus(delta_out, reset=reset)

    # 导入成功后才推进水位线（至少一次 + 幂等 = 有效恰好一次）
    new_last = int(norm.get("last_line") or cur_lines)
    write_watermark(new_last, events)

    out = {
        "ok": True, "changed": True, "mode": mode,
        "after_line": after_line, "last_line": new_last,
        "prev_last_line": last_line, "cur_lines": cur_lines,
        "emitted_messages": norm.get("emitted_messages", 0),
        "emitted_changes": norm.get("emitted_changes", 0),
        "sessions": norm.get("sessions", 0),
        "collection": config.COLLECTION_NAME,
        "collectionCount": imp.get("collectionCount"),
        "written": imp.get("written"),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=None)
    ap.add_argument("--full", action="store_true", help="忽略水位线，全量重灌")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    res = catch_up(a.events, force_full=a.full)
    if not a.quiet:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
