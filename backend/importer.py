#!/usr/bin/env python3
"""语料导入 CLI：把 Node ingest 导出的 corpus.jsonl 写入 seekdb，并打印集合统计。

用法（WSL2，~/recallloop-venv）：
  python importer.py [--corpus 路径] [--no-reset] [--sample]
默认全量重导入（reset 集合）。--sample 额外跑几条示例检索做冒烟验证。
"""

from __future__ import annotations

import argparse
import json
import sys

import config
import store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=None, help="语料 JSONL 路径（默认 config.CORPUS_PATH）")
    ap.add_argument("--no-reset", action="store_true", help="不重建集合，增量 upsert")
    ap.add_argument("--sample", action="store_true", help="导入后跑示例检索冒烟验证")
    args = ap.parse_args()

    corpus = args.corpus or str(config.CORPUS_PATH)
    print(f"[import] corpus={corpus}")
    print(f"[import] seekdb={config.SEEKDB_PATH}  collection={config.COLLECTION_NAME}  embed={config.EMBEDDING_PROVIDER}/{config.EMBEDDING_DIM}d")
    result = store.import_corpus(corpus, reset=not args.no_reset)
    print("[import] " + json.dumps(result, ensure_ascii=False))

    st = store.stats()
    print("[stats] " + json.dumps(st, ensure_ascii=False))

    if args.sample:
        print("\n[sample] 三路混合检索冒烟：")
        for q, kw in [
            ("单片机 实验 电路图", {"kind": "message"}),
            ("seekdb PowerContext 检索", {}),
            ("修改了哪些文件", {"kind": "change"}),
        ]:
            results, log = store.hybrid_search(q, top_k=3, **kw)
            print(f"\n  Q={q!r} filters={kw} candidates={log['candidates']} where={log['wherePushdown']}")
            for r in results:
                c = r["citation"]
                print(f"    {r['score']:.4f} (v{r['signals']['vector']:.3f}/k{r['signals']['keyword']:.3f}) "
                      f"[{r['meta'].get('kind')}/{r['meta'].get('owner')}] {r['text'][:70]!r} "
                      f"<- {c['srcFile']}:{c['srcLine']}")

        sessions = store.list_sessions()
        print(f"\n[sample] 会话列举：{len(sessions)} 个；首条={json.dumps(sessions[0], ensure_ascii=False)[:160] if sessions else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
