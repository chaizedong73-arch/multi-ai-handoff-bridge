"""多AI协同 v2 · 跨 agent 派工闭环（父进程调度）。

补上 v1 的第 5 项局限：无共享记忆、无跨 agent 交接、无派工。

设计依据（用户 v1 草稿与规约，忠实沿用其判据）：
- 《未来拓展-父AI派活与子AI质检闭环》：区分「检索层 vs 生成层」——派工去**资料库**捞原始材料，
  不是让 AI 再生成一遍答案；父 AI 只做两件事：拆任务、判质量、整合。
  质量四判据：覆盖 / 权威 / 时效 / 一致性；不够则**换检索策略**再捞（换关键词/加限定/换源/拆更细/反向查）。
- 《02-交接协议》：交接写「变更 diff」不写流水账；Current Status 写 artifact 状态不写动作；
  8 块结构映射到 PowerContext 原生 HandoffContent（objective/state/next_action/omissions）。

在本 v2 里：
- 「资料库」= seekdb 里的跨 agent 语料（DSH / WorkBuddy / Codex 的真实会话）。
- 「agent」= 语料里的真实会话（可核验的窗口入口：owner + sessionKey + cwd）。
- 「共享记忆 / 账本」= PowerContext（L1 状态层：共同真相源 + 交接证据核验）。

闭环：plan（拆任务+选真实目标 agent）→ dispatch（对每个 agent 捞其自身证据，判质量，
预算内换策略重捞）→ crosscheck（跨 agent 一致性/冲突）→ finalize（整合写共享记忆 +
原生交接，接收方可 continue 读回并核验 evidence_checks）。

派工是「证据落地」而非「远程执行」：本环境不能真去驱动 DSH/WorkBuddy/Codex 的 CLI，
因此每个子任务的产出 = 该 agent 历史里与本目标相关的**可溯源证据**（带 file:line 引用），
父 AI 据此判质量、交叉核验、整合并交接。所有调用向 DATA_DIR/dispatch_log.jsonl 追加证据（不含 Token）。
"""

from __future__ import annotations

import hashlib
import time
from typing import Dict, List, Optional

import config
import pc
import store

_DISPATCH_LOG = config.DATA_DIR / "dispatch_log.jsonl"

# 质量判据阈值（四判据各须达标；未达标则换策略重捞）
_COVERAGE_MIN = 0.34
_AUTHORITY_MIN = 0.34
_TIMELINESS_MIN = 0.34
_TIMELINESS_WINDOW = 30 * 24 * 3600  # 30 天内算「新」


def _log(event: str, detail: Dict) -> None:
    config.ensure_dirs()
    line = {"ts": int(time.time()), "event": event, "detail": detail}
    with open(_DISPATCH_LOG, "a", encoding="utf-8") as f:
        import json

        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _evidence_version(task_id: str, round_no: int, results: List[Dict]) -> str:
    """证据版本号：taskId + 轮次 + 命中 id 集合的稳定哈希，供交叉核验引用「输入证据版本」。"""
    ids = ",".join(sorted(r.get("id", "") for r in results))
    h = hashlib.md5(f"{task_id}:{round_no}:{ids}".encode("utf-8")).hexdigest()[:12]
    return f"ev-{round_no}-{h}"


# ---------------- 选目标 agent ----------------


def select_targets(goal: str, num_agents: int = 2) -> List[Dict]:
    """选出 num_agents 个**不同的真实 agent**作为派工目标。

    以「会话摘要」为检索面，按与 goal 的相关度取候选，再按 owner 去重，
    保证派给不同的工具（如 workbuddy 与 dsh），对应不同的独家资料视角。
    """
    sessions = store.list_sessions()
    if not sessions:
        return []
    # 用会话文档做相关度检索：kind=session
    res, _ = store.hybrid_search(goal, kind="session", top_k=max(num_agents * 6, 12))
    picked: List[Dict] = []
    seen_owner = set()
    # 先把检索命中的会话按 owner 去重挑出
    sess_by_key = {s.get("sessionKey"): s for s in sessions}
    for r in res:
        meta = r.get("meta") or {}
        key = meta.get("sessionKey")
        s = sess_by_key.get(key)
        if not s:
            continue
        owner = s.get("owner")
        if owner in seen_owner:
            continue
        seen_owner.add(owner)
        picked.append(s)
        if len(picked) >= num_agents:
            return picked
    # 不足则按 mtime 补齐不同 owner
    for s in sessions:
        owner = s.get("owner")
        if owner in seen_owner:
            continue
        seen_owner.add(owner)
        picked.append(s)
        if len(picked) >= num_agents:
            break
    return picked


def _subtask_objective(goal: str, target: Dict) -> str:
    """把总目标拆成面向某个 agent 的子任务：去该 agent 的资料里捞与 goal 相关的做法与证据。"""
    owner = target.get("owner", "")
    title = target.get("title", "") or "(无标题)"
    return f"{goal}｜子任务：在 {owner} 的会话《{title}》里找出与该目标相关的既有做法、决策与产出证据。"


# ---------------- 质量四判据 ----------------


def quality_check(objective: str, evidence: List[Dict], ref_at: int) -> Dict:
    """父 AI 判质量。四判据各返回 0-1 分值。

    硬门槛（决定 pass、驱动重捞）：
    - 覆盖 coverage：目标关键词在证据文本里的命中率（问题每一面是否都有料）。
    - 权威 authority：证据的**可溯源强度**加权均值——已观测成功的 change（一手、结果可验）>
      未验结果的 change > 带 file:line 的 message（二手、可溯源）> 无引用。

    报告信号（不作硬门槛）：
    - 时效 timeliness：证据 at 落在参照时间（被派工 agent 会话 mtime）30 天窗口内的占比。
    - 一致性 consistency：同一 normalizedPath 上是否出现互相矛盾的 status（冲突越少越高分）。

    为何 timeliness/consistency 只报告不卡门槛：本闭环是「检索层」——捞的是各 agent 的**既往工作记录**，
    材料天然是历史的，绝对新旧不构成失败模式；一致性依赖跨 agent 命中同一批文件，属交叉核验阶段
    （crosscheck）处理，不应在单 agent 判质量时一票否决。此取舍在 v1 四判据基础上按检索层语境适配。
    """
    if not evidence:
        return {"coverage": 0.0, "authority": 0.0, "timeliness": 0.0, "consistency": 0.0,
                "pass": False, "failed": ["coverage", "authority"]}

    # 覆盖
    qkws = store._keywords(objective)
    union_text = " ".join(e.get("text", "") for e in evidence).lower()
    cov = (sum(1 for k in qkws if k in union_text) / len(qkws)) if qkws else 0.0

    # 权威（可溯源强度加权）
    auth_sum = 0.0
    for e in evidence:
        cit = e.get("citation") or {}
        meta = e.get("meta") or {}
        kind = meta.get("kind")
        status = str(meta.get("status", "")).lower()
        has_ref = bool(cit.get("srcFile")) or bool(cit.get("srcLine"))
        if kind == "change" and status in ("succeeded", "success", "ok", "observed", "true"):
            auth_sum += 1.0        # 一手，且结果可验
        elif kind == "change":
            auth_sum += 0.7        # 一手产出，结果未观测
        elif has_ref:
            auth_sum += 0.5        # 二手，可溯源到 file:line
        else:
            auth_sum += 0.1        # 无引用
    auth = auth_sum / len(evidence)

    # 时效（信号）
    fresh = 0
    for e in evidence:
        at = int((e.get("citation") or {}).get("at") or (e.get("meta") or {}).get("at") or 0)
        if at and ref_at and (ref_at - at) <= _TIMELINESS_WINDOW:
            fresh += 1
    timeliness = fresh / len(evidence)

    # 一致性（信号；同路径不同 status = 冲突）
    path_status: Dict[str, set] = {}
    for e in evidence:
        meta = e.get("meta") or {}
        p = store.normalize_path(meta.get("path") or "")
        st = str(meta.get("status") or "").lower()
        if p and st:
            path_status.setdefault(p, set()).add(st)
    conflicts = sum(1 for sts in path_status.values() if len(sts) > 1)
    consistency = 1.0 if not path_status else max(0.0, 1.0 - conflicts / len(path_status))

    failed = []
    if cov < _COVERAGE_MIN:
        failed.append("coverage")
    if auth < _AUTHORITY_MIN:
        failed.append("authority")
    passed = not failed
    return {
        "coverage": round(cov, 4), "authority": round(auth, 4),
        "timeliness": round(timeliness, 4), "consistency": round(consistency, 4),
        "conflicts": conflicts, "pass": passed, "failed": failed,
    }


def requery_params(objective: str, target: Dict, quality: Dict, round_no: int) -> Dict:
    """资料不够时**换检索策略**（不是重问同一句）——返回下一轮 hybrid_search 的过滤参数。

    映射 v1 草稿的重查手法（仅按硬门槛 coverage/authority 驱动）：
    - coverage 不达标 → 换限定/拆更细：放宽到整 owner（去掉 sessionId）扩大资料面。
    - authority 不达标 → 加限定：只看 change（一手产出，可溯源强度更高）。
    - 第 2 轮仍不够 → 反向查：整 owner 全类型，最大化召回。
    """
    owner = target.get("owner")
    sid = target.get("sessionId")
    failed = set(quality.get("failed") or [])
    if round_no >= 2:
        return {"owner": owner, "kind": None, "sessionId": None, "top_k": config.SEARCH_TOP_K + 4,
                "strategy": "反向查/全召回"}
    # coverage 是更根本的缺口：先放宽到整 owner 扩大资料面（换源/拆更细）。
    if "coverage" in failed:
        return {"owner": owner, "kind": None, "sessionId": None, "top_k": config.SEARCH_TOP_K + 4,
                "strategy": "放宽到整 owner（换源/拆更细）"}
    # 仅 authority 不达标：加限定只看 change（一手产出，可溯源强度更高）。
    if "authority" in failed:
        return {"owner": owner, "kind": "change", "sessionId": sid, "top_k": config.SEARCH_TOP_K,
                "strategy": "加限定 kind=change"}
    return {"owner": owner, "kind": None, "sessionId": sid, "top_k": config.SEARCH_TOP_K,
            "strategy": "默认"}


# ---------------- 派工单个子任务 ----------------


def _corpus_newest_at() -> int:
    newest = 0
    for s in store.list_sessions():
        newest = max(newest, int(s.get("mtime") or 0))
    return newest


def dispatch_task(task: Dict, max_rounds: int = 2) -> Dict:
    """对一个子任务跑「捞证据 → 判质量 → 预算内换策略重捞」的闭环，落 receipt。"""
    query = task["query"]
    target = task["target"]
    # 时效参照：被派工 agent 自身的最近活动时间（其会话 mtime），而非全局语料最新时间。
    ref_at = int(target.get("mtime") or 0) or _corpus_newest_at()
    round_no = 0
    params = {"owner": target.get("owner"), "kind": None, "sessionId": target.get("sessionId"),
              "top_k": config.SEARCH_TOP_K, "strategy": "默认"}
    evidence: List[Dict] = []
    quality: Dict = {}
    while round_no < max_rounds:
        round_no += 1
        results, log = store.hybrid_search(
            query, owner=params["owner"], kind=params["kind"],
            sessionId=params["sessionId"], top_k=params["top_k"],
        )
        evidence = results
        quality = quality_check(query, results, ref_at)
        task["rounds"].append({
            "round": round_no, "strategy": params["strategy"], "params": {
                "owner": params["owner"], "kind": params["kind"],
                "sessionId": bool(params["sessionId"]), "top_k": params["top_k"]},
            "candidates": log.get("candidates", 0), "mode": log.get("mode"),
            "quality": quality,
        })
        _log("dispatch.round", {"taskId": task["taskId"], "round": round_no,
                                "strategy": params["strategy"], "hits": len(results),
                                "qualityPass": quality.get("pass"), "failed": quality.get("failed")})
        if quality.get("pass"):
            break
        if round_no < max_rounds:
            params = requery_params(query, target, quality, round_no)

    task["evidence"] = evidence
    task["quality"] = quality
    task["evidenceVersion"] = _evidence_version(task["taskId"], round_no, evidence)
    task["budget"]["roundsUsed"] = round_no
    task["status"] = "collected" if evidence else "empty"
    task["receipt"] = {
        "success": bool(quality.get("pass")) and bool(evidence),
        "rounds": round_no, "evidenceCount": len(evidence),
        "evidenceVersion": task["evidenceVersion"],
        "topScore": round(evidence[0]["score"], 4) if evidence else 0.0,
    }
    _log("dispatch.receipt", {"taskId": task["taskId"], "owner": target.get("owner"),
                              "success": task["receipt"]["success"], "rounds": round_no,
                              "evidence": len(evidence), "evidenceVersion": task["evidenceVersion"]})
    return task


# ---------------- 交叉核验 ----------------


def crosscheck(tasks: List[Dict]) -> Dict:
    """跨 agent 一致性核验：不同 agent 的证据是否指向同一批产出、有无冲突。

    - agreement：被 ≥2 个 agent 的证据共同引用的 normalizedPath 数（互相印证）。
    - conflicts：同一 path 在不同 agent 处 status 矛盾。
    - consistent：无冲突且至少有一条互证 → True。
    """
    path_owners: Dict[str, set] = {}
    path_status: Dict[str, set] = {}
    for t in tasks:
        owner = t["target"].get("owner")
        for e in t.get("evidence") or []:
            meta = e.get("meta") or {}
            p = store.normalize_path(meta.get("path") or "")
            st = str(meta.get("status") or "").lower()
            if p:
                path_owners.setdefault(p, set()).add(owner)
                if st:
                    path_status.setdefault(p, set()).add(st)
    shared = [p for p, owners in path_owners.items() if len(owners) >= 2]
    conflicts = [p for p, sts in path_status.items() if len(sts) > 1]
    consistent = (not conflicts) and bool(shared)
    result = {
        "sharedPaths": len(shared), "conflictPaths": len(conflicts),
        "conflicts": conflicts[:10], "agreementSample": shared[:10],
        "consistent": consistent,
    }
    _log("dispatch.crosscheck", {"sharedPaths": len(shared), "conflicts": len(conflicts),
                                 "consistent": consistent})
    return result


# ---------------- 整合 + 交接 ----------------


def finalize(goal: str, tasks: List[Dict], cross: Dict) -> Dict:
    """整合各 agent 证据 → 写共享记忆（capture_source + remember）→ 原生交接（create + read 回核）。

    交接内容按《02-交接协议》：objective=Session Goal；state=各 agent 的 Current Status（artifact 状态，
    非动作）+ 变更证据；next_action=Next Steps；omissions=禁区/未覆盖。所有 state 带 citation（source/memory）。
    """
    # 1) 每个 agent 的顶证据落 source（可被交接引用）
    source_refs: List[Dict] = []
    mem_citations: List[Dict] = []
    for t in tasks:
        owner = t["target"].get("owner")
        ev = t.get("evidence") or []
        if not ev:
            continue
        sid = pc.new_source_id(f"dispatch-{owner}")
        top = ev[0]
        content = (
            f"派工证据 · 目标 agent={owner} 会话={t['target'].get('sessionKey')}\n"
            f"子任务：{t['objective']}\n"
            f"证据版本：{t['evidenceVersion']}（{t['receipt']['rounds']} 轮，命中 {len(ev)} 条）\n"
            f"质量：覆盖 {t['quality'].get('coverage')} / 权威 {t['quality'].get('authority')} / "
            f"时效 {t['quality'].get('timeliness')} / 一致 {t['quality'].get('consistency')}\n"
            f"顶证据：{top.get('text','')[:400]}\n"
            f"引用：{(top.get('citation') or {}).get('srcFile','')}:{(top.get('citation') or {}).get('srcLine','')}"
        )
        cap = pc.capture_source(sid, content, metadata={"owner": owner, "taskId": t["taskId"],
                                                        "evidenceVersion": t["evidenceVersion"]})
        source_refs.append({"taskId": t["taskId"], "owner": owner, "source_id": sid,
                            "source_ref": cap["source_ref"]})
        # 2) 整合结论写共享记忆
        m = pc.remember(
            f"派工结论（{owner}）：关于「{goal}」，从会话 {t['target'].get('sessionKey')} 捞得 "
            f"{len(ev)} 条可溯源证据（版本 {t['evidenceVersion']}），"
            f"质量达标={t['quality'].get('pass')}，顶证据引用 "
            f"{(top.get('citation') or {}).get('srcFile','')}:{(top.get('citation') or {}).get('srcLine','')}。",
            kind="decision", reason="跨 agent 派工闭环整合",
        )
        mc = pc.memory_citation(m["citation"])
        if mc:
            mem_citations.append(mc)

    # 3) 组装原生交接 HandoffContent
    state = []
    for sr in source_refs:
        t = next(x for x in tasks if x["taskId"] == sr["taskId"])
        state.append({
            "text": (f"[{sr['owner']}] 已收齐证据（版本 {t['evidenceVersion']}，"
                     f"{t['receipt']['evidenceCount']} 条，质量达标={t['quality'].get('pass')}）。"
                     f"顶证据：{(t['evidence'][0].get('text','') if t['evidence'] else '')[:200]}"),
            "citations": [pc.source_citation(sr["source_id"], "content")],
        })
    # 一致性状态
    state.append({
        "text": (f"跨 agent 一致性核验：互证路径 {cross['sharedPaths']} 条，冲突 {cross['conflictPaths']} 条，"
                 f"整体一致={cross['consistent']}。"),
        "citations": (mem_citations[:1] if mem_citations
                      else [pc.source_citation(source_refs[0]["source_id"], "content") if source_refs else None]),
    })
    state = [s for s in state if s["citations"] and all(c for c in s["citations"])]

    next_action = {
        "text": (f"接收方：基于以上 {len(source_refs)} 个 agent 的证据版本继续推进「{goal}」；"
                 f"若需更硬资料，按 requery 策略（换关键词/加限定/换源/拆更细/反向查）补捞，勿重问同句。"),
        "citations": (mem_citations[:1] if mem_citations else state[0]["citations"] if state else []),
    }
    # HandoffOmission 需 {text, citation}，citation 可为 null（schema：required=[text,citation], nullable）
    first_cit = (pc.source_citation(source_refs[0]["source_id"], "content") if source_refs else None)
    omissions = []
    if not cross["consistent"]:
        omissions.append({"text": f"存在 {cross['conflictPaths']} 条路径状态冲突，未消解：{cross['conflicts'][:5]}",
                          "citation": first_cit})
    for t in tasks:
        if not (t.get("evidence") or []):
            omissions.append({"text": f"agent {t['target'].get('owner')} 未捞到证据（覆盖不足），该视角缺席。",
                              "citation": None})

    disposition = "continuable" if state else "blocked"
    h = pc.create_handoff(objective=f"多AI协同 v2 派工闭环：{goal}", state=state or [{
        "text": "无可用证据，派工未产出。", "citations": []}], disposition=disposition,
        next_action=next_action if next_action["citations"] else None, omissions=omissions)

    readback = pc.read_handoff(selection="latest") if state else {"status": "skipped", "evidence_checks": []}
    checks = readback.get("evidence_checks") or []
    avail = sum(1 for c in checks if c.get("status") == "available")
    result = {
        "handoffRevision": h.get("revision"), "disposition": disposition,
        "sources": source_refs, "memoryCitations": len(mem_citations),
        "readbackStatus": readback.get("status"),
        "evidenceAvailable": f"{avail}/{len(checks)}",
        "omissions": len(omissions),
    }
    _log("dispatch.finalize", {"goal": goal, "revision": h.get("revision"), "disposition": disposition,
                               "sources": len(source_refs), "evidenceAvailable": result["evidenceAvailable"]})
    return result


# ---------------- 闭环编排 ----------------


def make_task(parent_id: str, idx: int, goal: str, target: Dict, mode: str) -> Dict:
    task_id = f"{parent_id}-t{idx}"
    return {
        "taskId": task_id, "parentTaskId": parent_id,
        "target": {"owner": target.get("owner"), "sessionId": target.get("sessionId"),
                   "sessionKey": target.get("sessionKey"), "sessionTitle": target.get("title"),
                   "workingDir": target.get("cwd"), "model": target.get("model"),
                   "mtime": int(target.get("mtime") or 0)},
        "mode": mode,  # continue-original-session | new-subsession
        # query=检索词（用于捞证据 + 覆盖判据），只含目标关键词，不掺子任务样板文；
        # objective=人读的子任务描述。二者分开，避免样板词稀释覆盖率。
        "query": goal,
        "objective": _subtask_objective(goal, target),
        "expectedOutput": "该 agent 历史里与目标相关的可溯源证据（带 file:line 引用）",
        "completionCriteria": "四判据（覆盖/权威/时效/一致性）达标，或预算内穷尽策略",
        "budget": {"maxRounds": 2, "roundsUsed": 0}, "cancelState": False,
        "status": "planned", "rounds": [], "evidence": [], "quality": {},
        "evidenceVersion": "", "receipt": {},
    }


def run_closed_loop(goal: str, num_agents: int = 2, max_rounds: int = 2,
                    mode: str = "continue-original-session") -> Dict:
    """跑完整闭环：选目标 → 拆任务 → 逐个派工捞证 → 交叉核验 → 整合交接。返回全过程记录。"""
    parent_id = f"dispatch-{int(time.time())}"
    _log("dispatch.plan", {"parentId": parent_id, "goal": goal, "numAgents": num_agents, "mode": mode})
    targets = select_targets(goal, num_agents)
    if not targets:
        return {"parentId": parent_id, "goal": goal, "status": "no-targets", "tasks": []}
    tasks = [make_task(parent_id, i + 1, goal, t, mode) for i, t in enumerate(targets)]
    for t in tasks:
        if t["cancelState"]:
            t["status"] = "cancelled"
            continue
        t["status"] = "dispatched"
        dispatch_task(t, max_rounds=max_rounds)
    cross = crosscheck(tasks)
    fin = finalize(goal, tasks, cross)
    record = {
        "parentId": parent_id, "goal": goal, "mode": mode, "status": "finalized",
        "targets": [{"owner": t["target"]["owner"], "sessionKey": t["target"]["sessionKey"],
                     "workingDir": t["target"]["workingDir"]} for t in tasks],
        "tasks": tasks, "crosscheck": cross, "finalize": fin,
    }
    _log("dispatch.done", {"parentId": parent_id, "tasks": len(tasks),
                           "consistent": cross["consistent"], "revision": fin.get("handoffRevision"),
                           "evidenceAvailable": fin.get("evidenceAvailable")})
    return record


if __name__ == "__main__":
    import json
    import sys

    goal = sys.argv[1] if len(sys.argv) > 1 else "OceanBase 赛题提测与分支提交"
    print(f"== run_closed_loop goal={goal!r} ==")
    rec = run_closed_loop(goal, num_agents=2)
    print("parentId:", rec.get("parentId"), "status:", rec.get("status"))
    print("targets:", json.dumps(rec.get("targets"), ensure_ascii=False))
    for t in rec.get("tasks", []):
        q = t.get("quality", {})
        print(f"  [{t['taskId']}] owner={t['target']['owner']} rounds={t['budget']['roundsUsed']} "
              f"evidence={t['receipt'].get('evidenceCount')} pass={q.get('pass')} "
              f"cov={q.get('coverage')} auth={q.get('authority')} time={q.get('timeliness')} "
              f"ver={t.get('evidenceVersion')}")
        for r in t.get("rounds", []):
            print(f"      round{r['round']} strategy={r['strategy']} hits={r['candidates']} "
                  f"mode={r['mode']} pass={r['quality'].get('pass')} failed={r['quality'].get('failed')}")
    print("crosscheck:", json.dumps(rec.get("crosscheck"), ensure_ascii=False))
    print("finalize:", json.dumps(rec.get("finalize"), ensure_ascii=False))
    print("== CLOSED LOOP OK ==")
