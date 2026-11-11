#!/usr/bin/env python3
"""评测驱动优化的"打分器"：把交接简报对着扎根真实 events.jsonl 的标准答案打分。

标准答案（answer key）来自对全源 37k 事件的扫描 + 人工按项目归属分类，
每条都绑定真实行号，可审计、可复核。grading 全机械（行号/正则命中），不靠主观。

多场景：CASES 里每个用例 = 一次真实跨 agent 接手，各带自己的 key + 生成参数。
- window : chatgpt→zcode  窗口提醒/工作现场恢复（原始用例）
- proc   : deepseek→chatgpt 常驻进程登记/AI进程监测
- bidu   : deepseek→chatgpt BiDu/哔嘟 B站视频客户端（关键词"B站"与采集工作流串台，精度压力测试）

用法：
  python eval_brief.py <brief.md> [case_name]   # 单例打分（默认 case=window）
  run_eval_suite.py                             # 冷启动一次，生成+打分全部用例
"""
from __future__ import annotations

import re
import sys
from typing import Dict, List, Set

# ================= 标准答案（answer key，扎根真实 events.jsonl 行号） =================

_CASE_WINDOW = {
    "intent": "zcode 接手 chatgpt 的窗口提醒/任务跟踪项目",
    "topic": "窗口提醒 任务跟踪 提醒项目 工作环境 现场恢复 工作现场 工作现场一键恢复 真实笔记",
    "src": "chatgpt", "dst": "zcode",
    "MUST_OPEN": {
        15542: "自动发现工作场景还没实现/还没做持续识别自动检测",
        15730: "还没做到完整的工作现场采集",
        28244: "失效原因无法确定 + OCR/字幕还不会自动生成 + 真实笔记AI联调尚未完成",
        15332: "真实AI规划尚待提交验证 + 浏览器/Obsidian需首次启用扩展",
        14418: "界面尚未实现",
        9079: "其他软件的子会话尚未验证",
        8974: "标注尚未复现 + 删除待验证",
        14246: "工作现场一键恢复：方案已复核，还没跑通真实恢复",
        2103: "录音采集进程已退出但尚未注销",
        9220: "尚未制作自动检查器",
    },
    "MUST_FACT": {
        34228: "zcode 接手请求本身",
        2103: "做成桌面两入口：AI进程监测 + AI窗口提醒",
        15332: "工作环境第一版，18项测试通过",
        15542: "按需采集/按需分析，非持续自动识别",
        9079: "隐式子会话泄漏修复：24会话泄漏0",
        7317: "提醒漏报修复：WorkBuddy返回working原代码只识别running",
        33865: "决策：整合进窗口提醒并改名",
        981: "workbuddy 原始灵感：工作现场一键恢复 + screenpipe查证",
        1373: "workbuddy 定的分工边界：接手AI只写代码不改Obsidian",
    },
    "MUST_NOT_LINE": {
        26752: "录音记忆项目（跨项目噪声）",
        28195: "网络恢复了不用接替（跨项目噪声）",
        6366: "知乎/Duck.ai 网站验证（别的项目）",
        4276: "工作台四模块表格（线索词误报）",
        34211: "LoRA-MGPO复现（完全无关项目）",
    },
}

_CASE_PROC = {
    "intent": "chatgpt 接手整合 deepseek 的常驻进程登记 / AI进程监测项目",
    "topic": "进程登记 常驻进程 进程监测 注销 进程追踪 登记规范 AI进程监测 已死未注销",
    "src": "deepseek", "dst": "chatgpt",
    # 项目真实未完成项（注意：多数由接手方 chatgpt 在评估/整合时说出，不是交出方 deepseek）
    "MUST_OPEN": {
        1820: "基础登记可用，但'防止AI忘记后台进程'还没完全实现（并发丢记录/活进程被隐藏等5问题）",
        3945: "短时进程追踪没抓到启动者，还没确认根因也没修好",
        4733: "进程监测入口仍打开原有工作台页面，尚未全部统一视觉",
        3015: "常驻规则没有完全做到，不能回答'每次都完整执行'",
        17406: "ZCode 接入尚未验证实际加载成功",
        35343: "已确认文件部署，尚未确认客户端实际注入（Trae）",
    },
    "MUST_FACT": {
        1695: "deepseek 推送完成，DSH规则生效；机制=创建常驻进程前必须登记、结束必须注销",
        2411: "deepseek 按钮改好编译通过(PID4892)，关闭=杀3进程+注销登记",
        2455: "进程登记规范.md 写入 .ai-snapshot",
        2477: "验证通过：假PID999999→'已死未注销'→status变died",
        1727: "用户让 chatgpt 去看 deepseek 做的常驻进程监测项目（接手请求）",
        2103: "做成桌面两入口：AI进程监测 + AI窗口提醒",
        1820: "chatgpt 独立评估：隔离副本复现5项问题，生产代码与录音服务未改动",
    },
    "MUST_NOT_LINE": {
        33181: "audio-memory 录音记忆正式版任务栏（别的项目，仅提'登记'）",
        4084: "会话检索器/AI工作箱注入路径（别的功能）",
        5827: "risk_level 审批JSON（日志噪声）",
        14246: "24个未归档项目排序表（别的项目）",
        15542: "工作现场恢复的按需采集（别的项目）",
    },
}

_CASE_BIDU = {
    "intent": "chatgpt 接手 deepseek 的 BiDu/哔嘟 B站视频桌面客户端项目",
    # 刻意保留歧义词"B站/视频"——真实用户就会这么说；这是精度压力测试
    "topic": "BiDu 哔嘟 bilisoft B站 视频客户端 代码签名 SmartScreen 帧批注 抽帧 播放 字幕",
    "src": "deepseek", "dst": "chatgpt",
    "MUST_OPEN": {
        88: "这次还没有逐项操作验证播放、字幕等功能",
        207: "接手报告列出关键问题：并发保存会丢笔记",
        1742: "当前exe仍未签名，没验证过能让BiDu本机稳定免SmartScreen提示的方案",
        1917: "还没找到你说的新项目，bilisoft/AGENTS.md 也已不存在",
        1628: "还没取得官方播放器实际编码与缓存数据，不能确定根因",
        1611: "还不能区分是电脑、网络线路还是B站",
    },
    "MUST_FACT": {
        88: "BiDu=偏学习/记笔记的B站桌面客户端，bilisoft目录，功能清单（播放/收藏/时间点笔记/帧批注/ASR字幕）",
        207: "完整接手报告已保存；搜索/视频解析/分段取流实测通过",
        1679: "BiDu.exe 无数字签名 → SmartScreen 应用信誉提示（非病毒）",
        1725: "已加入签名与AI排查指引 + 检查/签名脚本 + 项目AGENTS.md",
        1964: "deepseek 实测排除四条路，双击'哔嘟'不再弹（pid22968）",
        1983: "写入笔记：哔嘟(BiDu)项目主页.md（能力清单/架构/踩坑/待办）",
        2169: "哔嘟后端已恢复并按新规范登记（port=8787 loggedIn=true）",
        2243: "抽帧三段实现：后端/api/resolve→DASH视频轨→Node",
    },
    # 串台噪声：workbuddy 的"B站/抖音视频采集双写工作流"、别的项目——topic 含"B站/视频/字幕/抽帧"会误捞
    "MUST_NOT_LINE": {
        14246: "24个未归档项目排序表（别的项目）",
        33727: "抖音图文双写（采集工作流，非BiDu客户端）",
        12163: "抖音字幕/抽帧失败排查（采集工作流）",
        6119: "FDE视频字幕双写进笔记（采集工作流）",
        10857: "STM32课程B站API直取（学习工作流）",
        34188: "拍PPT→变化帧→OCR→字幕（教学视频项目）",
    },
}

CASES = {"window": _CASE_WINDOW, "proc": _CASE_PROC, "bidu": _CASE_BIDU}

# ⑥ 路径噪声模式：目录树半截碎片（单段 /name.ext，无盘符无多级）
_BAD_PATH_RE = re.compile(r"^/[^/]+?\.[A-Za-z0-9]{1,6}$")
# URL 尾巴带中文/全角标点 = 过度捕获
_URL_CJK_RE = re.compile(r"[\u4e00-\u9fff（），。、]")

WEIGHTS = {"open_recall": 34, "fact_recall": 26, "no_noise": 20, "path_clean": 12, "struct": 8}


def _cited_lines(section_text: str) -> Set[int]:
    return {int(x) for x in re.findall(r"events\.jsonl:(\d+)", section_text)}


def _all_lines(md: str) -> Set[int]:
    return _cited_lines(md)


def _section(md: str, head: str) -> str:
    """抽取以 '## <head>' 开头到下一个 '## ' 之间的文本。"""
    m = re.search(r"^##\s*" + re.escape(head) + r".*?$(.*?)(?=^##\s|\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


def _paths_urls(md: str):
    sec = _section(md, "⑥")
    paths = re.findall(r"^\s*-\s*`([^`]+)`", sec, re.M)
    urls = re.findall(r"^\s*-\s*(https?://\S+)", sec, re.M)
    return paths, urls


def grade(md: str, case: Dict) -> Dict:
    MUST_OPEN = case["MUST_OPEN"]; MUST_FACT = case["MUST_FACT"]; MUST_NOT_LINE = case["MUST_NOT_LINE"]
    lines = _all_lines(md)
    open_sec = _section(md, "④")
    open_lines = _cited_lines(open_sec)

    hit_open = {ln for ln in MUST_OPEN if ln in open_lines}
    miss_open = {ln: MUST_OPEN[ln] for ln in MUST_OPEN if ln not in open_lines}
    s_open = len(hit_open) / max(1, len(MUST_OPEN))

    hit_fact = {ln for ln in MUST_FACT if ln in lines}
    miss_fact = {ln: MUST_FACT[ln] for ln in MUST_FACT if ln not in lines}
    s_fact = len(hit_fact) / max(1, len(MUST_FACT))

    bad_lines = {ln: MUST_NOT_LINE[ln] for ln in MUST_NOT_LINE if ln in lines}
    s_noise = 1.0 - (len(bad_lines) / max(1, len(MUST_NOT_LINE)))

    paths, urls = _paths_urls(md)
    bad_paths = [p for p in paths if _BAD_PATH_RE.match(p)]
    bad_urls = [u for u in urls if _URL_CJK_RE.search(u)]
    tot_pu = max(1, len(paths) + len(urls))
    s_path = 1.0 - ((len(bad_paths) + len(bad_urls)) / tot_pu)

    struct_ok = 0; struct_tot = 3
    if re.search(r"\(线索词:|\(规约标记:|interrupted/failed", open_sec):
        struct_ok += 1
    tl = _section(md, "⑧")
    times = re.findall(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", tl)
    if times and times == sorted(times):
        struct_ok += 1
    core = "".join(_section(md, h) for h in ("①", "②", "③", "④", "⑤"))
    core_lines = core.splitlines()
    items = [l for l in core_lines if l.strip().startswith("- ")]
    cites = [l for l in core_lines if "events.jsonl:" in l]
    if items and len(cites) / len(items) >= 0.8:
        struct_ok += 1
    s_struct = struct_ok / struct_tot

    total = 100 * (
        WEIGHTS["open_recall"] * s_open + WEIGHTS["fact_recall"] * s_fact
        + WEIGHTS["no_noise"] * s_noise + WEIGHTS["path_clean"] * s_path
        + WEIGHTS["struct"] * s_struct
    ) / sum(WEIGHTS.values())

    return {
        "total": round(total, 1),
        "s_open": round(s_open, 3), "hit_open": sorted(hit_open), "miss_open": miss_open,
        "s_fact": round(s_fact, 3), "hit_fact": sorted(hit_fact), "miss_fact": miss_fact,
        "s_noise": round(s_noise, 3), "bad_lines": bad_lines,
        "s_path": round(s_path, 3), "bad_paths": bad_paths, "bad_urls": bad_urls,
        "n_paths": len(paths), "n_urls": len(urls),
        "s_struct": round(s_struct, 3), "struct_ok": struct_ok,
    }


def render(path: str, g: Dict, case: Dict) -> str:
    MUST_OPEN = case["MUST_OPEN"]; MUST_FACT = case["MUST_FACT"]
    out = []
    out.append("====== 交接简报评分卡  (%s) ======" % path)
    out.append("总分: %.1f / 100" % g["total"])
    out.append("")
    out.append("[④未完成项召回] 权重%d  得分率%.0f%%  命中%d/%d" % (
        WEIGHTS["open_recall"], g["s_open"] * 100, len(g["hit_open"]), len(MUST_OPEN)))
    out.append("  命中行号: %s" % g["hit_open"])
    if g["miss_open"]:
        out.append("  漏掉(must-have):")
        for ln, d in sorted(g["miss_open"].items()):
            out.append("    :%d  %s" % (ln, d))
    out.append("")
    out.append("[关键事实召回] 权重%d  得分率%.0f%%  命中%d/%d" % (
        WEIGHTS["fact_recall"], g["s_fact"] * 100, len(g["hit_fact"]), len(MUST_FACT)))
    if g["miss_fact"]:
        out.append("  漏掉:")
        for ln, d in sorted(g["miss_fact"].items()):
            out.append("    :%d  %s" % (ln, d))
    out.append("")
    out.append("[无噪声] 权重%d  得分率%.0f%%" % (WEIGHTS["no_noise"], g["s_noise"] * 100))
    if g["bad_lines"]:
        for ln, d in sorted(g["bad_lines"].items()):
            out.append("    混入噪声 :%d  %s" % (ln, d))
    else:
        out.append("    干净，无跨项目噪声")
    out.append("")
    out.append("[路径/URL干净] 权重%d  得分率%.0f%%  (路径%d URL%d)" % (
        WEIGHTS["path_clean"], g["s_path"] * 100, g["n_paths"], g["n_urls"]))
    if g["bad_paths"]:
        out.append("    碎片路径: %s" % g["bad_paths"])
    if g["bad_urls"]:
        out.append("    URL带中文尾: %s" % [u[:50] for u in g["bad_urls"]])
    out.append("")
    out.append("[结构] 权重%d  得分率%.0f%%  (%d/3: ④可审计/⑧时间有序/出处覆盖≥80%%)" % (
        WEIGHTS["struct"], g["s_struct"] * 100, g["struct_ok"]))
    return "\n".join(out)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "_handoff_brief2.md"
    case_name = sys.argv[2] if len(sys.argv) > 2 else "window"
    case = CASES[case_name]
    with open(path, encoding="utf-8") as f:
        md = f.read()
    g = grade(md, case)
    txt = render(path, g, case)
    with open("_eval_report.txt", "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt.encode("ascii", "replace").decode())


if __name__ == "__main__":
    main()
