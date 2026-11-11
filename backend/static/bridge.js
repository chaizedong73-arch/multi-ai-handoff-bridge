/* 交接桥控制台 · 前端逻辑（无构建、无依赖；状态一律来自后端真实探测） */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json; charset=utf-8" },
    ...opts,
  });
  let body = null;
  try { body = await r.json(); } catch { /* non-json */ }
  if (!r.ok) {
    const msg = (body && (body.error || body.reason || body.detail)) || `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return body;
}

/* ---------------- tab 切换 ---------------- */
let activeTab = "stage1";
function showTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === "panel-" + name));
  if (name === "ledger") { loadLedger(); loadEval(); }
  if (name === "service") renderService(lastOverview);
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => showTab(t.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach(b => b.addEventListener("click", () => showTab(b.dataset.goto)));

/* 技术说明 tooltip（沿用旧页交互的最小实现） */
const tip = $("techTip");
document.querySelectorAll(".tech-badge").forEach(b => {
  b.addEventListener("mouseenter", () => {
    tip.textContent = b.dataset.tech || "";
    const r = b.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - 360, Math.max(8, r.left)) + "px";
    tip.style.top = (r.bottom + 6) + "px";
    tip.classList.remove("hidden");
  });
  b.addEventListener("mouseleave", () => tip.classList.add("hidden"));
});

/* ---------------- ① 采集 ---------------- */
function renderStage1(o) {
  const f = o.stage1.file, m = o.stage1.meta || {};
  const owners = Object.entries(m.byOwner || {}).map(([k, v]) => `<span class="chip">${esc(k)} ${v}</span>`).join(" ");
  const kinds = Object.entries(m.byKind || {}).map(([k, v]) => `<span class="chip dim">${esc(k)} ${v}</span>`).join(" ");
  $("s1Meta").innerHTML = `
    <table class="kv">
      <tr><td>文件</td><td class="mono">${esc(f.path)}</td></tr>
      <tr><td>状态</td><td>${f.exists ? "存在 · 只读" : "缺失"} · ${fmtBytes(f.bytes)} · 更新于 ${esc(f.mtimeIso || "")}</td></tr>
      <tr><td>总行数</td><td>${m.counting ? '<span class="dim">后台扫描中…（/mnt/c 较慢，数秒后自动刷新）</span>' : `<b>${m.lines}</b> 行`}</td></tr>
      <tr><td>按 agent</td><td>${owners || (m.counting ? "…" : "—")}</td></tr>
      <tr><td>按类型</td><td>${kinds || (m.counting ? "…" : "—")}</td></tr>
    </table>
    <p class="hint">${esc(o.stage1.note)}</p>`;
  $("pm1").textContent = m.lines ? `${m.lines} 行` : (f.exists ? "扫描中" : "缺失");
  setDot("pd1", f.exists);
}
async function loadEvents() {
  const start = Math.max(1, parseInt($("evStart").value || "1", 10));
  const limit = Math.min(200, Math.max(1, parseInt($("evLimit").value || "10", 10)));
  $("evRows").innerHTML = '<div class="loading">读取中…</div>';
  try {
    const d = await api(`/api/bridge/events/inspect?start=${start}&limit=${limit}`);
    $("evRows").innerHTML = d.rows.length ? d.rows.map(rowHtml).join("") : '<div class="empty">超出文件末尾</div>';
  } catch (e) { $("evRows").innerHTML = `<div class="error">读取失败：${esc(e.message)}</div>`; }
}
function rowHtml(r) {
  return `<div class="ev-row" id="evline-${r.n}">
    <span class="ev-n mono">L${r.n}</span>
    <span class="chip dim">${esc(r.source || "?")}/${esc(r.kind || "?")}</span>
    <span class="ev-t">${esc((r.summary || "").slice(0, 260))}</span></div>`;
}
$("btnEvLoad").addEventListener("click", loadEvents);

/* 跨环节跳转：点击 events.jsonl:NNN 引用 → ① 里核对原文 */
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-evline]");
  if (!t) return;
  const n = parseInt(t.dataset.evline, 10);
  showTab("stage1");
  $("evStart").value = Math.max(1, n - 2);
  $("evLimit").value = 5;
  loadEvents().then(() => {
    const el = $("evline-" + n);
    if (el) { el.classList.add("hl"); el.scrollIntoView({ block: "center" }); }
  });
});

/* ---------------- ② 归一化 ---------------- */
function renderStage2(o) {
  const s = o.stage2, wm = s.watermark || {};
  const newLines = s.newLinesSinceWatermark;
  $("s2Stats").innerHTML = `
    <table class="kv">
      <tr><td>水位线</td><td>last_line=<b>${esc(wm.last_line ?? "—")}</b>${newLines != null ? ` · <span class="${newLines > 0 ? "warn" : "ok"}">水位线后新事件 ${newLines} 行</span>` : ""} · 更新于 ${wm.updated_at ? new Date(wm.updated_at * 1000).toLocaleString("zh-CN") : "—"}</td></tr>
      <tr><td>归一化语料</td><td>${fileLine(s.corpus)}</td></tr>
      <tr><td>增量 delta</td><td>${fileLine(s.delta)}</td></tr>
      <tr><td>产物 meta</td><td>${s.meta ? `emitted ${esc(s.meta.emitted_messages)} msg / ${esc(s.meta.sessions)} session · last_line=${esc(s.meta.last_line)}` : "—"}</td></tr>
    </table>
    <p class="hint">${esc(s.note)}</p>`;
  $("pm2").textContent = wm.last_line ? `水位 L${wm.last_line}` : "—";
  setDot("pd2", !!s.corpus || !!s.corpus.exists);
}
const fileLine = (f) => f && f.exists
  ? `存在 · ${fmtBytes(f.bytes)} · ${esc(f.mtimeIso || "")}`
  : `缺失 <span class="mono dim">${esc((f && f.path) || "")}</span>`;
$("btnCatchup").addEventListener("click", async () => {
  $("catchupMsg").textContent = "运行中…";
  $("catchupOut").classList.add("hidden");
  try {
    const d = await api("/api/bridge/catchup", { method: "POST" });
    $("catchupOut").classList.remove("hidden");
    $("catchupOut").textContent = JSON.stringify(d, null, 2);
    $("catchupMsg").textContent = `完成，耗时 ${d.duration_ms} ms`;
    refreshOverview();
  } catch (e) { $("catchupMsg").textContent = `失败：${e.message}`; }
});
$("btnCpLoad").addEventListener("click", async () => {
  const start = Math.max(1, parseInt($("cpStart").value || "1", 10));
  const limit = Math.min(200, Math.max(1, parseInt($("cpLimit").value || "8", 10)));
  $("cpRows").innerHTML = '<div class="loading">读取中…</div>';
  try {
    const d = await api(`/api/bridge/corpus/inspect?start=${start}&limit=${limit}`);
    $("cpRows").innerHTML = d.rows.map(r => `<div class="ev-row"><span class="ev-n mono">L${r.n}</span>
      <span class="chip dim">${esc(r.owner || "?")}/${esc(r.type || "?")}</span>
      <span class="ev-t">${esc(r.text.slice(0, 260))}</span></div>`).join("") || '<div class="empty">空</div>';
  } catch (e) { $("cpRows").innerHTML = `<div class="error">读取失败：${esc(e.message)}</div>`; }
});

/* ---------------- ③ 检索 ---------------- */
function renderStage3(o) {
  const s = o.stage3;
  $("s3Status").innerHTML = `
    <table class="kv">
      <tr><td>集合</td><td class="mono">${esc(s.collection)}</td></tr>
      <tr><td>数据目录</td><td class="mono">${esc(s.seekdbPath)} <span class="dim">（WSL 原生，非 /mnt/c）</span></td></tr>
      <tr><td>驻留状态</td><td>${s.open
        ? `<span class="ok">已打开</span>${s.lastLoad ? ` · 上次加载 ${s.lastLoad.load_ms} ms · RSS ${s.lastLoad.rss_before_mb}→${s.lastLoad.rss_after_mb} MB` : ""}`
        : '<span class="dim">未打开（不占模型内存；点下方按钮显式加载）</span>'}</td></tr>
      <tr><td>嵌入模型</td><td>${s.st_model_loaded ? '<span class="ok">已驻留</span>' : '<span class="dim">未加载</span>'}</td></tr>
    </table>`;
  $("pm3").textContent = s.open ? "已加载" : "未加载";
  setDot("pd3", s.open);
}
$("btnS3Load").addEventListener("click", async () => {
  $("s3LoadMsg").textContent = "加载中…（首次约 20-40s：pyseekdb + torch + ST 模型）";
  try {
    const d = await api("/api/bridge/stage3/load", { method: "POST" });
    const st = d.stats || {};
    $("s3LoadMsg").textContent = `已打开：${st.count} 条` +
      (d.load ? ` · 加载 ${d.load.load_ms} ms · RSS ${d.load.rss_before_mb}→${d.load.rss_after_mb} MB` : "");
    fillOwnerFilter(st.byOwner);
    refreshOverview();
  } catch (e) { $("s3LoadMsg").textContent = `失败：${e.message}`; }
});
function fillOwnerFilter(byOwner) {
  const sel = $("qOwner");
  const cur = sel.value;
  const names = new Set(["dsh", "workbuddy", "codex", "chatgpt"]);
  Object.keys(byOwner || {}).forEach(k => names.add(k));
  sel.innerHTML = '<option value="">全部</option>' + [...names].map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = cur;
}
$("btnSearch").addEventListener("click", async () => {
  const q = $("qInput").value.trim();
  if (!q) { $("searchMsg").textContent = "先输入检索词"; return; }
  $("searchMsg").textContent = "检索中…";
  const body = { query: q, owner: $("qOwner").value || null, kind: $("qKind").value || null, top_k: parseInt($("qTopK").value || "6", 10) };
  try {
    const d = await api("/api/search", { method: "POST", body: JSON.stringify(body) });
    $("searchMsg").textContent = `${d.results.length} 条 · ${d.engine || ""}`;
    $("s3Results").innerHTML = d.results.map(r => {
      const line = r.citation && r.citation.srcLine;
      return `<div class="result">
        <div class="score-bar"><span style="width:${Math.min(100, r.score * 100)}%"></span><b>${Number(r.score).toFixed(3)}</b></div>
        <div class="r-text">${esc(String(r.text || "").slice(0, 300))}</div>
        <div class="r-meta">
          <span class="chip dim">${esc((r.meta && r.meta.owner) || "?")}/${esc((r.meta && r.meta.kind) || "?")}</span>
          <span class="chip dim">vec ${sig(r.signals, "vector")} · kw ${sig(r.signals, "keyword")}</span>
          ${line ? `<button class="cite mono" data-evline="${line}">events.jsonl:${line} ↗</button>` : ""}
        </div></div>`;
    }).join("") || '<div class="empty">无命中</div>';
    loadRlog();
  } catch (e) { $("searchMsg").textContent = `失败：${e.message}`; }
});
const sig = (s, k) => (s && s[k] != null) ? Number(s[k]).toFixed(2) : "—";
async function loadRlog() {
  try {
    const d = await api("/api/bridge/retrieval_log?limit=10");
    $("s3Rlog").textContent = d.rows.map(r =>
      `${new Date(r.ts * 1000).toLocaleString("zh-CN")} mode=${r.mode} top=${(r.results || []).length} q=${String(r.query || "").slice(0, 40)}`).join("\n") || "（空）";
  } catch { $("s3Rlog").textContent = "读取失败"; }
}

/* ---------------- ④ 简报 ---------------- */
let briefPoller = null;
$("btnBrief").addEventListener("click", async () => {
  $("briefMsg").textContent = "提交中…";
  try {
    const d = await api("/api/bridge/brief/run", {
      method: "POST",
      body: JSON.stringify({
        intent: $("bIntent").value.trim(), topic: $("bTopic").value.trim(),
        src: $("bSrc").value || null, dst: $("bDst").value || null,
        catchup: $("bCatchup").checked,
      }),
    });
    $("briefMsg").textContent = `任务 ${d.jobId} 已提交`;
    $("s4Phases").classList.remove("hidden");
    clearInterval(briefPoller);
    briefPoller = setInterval(pollBrief, 1000);
    pollBrief();
  } catch (e) { $("briefMsg").textContent = `提交失败：${e.message}`; }
});
async function pollBrief() {
  let d;
  try { d = await api("/api/bridge/brief/status"); } catch { return; }
  const job = d.job;
  if (!job) { clearInterval(briefPoller); return; }
  renderPhases(job);
  if (!d.active) {
    clearInterval(briefPoller);
    if (job.phase === "error") {
      $("briefMsg").textContent = `失败：${job.error}`;
      return;
    }
    const r = job.result;
    $("briefMsg").textContent = `完成 · ${fmtBytes(r.bytes)} · 已存 ${r.path}`;
    showBrief(r.markdown, r);
    refreshOverview();
  }
}
function renderPhases(job) {
  const order = ["queued", "catchup(懒补)", "warmup(载入模型)", "build(检索+组装)", "done"];
  $("s4Phases").innerHTML = order.map((name) => {
    const hit = job.phases.find(p => p.name === name);
    const cur = job.phase === name;
    const cls = hit ? (cur && job.phase !== "done" ? "ph-cur" : "ph-done") : "ph-wait";
    return `<span class="ph ${cls}">${name}${hit ? ` · ${hit.at}s` : ""}</span>`;
  }).join('<span class="ph-arrow">→</span>')
    + (job.phase === "error" ? `<span class="ph ph-err">error：${esc(job.error || "")}</span>` : "");
}
function showBrief(md, result) {
  $("s4ResultCard").classList.remove("hidden");
  $("s4Meta").textContent = result ? `${fmtBytes(result.bytes)} · ${result.path}` : "";
  $("s4Timing").innerHTML = result && result.timing
    ? `<span class="chip">懒补 ${result.timing.catchup_ms} ms</span><span class="chip">载入模型 ${result.timing.warmup_ms} ms</span><span class="chip">检索+组装 ${result.timing.build_ms} ms</span><span class="chip">共 ${result.timing.total_ms} ms</span><span class="chip dim">载入后 RSS ${result.timing.rss_after_warmup_mb} MB</span>`
    : "";
  const isRaw = $("btnBriefRaw").dataset.raw === "1";
  $("s4Brief").innerHTML = isRaw
    ? `<pre class="context-box">${esc(md)}</pre>`
    : renderBriefSections(md);
}
$("btnBriefRaw").addEventListener("click", () => {
  const b = $("btnBriefRaw");
  b.dataset.raw = b.dataset.raw === "1" ? "0" : "1";
  b.textContent = b.dataset.raw === "1" ? "分节视图" : "原文/分节切换";
  const md = b.dataset.md || "";
  if (md) showBrief(md, { bytes: md.length, path: "", timing: null });
});
function linkifyCites(s) {
  return s.replace(/events\.jsonl:(\d+)/g,
    '<button class="cite mono" data-evline="$1">events.jsonl:$1 ↗</button>');
}
function renderBriefSections(md) {
  const parts = md.split(/^## /m).filter(Boolean);
  return parts.map((chunk, i) => {
    let title = i === 0 ? "头部" : chunk.split("\n")[0];
    let body = i === 0 ? chunk : chunk.split("\n").slice(1).join("\n");
    let html = esc(body.trim())
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    html = linkifyCites(html);
    return `<div class="brief-sec"><h4>${esc(title)}</h4><pre class="brief-text">${html}</pre></div>`;
  }).join("");
}
let _lastBriefsSig = "";
function renderHistory(briefs) {
  const sig = JSON.stringify((briefs || []).map(b => b.path + "|" + b.bytes));
  if (sig === _lastBriefsSig) return; // 内容没变就别重建 DOM，避免按钮在点击瞬间被替换
  _lastBriefsSig = sig;
  $("s4History").innerHTML = (briefs || []).length
    ? briefs.map(b => `<div class="brief-item">
        <button class="ghost small" data-brief="${esc(b.path)}">${esc(b.name)}</button>
        <span class="chip dim">${esc(b.source)}</span><span class="dim">${fmtBytes(b.bytes)} · ${esc(b.mtimeIso)}</span>
      </div>`).join("")
    : '<div class="empty">还没有简报——上面点「生成简报」跑一份真实的</div>';
  document.querySelectorAll("[data-brief]").forEach(el =>
    el.addEventListener("click", async () => {
      try {
        const d = await api("/api/bridge/brief/file?path=" + encodeURIComponent(el.dataset.brief));
        $("btnBriefRaw").dataset.md = d.markdown;
        showBrief(d.markdown, null);
      } catch (e) { $("briefMsg").textContent = `读取失败：${e.message}`; }
    }));
}
$("btnBrief").addEventListener("click", () => { $("btnBriefRaw").dataset.raw = "0"; $("btnBriefRaw").textContent = "原文/分节切换"; });

/* ---------------- 台账 · 评测 ---------------- */
async function loadLedger() {
  $("ledgerRows").innerHTML = '<div class="loading">读取中…</div>';
  try {
    const d = await api("/api/bridge/ledger?limit=50");
    $("ledgerRows").innerHTML = d.rows.length
      ? d.rows.map(r => `<div class="ev-row">
          <span class="chip dim">${esc((r.ts || "").replace("T", " ").slice(0, 19))}</span>
          <span class="chip">${esc(r.host || "?")}</span>
          <span class="chip dim">${esc(r.src || "?")} → ${esc(r.dst || "?")}</span>
          <span class="chip ${r.ok ? "ok" : "warn"}">${r.ok ? "成功" : "失败"}</span>
          <span class="chip dim">${r.duration_ms} ms · ${fmtBytes(r.bytes)}</span>
          <span class="ev-t">${esc(String(r.intent || r.topic || "").slice(0, 120))}</span>
        </div>`).join("")
      : '<div class="empty">台账为空——还没有 agent 通过 MCP 调过桥</div>';
  } catch (e) { $("ledgerRows").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}
$("btnLedgerReload").addEventListener("click", loadLedger);
async function loadEval() {
  try {
    const d = await api("/api/bridge/eval");
    $("evalCards").innerHTML = d.reports.filter(r => !r.missing).map(r => `
      <div class="eval-card">
        <div class="eval-avg"><b>${r.parsed.avg ?? "—"}</b><span>/ 100</span></div>
        <div class="eval-label">${esc(r.label)}<br><span class="dim mono">${esc(r.file)} · ${esc(r.mtimeIso || "")}</span></div>
        ${r.parsed.cases.map(c => `<div class="eval-case"><span class="mono">${esc(c.case)}</span><b>${c.total}</b>
          <span class="dim">线头${c.open}% 事实${c.fact}% 噪声${c.noise}% 路径${c.path}% 结构${c.struct}%</span></div>`).join("")}
        <details><summary>原始报告</summary><pre class="context-box">${esc(r.raw)}</pre></details>
      </div>`).join("") + `<p class="hint">权重：${esc(d.weights)}</p>`;
  } catch (e) { $("evalCards").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

/* ---------------- 服务 · 生命周期 ---------------- */
let shuttingDown = false, failCount = 0;
function renderService(o) {
  const chip = $("svcStateChip"), body = $("svcBody");
  if (shuttingDown) { chip.textContent = "关闭中…"; chip.className = "chip warn"; return; }
  if (!o) {
    chip.textContent = failCount > 2 ? "已停止" : "连接中…";
    chip.className = "chip " + (failCount > 2 ? "" : "warn");
    body.innerHTML = failCount > 2
      ? `<p>服务未运行（连接被拒）。<b>再次启动：双击 <code>multi-ai\\start-bridge.cmd</code></b>，启动后本页恢复可用；历史简报与台账都保留在磁盘。</p>`
      : "探测中…";
    return;
  }
  const p = o.server;
  chip.textContent = "运行中";
  chip.className = "chip ok";
  body.innerHTML = `
    <table class="kv">
      <tr><td>PID</td><td class="mono">${p.pid} <span class="dim">（WSL 内 uvicorn；宿主侧经 reg.cmd 登记 owner=zcode）</span></td></tr>
      <tr><td>内存 RSS</td><td><b>${p.rss_mb} MB</b>${p.collection_open ? '（含 pyseekdb/torch/嵌入模型驻留）' : '（轻启动：重资产未加载）'}</td></tr>
      <tr><td>运行时长</td><td>${Math.floor(p.uptime_s / 60)} 分 ${p.uptime_s % 60} 秒</td></tr>
      <tr><td>集合</td><td class="mono">${esc(p.collection)} · ${p.collection_open ? "已打开" : "未打开"}</td></tr>
    </table>`;
}
$("btnShutdown").addEventListener("click", async () => {
  shuttingDown = true;
  $("svcMsg").textContent = "发送 SIGTERM…";
  try { await api("/api/bridge/shutdown", { method: "POST" }); } catch (e) { /* 服务可能已退 */ }
});

/* ---------------- 总览轮询（5s，真实状态） ---------------- */
let lastOverview = null;
function setDot(id, ok) { const d = $(id); d.className = "pc-dot " + (ok ? "on" : "off"); }
function fmtBytes(b) {
  if (b == null) return "—";
  if (b > 1048576) return (b / 1048576).toFixed(1) + " MB";
  if (b > 1024) return (b / 1024).toFixed(1) + " KB";
  return b + " B";
}
async function refreshOverview() {
  try {
    const o = await api("/api/bridge/overview");
    failCount = 0;
    if (shuttingDown) return; // 关闭流程里不再乐观显示运行中
    lastOverview = o;
    const p = o.server;
    $("procPill").textContent = `服务运行中 · PID ${p.pid} · RSS ${p.rss_mb} MB · ${Math.floor(p.uptime_s / 60)}min`;
    $("procPill").className = "status-pill on";
    renderStage1(o); renderStage2(o); renderStage3(o); renderService(o);
    $("pm4").textContent = `${o.stage4.briefsCount} 份`;
    setDot("pd4", o.stage4.briefsCount > 0);
    renderHistory(o.stage4.briefs);
    if (o.stage1.meta && o.stage1.meta.counting !== true) {
      // 语料 meta 可能晚于 events 元数据，重算新增行
      o.stage2.newLinesSinceWatermark = null;
    }
  } catch (e) {
    failCount++;
    $("procPill").textContent = failCount > 2 ? "服务未连接" : "连接中断…";
    $("procPill").className = "status-pill off";
    lastOverview = null;
    renderService(null);
  }
}
refreshOverview();
setInterval(refreshOverview, 5000);
loadEvents();
