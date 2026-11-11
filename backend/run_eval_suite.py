#!/usr/bin/env python3
"""一次冷启动，生成并打分全部评测用例。

模型只加载一次，循环调用 build_brief + grade，把每个用例的简报与评分卡落盘，
最后汇总一份 suite 报告。用法： python run_eval_suite.py
"""
from __future__ import annotations

import eval_brief
import handoff_brief


def main() -> None:
    cases = eval_brief.CASES
    rows = []
    for name, case in cases.items():
        md = handoff_brief.build_brief(
            case["intent"], case["topic"], case.get("src"), case.get("dst")
        )
        brief_path = "_brief_%s.md" % name
        with open(brief_path, "w", encoding="utf-8") as f:
            f.write(md)
        g = eval_brief.grade(md, case)
        eval_brief.render("_eval_%s.txt" % name, g, case)
        rows.append((name, g, len(md)))

    out = []
    out.append("====== 交接简报评测 · 全套汇总 ======")
    out.append("用例数: %d" % len(rows))
    out.append("")
    out.append("%-10s %8s %8s %8s %8s %8s %8s" % (
        "case", "total", "open", "fact", "noise", "path", "struct"))
    for name, g, _ in rows:
        out.append("%-10s %8.1f %8.0f%% %8.0f%% %8.0f%% %8.0f%% %8.0f%%" % (
            name, g["total"], g["s_open"] * 100, g["s_fact"] * 100,
            g["s_noise"] * 100, g["s_path"] * 100, g["s_struct"] * 100))
    avg = sum(g["total"] for _, g, _ in rows) / max(1, len(rows))
    out.append("")
    out.append("平均总分: %.1f / 100" % avg)
    out.append("")
    for name, g, _ in rows:
        out.append("--- %s ---" % name)
        if g["miss_open"]:
            out.append("  ④漏掉: %s" % sorted(g["miss_open"].keys()))
        if g["miss_fact"]:
            out.append("  事实漏: %s" % sorted(g["miss_fact"].keys()))
        if g["bad_lines"]:
            out.append("  混入噪声: %s" % sorted(g["bad_lines"].keys()))
        if g["bad_paths"]:
            out.append("  碎片路径: %s" % g["bad_paths"])
        if g["bad_urls"]:
            out.append("  URL脏尾: %s" % [u[:40] for u in g["bad_urls"]])
        if not any([g["miss_open"], g["miss_fact"], g["bad_lines"], g["bad_paths"], g["bad_urls"]]):
            out.append("  全部命中，无噪声")
        out.append("")

    txt = "\n".join(out)
    with open("_eval_suite_report.txt", "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt.encode("ascii", "replace").decode())


if __name__ == "__main__":
    main()
