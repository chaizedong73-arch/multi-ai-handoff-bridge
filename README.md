# 多AI交接桥 (Multi-AI Handoff Bridge)

> 把 A agent 干过的事，送到 B agent 手里——**跨产品、全本地、零额度、不上传任何数据**。

你同时用 ChatGPT、Codex、Claude、Trae 等多个 AI，它们的会话格式互相隔离，换手时新 AI 不知道旧 AI 干过什么。本桥自动读取各 agent 留在本机的公开会话记录，建一个可检索的证据库；接手方通过 MCP 工具 `get_handoff_brief` 按需生成一份《交接简报》——**每条都带原始日志行号，可回查核对**。

**按需冷启动、跑完即退，无常驻服务；全链路不调用任何 LLM、不联网上传，隐私数据不出本机。**

## 它是什么 / 不是什么（诚实边界）

| 能做（实测） | 不能做 |
|---|---|
| 跨产品采集会话与文件操作证据（redact 掩码） | 不做实时共享记忆（agents 不写共享存储） |
| 增量懒补（行偏移水位线，无新行不耗资源） | 不做任务派发/消息（等 A2A 生态，见 Roadmap） |
| 三路混合检索（向量+关键词+结构化过滤） | 简报无 LLM 综合——**是线索+出处，不是终稿** |
| 交接简报：意图/成果/决策/未完成线头/路径/时间线/待办 | 长线项目历史召回有上限（实测 78.9/100，见验收） |
| 行号回查（控制台/API 可逐条核对原始事件） | 封闭产品（如 ChatGPT 桌面版）需配套事件监控器采集 |

**验收数据**：3 用例回归评测 94.6/100；真实交接场景硬验收：录音记忆项目 100/100、任务提醒项目 78.9/100（答案 key 机械打分、行号扎根原始日志）。全程内存 ~0.5GB 峰值、生成一份简报约 5-8 秒。

## 架构（四个环节，全部可检查）

```
① 采集   ingest/export-corpus.mjs 只读导出各 agent 公开会话（redact 掩码）
            └─ 可选：外部事件监控器的 events.jsonl（增量懒补）
② 归一化  events_to_corpus.py → 语料 JSONL（水位线幂等）
③ 检索   store_light.py：docs.jsonl + emb.npy + numpy 融合（零 torch）
            └─ 嵌入：ONNX（默认 int8，可选 fp32 与 ST 语义逐位一致）
④ 简报   handoff_brief.py → 《交接简报》（分八节，每条带 events.jsonl:行号）
```

存储后端默认 `light`（纯 numpy，~470MB）；保留 `MULTI_AI_STORE=seekdb` 旧路径作对照。

## 快速开始

前提：Windows + WSL2（Ubuntu 等含 python3、node）。

```bash
# WSL 内，仓库根目录
bash install.sh                                  # venv + 依赖 + 嵌入模型(int8) + 自检

# 采集你本机的 agent 会话（内置：DeepSeek Harness / WorkBuddy / Codex CLI）
node ingest/export-corpus.mjs --out ingest/out/corpus.jsonl

# 导入检索库（首次全量；此后可用 catchup 增量）
"$HOME/handoff-bridge-venv/bin/python" backend/importer.py

# 试一份简报
"$HOME/handoff-bridge-venv/bin/python" backend/handoff_brief.py \
  --intent "B 接手 A 的某项目" --topic "项目名 关键词" --src agentA --dst agentB \
  --out /tmp/brief.md
```

人工检查界面（可选）：Windows 侧双击 `start-bridge.cmd` → `http://localhost:8765`，四个环节逐项可查、简报引用可逐条回查原始事件。`stop-bridge.cmd` 停止。

### 让 AI 直接调用（推荐）

注册 MCP 工具 `get_handoff_brief`（参数：`src`=交出方、`dst`=接手方、`topic`=主题关键词、`intent`=接手意图），AI 在接手场景自动调用，无需启动引擎：

<details>
<summary>ZCode（<code>~/.zcode/cli/config.json</code> → mcp.servers）</summary>

```json
"handoff-bridge": {
  "command": "node",
  "args": ["<仓库路径>/mcp/handoff-bridge-mcp.mjs"],
  "env": { "HB_AGENT": "zcode", "HB_TIMEOUT_MS": "300000" }
}
```
</details>

<details>
<summary>Codex CLI（<code>~/.codex/config.toml</code>）</summary>

```toml
[mcp_servers.handoff-bridge]
command = "node"
args = ["<仓库路径>/mcp/handoff-bridge-mcp.mjs"]
env = { HB_AGENT = "codex", HB_TIMEOUT_MS = "300000" }
```
</details>

<details>
<summary>Trae / 其他 MCP 客户端（stdio MCP 通用格式）</summary>

```json
{ "command": "node", "args": ["<仓库路径>/mcp/handoff-bridge-mcp.mjs"],
  "env": { "HB_AGENT": "trae", "HB_TIMEOUT_MS": "300000" } }
```
</details>

环境变量：`HB_CATCHUP=0` 关闭简报前增量补录；`MULTI_AI_EVENTS` 指向外部事件流（可选）；`HB_REGISTRY` 指向进程登记中心（可选，未配置则跳过登记）。

## 隐私立场

- 全部计算与数据留在本机；简报管道**不调用任何 LLM、不联网**（嵌入模型本地 ONNX 推理）
- 采集器只读导出 agent 的**公开**会话与文件操作，带 redact 掩码；密钥类内容不入库
- `events.jsonl`、语料、向量库全部落本机数据目录；本仓库不含任何用户数据

## 采集器适配现状

内置：**DeepSeek Harness / WorkBuddy / Codex CLI** 三类本地 agent 会话。其他产品（Claude Code、ChatGPT 桌面版等）需要为它们的私有会话格式写适配器——欢迎 PR；`ingest/lib/session-index.mjs` 是适配器基座。

## Roadmap

- **本地小模型简报**：用本地 LLM（如 Qwen 4B 级）做简报综合，解决无 LLM 管道的条目级筛选上限（红线不破：仍本地、零额度）
- **多产品采集器**：逐步内置更多 agent 的会话适配器
- **A2A 接入**：协议生态成熟后，桥的证据层直接作为 agent 互任务的事实来源

## 版本

- **v0.7.2**（2026-11-12）：内存优化（light 后端，简报全链 1458MB→472MB）；实时性硬验收（时间窗检索 + 识别词会话归属）；MCP 接入 ZCode；本仓库首次公开发布。
- 0.4.x-0.6.x 的演进细节见各 agent 维护记录（检索/简报/插件化/工作台模组化）。

## License

MIT
