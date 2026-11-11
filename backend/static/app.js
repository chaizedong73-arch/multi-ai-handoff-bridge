// 多AI协同 v2 前端逻辑：四页交互 + 无后端时用真实运行快照（window.DEMO）演示。
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const DEMO = window.DEMO || {};
let LIVE = false;

// ---------- 通用 ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function shortPath(p) {
  if (!p) return "";
  const parts = String(p).split(/[\\/]/).filter(Boolean);
  return parts.length <= 2 ? String(p) : "…/" + parts.slice(-2).join("/");
}
function ownerDot(o) { return `<span class="dot"></span>`; }

// ---------- 状态 & Tab ----------
async function checkHealth() {
  try {
    await api("/api/health");
    LIVE = true;
    $("#statusPill").textContent = "● 已连接后端";
    $("#statusPill").className = "status-pill ok";
    $("#demoBanner").classList.add("hidden");
  } catch (e) {
    LIVE = false;
    $("#statusPill").textContent = "● 演示模式（真实快照）";
    $("#statusPill").className = "status-pill demo";
    $("#demoBanner").classList.remove("hidden");
  }
}
function initTabs() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => activateTab(b.dataset.goto)));
}
function activateTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.remove("active"));
  const p = $("#panel-" + name);
  if (p) p.classList.add("active");
  if (name === "memory") { loadMemory(); loadHandoff(); }
  if (name === "search") loadStats();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ---------- 技术说明气泡 ----------
function initTechTips() {
  const tip = $("#techTip");
  $$("[data-tech]").forEach((el) => {
    el.addEventListener("mouseenter", (e) => {
      tip.textContent = el.dataset.tech;
      tip.classList.remove("hidden");
      const r = el.getBoundingClientRect();
      tip.style.top = (r.bottom + window.scrollY + 8) + "px";
      tip.style.left = Math.min(r.left + window.scrollX, window.innerWidth - 340) + "px";
    });
    el.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
}

// ---------- 渲染：引用 / 证据卡 ----------
function citeHtml(cit, meta) {
  cit = cit || {}; meta = meta || {};
  const owner = cit.owner || meta.owner || "";
  const kind = cit.kind || meta.kind || "";
  const tags = [];
  if (owner) tags.push(`<span class="tag owner">${esc(owner)}</span>`);
  if (kind) tags.push(`<span class="tag kind-${esc(kind)}">${esc(kind)}</span>`);
  if (meta.role) tags.push(`<span class="tag">${esc(meta.role)}</span>`);
  if (meta.action) tags.push(`<span class="tag">${esc(meta.action)}${meta.status ? "·" + esc(meta.status) : ""}</span>`);
  const loc = cit.srcFile ? `<span class="mono" title="${esc(cit.srcFile)}">${esc(shortPath(cit.srcFile))}${cit.srcLine ? ":" + esc(cit.srcLine) : ""}</span>` : "";
  const sess = cit.sessionTitle ? `<span>《${esc(cit.sessionTitle)}》</span>` : "";
  return { tags: tags.join(""), loc, sess };
}
function evCard(r) {
  const c = citeHtml(r.citation, r.meta);
  const sig = r.signals ? `<span class="sig">向量 ${r.signals.vector} · 关键词 ${r.signals.keyword}</span>` : "";
  return `<div class="ev-card">
    <p class="text">${esc(r.text)}</p>
    <div class="cite"><span class="score">${(r.score != null ? r.score : 0).toFixed ? r.score.toFixed(3) : r.score}</span>${c.tags}${c.sess}${c.loc}${sig}</div>
  </div>`;
}

// ---------- 关于：agent 卡片 + 快照 ----------
function renderAgentGrid(stats) {
  const grid = $("#agentGrid");
  if (!grid) return;
  const byOwner = (stats && stats.byOwner) || {};
  const byKind = (stats && stats.byKind) || {};
  const names = { dsh: "DSH", workbuddy: "WorkBuddy", codex: "Codex" };
  const order = ["dsh", "workbuddy", "codex"];
  grid.innerHTML = order.map((o) => `
    <div class="agent-card own-${o}">
      <div class="own">${ownerDot(o)}${names[o] || o}</div>
      <div class="kv">语料块：<strong>${byOwner[o] != null ? byOwner[o] : "—"}</strong><br>
      角色：本地 AI 工具会话日志（只读采集，凭据脱敏）</div>
    </div>`).join("");
  const tag = $("#corpusTag");
  if (tag && stats) tag.textContent = `共 ${stats.count} 块 · message ${byKind.message || 0} / change ${byKind.change || 0} / session ${byKind.session || 0}`;
}
async function loadStats() {
  let stats = DEMO.stats;
  if (LIVE) { try { const r = await api("/api/stats"); if (r && r.available) stats = r; } catch (e) {} }
  renderAgentGrid(stats);
}
function renderAboutShots() {
  // ① 检索
  const ss = $("#shotSearch");
  if (ss) {
    const top = (DEMO.search && DEMO.search.results || []).slice(0, 2);
    ss.innerHTML = `<p class="q">🔎 ${esc((DEMO.search || {}).query || "")}</p>` +
      top.map((r) => { const c = citeHtml(r.citation, r.meta);
        return `<div class="result-card"><p class="text">${esc(r.text.slice(0, 120))}…</p>
        <div class="src"><span class="score">${r.score.toFixed(3)}</span>${c.tags}${c.loc}</div></div>`; }).join("");
  }
  // ② 派工
  const sd = $("#shotDispatch");
  if (sd && DEMO.dispatch) {
    const t = DEMO.dispatch.tasks[0] || {};
    const q = t.quality || {};
    sd.innerHTML = `<p class="q">🎯 目标：${esc((DEMO.dispatch.goal || "").slice(0, 40))}</p>
      <div class="mini-stat"><div class="stat"><div class="n">${(t.receipt || {}).evidenceCount || 0}</div><div class="l">证据</div></div>
      <div class="stat"><div class="n">${((q.coverage || 0) * 100).toFixed(0)}%</div><div class="l">覆盖</div></div>
      <div class="stat"><div class="n">${(t.rounds || []).length}</div><div class="l">轮次</div></div></div>
      <p class="doc-meta" style="margin-top:8px">策略：${esc((t.rounds || []).map((r) => r.strategy).join(" → "))}</p>`;
  }
  // ③ 交接
  const sh = $("#shotHandoff");
  if (sh && DEMO.handoff) {
    const h = DEMO.handoff;
    sh.innerHTML = `<p class="q">🤝 ${esc((h.objective || "").slice(0, 46))}</p>
      <div class="ctx">disposition: ${esc(h.disposition)} · state ${h.stateCount} 条\n证据核验：${esc(h.evidenceAvailable)} available</div>
      ${(h.evidenceChecks || []).slice(0, 3).map((c) => `<div class="plan-li"><span class="doc-meta">${esc(c.claim)}${c.stateIndex != null ? "[" + c.stateIndex + "]" : ""}</span><span style="margin-left:auto;color:var(--pass)">${esc(c.status)}</span></div>`).join("")}`;
  }
}

// ---------- 检索页 ----------
async function doSearch() {
  const query = $("#qInput").value.trim();
  if (!query) { $("#searchMsg").textContent = "请输入检索词"; return; }
  const owner = $("#qOwner").value || null;
  const kind = $("#qKind").value || null;
  const top_k = parseInt($("#qTopK").value || "6", 10);
  const out = $("#searchResults");
  $("#btnSearch").disabled = true;
  $("#searchMsg").textContent = "";
  out.innerHTML = `<div class="loading"><span class="spinner"></span>检索中…</div>`;
  let data = null;
  if (LIVE) {
    try { data = await api("/api/search", { method: "POST", body: { query, owner, kind, top_k } }); }
    catch (e) { $("#searchMsg").textContent = "后端检索失败，回退演示快照"; $("#searchMsg").className = "msg err"; }
  }
  if (!data || !data.available) {
    data = { available: false, mode: (DEMO.search || {}).mode, results: (DEMO.search || {}).results || [] };
  }
  $("#btnSearch").disabled = false;
  const res = data.results || [];
  $("#searchMsg").textContent = `命中 ${res.length} 条 · 模式 ${data.mode || "—"}${data.available ? "" : "（演示快照）"}`;
  $("#searchMsg").className = "msg" + (data.available ? " ok" : "");
  out.innerHTML = res.length ? res.map(evCard).join("") : `<div class="loading">无命中</div>`;
}
async function doFindFile() {
  const path = $("#fileInput").value.trim();
  if (!path) return;
  const out = $("#fileResults");
  out.innerHTML = `<div class="loading"><span class="spinner"></span>反查中…</div>`;
  let data = null;
  if (LIVE) { try { data = await api("/api/find_file", { method: "POST", body: { path } }); } catch (e) {} }
  if (!data || !data.available) data = { available: false, ...(DEMO.findByFile || {}) };
  const matches = data.matches || [];
  out.innerHTML = `<p class="hint">归一化：<span class="mono">${esc(data.normalized || "")}</span> · 命中 ${data.matchCount != null ? data.matchCount : matches.length} 条${data.available ? "" : "（演示快照）"}</p>` +
    (matches.length ? matches.map((m) => {
      const cit = m.citation || {};
      return `<div class="ev-card"><p class="text"><span class="tag kind-change">${esc(m.action || "change")}${m.status ? "·" + esc(m.status) : ""}</span> ${esc(m.path || "")}</p>
      <div class="cite">${cit.owner ? `<span class="tag owner">${esc(cit.owner)}</span>` : ""}${cit.sessionTitle ? `<span>《${esc(cit.sessionTitle)}》</span>` : ""}${cit.srcFile ? `<span class="mono">${esc(shortPath(cit.srcFile))}${cit.srcLine ? ":" + esc(cit.srcLine) : ""}</span>` : ""}</div></div>`;
    }).join("") : `<div class="loading">无反查结果</div>`);
}

// ---------- 派工闭环页 ----------
function qbars(q) {
  q = q || {};
  const bar = (label, val, gate) => `<div class="qbar ${gate ? "gate" : ""}">
    <div class="l"><span>${label}${gate ? ' <span class="gate-tag">门槛</span>' : ' <span class="gate-tag">信号</span>'}</span><span>${((val || 0) * 100).toFixed(0)}%</span></div>
    <div class="track"><div class="fill" style="width:${Math.max(0, Math.min(1, val || 0)) * 100}%"></div></div></div>`;
  return `<div class="qbars">${bar("覆盖", q.coverage, true)}${bar("权威", q.authority, true)}${bar("时效", q.timeliness, false)}${bar("一致", q.consistency, false)}</div>`;
}
function normTask(t) {
  const tgt = t.target || {};
  return {
    taskId: t.taskId, owner: t.owner || tgt.owner, sessionKey: t.sessionKey || tgt.sessionKey,
    sessionTitle: t.sessionTitle || tgt.sessionTitle, workingDir: t.workingDir || tgt.workingDir,
    mode: t.mode, objective: t.objective, query: t.query, rounds: t.rounds || [],
    quality: t.quality || {}, receipt: t.receipt || {}, evidenceVersion: t.evidenceVersion,
    evidence: t.evidence || [],
  };
}
function renderDispatch(rec) {
  const out = $("#dispatchOut");
  if (!rec) { out.innerHTML = ""; return; }
  const tasks = (rec.tasks || []).map(normTask);
  const cross = rec.crosscheck || {};
  const fin = rec.finalize || {};
  const targets = (rec.targets || []).map((t) => `<span class="tag owner">${esc(t.owner)}</span> <span class="mono">${esc(t.sessionKey || "")}</span>`).join(" &nbsp;·&nbsp; ");
  let html = `<div class="card"><h3>派工计划 <span class="pill-rev">${esc(rec.parentId || "")}</span></h3>
    <p class="hint">目标：${esc(rec.goal || "")} · 状态：${esc(rec.status || "")}${rec.available === false ? " ·（演示快照）" : ""}</p>
    <p style="font-size:13px;margin:8px 0 0">派给：${targets}</p></div>`;

  html += tasks.map((t) => {
    const v = t.quality.pass ? `<span class="verdict pass">质量达标</span>` : `<span class="verdict fail">未达标（已尽力）</span>`;
    const rounds = (t.rounds || []).map((r) => `<li><span class="rn">${r.round}</span><div>
      <div>策略：<strong>${esc(r.strategy)}</strong> · 命中 ${r.candidates} · 模式 ${esc(r.mode)}</div>
      <div class="st">覆盖 ${((r.quality.coverage || 0) * 100).toFixed(0)}% · 权威 ${((r.quality.authority || 0) * 100).toFixed(0)}% →
      <span class="${r.quality.pass ? "ok" : "no"}">${r.quality.pass ? "达标" : "未达标 " + (r.quality.failed || []).join("/")}</span></div></div></li>`).join("");
    const ev = (t.evidence || []).map(evCard).join("");
    return `<div class="card task-block">
      <div class="task-head"><div class="who"><span class="tag owner">${esc(t.owner)}</span> ${esc(t.sessionTitle || "")}</div>${v}</div>
      <p class="hint mono">${esc(t.sessionKey || "")} · cwd ${esc(shortPath(t.workingDir) || "—")} · 模式 ${esc(t.mode || "")}</p>
      <p style="font-size:13px;margin:8px 0">${esc(t.objective || "")}</p>
      ${qbars(t.quality)}
      <h3 style="font-size:13px">重捞轨迹（预算 ${t.receipt.rounds || 0} 轮 · 证据版本 <span class="mono">${esc(t.evidenceVersion || "")}</span>）</h3>
      <ul class="rounds">${rounds}</ul>
      <h3 style="font-size:13px">顶证据（${(t.evidence || []).length}）</h3>${ev || '<div class="loading">无证据</div>'}
    </div>`;
  }).join("");

  html += `<div class="card"><h3>跨 agent 交叉核验</h3><div class="crosscheck">
    <div class="cc-stat ${cross.sharedPaths ? "good" : ""}"><div class="n">${cross.sharedPaths || 0}</div><div class="l">互证路径</div></div>
    <div class="cc-stat ${cross.conflictPaths ? "bad" : "good"}"><div class="n">${cross.conflictPaths || 0}</div><div class="l">冲突路径</div></div>
    <div class="cc-stat ${cross.consistent ? "good" : "bad"}"><div class="n">${cross.consistent ? "一致" : "不一致"}</div><div class="l">整体结论</div></div>
  </div><p class="hint">一致性=不同 agent 的证据是否指向同一批产出、有无状态冲突。互证为 0 说明两 agent 各自在不同目录工作（诚实结论，非缺陷）。</p></div>`;

  html += `<div class="card"><h3>整合 → 共享记忆 → 交接</h3>
    <div class="stat-grid">
      <div class="stat"><div class="n">${fin.handoffRevision != null ? fin.handoffRevision : "—"}</div><div class="l">交接 revision</div></div>
      <div class="stat"><div class="n">${(fin.sources || []).length}</div><div class="l">落 source</div></div>
      <div class="stat"><div class="n">${fin.memoryCitations || 0}</div><div class="l">记忆引用</div></div>
      <div class="stat"><div class="n">${esc(fin.evidenceAvailable || "—")}</div><div class="l">证据核验</div></div>
    </div>
    <p class="hint">disposition：${esc(fin.disposition || "—")} · 读回：${esc(fin.readbackStatus || "—")} · 未覆盖项：${fin.omissions || 0}</p>
    <p class="hint">整合结论已写入 PowerContext 共享记忆，交接工件每条 state 挂引用；接收方 continue 时服务端逐条核验证据可得性（见「共享记忆 · 交接」页）。</p>
  </div>`;
  out.innerHTML = html;
}
async function doDispatch() {
  const goal = $("#goalInput").value.trim();
  if (!goal) { $("#dispatchMsg").textContent = "请输入总目标"; return; }
  const num_agents = parseInt($("#numAgents").value || "2", 10);
  const max_rounds = parseInt($("#maxRounds").value || "2", 10);
  const mode = $("#dispMode").value;
  const out = $("#dispatchOut");
  $("#btnDispatch").disabled = true;
  $("#dispatchMsg").textContent = "";
  out.innerHTML = `<div class="loading"><span class="spinner"></span>派工闭环运行中（多轮检索 + 共享记忆写入 + 交接核验，约数十秒）…</div>`;
  let rec = null;
  if (LIVE) {
    try { rec = await api("/api/dispatch", { method: "POST", body: { goal, num_agents, max_rounds, mode } }); }
    catch (e) { $("#dispatchMsg").textContent = "后端派工失败，回退演示快照"; $("#dispatchMsg").className = "msg err"; }
  }
  if (!rec || !rec.available) { rec = { available: false, ...(DEMO.dispatch || {}) }; }
  $("#btnDispatch").disabled = false;
  $("#dispatchMsg").textContent = rec.available ? "闭环完成（实时）" : "闭环完成（演示快照）";
  $("#dispatchMsg").className = "msg ok";
  renderDispatch(rec);
}

// ---------- 共享记忆 · 交接页 ----------
async function loadMemory() {
  let status = { available: false };
  if (LIVE) { try { status = await api("/api/memory/status"); } catch (e) {} }
  const chip = $("#memStatus");
  if (status.available) { chip.textContent = "已连接 · " + (status.scope_id || "").slice(0, 18); chip.className = "chip ready"; }
  else { chip.textContent = "演示快照"; chip.className = "chip empty"; }
  // 历史上下文
  let ctx = DEMO.memoryContext || { status: "unavailable", content: "" };
  if (LIVE) { try { const r = await api("/api/memory/context?query=" + encodeURIComponent("多AI协同 共享记忆 交接 进度")); if (r && r.available) ctx = r; } catch (e) {} }
  const box = $("#memContext");
  box.textContent = ctx.content || (ctx.status === "empty" ? "（无历史上下文）" : "（不可用）");
}
async function doMemSearch() {
  const query = $("#memQuery").value.trim() || (DEMO.dispatch || {}).goal || "派工结论";
  const list = $("#memHits");
  list.innerHTML = `<li class="empty">检索中…</li>`;
  let data = null;
  if (LIVE) { try { data = await api("/api/memory/search", { method: "POST", body: { query, limit: 8 } }); } catch (e) {} }
  if (!data || !data.available) data = { available: false, ...(DEMO.memorySearch || {}) };
  const hits = data.hits || [];
  list.innerHTML = hits.length ? hits.map((h) => `<li><div><div style="font-size:13px;line-height:1.6">${esc(h.text)}</div>
    <div class="doc-meta">score ${(h.score || 0).toFixed ? (h.score || 0).toFixed(3) : h.score} · ${esc((h.matched_by || []).join ? (h.matched_by || []).join(",") : h.matched_by || "")} · 模式 ${esc(data.mode || "")}${data.available ? "" : " · 演示快照"}</div></div></li>`).join("")
    : `<li class="empty">无记忆命中</li>`;
}
async function loadHandoff() {
  const body = $("#handoffBody");
  body.innerHTML = `<div class="loading"><span class="spinner"></span>读取交接…</div>`;
  let data = null;
  if (LIVE) { try { data = await api("/api/handoff"); } catch (e) {} }
  if (!data || !data.available) {
    const h = DEMO.handoff || {};
    $("#hoRev").textContent = "演示快照";
    body.innerHTML = `<p class="hint">objective：${esc(h.objective || "—")}</p>
      <p class="hint">disposition：${esc(h.disposition || "—")} · state ${h.stateCount || 0} 条 · 证据核验 ${esc(h.evidenceAvailable || "—")}</p>
      <div class="echecks">${(h.evidenceChecks || []).map((c) => `<div class="echeck"><span>${esc(c.claim)}${c.stateIndex != null ? " [" + c.stateIndex + "]" : ""}</span><span class="st ${esc(c.status)}">${esc(c.status)}</span></div>`).join("")}</div>`;
    return;
  }
  // live
  const content = data.content || {};
  const checks = data.evidence_checks || [];
  const avail = checks.filter((c) => c.status === "available").length;
  $("#hoRev").textContent = "status " + (data.status || "") + " · 证据 " + avail + "/" + checks.length;
  const state = (content.state || []).map((s, i) => `<div class="ho-state"><div class="t">${esc(s.text)}</div>
    <div class="c">${(s.citations || []).map((c) => esc(c.kind) + (c.source_ref ? ":" + esc(c.source_ref.source_id).slice(0, 24) : c.memory_citation ? ":" + esc(c.memory_citation.entry_id || "").slice(0, 24) : "")).join(" · ")}</div></div>`).join("");
  const na = content.next_action ? `<div class="ho-state"><div class="t"><strong>Next：</strong>${esc(content.next_action.text)}</div></div>` : "";
  body.innerHTML = `<p class="hint">objective：${esc(content.objective || "—")} · disposition：${esc(content.disposition || "—")}</p>
    ${state}${na}
    <h3 style="font-size:13px;margin-top:10px">服务端证据核验（handoff/continue）</h3>
    <div class="echecks">${checks.map((c) => `<div class="echeck"><span>${esc(c.claim)}${c.state_index != null ? " [" + c.state_index + "]" : ""}</span><span class="st ${esc(c.status)}">${esc(c.status)}</span></div>`).join("")}</div>`;
}

// ---------- 初始化 ----------
function init() {
  initTabs();
  initTechTips();
  renderAboutShots();
  $("#btnSearch").addEventListener("click", doSearch);
  $("#qInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $("#btnFindFile").addEventListener("click", doFindFile);
  $("#btnDispatch").addEventListener("click", doDispatch);
  $("#goalInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doDispatch(); });
  $("#btnMemSearch").addEventListener("click", doMemSearch);
  $("#btnReloadHandoff").addEventListener("click", loadHandoff);
  // 预填演示检索词/目标
  if (DEMO.search) $("#qInput").value = DEMO.search.query || "";
  if (DEMO.dispatch) $("#goalInput").value = DEMO.dispatch.goal || "";
  if (DEMO.findByFile && DEMO.findByFile.file) $("#fileInput").value = DEMO.findByFile.file;
  checkHealth().then(loadStats);
}
document.addEventListener("DOMContentLoaded", init);
