#!/usr/bin/env node
// 语料导出器：把 v1 ingest 解析出的三源会话归一化为 JSONL 语料，供 Python 侧写入 seekdb。
// 只读源日志；文本先经 parseRecords 内 redact()，导出时再 redact() 一次（幂等，纵深防御）。
// 仅导出 user/assistant 公开消息与结构化文件操作；私有推理不导出。
//
// 用法：
//   node export-corpus.mjs [--out 路径] [--owner all|dsh|workbuddy|codex]
//        [--since 30m|2h|1d|ISO] [--max-sessions N] [--max-messages N]
//        [--include-diff] [--cache-dir 路径] [--quiet]
// 默认 --out = ./out/corpus.jsonl（真实聊天语料，已被 .gitignore 排除，不入仓库）。
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {listSessions, readSession, redact, SCHEMA_VERSION} from './lib/session-index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const options = {
  owner: 'all',
  out: path.join(root, 'out', 'corpus.jsonl'),
  cacheDir: path.join(root, 'out', '.cache'),
  maxSessions: 0,
  maxMessages: 0,
  includeDiff: false,
  quiet: false,
};
const values = new Set(['--out', '--owner', '--since', '--max-sessions', '--max-messages', '--cache-dir']);
const flags = new Set(['--include-diff', '--quiet', '--help']);

function parse() {
  const a = process.argv.slice(2);
  for (let i = 0; i < a.length; i++) {
    const k = a[i];
    if (values.has(k)) {
      if (!a[i + 1] || a[i + 1].startsWith('--')) throw Error(`缺少参数值：${k}`);
      const v = a[++i];
      if (k === '--owner') options.owner = v;
      else if (k === '--out') options.out = path.resolve(v);
      else if (k === '--since') options.since = v;
      else if (k === '--max-sessions') options.maxSessions = Number(v);
      else if (k === '--max-messages') options.maxMessages = Number(v);
      else if (k === '--cache-dir') options.cacheDir = path.resolve(v);
    } else if (flags.has(k)) {
      if (k === '--include-diff') options.includeDiff = true;
      else if (k === '--quiet') options.quiet = true;
      else {
        console.log('语料导出器 v2\n--out 路径 --owner all|dsh|workbuddy|codex --since 30m|2h|1d|ISO\n--max-sessions N --max-messages N --include-diff --cache-dir 路径 --quiet');
        process.exit(0);
      }
    } else throw Error(`未知参数：${k}`);
  }
  if (!['all', 'dsh', 'workbuddy', 'codex'].includes(options.owner)) throw Error('owner 应为 all/dsh/workbuddy/codex');
}

function floor() {
  if (!options.since) return 0;
  const m = /^(\d+)([mhd])$/.exec(options.since);
  if (m) return Date.now() - Number(m[1]) * ({m: 60000, h: 3600000, d: 86400000}[m[2]]);
  const d = Date.parse(options.since);
  if (!Number.isFinite(d)) throw Error('since 应为 30m/2h/1d 或 ISO 日期');
  return d;
}

function safeText(t) {
  return redact(String(t || '')).slice(0, 6000);
}

parse();
const cutoff = floor();
const discovered = listSessions();
let sessions = discovered.sessions.filter(s => (options.owner === 'all' || s.owner === options.owner) && s.mtime >= cutoff);
if (options.maxSessions > 0) sessions = sessions.slice(0, options.maxSessions);

fs.mkdirSync(path.dirname(options.out), {recursive: true});
const outFd = fs.openSync(options.out, 'w');
const put = obj => fs.writeSync(outFd, JSON.stringify(obj) + '\n');

const exportedAt = new Date().toISOString();
const stats = {sessions: 0, messages: 0, changes: 0, errors: [], byOwner: {dsh: 0, workbuddy: 0, codex: 0}};

for (const s of sessions) {
  let detail;
  try {
    detail = readSession(s, {cacheDir: options.cacheDir, refresh: false});
  } catch (e) {
    stats.errors.push({key: s.key, error: e.message});
    continue;
  }
  stats.sessions++;
  stats.byOwner[s.owner] = (stats.byOwner[s.owner] || 0) + 1;

  const sess = detail.session || s;
  let msgs = detail.messages || [];
  if (cutoff) msgs = msgs.filter(m => (m.at || 0) >= cutoff);
  if (options.maxMessages > 0) msgs = msgs.slice(-options.maxMessages);
  let changes = detail.changes || [];
  if (cutoff) changes = changes.filter(c => (c.at || 0) >= cutoff);

  put({
    type: 'session',
    owner: sess.owner,
    id: sess.id,
    key: sess.key,
    title: safeText(sess.title || sess.id),
    cwd: sess.cwd || '',
    model: sess.model || '',
    state: detail.state || 'unknown',
    msgCount: msgs.length,
    changeCount: changes.length,
    mtime: sess.mtime || 0,
    size: sess.size || 0,
    logFile: sess.file || '',
    depth: sess.depth ?? null,
    filterUncertain: !!sess.filterUncertain,
    coverageComplete: !!(detail.coverage && detail.coverage.complete),
    exportedAt,
  });

  for (const m of msgs) {
    const text = safeText(m.text);
    if (!text) continue;
    stats.messages++;
    put({
      type: 'message',
      owner: sess.owner,
      sessionId: sess.id,
      sessionKey: sess.key,
      sessionTitle: safeText(sess.title || sess.id),
      cwd: sess.cwd || '',
      role: m.role,
      turn: m.turn || null,
      text,
      truncated: !!m.truncated,
      at: m.at || 0,
      srcFile: (m.source && m.source.file) || sess.file || '',
      srcLine: (m.source && m.source.line) ?? null,
    });
  }

  for (const c of changes) {
    stats.changes++;
    const rec = {
      type: 'change',
      owner: sess.owner,
      sessionId: sess.id,
      sessionKey: sess.key,
      sessionTitle: safeText(sess.title || sess.id),
      cwd: sess.cwd || '',
      action: c.action || '',
      path: c.path || '',
      absPath: c.absPath || c.path || '',
      normalizedPath: c.normalizedPath || '',
      status: c.status || 'unknown',
      resultObserved: !!c.resultObserved,
      turn: c.turn || null,
      at: c.at || 0,
      srcFile: (c.source && c.source.file) || sess.file || '',
      srcLine: (c.source && c.source.line) ?? null,
      hasDiff: !!(c.requestedDiff),
    };
    if (options.includeDiff && c.requestedDiff) {
      rec.diffBefore = safeText(c.requestedDiff.before).slice(0, 4000);
      rec.diffAfter = safeText(c.requestedDiff.after).slice(0, 4000);
    }
    put(rec);
  }

  if (!options.quiet) process.stderr.write(`[${stats.sessions}/${sessions.length}] ${sess.key}  msg=${msgs.length} chg=${changes.length}\n`);
}

fs.closeSync(outFd);

const meta = {
  schemaVersion: SCHEMA_VERSION,
  corpusVersion: 2,
  generatedAt: exportedAt,
  source: 'ai-agent-monitor v1 ingest (vendored)',
  ownerFilter: options.owner,
  since: options.since || null,
  warnings: discovered.warnings || [],
  stats,
  outPath: options.out,
  notice: '语料为只读导出，含 redact 掩码；仅公开消息与结构化文件操作，不含私有推理。真实聊天留本机，不入仓库。',
};
fs.writeFileSync(path.join(path.dirname(options.out), 'corpus-meta.json'), JSON.stringify(meta, null, 2));

if (!options.quiet) {
  console.log(JSON.stringify({ok: true, out: options.out, sessions: stats.sessions, messages: stats.messages, changes: stats.changes, byOwner: stats.byOwner, errors: stats.errors.length, warnings: (discovered.warnings || []).length}, null, 2));
}
