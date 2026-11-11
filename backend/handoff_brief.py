#!/usr/bin/env python3
"""④ 交接闭环第一刀：按需《交接简报》生成器。

输入一个接手意图，冷启动多 facet 检索真实事件语料（multi_ai_events），
按事件类型分桶组装成一份自包含、可回溯（每条带 events.jsonl:行号）的交接简报。

刻意不做的事：
- 不调用任何外部 LLM（隐私：对话不出本地），不做 AI 摘要——综合判断是接手方 agent 的活，
  我们只负责把散落在各 agent 的原始过程「捞全 + 去重 + 结构化 + 标出处」。
- 不常驻：一次冷启动跑完即退，内存随即被 WSL 回收。

三处针对性设计：
- ④ 未解决线头：双通道。①规约标记 [未完成]（精确，源头改规约后生效）；
  ②线索词挖掘（尚未/待/需首次…，可审计、可扩展，兜住历史事件）。
- ⑥ 路径抽取：优先级 + 最长匹配的小词法器（maximal munch），URL 先整体吃掉，
  扩展名走白名单，结构性消灭 "s://…" 断尾与 ".trash" 误判。
- ① 意图锚定：必须命中 topic 关键词或高分，杜绝凑数的跨项目 started。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import math
import os
import re
from typing import Dict, List, Optional, Tuple

os.environ.setdefault("MULTI_AI_COLLECTION", "multi_ai_events")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# EMBEDDING_PROVIDER 由 config 按 MULTI_AI_STORE 联动（0.7.0），不在此写死

import config  # noqa: E402
import store   # noqa: E402


# ============ ④ 未解决线头：线索词（可扩展，可用环境变量 MULTI_AI_OPEN_CUES 覆盖） ============
_DEFAULT_CUES = [
    "尚未", "尚待", "有待", "待验证", "待提交", "待联调", "待办", "待定",
    "未完成", "没完成", "没能完成", "未实现", "没实现", "未联调", "未能",
    "还不会", "还没", "还不能", "暂无法", "暂未", "暂不",
    "被拦截", "需首次", "需验证", "后续需", "无法确定", "来不及", "尚未完成",
    # 真实语料里接手方/交出方表达"没做完/没把握/有隐患"的常见说法（评测集扩充时补）
    "没有完全", "不能完全", "不能回答", "不能保证", "不算解决", "没有实际",
    "没有验证", "没验证", "仍未", "没修好", "没有修好", "没抓到", "没有抓到",
    "会丢", "丢记录", "丢笔记", "未确认", "没确认", "还没有", "没找到",
    # 0.7.2 补（录音记忆首考 ④ 漏掉 11 月线头的词形）：未签名 / 还需试 / 待试
    "未签名", "还需", "待试",
]
_OPEN_CUES = [c for c in os.environ.get("MULTI_AI_OPEN_CUES", "").split(",") if c.strip()] or _DEFAULT_CUES
# 规约标记：源头改规约后，agent 在结果里写 [未完成] xxx，这里精确抽取
_MARKER_RE = re.compile(r"\[未完成\]\s*([^\[]+)")


# ============ ⑥ 路径抽取：优先级 + 最长匹配词法器 ============
_EXT = (r"(?:md|markdown|py|mjs|c?js|ts|tsx|json|jsonl|txt|log|sqlite3?|db|"
        r"ya?ml|csv|html?|exe|dll|onnx|png|jpe?g|gif|pdf|toml|ini|cfg|conf|sh|bat|ps1)")
_SEG = r"[^\s<>:\"'|?*\\/]"          # 路径段字符（不含分隔符/非法字符）
_URL_BODY = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"  # RFC3986 允许字符集（天然排除中文/全角）
_TOKEN_RULES: List[Tuple[str, "re.Pattern"]] = [
    # 优先级从高到低；同长时高优先级胜出。URL 先整体吃掉 → 内部 "s:" 不再被重扫。
    ("url",   re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://" + _URL_BODY + r"+")),
    ("unc",   re.compile(r"\\\\[^\s<>\"'|?*]+?\." + _EXT + r"(?![A-Za-z0-9])")),
    ("win",   re.compile(r"[A-Za-z]:[\\/](?:" + _SEG + r"+[\\/])*" + _SEG + r"+?\." + _EXT + r"(?![A-Za-z0-9])", re.I)),
    # posix 要求≥2段，杜绝目录树里 "/项目主页.md" 这种半截碎片
    ("posix", re.compile(r"(?:/[\w.\-]+){2,}\." + _EXT + r"(?![A-Za-z0-9])", re.I)),
]
_URL_TRAIL = ".,;:!?)]}'\""


def _lex_paths(text: str) -> Tuple[List[str], List[str]]:
    """maximal-munch 扫描：每个位置取最长匹配（同长取高优先级），消费整块后前进。"""
    paths: List[str] = []
    urls: List[str] = []
    i, n = 0, len(text)
    while i < n:
        best: Optional[Tuple[int, int, str, str]] = None  # (len, prio, type, value)
        for prio, (ttype, rx) in enumerate(_TOKEN_RULES):
            m = rx.match(text, i)
            if m and m.end() > i:
                ln = m.end() - i
                if best is None or ln > best[0] or (ln == best[0] and prio < best[1]):
                    best = (ln, prio, ttype, m.group(0))
        if best:
            ln, _prio, ttype, val = best
            if ttype == "url":
                urls.append(val.rstrip(_URL_TRAIL))
            else:
                paths.append(val.rstrip(".,;、"))
            i += ln
        else:
            i += 1
    return paths, urls



# ============ 通用小工具 ============
def _fmt_at(a) -> str:
    try:
        f = float(a)
        if f > 1e9:
            return _dt.datetime.fromtimestamp(f, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        if f > 0:
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return ""


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def _strip_prefix(t: str) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", _clean(t))


def _cite(r: Dict) -> str:
    c = r.get("citation", {})
    line = c.get("srcLine", "")
    src = os.path.basename(c.get("srcFile", "") or "events.jsonl")
    at = _fmt_at(c.get("at"))
    tag = f"{src}:{line}" if line else src
    return tag + (f" · {at}" if at else "")


def _cite_short(r: Dict) -> str:
    return _cite(r).split(" · ")[0]


def _retrieve(query: str, top_k: int, owner: Optional[str] = None, after: Optional[float] = None) -> List[Dict]:
    res, _log = store.hybrid_search(query, top_k=top_k, owner=owner, after=after)
    return res or []


def _role(r: Dict) -> str:
    return (r.get("meta", {}) or {}).get("role", "") or ""


def _owner(r: Dict) -> str:
    return (r.get("meta", {}) or {}).get("owner", "") or ""


def _kind(r: Dict) -> str:
    return (r.get("meta", {}) or {}).get("kind", "") or ""


def _sid(r: Dict) -> str:
    return (r.get("meta", {}) or {}).get("sessionKey", "") or ""


_CORPUS_SESS_CACHE: Optional[Dict[str, str]] = None


def _corpus_sessions() -> Dict[str, str]:
    """检索库（seekdb）实际索引的全量 message 文档：sessionKey -> 拼接小写正文，进程级缓存。

    为什么用 store 而不是 corpus.jsonl：seekdb 可能由更早/更全的语料导入，磁盘上的
    corpus.jsonl 未必是同一份（实测两者会话数对不上，按文件算 df 会把库里真实存在的
    词误判成 df=0）。直接读 store 的全量缓存，保证 df 的分母与检索宇宙一致。
    为什么不用池内 df：检索池已按 topic 过滤，泛词（B站/视频/字幕）在池里显得"稀有"，
    idf 反而高，串台的采集工作流会话因此被误判成锚。全库 df 才能区分识别性词与泛词。
    """
    global _CORPUS_SESS_CACHE
    if _CORPUS_SESS_CACHE is not None:
        return _CORPUS_SESS_CACHE
    buckets: Dict[str, list] = {}
    try:
        scan = store._scan_all()
        docs = scan.get("documents") or []
        metas = scan.get("metadatas") or []
        for doc, m in zip(docs, metas):
            if not doc:
                continue
            m = m or {}
            if m.get("kind") != "message":
                continue
            sid = m.get("sessionKey") or ""
            if not sid:
                continue
            buckets.setdefault(sid, []).append(str(doc).lower())
    except Exception:
        buckets = {}
    _CORPUS_SESS_CACHE = {k: "\n".join(v) for k, v in buckets.items()}
    return _CORPUS_SESS_CACHE


def _corpus_idf(kws: List[str]):
    """基于全语料（seekdb 实际索引宇宙）的类 BM25 idf + 识别性词集合。

    df=含该词的会话数，N=会话总数。返回 (idf, N, df, ident)：
    ident = 「识别性词」= df ≤ max(2, 7%N) 的词——只出现在极少数会话，是项目的
    专属标识（哔嘟/bilisoft/进程监测）。泛词（B站 df9/字幕 df12/播放 df13）不入 ident。
    锚会话质量只按 ident 累加，避免"大会话靠泛词刷屏"被误判成锚。
    """
    cs = _corpus_sessions()
    N = len(cs)
    if N == 0:
        return {}, 0, {}, set()
    df = {k: 0 for k in kws}
    for t in cs.values():
        for k in kws:
            if k.lower() in t:
                df[k] += 1
    idf = {k: math.log((N - df[k] + 0.5) / (df[k] + 0.5) + 1.0) for k in kws}
    cut = max(2, int(round(0.07 * N)))
    ident = {k for k in kws if df[k] <= cut}
    return idf, N, df, ident


def _detect_anchors(dedup: "_Dedup", kws: List[str], dbg_tag: str = "", owners: Optional[set] = None) -> set:
    """锚会话检测：项目的真实事件会在少数几个会话里反复出现。

    owners=None 对全池评分；传入 owner 集合时只在该 owner 的消息内评锚——
    0.7.2 实测修复：接手方当前大会话会在全局质量上压垮交出方会话、吸走全部锚，
    交出方近 10 天的成果因此进不了②③④（录音记忆首考 before 38.9/100 的根因）。
    build_brief 对 src/dst 各跑一次保底锚并集。
    """
    msgs = [r for r in dedup.all() if _kind(r) == "message"
            and (owners is None or _owner(r) in owners)]
    idf, N, df, ident = _corpus_idf(kws)
    if not idf:  # 语料缺失 → 退回池内 df 估计
        sess0: set = set()
        df0: Dict[str, set] = {k: set() for k in kws}
        for r in msgs:
            sid = _sid(r)
            if not sid:
                continue
            sess0.add(sid)
            t = (r.get("text", "") or "").lower()
            for k in kws:
                if k.lower() in t:
                    df0[k].add(sid)
        N = max(1, len(sess0))
        idf = {k: math.log((N - len(df0[k]) + 0.5) / (len(df0[k]) + 0.5) + 1.0) for k in kws}
        df = {k: len(v) for k, v in df0.items()}
        ident = set(kws)
    if not ident:  # 全是泛词（极端情况）→ 退回全部词，避免无锚
        ident = set(kws)
    # 识别性词 idf 加权累加每会话质量（泛词贡献 0）
    mass: Dict[str, float] = {}
    for r in msgs:
        sid = _sid(r)
        if not sid:
            continue
        t = (r.get("text", "") or "").lower()
        w = 0.0
        for k in kws:
            if k in ident and k.lower() in t:
                w += idf.get(k, 0.0)
        if w > 0:
            mass[sid] = mass.get(sid, 0.0) + w
    if dbg_tag:
        try:
            with open("_anchor_dbg.txt", "a", encoding="utf-8") as f:
                f.write("=== %s  Ncorpus=%d  ident=%s ===\n" % (dbg_tag, N, sorted(ident)))
                f.write("df:  %s\n" % {k: df.get(k) for k in sorted(kws, key=lambda x: df.get(x, 0))})
                for s, c in sorted(mass.items(), key=lambda x: -x[1])[:18]:
                    f.write("  %7.2f  %s\n" % (c, s))
        except Exception:
            pass
    if not mass:
        return set()
    top = max(mass.values())
    thresh = max(2.0, 0.22 * top)
    return {s for s, c in mass.items() if c >= thresh}


# ============ PowerContext 记忆复用（0.7.5，可选） ============
# 对齐赛题要求「记忆被保存并在后续使用中复用」：生成前搜历史交接记忆（跨次接手复用），
# 落盘后 remember 本次交接 + 客户端自播 handoff 工件（upsert）。Server 不可用时全部
# 静默跳过，简报生成本身不受影响。

def _pc_read(topic: str) -> Optional[Dict]:
    """生成前：按主题搜 PowerContext 历史交接记忆。不可用返回 None。"""
    try:
        import pc
        if not pc.is_available():
            return None
        res = pc.search(topic, limit=5)
        hits = [h for h in (res.get("hits") or []) if str(h.get("text") or "").strip()]
        return {"hits": hits[:5]} if hits else None
    except Exception:
        return None


def _pc_write(intent: str, topic: str, src, dst, md: str, out_path: str) -> Optional[Dict]:
    """落盘后：remember 一条交接记录 + 客户端自播 handoff 工件（upsert）。失败不致命。"""
    try:
        import pc
        if not pc.is_available():
            return None
        rec = pc.remember(
            f"交接简报已生成：topic={topic}（{src}→{dst}）；intent={intent}；"
            f"产物={out_path}（{len(md.encode('utf-8'))} bytes）。未完成线头见简报④。",
            kind="working_note", reason="④交接简报落盘（PowerContext 记忆复用）")
        art = pc.create_handoff(
            objective=f"交接简报：{topic}",
            state=[{"summary": f"intent={intent}；产物={out_path}", "citations": []}],
            disposition="continuable",
            next_action={"summary": "接手方按简报④未解决线头继续，终审对照 vault 维护日志"},
            omissions=[{"summary": "简报为机械检索线索（无 LLM 综合），非终稿"}])
        return {"entry_id": (rec.get("citation") or {}).get("entry_id"),
                "artifact_id": (art or {}).get("artifact_id")}
    except Exception as e:
        print(f"[pc] PowerContext 记忆写入跳过（不致命）：{type(e).__name__}: {e}")
        return None



class _Dedup:
    """跨 facet 去重：同一段文本只保留分数最高的一次出现。"""

    def __init__(self) -> None:
        self._seen: Dict[str, Dict] = {}

    def add(self, r: Dict) -> None:
        key = _clean(r.get("text", ""))[:200]
        if not key:
            return
        prev = self._seen.get(key)
        if prev is None or r.get("score", 0) > prev.get("score", 0):
            self._seen[key] = r

    def all(self) -> List[Dict]:
        return list(self._seen.values())

    def take(self, pred, limit: int) -> List[Dict]:
        out = [r for r in self._seen.values() if pred(r)]
        out.sort(key=lambda r: r.get("score", 0), reverse=True)
        return out[:limit]


def _topic_kws(topic: str) -> List[str]:
    return [k for k in re.split(r"[\s,，、]+", topic) if k]


def _topic_hit(text: str, kws: List[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in kws)


# ============ ④ 未解决线头挖掘 ============
def _mine_open(dedup: _Dedup, anchors: set, kws: List[str],
               src: Optional[str], dst: Optional[str], recent_cut: float = 0.0) -> List[Dict]:
    """双通道抽取未完成项：规约标记（精确）优先，其次线索词（可审计）。

    在「交接双方」的完成汇报里挖（role=finished/summary、owner∈{src,dst}）——
    未完成项既可能写在交出方的成功汇报里，更常由接手方在评估/整合时点出
    （如 deepseek→chatgpt 的进程登记项目，5 个问题全是接手方 chatgpt 复现的）。
    只卡 src 会漏掉接手方的线头，故放宽到交接这一对 agent；user turn / started
    里的"尚未"多是误报，仍排除。
    相关性双闸：事件属于锚会话，或该小句自身命中 topic 词。
    这样能保住串台会话里真属于本项目的那一句（如 01a0c29a 的"真实笔记 AI 联调"），
    又能挡掉别的项目的完成汇报（如知乎/Duck.ai 网站验证）。
    """
    owners = {o for o in (src, dst) if o}
    items: List[Dict] = []
    seen_clause = set()
    for r in dedup.all():
        if _kind(r) != "message" or _role(r) not in ("finished", "summary"):
            continue
        if owners and _owner(r) not in owners:
            continue
        in_anchor = _sid(r) in anchors
        try:
            r_at = float(r.get("citation", {}).get("at") or 0)
        except (TypeError, ValueError):
            r_at = 0.0
        recent = bool(recent_cut) and r_at >= recent_cut
        text = _strip_prefix(r.get("text", ""))
        # 通道1：规约标记 [未完成]（精确，保持旧语义一律 tier0）
        for m in _MARKER_RE.finditer(text):
            clause = _clean(m.group(1))[:160]
            if clause and clause not in seen_clause and (in_anchor or _topic_hit(clause, kws) or recent):
                seen_clause.add(clause)
                items.append({"conf": "规约标记", "cue": "[未完成]", "clause": clause, "r": r, "tier": 0})
        # 通道2：线索词，按小句切分，只保留含线索词的那一句（可审计）
        for clause in re.split(r"[。；;\n]", text):
            clause = _clean(clause)
            if not clause or clause in seen_clause:
                continue
            for cue in _OPEN_CUES:
                if cue in clause:
                    clause_hit = _topic_hit(clause, kws)
                    if in_anchor or clause_hit or recent:
                        seen_clause.add(clause)
                        # tier1=小句点名项目；tier2=锚会话邻近（旧语义）；tier3=近期窗口+线索词（0.7.2）
                        items.append({"conf": "线索词", "cue": cue, "clause": clause[:160], "r": r,
                                      "tier": 1 if clause_hit else (2 if in_anchor else 3)})
                    break
    # 排序：规约标记 > 小句点名项目 > 锚会话邻近 > 近期窗口兜底，同层按分数
    items.sort(key=lambda x: (x["tier"], -x["r"].get("score", 0)))
    return items




def _line(r: Dict, maxlen: int = 320) -> str:
    txt = _strip_prefix(r.get("text", ""))
    if len(txt) > maxlen:
        txt = txt[:maxlen] + "…"
    return f"  - {txt}\n    ← {_cite(r)}  [{_owner(r)}/{_role(r)} score={r.get('score')}]"


def build_brief(intent: str, topic: str, src: Optional[str], dst: Optional[str], pc_read: Optional[Dict] = None) -> str:
    _NOW = _dt.datetime.now().timestamp()
    _RECENT_CUT = _NOW - 14 * 86400  # 最近 14 天（0.7.2 实时性保障的窗口）

    def _at(r: Dict) -> float:
        try:
            return float(r.get("citation", {}).get("at") or 0)
        except (TypeError, ValueError):
            return 0.0

    dedup = _Dedup()
    facets: List[Tuple[str, Optional[str], int]] = [
        (f"{topic} 接手 项目 交接", None, 30),
        (f"{topic} 已完成 实现 修复 做成 结果", src, 50),
        (f"{topic} 决策 方案 结论 改名 整合", src, 30),
        (f"{topic} 问题 失败 中断 无法 未 待验证", src, 30),
        # 专门捞"未完成"语义：topic + 线索词，提升 ④ 召回（topic 在前保住精度，不误捞别的项目）
        (f"{topic} 尚未 还没 未实现 待验证 需首次 未完成 暂不 无法确定", src, 60),
        (f"{topic} 尚未 还没 未实现 待验证 需首次", None, 50),
        (f"{topic}", None, 60),
        # 纯 topic 按 owner 召回： peripheral 提及（如"按钮编译PID""基站主页里顺带说进程监测入口"）
        # 在 cue 混合查询里排名很低（实测 deepseek 2411 排 130、chatgpt 4733 排 136），
        # 用不带 cue 的纯 topic 查询各自深捞一遍（k=150），保住召回；精度交给下游锚/小句闸。
        (f"{topic}", src, 150),
    ]
    if dst:
        # 接手方的完成汇报与未完成线头常由 dst 说出（评估/整合阶段），需对称召回
        facets.append((f"{intent}", dst, 24))
        facets.append((f"{topic} 已完成 实现 修复 做成 结果 评估 整合", dst, 40))
        facets.append((f"{topic} 问题 失败 尚未 还没 未实现 待验证 无法确定", dst, 60))
        facets.append((f"{topic}", dst, 150))
    # 0.7.2 实时性 facet：只在最近 14 天窗口内检索。历史高分条目会整体淹没近期
    # 小会话（实测 11 月里程碑检索分仅 0.26-0.30、进不了 top150），必须靠时间窗
    # 把近期候选单独捞进池。窗口内宇宙小，k 给足（实测 40 会把 Release/安卓交付
    # 这类低分里程碑排在窗口外），识别词会话判定才凑得齐 ≥2 条命中。
    facets.append((f"{topic}", src, 120, _RECENT_CUT))
    facets.append((f"{topic} 已完成 实现 修复 发布 交付 验证", None, 40, _RECENT_CUT))
    facets.append((f"{topic}", None, 80, _RECENT_CUT))
    if dst:
        facets.append((f"{topic}", dst, 60, _RECENT_CUT))
    for facet in facets:
        q, own, k = facet[0], facet[1], facet[2]
        after = facet[3] if len(facet) > 3 else None
        for r in _retrieve(q, k, owner=own, after=after):
            dedup.add(r)

    kws = _topic_kws(topic)
    anchors = _detect_anchors(dedup, kws, dbg_tag=topic[:24])
    # 每 owner 保底锚（0.7.2）：全局锚容易被接手方当前大会话独占；
    # src/dst 各自按同判据评一次锚并取并集，交出方的近期会话才浮得上来。
    for _ow in {src, dst} - {None}:
        anchors |= _detect_anchors(dedup, kws, dbg_tag=f"{topic[:12]}:{_ow}", owners={_ow})
    HIGH = 0.55  # 非锚会话需高分才算相关；锚会话内低分事件也算（如 15730 score 0.39）

    # 0.7.2 会话归属判定：只用「识别性词」（全库低词频、项目专属，锚检测同一 idf 机制）
    # 命中的会话才算本项目的近期会话。topic 泛词（B站/视频/播放）与裸分数都会把
    # 跨项目近期条目放进来（实测 33181/34188 两种路径都漏），识别词是唯一稳的。
    _idfN, _Nc, _dfc, _ident = _corpus_idf(kws)
    if not _ident:
        _ident = set(kws)  # 语料缺失（idf 不可算）时退回全部主题词
    # 识别词只统计【近期窗口内】的会话消息，且须 ≥2 条命中（防单句偶发误归属，
    # 实测 33181 靠一句「登记」混入进程考题）：混合长会话的历史不算数，
    # 「最近两周反复在谈本项目」才是归属证据。
    _ident_hits: Dict[str, int] = {}
    for r in dedup.all():
        if _at(r) < _RECENT_CUT:
            continue
        t = (r.get("text", "") or "").lower()
        if any(k.lower() in t for k in _ident):
            sk = _sid(r)
            _ident_hits[sk] = _ident_hits.get(sk, 0) + 1
    # 会话标题命中识别词 = 2 条证据（标题是用户给整场对话定的主题，比单条正文更强；
    # 实测安卓/配对会话标题「排查录音记忆项目网关联动问题」正文里反而没有「录音记忆」字样）。
    try:
        _sess_titles = {s.get("sessionKey", ""): (s.get("title", "") or "") for s in store.list_sessions()}
    except Exception:
        _sess_titles = {}
    for sk, title in _sess_titles.items():
        t = (title or "").lower()
        if sk and t and any(k.lower() in t for k in _ident):
            _ident_hits[sk] = _ident_hits.get(sk, 0) + 2
    _ident_sessions = {sk for sk, n in _ident_hits.items() if n >= 2}

    def _recent_ok(r: Dict) -> bool:
        """最近 14 天条目的收录判据：所属会话被识别词命中（本项目会话），
        或自身相关分≥0.5（强相关兜底，覆盖识别词表述变体）。"""
        return (_at(r) >= _RECENT_CUT
                and (_sid(r) in _ident_sessions
                     or r.get("score", 0) >= 0.5))

    def rel(r: Dict) -> bool:
        return _sid(r) in anchors or r.get("score", 0) >= HIGH

    # ① 意图：必须命中 topic 关键词或高分（≥0.65），且相关（锚会话/高分）
    started = [r for r in dedup.take(lambda r: _role(r) == "started" and _kind(r) == "message" and rel(r), 12)
               if _topic_hit(r.get("text", ""), kws) or r.get("score", 0) >= 0.65]
    started = started[:4]
    finished = dedup.take(lambda r: _role(r) == "finished" and _kind(r) == "message" and rel(r), 10)
    # 0.7.2 实时性保障：最近 14 天的成果单独成区，不被历史高分条目挤出②。
    # 门槛放宽（topic 命中或 score≥0.45），因为近期小会话拿不到锚、也常够不到 0.55。
    recent_finished = [r for r in dedup.all()
                       if _kind(r) == "message" and _role(r) == "finished"
                       and _recent_ok(r)
                       and r["text"][:200] not in {_x.get("text", "")[:200] for _x in finished}]
    recent_finished.sort(key=_at, reverse=True)
    recent_finished = recent_finished[:8]
    decisions = dedup.take(lambda r: _role(r) == "summary" and _kind(r) == "message" and rel(r)
                           and _topic_hit(r.get("text", ""), kws), 8)
    recent_decisions = [r for r in dedup.all()
                        if _kind(r) == "message" and _role(r) == "summary"
                        and _recent_ok(r)
                        and r["text"][:200] not in {_x.get("text", "")[:200] for _x in decisions}]
    recent_decisions.sort(key=_at, reverse=True)
    recent_decisions = recent_decisions[:6]
    typed_open = dedup.take(lambda r: _role(r) in ("interrupted", "failed") and _kind(r) == "message" and rel(r), 6)
    # ⑤ 跨 agent：本次双方之外、且属于锚会话的痕迹（含 started，保住 workbuddy 原始灵感）
    others = dedup.take(lambda r: _kind(r) == "message" and _owner(r) not in (src, dst)
                        and _role(r) in ("started", "finished", "summary")
                        and _sid(r) in anchors, 8)
    sessions = dedup.take(lambda r: _kind(r) == "session" and rel(r), 4)

    open_items = _mine_open(dedup, anchors, kws, src, dst, recent_cut=_RECENT_CUT)


    # ⑥ 词法器抽路径（对全池文本），去重保序
    paths: List[str] = []
    urls: List[str] = []
    seen_p, seen_u = set(), set()
    for r in dedup.all():
        ps, us = _lex_paths(r.get("text", "") or "")
        for p in ps:
            if p not in seen_p:
                seen_p.add(p)
                paths.append(p)
        for u in us:
            if u not in seen_u:
                seen_u.add(u)
                urls.append(u)

    timeline = dedup.take(lambda r: _kind(r) == "message" and _role(r) in ("started", "finished", "failed", "interrupted")
                          and _sid(r) in anchors, 40)
    # 0.7.2：时间线并入最近 14 天的相关事件（小会话无锚也要在时间线留痕），去重后按时间排。
    _tl_seen = {_sid(r) + "|" + _clean(r.get("text", ""))[:60] for r in timeline}
    _recent_tl = [r for r in dedup.all()
                  if _kind(r) == "message" and _role(r) in ("started", "finished", "failed", "interrupted")
                  and _recent_ok(r)
                  and _sid(r) + "|" + _clean(r.get("text", ""))[:60] not in _tl_seen]
    timeline = timeline + _recent_tl[:12]
    timeline.sort(key=_at)

    L: List[str] = []
    L.append("# 交接简报")
    L.append("")
    L.append(f"- **接手意图**：{intent}")
    L.append(f"- **交出方 → 接手方**：{src or '(不限)'} → {dst or '(不限)'}")
    L.append(f"- **检索主题**：{topic}")
    L.append(f"- **锚定会话**：{len(anchors)} 个（topic 命中质量达阈值=项目核心；只 name-drop 一两次的串台会话被排除）")
    L.append(f"- **语料库**：{config.COLLECTION_NAME}（真实 events.jsonl 全量 6 agent）")
    L.append(f"- **生成时间**：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（按需冷启动，非常驻）")
    if pc_read and pc_read.get("hits"):
        L.append(f"- **历史交接记忆**：PowerContext 命中 {len(pc_read['hits'])} 条相关记录（跨次交接复用），"
                 f"最近一条：{_clean(pc_read['hits'][0].get('text', ''))[:80]}")
    L.append("")
    L.append("> 每条均带 `events.jsonl:行号` 出处，可回溯核对。本简报只做「捞全+去重+标出处」，")
    L.append("> 不做 AI 摘要——综合与决策留给接手方 agent。")
    L.append("")

    def section(title: str, items: List[Dict], empty: str) -> None:
        L.append(f"## {title}")
        if not items:
            L.append(f"  （{empty}）")
        else:
            for r in items:
                L.append(_line(r))
        L.append("")

    section("① 项目意图 / 交接请求（已锚定 topic）", started, "无强相关 started 事件")
    L.append("## ② 已完成的工作（真实结果）")
    if recent_finished:
        L.append("  【最近 14 天成果（0.7.2 实时性保障，小会话无锚也在列）】")
        for r in recent_finished:
            L.append(_line(r))
        L.append("")
    if finished:
        L.append("  【历史成果（按相关分）】")
        for r in finished:
            L.append(_line(r))
    if not recent_finished and not finished:
        L.append("  （无 finished 结果）")
    L.append("")
    L.append("## ③ 关键决策 / 结论")
    if recent_decisions:
        L.append("  【最近 14 天决策（0.7.2 实时性保障）】")
        for r in recent_decisions:
            L.append(_line(r))
        L.append("")
    if decisions:
        L.append("  【历史决策（按相关分）】")
        for r in decisions:
            L.append(_line(r))
    if not recent_decisions and not decisions:
        L.append("  （无 summary 决策）")
    L.append("")

    # ④ 未解决线头：类型事件 + 正文挖掘
    L.append("## ④ 未解决的线头（类型事件 + 正文线索词/规约标记挖掘）")
    if typed_open:
        L.append("  【显式 interrupted/failed 事件】")
        for r in typed_open:
            L.append(_line(r))
    if open_items:
        L.append("  【从成功汇报正文里挖出的未完成项（标注命中依据，可审计）】")
        for it in open_items[:40]:
            r = it["r"]
            L.append(f"  - ({it['conf']}:{it['cue']}) {it['clause']}")
            L.append(f"    ← {_cite(r)}  [{_owner(r)}/{_role(r)}]")
    if not typed_open and not open_items:
        L.append("  （确无未解决线头）")
    L.append("")

    section("⑤ 跨 agent 痕迹（本次交接双方之外的 agent 也碰过）", others, "无其他 agent 相关记录")

    L.append("## ⑥ 关键路径 / 产物（词法器抽取，扩展名白名单）")
    if paths:
        for p in paths[:24]:
            L.append(f"  - `{p}`")
    else:
        L.append("  （未抽到文件路径）")
    if urls:
        L.append("  参考链接（URL，已与路径分离）：")
        for u in urls[:8]:
            L.append(f"  - {u}")
    L.append("")

    L.append("## ⑦ 相关会话")
    if sessions:
        for r in sessions:
            sk = (r.get("meta", {}) or {}).get("sessionKey", "")
            L.append(f"  - {sk}  ← {_cite(r)}  score={r.get('score')}")
    else:
        L.append("  （无 session 记录）")
    L.append("")

    L.append("## ⑧ 时间线（started/finished/failed，按时间）")
    if timeline:
        for r in timeline[:20]:
            at = _fmt_at(r.get("citation", {}).get("at"))
            txt = _strip_prefix(r.get("text", ""))[:110]
            L.append(f"  - {at}  [{_owner(r)}/{_role(r)}] {txt}  ← {_cite_short(r)}")
    else:
        L.append("  （无可排序时间线）")
    L.append("")

    L.append("## ⑨ 建议接手方优先处理")
    L.append("  （由 ④ 未解决线头机械推导，非 AI 生成）")
    if open_items or typed_open:
        src_items = ([{"clause": _strip_prefix(r.get("text", ""))[:120], "r": r, "conf": "事件", "cue": _role(r), "tier": 0} for r in typed_open]
                     + open_items)
        # 0.7.2：最近 14 天的线头排最前（接手方最该先看的是「现在还没完的」），再按层级与分数
        src_items.sort(key=lambda x: (0 if _at(x["r"]) >= _RECENT_CUT else 1, x.get("tier", 0), -x["r"].get("score", 0)))
        for it in src_items[:5]:
            L.append(f"  - [待办·{it['conf']}:{it['cue']}] {it['clause']}  ← {_cite_short(it['r'])}")
    elif finished:
        L.append(f"  - 无显式未完成项；建议从 ② 最新结果接着做：{_strip_prefix(finished[0].get('text',''))[:100]}  ← {_cite_short(finished[0])}")
    else:
        L.append("  - （无足够信息推导，请接手方先读 ②③）")
    L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--src", default=None)
    ap.add_argument("--dst", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--catchup", action="store_true",
                    help="生成前先按水位线增量补录 events.jsonl 新事件（同进程，模型只加载一次）")
    ap.add_argument("--full", action="store_true",
                    help="配合 --catchup：忽略水位线全量重灌")
    a = ap.parse_args()

    # ① 懒补：与检索同进程，cold-start 只加载一次嵌入模型；无新行时 early-return 不碰模型
    if a.catchup:
        import catchup
        cu = catchup.catch_up(force_full=a.full)
        print(f"[catchup] changed={cu.get('changed')} mode={cu.get('mode')} "
              f"last_line={cu.get('last_line')} emitted={cu.get('emitted_messages')} "
              f"collectionCount={cu.get('collectionCount')}")

    st = store.stats()
    print(f"[brief] collection={config.COLLECTION_NAME} count={st.get('count')} byOwner={st.get('byOwner')}")
    pc_read = _pc_read(a.topic)
    if pc_read:
        print(f"[pc] 历史交接记忆命中 {len(pc_read['hits'])} 条（写入简报头部）")
    md = build_brief(a.intent, a.topic, a.src, a.dst, pc_read=pc_read)
    out = a.out or os.path.join(str(config.DATA_DIR), "briefs", "handoff-brief.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    pc_write = _pc_write(a.intent, a.topic, a.src, a.dst, md, out)
    if pc_write:
        print(f"[pc] PowerContext 已记录：entry={pc_write.get('entry_id')} artifact={pc_write.get('artifact_id')}")
    print(f"[brief] written={out} bytes={len(md.encode('utf-8'))}")


if __name__ == "__main__":
    main()
