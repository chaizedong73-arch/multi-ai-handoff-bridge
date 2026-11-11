#!/usr/bin/env python3
"""真实事件流适配器：把 v1 监控产出的 events.jsonl 归一化为 seekdb 语料。

与旧的 export-corpus.mjs（只认 dsh/workbuddy/codex 三源、读原始日志）不同，
这里直接吃 v1 已经归一化好的实时事件流：一条 schema、六个 source、随监控实时追加。
因此新增任何 agent（zcode/trae/qoder/...）都无需改解析器——owner 即 source。

事件 kind：started / summary / updated / finished / interrupted / failed。
只保留携带文本（summary/result/reason）或产物（artifact）的事件为可检索块，
空的 updated 心跳丢弃，避免 32k 条噪声淹没检索。

输出记录沿用 store.build_doc 认识的三类：message / change / session。
额外保留 taskId / dependsOn（任务级协同的骨架）。

增量（①懒补）：--after-line N 只把「行号 > N」的事件产成 message/change 块，
但会话聚合（session 记录）永远按全量重算——因为 session 是可变的汇总记录，
它的 msgCount/state/mtime 依赖整条会话的历史，只喂增量会得到残缺会话，
而残缺会话经 stable id（ses_+hash(skey)）upsert 会覆盖掉之前完整的记录。
这就是「聚合记录陷阱」：增量对不可变的 message/change 安全，对可变的 session 必须全量。

用法：
  python events_to_corpus.py [--events 路径] [--out 路径]
                             [--since 30m|2h|1d|ISO] [--after-line N]
                             [--max-events N] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

DEFAULT_EVENTS = os.environ.get("MULTI_AI_EVENTS", "")  # 指向你的 agent 事件流（可选；无事件流时用 ingest 语料导入）

# 轻量脱敏：常见 token/key/密码形态。事件流本身已是监控的公开摘要，这里是纵深防御。
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{12,})"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]{16,}"), "bearer [REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GH]"),
]


def redact(text: str) -> str:
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def to_epoch(iso: str) -> int:
    if not iso:
        return 0
    try:
        s = iso.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


def since_floor(spec: str | None) -> int:
    if not spec:
        return 0
    m = re.fullmatch(r"(\d+)([mhd])", spec)
    if m:
        return int(time.time()) - int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    try:
        return int(datetime.fromisoformat(spec.replace("Z", "+00:00")).timestamp())
    except Exception:
        raise SystemExit(f"--since 应为 30m/2h/1d 或 ISO 日期，收到：{spec}")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def normalize(events_path: str, out_path: str, after_line: int = 0,
              since: str | None = None, max_events: int = 0) -> dict:
    """把 events.jsonl 归一化为 corpus.jsonl，返回统计字典。可被 catchup.py 直接调用。

    after_line>0 为增量模式：message/change 仅产出行号 > after_line 的事件；
    session 记录永远全量重算（见文件头「聚合记录陷阱」）。after_line=0 即全量。
    写文件（含 meta）后返回 out；不打印——是否打印由调用方决定。
    """
    if not os.path.exists(events_path):
        raise SystemExit(f"事件流不存在：{events_path}")

    cutoff = since_floor(since)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    stats = {
        "read": 0, "kept": 0, "emitted_messages": 0, "emitted_changes": 0,
        "skipped_empty": 0, "skipped_dup": 0, "skipped_old": 0, "artifacts": 0,
        "last_line": 0, "by_source": {}, "by_kind": {}, "sessions": {},
    }
    fout = open(out_path, "w", encoding="utf-8")

    def put(obj: dict) -> None:
        fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

    with open(events_path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            stats["last_line"] = lineno
            line = line.strip()
            if not line:
                continue
            stats["read"] += 1
            try:
                e = json.loads(line)
            except Exception:
                continue

            at = to_epoch(e.get("at", ""))
            if cutoff and at and at < cutoff:
                stats["skipped_old"] += 1
                continue

            src = e.get("source") or "unknown"
            kind = e.get("kind") or "?"
            sid = e.get("sessionId") or ""
            skey = f"{src}:{sid}"
            title = clean(e.get("sessionName"))
            cwd = e.get("workspaceDirectory") or ""
            task_id = e.get("taskId") or ""
            depends = e.get("dependsOn") or []
            if isinstance(depends, list):
                depends = ",".join(str(x) for x in depends if x)

            summary = clean(e.get("summary"))
            result = clean(e.get("result"))
            reason = clean(e.get("reason"))
            artifact = clean(e.get("artifact"))

            # 会话级聚合（每个 source:session 一条 session 记录）——不受 after_line 影响，全量累计
            sess = stats["sessions"].setdefault(skey, {
                "owner": src, "id": sid, "key": skey, "title": title, "cwd": cwd,
                "msgCount": 0, "changeCount": 0, "mtime": 0, "state": kind,
                "taskIds": set(), "seen": set(),
            })
            sess["mtime"] = max(sess["mtime"], at)
            sess["state"] = kind
            if title and (not sess["title"] or sess["title"].startswith("会话 ")):
                sess["title"] = title
            if cwd and not sess["cwd"]:
                sess["cwd"] = cwd
            if task_id:
                sess["taskIds"].add(task_id)

            emit = lineno > after_line  # 增量闸：仅新行产 message/change

            # 文本块：把 summary/result/reason 组装成一条可检索消息
            parts = []
            if summary:
                parts.append(summary)
            if result:
                parts.append(f"[结果] {result}")
            if reason:
                parts.append(f"[原因] {reason}")
            text = redact(" ".join(parts)).strip()

            if text and text in sess["seen"]:
                stats["skipped_dup"] += 1
            elif text:
                sess["seen"].add(text)
                stats["kept"] += 1
                stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
                stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
                sess["msgCount"] += 1
                if emit:
                    stats["emitted_messages"] += 1
                    put({
                        "type": "message",
                        "owner": src,
                        "sessionId": sid,
                        "sessionKey": skey,
                        "sessionTitle": title,
                        "role": kind,
                        "text": text[:6000],
                        "turn": e.get("seq"),
                        "at": at,
                        "srcFile": events_path,
                        "srcLine": lineno,
                        "cwd": cwd,
                        "taskId": task_id,
                        "dependsOn": depends,
                        "taskDone": bool(e.get("taskDone")),
                    })
            else:
                stats["skipped_empty"] += 1

            # 产物块：artifact 作为 change 记录（协同交接的“交付物”锚点）
            if artifact:
                stats["artifacts"] += 1
                sess["changeCount"] += 1
                if emit:
                    stats["emitted_changes"] += 1
                    put({
                        "type": "change",
                        "owner": src,
                        "sessionId": sid,
                        "sessionKey": skey,
                        "sessionTitle": title,
                        "action": "artifact",
                        "path": redact(artifact)[:1000],
                        "status": kind,
                        "at": at,
                        "srcFile": events_path,
                        "srcLine": lineno,
                        "cwd": cwd,
                        "taskId": task_id,
                    })

            if max_events and stats["emitted_messages"] >= max_events:
                break

    # 落 session 记录（永远全量：聚合记录必须整条重算，见文件头）
    for sess in stats["sessions"].values():
        put({
            "type": "session",
            "owner": sess["owner"],
            "id": sess["id"],
            "key": sess["key"],
            "title": redact(sess["title"])[:300],
            "model": "",
            "cwd": sess["cwd"],
            "state": sess["state"],
            "msgCount": sess["msgCount"],
            "changeCount": sess["changeCount"],
            "mtime": sess["mtime"],
            "taskIds": ",".join(sorted(sess["taskIds"]))[:500],
        })

    fout.close()

    out = {
        "ok": True,
        "out": out_path,
        "after_line": after_line,
        "last_line": stats["last_line"],
        "read": stats["read"],
        "kept_messages": stats["kept"],
        "emitted_messages": stats["emitted_messages"],
        "emitted_changes": stats["emitted_changes"],
        "artifacts": stats["artifacts"],
        "sessions": len(stats["sessions"]),
        "skipped_empty": stats["skipped_empty"],
        "skipped_dup": stats["skipped_dup"],
        "skipped_old": stats["skipped_old"],
        "by_source": stats["by_source"],
        "by_kind": stats["by_kind"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "notice": "只读导出 v1 实时事件流；含 redact 掩码；不含私有推理。真实聊天留本机。",
    }
    meta_path = os.path.join(os.path.dirname(out_path), "events-corpus-meta.json")
    with open(meta_path, "w", encoding="utf-8") as mf:
        json.dump(out, mf, ensure_ascii=False, indent=2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=os.environ.get("MULTI_AI_EVENTS", DEFAULT_EVENTS))
    ap.add_argument("--out", default=os.path.expanduser("~/multi-ai-data/events-corpus.jsonl"))
    ap.add_argument("--since", default=None)
    ap.add_argument("--after-line", type=int, default=0,
                    help="增量：仅产出行号 > N 的 message/change；session 仍全量重算")
    ap.add_argument("--max-events", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    out = normalize(args.events, args.out, after_line=args.after_line,
                    since=args.since, max_events=args.max_events)
    if not args.quiet:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
