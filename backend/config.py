"""多AI协同 v2 · 集中配置：全部敏感项只从环境变量读取，代码与仓库不含任何密钥。

镜像 RecallLoop（qoder/app/config.py）已验证的约定：
- pyseekdb 默认 embedding function 会联网下载 HF 模型；我们自带向量、从不用它，强制离线。
- seekdb 嵌入式数据库目录必须位于本地文件系统，不能放 /mnt/c 等挂载路径
  （本机仓库在 /mnt/c 下，故默认落到 WSL 原生 ~/multi-ai-data/seekdb）。
- PowerContext token 仅进程环境，绝不落盘/入日志。
"""

import os
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

BACKEND_DIR = Path(__file__).resolve().parent

# 运行时数据目录（seekdb、日志、scope）。默认 WSL 原生 home，避开 /mnt/c。
DATA_DIR = Path(os.environ.get("MULTI_AI_DATA_DIR", str(Path.home() / "multi-ai-data")))
SEEKDB_PATH = Path(os.environ.get("SEEKDB_PATH", str(DATA_DIR / "seekdb")))

# 语料 JSONL（Node ingest 导出）。默认指向仓库内 ingest/out/corpus.jsonl（读 /mnt/c 没问题，
# 只有 seekdb 数据目录不能放 /mnt/c）。可用 --corpus 或环境变量覆盖。
_DEFAULT_CORPUS = BACKEND_DIR.parent / "ingest" / "out" / "corpus.jsonl"
CORPUS_PATH = Path(os.environ.get("MULTI_AI_CORPUS", str(_DEFAULT_CORPUS)))

# PowerContext Server（本地，无云）
POWERCONTEXT_URL = os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
POWERCONTEXT_SCOPE_ID = os.environ.get("POWERCONTEXT_SCOPE_ID", "")  # 空则自动创建；scope 由应用决定
POWERCONTEXT_TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")  # 仅进程环境，绝不落盘/入日志
POWERCONTEXT_TIMEOUT = float(os.environ.get("POWERCONTEXT_TIMEOUT", "8"))

# 存储后端（0.7.0 内存优化）：light = 零 torch（docs.jsonl+emb.npy+numpy 融合，store_light.py）；
# seekdb = pyseekdb 基线（0.4.4 实测 torch 地板 ~569MB + 默认 EF →885MB，保留作 A/B 对照）。
# 2026-11-11 起默认 light：评测 94.6 与 ST+seekdb 基线逐用例一致，全链内存 472MB vs 1458MB（-68%）。
STORE_PROVIDER = os.environ.get("MULTI_AI_STORE", "light").strip().lower()

# Embedding
# 未显式指定 EMBEDDING_PROVIDER 时与存储后端联动：light ⇒ onnx（int8，零 torch）；
# seekdb ⇒ local/ST（历史行为不变）。显式设置一律尊重（A/B 跑法用）。
_EMBEDDING_PROVIDER_ENV = os.environ.get("EMBEDDING_PROVIDER", "").strip().lower()
EMBEDDING_PROVIDER = _EMBEDDING_PROVIDER_ENV or ("onnx" if STORE_PROVIDER == "light" else "local")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))  # 须与 pyseekdb 默认 EF 维度一致
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "")

# ONNX provider（② 内存足迹）：用 int8 量化 ONNX + onnxruntime CPU 推理，绕开 torch 常驻。
# 模型与 tokenizer 落在固定的 ~/multi-ai-data/onnx，运行时不依赖 HF 缓存路径。
# 注意：onnx 目录与 seekdb 的 DATA_DIR 解耦——测试里改 MULTI_AI_DATA_DIR（隔离集合）
# 不该顺带把 onnx 模型路径也改没；用独立 env MULTI_AI_ONNX_DIR 覆盖。
ONNX_DIR = Path(os.environ.get("MULTI_AI_ONNX_DIR", str(DATA_DIR / "onnx")))
# 线程必须钉 1：默认 16 intra-op 会让 arena 峰值从 ~666MB 涨回 ~1676MB（见 onnx_measure.py）。
# 0.7.0 实测：fp32 与 int8 会话常驻几乎相同（~425-465MB，ORT 内部都会占同量工作区），
# 但 fp32 与 ST 语义逐位一致（实测 cosine=1.0）、int8 在中文长文检索上损失 ~1 分（bidu fact 召回）。
# 故 light 后端默认 fp32；int8 保留为「量过的可选支路」（显式设 EMBEDDING_ONNX_PATH 可切回）。
EMBEDDING_ONNX_PATH = os.environ.get("EMBEDDING_ONNX_PATH", str(ONNX_DIR / "model.int8.onnx"))
EMBEDDING_TOKENIZER_PATH = os.environ.get("EMBEDDING_TOKENIZER_PATH", str(ONNX_DIR / "tokenizer.json"))
ONNX_MAX_SEQ = int(os.environ.get("ONNX_MAX_SEQ", "128"))  # 复刻 ST 窗口：该 MiniLM 的 max_seq_length=128（非位置上限 512）；设 512 会让长文 cosine 掉到 ~0.57
ONNX_INTRA_THREADS = int(os.environ.get("ONNX_INTRA_THREADS", "1"))
ONNX_INTER_THREADS = int(os.environ.get("ONNX_INTER_THREADS", "1"))
# 批量嵌入的中间激活随 batch 线性放大：实测 batch=64 全量重建峰值 ~1.6GB（fp32）。
# 降到 16 换一次性导入峰值大幅下降，代价只是全量重建变慢（增量懒补通常只有几十行，无感）。
ONNX_BATCH = int(os.environ.get("ONNX_BATCH", "16"))
# onnx 会话加载失败时的兜底：light 后端下禁止退回 ST（会拉 torch），退确定性哈希向量。
# 未显式设置时随 STORE_PROVIDER 联动：light ⇒ 1，seekdb ⇒ 0（保留 ST 兜底的历史行为）。
EMBEDDING_ONNX_STRICT = (os.environ.get("EMBEDDING_ONNX_STRICT", "").strip().lower()
                         or ("1" if STORE_PROVIDER == "light" else "0")) == "1"

# 检索参数
VECTOR_CANDIDATES = int(os.environ.get("VECTOR_CANDIDATES", "48"))
SEARCH_TOP_K = int(os.environ.get("SEARCH_TOP_K", "8"))
KEYWORD_WEIGHT = float(os.environ.get("KEYWORD_WEIGHT", "0.4"))

COLLECTION_NAME = os.environ.get("MULTI_AI_COLLECTION", "multi_ai_corpus")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
