#!/usr/bin/env node
// handoff-bridge · MCP 轻壳（跨 agent 交接桥）
// ---------------------------------------------------------------------------
// 手写 JSON-RPC 2.0 over stdio（MCP stdio transport，换行分隔），不依赖任何 SDK。
// 本进程【不加载模型、常驻仅几 MB】；每次 tools/call 才 spawn WSL 重 worker
// (handoff_brief.py；0.7.0 light 后端实测冷启动全链峰值 ~0.5GB、~12s，跑完即退)。worker 经 AI 工作台 registry.mjs
// 登记【Windows 侧 wsl.exe 的 PID】——这样进程页能看到、alive() 能判活；退出即注销。
// 红线：stdout 只准写 JSON-RPC；一切日志走 stderr。密钥不经手。共享机器不 pkill。
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

import { fileURLToPath } from 'node:url';
const TOOL_DIR = process.env.HB_TOOL_DIR || path.dirname(fileURLToPath(import.meta.url));
const CFG = {
  backendWsl: process.env.HB_BACKEND_WSL || '$HOME/handoff-bridge/backend',
  pyWsl: process.env.HB_PY_WSL || '$HOME/handoff-bridge-venv/bin/python',
  registry: process.env.HB_REGISTRY || '',  // 可选：指向 ai-agent-monitor 的 registry.mjs；未配置时跳过进程登记
  agent: process.env.HB_AGENT || 'agent',
  wsl: process.env.HB_WSL || 'wsl.exe',
  timeoutMs: Number(process.env.HB_TIMEOUT_MS || 300000),
  tmpDir: process.env.HB_TMP || path.join(TOOL_DIR, 'tmp'),
  // ① 懒补：出简报前先按水位线补录 events.jsonl 新事件（同进程、模型只加载一次）。
  // 无新行时 early-return 不碰模型。默认开；置 HB_CATCHUP=0 可关（评测复现旧库时用）。
  catchup: (process.env.HB_CATCHUP || '1') !== '0',
  // 调用台账：每次 tools/call 追加一行 JSONL（谁调的/参数/耗时/成败/简报落盘路径）。
  // 痕落【桥】上而非调用方，跨 agent 统一可查——这是「确实调用了」的独立证据。
  ledger: process.env.HB_LEDGER || path.join(TOOL_DIR, 'call-ledger.jsonl'),
  briefDir: process.env.HB_BRIEF_DIR || path.join(TOOL_DIR, 'tmp', 'briefs'),
};
const SERVER_INFO = { name: 'handoff-bridge', version: '0.4.3' };
const DEFAULT_PROTOCOL = '2024-11-05';

const log = (...a) => process.stderr.write('[handoff-bridge] ' + a.join(' ') + '\n');

// bash 单引号安全转义（用户数据一律走这个，杜绝注入）
const q = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";

// Windows 路径 -> WSL /mnt/c 路径
function winToWsl(p) {
  return String(p).replace(/\\/g, '/').replace(/^([A-Za-z]):/, (_, d) => `/mnt/${d.toLowerCase()}`);
}

const TOOL = {
  name: 'get_handoff_brief',
  description:
    '跨 agent 交接桥。当你要接手 / 继续另一个 agent 做过的项目时调用它，拉取一份《交接简报》：' +
    '机械汇总交出方在该项目上的最新结果、未解决线头、关键路径/产物、相关会话、时间线，' +
    '每条都带 events.jsonl 行号出处，可回溯核对。它只做「捞全 + 去重 + 标出处」，不做 AI 摘要——' +
    '综合与判断留给你。参数里 topic 给空格分隔的项目关键词（越具体越好），src 是交出方、dst 是接手方（通常是你自己）。',
  inputSchema: {
    type: 'object',
    additionalProperties: false,
    properties: {
      src: { type: 'string', description: '交出方 agent：chatgpt / deepseek / workbuddy / zcode / qoder / trae 之一' },
      dst: { type: 'string', description: '接手方 agent（通常填你自己）' },
      topic: { type: 'string', description: '项目主题关键词，空格分隔，例如「窗口提醒 任务跟踪 现场恢复」' },
      intent: { type: 'string', description: '一句话接手意图，例如「zcode 接手 chatgpt 的窗口提醒项目」' },
    },
    required: ['src', 'dst', 'topic', 'intent'],
  },
};

// ---------- registry（Windows 侧 node registry.mjs）----------
function regRegister({ owner, name, pid, desc }) {
  try {
    const r = spawnSync('node', [CFG.registry, 'register', '--owner', owner, '--name', name, '--pid', String(pid), '--desc', desc], { encoding: 'utf8' });
    const id = (r.stdout || '').trim().split(/\s+/)[0];
    return id && id !== '' ? id : null;
  } catch (e) { log('register 失败（忽略，不影响出简报）:', e.message); return null; }
}
function regUnregister(id) {
  if (!id) return;
  try { spawnSync('node', [CFG.registry, 'unregister', '--id', id], { encoding: 'utf8' }); } catch {}
}

// ---------- 跑一次 worker ----------
function runBrief({ src, dst, topic, intent }) {
  return new Promise((resolve) => {
    fs.mkdirSync(CFG.tmpDir, { recursive: true });
    const token = `${Date.now()}-${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
    const outWsl = `/tmp/hb-mcp-${token}.md`;
    const errWsl = `/tmp/hb-mcp-${token}.err`;
    const scriptWin = path.join(CFG.tmpDir, `run-${token}.sh`);
    const scriptWsl = winToWsl(scriptWin);

    // 用户数据一律 q() 单引号转义；路径来自受信任的环境配置，用双引号让 bash 展开 $HOME。
    const catchupFlag = CFG.catchup ? ' --catchup' : '';
    const script = [
      '#!/usr/bin/env bash',
      `cd "${CFG.backendWsl}" || { echo "cd backend failed" >&2; exit 3; }`,
      `"${CFG.pyWsl}" handoff_brief.py --intent ${q(intent)} --topic ${q(topic)} --src ${q(src)} --dst ${q(dst)}${catchupFlag} --out "${outWsl}" >/dev/null 2>"${errWsl}"`,
      'rc=$?',
      'if [ $rc -ne 0 ]; then echo "__HB_WORKER_FAILED__ rc=$rc" >&2; tail -c 3000 "${errWsl}" >&2; exit $rc; fi',
      `cat "${outWsl}"`,
      `rm -f "${outWsl}" "${errWsl}"`,
      '',
    ].join('\n'); // LF，避免 CRLF 破坏 bash

    fs.writeFileSync(scriptWin, script, { encoding: 'utf8' });
    const startedAt = Date.now();

    const child = spawn(CFG.wsl, ['bash', scriptWsl], { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    const regId = regRegister({ owner: CFG.agent, name: 'Handoff-Bridge-Worker', pid: child.pid, desc: `交接简报 ${src}->${dst}｜topic: ${String(topic).slice(0, 40)}` });

    let out = '', err = '', done = false;
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; });

    const timer = setTimeout(() => {
      if (done) return;
      log(`worker 超时 ${CFG.timeoutMs}ms，按 PID 终止（不 pkill）: pid=${child.pid}`);
      try { spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { encoding: 'utf8' }); } catch {}
      child.kill('SIGKILL');
    }, CFG.timeoutMs);

    const finish = (payload) => {
      if (done) return; done = true;
      clearTimeout(timer);
      regUnregister(regId);
      try { fs.unlinkSync(scriptWin); } catch {}
      try {
        let briefPath = null;
        if (payload.ok) {
          fs.mkdirSync(CFG.briefDir, { recursive: true });
          briefPath = path.join(CFG.briefDir, `brief-${token}.md`);
          fs.writeFileSync(briefPath, payload.text, { encoding: 'utf8' });
        }
        const rec = {
          ts: new Date().toISOString(),
          host: CFG.agent, src, dst,
          topic: String(topic), intent: String(intent),
          ok: !!payload.ok,
          duration_ms: Date.now() - startedAt,
          bytes: Buffer.byteLength(payload.text || '', 'utf8'),
          worker_pid: child.pid, reg_id: regId,
          brief_path: briefPath,
          error: payload.ok ? null : String(payload.text || '').slice(0, 300),
        };
        fs.appendFileSync(CFG.ledger, JSON.stringify(rec) + '\n', { encoding: 'utf8' });
        log(`ledger +1 (${src}->${dst}, ok=${rec.ok}, ${rec.duration_ms}ms)`);
      } catch (e) { log('ledger 写入失败（不影响出简报）:', e.message); }
      resolve(payload);
    };

    child.on('error', (e) => finish({ ok: false, text: `worker 启动失败: ${e.message}` }));
    child.on('exit', (code) => {
      if (code === 0 && out.trim().length > 0) finish({ ok: true, text: out });
      else finish({ ok: false, text: `worker 退出码 ${code}\n${(err || out).slice(-3000)}` });
    });
  });
}

// ---------- JSON-RPC ----------
function send(msg) { process.stdout.write(JSON.stringify(msg) + '\n'); }
function reply(id, result) { send({ jsonrpc: '2.0', id, result }); }
function replyErr(id, code, message) { send({ jsonrpc: '2.0', id, error: { code, message } }); }

async function handle(m) {
  const { id, method, params } = m;
  const isReq = id !== undefined && id !== null;
  switch (method) {
    case 'initialize': {
      const pv = (params && typeof params.protocolVersion === 'string') ? params.protocolVersion : DEFAULT_PROTOCOL;
      if (isReq) reply(id, { protocolVersion: pv, capabilities: { tools: { listChanged: false } }, serverInfo: SERVER_INFO });
      return;
    }
    case 'notifications/initialized':
    case 'initialized':
      return; // 通知，无响应
    case 'ping':
      if (isReq) reply(id, {});
      return;
    case 'tools/list':
      if (isReq) reply(id, { tools: [TOOL] });
      return;
    case 'resources/list':
      if (isReq) reply(id, { resources: [] });
      return;
    case 'prompts/list':
      if (isReq) reply(id, { prompts: [] });
      return;
    case 'tools/call': {
      if (!isReq) return;
      const name = params && params.name;
      const args = (params && params.arguments) || {};
      if (name !== TOOL.name) return replyErr(id, -32602, `unknown tool: ${name}`);
      const { src, dst, topic, intent } = args;
      const missing = ['src', 'dst', 'topic', 'intent'].filter((k) => !args[k] || !String(args[k]).trim());
      if (missing.length) return reply(id, { content: [{ type: 'text', text: `缺少参数: ${missing.join(', ')}` }], isError: true });
      log(`tools/call ${src}->${dst} topic="${String(topic).slice(0, 50)}" (host=${CFG.agent})`);
      const r = await runBrief({ src: String(src), dst: String(dst), topic: String(topic), intent: String(intent) });
      reply(id, { content: [{ type: 'text', text: r.text }], isError: !r.ok });
      return;
    }
    default:
      if (isReq) replyErr(id, -32601, `method not found: ${method}`);
  }
}

// ---------- stdio 循环（换行分隔 JSON）----------
let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let m;
    try { m = JSON.parse(line); } catch (e) { log('bad json line:', e.message); continue; }
    Promise.resolve(handle(m)).catch((e) => {
      log('handler error:', e && e.stack || e);
      if (m && m.id !== undefined && m.id !== null) replyErr(m.id, -32603, String(e && e.message || e));
    });
  }
});
process.stdin.on('end', () => { log('stdin closed, 退出（壳随 agent 会话结束而退，不残留）'); process.exit(0); });
log(`ready (host=${CFG.agent}, registry=${CFG.registry})`);
