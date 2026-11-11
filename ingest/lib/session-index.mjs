// 跨 agent 会话 ingest 核心。
// 适配自 v1「多AI协同底座」的会话索引（本仓库内置，覆盖 DeepSeek Harness / WorkBuddy / Codex CLI 三类本地 agent）
// （45 项测试验证、三源只读发现+解析）。v2 保留其只读发现、DSH zstd 帧解码、
// 消息/文件操作解析、凭据掩码与覆盖范围语义，作为语料导出层的稳定基座。
//
// 只读：绝不修改源聊天日志、不覆盖内部记忆、不启动新模型会话。
// 隐私：仅导出 user/assistant 公开消息，私有推理不导出；redact() 先掩码常见凭据。
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import zlib from 'node:zlib';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';

export const SCHEMA_VERSION=1;
const CACHE_VERSION=2;
const MAX_READ=8*1024*1024,MAX_COMPRESSED=64*1024*1024;
function walk(root,suffix,out=[]){try{for(const e of fs.readdirSync(root,{withFileTypes:true})){const p=path.join(root,e.name);if(e.isDirectory())walk(p,suffix,out);else if(e.name.endsWith(suffix)){const st=fs.statSync(p);out.push({file:p,mtime:st.mtimeMs,size:st.size});}}}catch{}return out;}
function readRange(file,start,length){const fd=fs.openSync(file,'r');try{const b=Buffer.alloc(length);return b.subarray(0,fs.readSync(fd,b,0,length,start));}finally{fs.closeSync(fd);}}
function json(value){try{return typeof value==='string'?JSON.parse(value):value;}catch{return null;}}
export function timestamp(value){if(typeof value==='number')return value<1e11?value*1000:value;const ms=Date.parse(value);return Number.isFinite(ms)?ms:null;}
export function redact(text){return String(text||'').replace(/\b(?:sk-|ark-)[A-Za-z0-9_-]{12,}/g,'[凭据已遮盖]').replace(/(Bearer\s+)[A-Za-z0-9._~-]{12,}/gi,'$1[已遮盖]').replace(/((?:api[_-]?key|password|access[_-]?token)\s*[=:]\s*["']?)[^\s"',;]{8,}/gi,'$1[已遮盖]');}
function textOf(value){if(typeof value==='string')return value;if(Array.isArray(value))return value.filter(x=>['text','input_text','output_text'].includes(x?.type)).map(x=>x.text||'').join('\n');return '';}
export function frameEnd(buffer,offset=0){
 if(offset+4>buffer.length)return null;
 const magic=buffer.readUInt32LE(offset);
 if((magic&0xfffffff0)===0x184d2a50){if(offset+8>buffer.length)return null;const end=offset+8+buffer.readUInt32LE(offset+4);return end<=buffer.length?end:null;}
 if(magic!==0xfd2fb528)throw Error(`Invalid zstd frame at ${offset}`);
 if(offset+5>buffer.length)return null;
 const descriptor=buffer[offset+4],single=(descriptor>>5)&1,flag=descriptor>>6;
 if(descriptor&8)throw Error('Reserved zstd frame flag');
 let at=offset+5+(single?0:1)+[0,1,2,4][descriptor&3]+(flag===0?(single?1:0):[0,2,4,8][flag]);
 if(at>buffer.length)return null;
 while(true){if(at+3>buffer.length)return null;const h=buffer.readUIntLE(at,3);at+=3;const type=(h>>1)&3;if(type===3)throw Error('Reserved zstd block');at+=type===1?1:h>>>3;if(at>buffer.length)return null;if(h&1)break;}
 at+=(descriptor&4)?4:0;return at<=buffer.length?at:null;}
export function decodeFrames(buffer,maxBytes=MAX_READ){
 const frames=[];let at=0,trailing=false;
 while(at<buffer.length){const end=frameEnd(buffer,at);if(end===null){trailing=true;break;}frames.push([at,end]);at=end;}
 const parts=[];let size=0,omitted=false;
 for(let i=frames.length-1;i>=0;i--){const [start,end]=frames[i];if((buffer.readUInt32LE(start)&0xfffffff0)===0x184d2a50)continue;
   let chunk;try{chunk=zlib.zstdDecompressSync(buffer.subarray(start,end),{maxOutputLength:maxBytes});}catch(e){return {text:'',complete:false,error:e.message,frames:frames.length};}
   if(size+chunk.length>maxBytes){omitted=true;break;}parts.unshift(chunk);size+=chunk.length;
 }
 let text=Buffer.concat(parts).toString('utf8');if(omitted){const nl=text.indexOf('\n');text=nl>=0?text.slice(nl+1):'';}
 return {text,complete:!trailing&&!omitted,trailing,omittedPrefix:omitted,frames:frames.length,bytes:size};
}
function readLog(session){
 const size=fs.statSync(session.file).size;
 if(session.owner==='dsh'){
   if(size>MAX_COMPRESSED)return {lines:[],coverage:{complete:false,error:'Compressed log exceeds 64 MiB read budget',sourceBytes:size}};
   const decoded=decodeFrames(readRange(session.file,0,size));return {lines:decoded.text.split('\n'),coverage:{...decoded,text:undefined,sourceBytes:size}};
 }
 const start=Math.max(0,size-MAX_READ);let text=readRange(session.file,start,size-start).toString('utf8');if(start){const nl=text.indexOf('\n');text=nl<0?'':text.slice(nl+1);}
 return {lines:text.split('\n'),coverage:{complete:start===0,omittedPrefix:start>0,sourceBytes:size,readStart:start,readEnd:size}};
}
function header(file){try{const size=Math.min(fs.statSync(file).size,65536),b=readRange(file,0,size),end=frameEnd(b);if(!end)return {};return json(zlib.zstdDecompressSync(b.subarray(0,end)).toString('utf8').split('\n')[0])||{};}catch{return {};}}
function query(file,sql){let db;try{db=new DatabaseSync(file,{readOnly:true});return db.prepare(sql).all();}finally{db?.close();}}
export function listSessions(base=os.homedir()){
 const sessions=[],warnings=[];
 const dsh=walk(path.join(base,'.dsh/sessions'),'.jsonl.zstd');
 for(const f of dsh){const id=path.basename(path.dirname(f.file)),meta=json(fs.existsSync(path.join(base,'.dsh/storages/session_projcache/sessions',id+'.json'))?fs.readFileSync(path.join(base,'.dsh/storages/session_projcache/sessions',id+'.json'),'utf8'):'{}')||{},h=header(f.file),rows=meta.record?.rows||{};
   if((h.delegationDepth||0)>0)continue;
   sessions.push({...f,owner:'dsh',id,key:`dsh:${id}`,title:rows.title?.val||id,cwd:h.cwd||meta.record?.identity?.cwd||'',model:rows.modelSelection?.val?.lastUsed?.model||'',depth:h.delegationDepth??null});
 }
 const wbFiles=walk(path.join(base,'.workbuddy/projects'),'.jsonl'),wbMap=new Map(wbFiles.map(f=>[path.basename(f.file,'.jsonl'),f]));
 try{for(const r of query(path.join(base,'.workbuddy/workbuddy.db'),"SELECT id,title,custom_title,cwd,model,updated_at FROM sessions WHERE deleted_at IS NULL AND COALESCE(is_background_automation,0)=0")){const f=wbMap.get(r.id);if(f)sessions.push({...f,owner:'workbuddy',id:r.id,key:`workbuddy:${r.id}`,title:r.custom_title||r.title||r.id,cwd:r.cwd||'',model:r.model||'',updatedAt:timestamp(r.updated_at)});}}
 catch(e){warnings.push({owner:'workbuddy',error:e.message,scope:'Database unavailable; unfiltered log fallback'});for(const f of wbFiles)sessions.push({...f,owner:'workbuddy',id:path.basename(f.file,'.jsonl'),key:`workbuddy:${path.basename(f.file,'.jsonl')}`,title:path.basename(f.file),cwd:'',model:'',filterUncertain:true});}
 try{for(const r of query(path.join(base,'.codex/state_5.sqlite'),"SELECT id,rollout_path,title,cwd,model,updated_at_ms,updated_at,source FROM threads WHERE archived=0")){if(!r.rollout_path||!fs.existsSync(r.rollout_path))continue;const st=fs.statSync(r.rollout_path);let sub=false;try{sub=JSON.stringify(json(r.source)||{}).includes('subagent');}catch{}if(sub)continue;sessions.push({owner:'codex',id:r.id,key:`codex:${r.id}`,title:r.title||r.id,cwd:r.cwd||'',model:r.model||'',file:r.rollout_path,mtime:st.mtimeMs,size:st.size,updatedAt:r.updated_at_ms||timestamp(r.updated_at)});}}
 catch(e){warnings.push({owner:'codex',error:e.message,scope:'Database unavailable; log fallback'});for(const f of walk(path.join(base,'.codex/sessions'),'.jsonl')){const id=path.basename(f.file,'.jsonl').match(/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i)?.[0]||path.basename(f.file,'.jsonl');sessions.push({...f,owner:'codex',id,key:`codex:${id}`,title:id,cwd:'',model:'',filterUncertain:true});}}
 return {sessions:sessions.sort((a,b)=>b.mtime-a.mtime),warnings};
}
function resolveAbs(value,cwd=''){if(!value||typeof value!=='string')return null;const raw=String(value).replace(/\\/g,'/');if(path.win32.isAbsolute(raw))return path.win32.normalize(raw);if(!cwd)return null;return path.win32.normalize(path.win32.join(String(cwd).replace(/\\/g,'/'),raw));}
export function normalizePath(value,cwd=''){if(!value||typeof value!=='string')return null;const clean=s=>s.replace(/\\/g,'/').replace(/^\/\/[?.]\/UNC\//i,'//').replace(/^\/\/[?.]\//,'');const raw=clean(value);return (path.win32.isAbsolute(raw)?path.win32.normalize(raw):path.win32.join(clean(cwd||'.'),raw)).replace(/\\/g,'/').toLowerCase();}
function literal(value){if(value[0]==='"')return json(value);if(value[0]==='`'&&value.includes('${'))return null;const s=value.slice(1,-1);return s.replace(/\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[\s\S])/g,(_,v)=>v[0]==='u'||v[0]==='x'?String.fromCharCode(parseInt(v.slice(1),16)):({n:'\n',r:'\r',t:'\t'}[v]??v));}
export function patchPaths(input,cwd){let patches=[];if(input.startsWith('*** Begin Patch'))patches.push(input);const re=/\bapply_patch\s*\(\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)/g;let m;while((m=re.exec(input))){const text=literal(m[1]);if(text)patches.push(text);}const out=[];for(const p of patches){for(const match of p.matchAll(/^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$/gm)){const file=match[2].trim();out.push({path:file,absPath:resolveAbs(file,cwd),normalizedPath:normalizePath(file,cwd),action:({'Add File':'write','Update File':'edit','Delete File':'delete','Move to':'move'})[match[1]],evidence:'patch-header'});}}return out;}
function operationPaths(name,args,input,cwd){if(/(?:^|\.)apply_patch$/.test(name)||name==='exec')return patchPaths(input||'',cwd);if(!['write','edit','multiedit','create'].includes(name.toLowerCase()))return [];const p=args?.file_path||args?.path||args?.filePath;return p?[{path:p,absPath:resolveAbs(p,cwd),normalizedPath:normalizePath(p,cwd),action:name.toLowerCase(),evidence:'structured-tool-argument'}]:[];}
function resultStatus(value){const parsed=json(value),obj=parsed&&typeof parsed==='object'?parsed:value;if(obj&&typeof obj==='object'){if(obj.isError===true||obj.success===false||obj.status==='failed'||obj.error)return 'failed';if(obj.success===true)return 'succeeded';}const s=typeof value==='string'?value:JSON.stringify(value||'');if(/(?:exit[_ ]code["'\s:]*[1-9]|"isError"\s*:\s*true|Traceback \(most recent call last\))/i.test(s))return 'failed';if(/Success\. Updated the following files:|Successfully (?:wrote|updated|edited|created)|patch applied successfully/i.test(s))return 'succeeded';return 'unknown';}
export function parseRecords(records,session){
 const messages=[],changes=[],calls=new Map();let turn=null,turnSeq=0,cwd=session.cwd||'',state='unknown',parseUnknown=0;
 const addMessage=(role,text,id,at,offset)=>{if(!['user','assistant'].includes(role)||!text)return;if(role==='user'&&!turn)turn=`observed-${++turnSeq}`;messages.push({role,text:redact(text).slice(0,6000),truncated:text.length>6000,id,at,turn,source:{file:session.file,line:offset}});if(messages.length>100)messages.shift();};
 for(const [index,r] of records.entries()){
  const p=r.payload||r.data||{},at=timestamp(r.timestamp??r.time??r.at),offset=r.__line??index+1;
  if(session.owner==='dsh'&&p.turn!==undefined)turn=String(p.turn);
  if(r.type==='turn/start'){state='running';continue;}if(r.type==='turn/end'){state='finished';continue;}
  if(r.type==='session_meta'){cwd=p.cwd||cwd;continue;}
  if(r.type==='turn_context'){turn=p.turn_id||turn;cwd=p.cwd||cwd;continue;}
  if(r.type==='event_msg'){if(p.type==='task_started'){turn=p.turn_id||p.root_turn_id;state='running';}else if(p.type==='task_complete')state='finished';else if(['turn_aborted','task_interrupted'].includes(p.type))state='interrupted';continue;}
  if((r.type==='response_item'&&p.type==='message')||r.type==='message'){
   const role=r.role||p.role,text=textOf(r.content??p.content??p.text);if(role==='user'&&(session.owner!=='codex'||!turn))turn=`observed-${++turnSeq}`;addMessage(role,text,r.id||p.id,at,offset);continue;
  }
  if(['user/message','assistant/message'].includes(r.type)){const role=r.type.split('/')[0];if(role==='user'&&!turn)turn=`observed-${++turnSeq}`;addMessage(role,textOf(p.message?.content||p.text||p.content||r.content),r.id||p.id||p.message?.id,at,offset);continue;}
  const call=(r.type==='response_item'&&['custom_tool_call','function_call'].includes(p.type))||r.type==='function_call'||r.type==='tool/call';
  if(call){const name=r.name||p.name||'',args=json(r.arguments??p.arguments)||{},input=p.input||'',id=r.callId||r.call_id||p.callId||p.call_id||p.id||r.id||`${offset}`;const found=operationPaths(name,args,input,cwd);const own=[];for(const f of found){const op={...f,callId:id,at,turn,owner:session.owner,sessionId:session.id,status:'unknown',resultObserved:false,source:{file:session.file,line:offset}};if(typeof args.old_string==='string'||typeof args.new_string==='string'){op.requestedDiff={before:redact(args.old_string||'').slice(0,4000),after:redact(args.new_string||'').slice(0,4000),truncated:(args.old_string?.length||0)>4000||(args.new_string?.length||0)>4000};}changes.push(op);own.push(op);}calls.set(id,own);continue;}
  if((r.type==='response_item'&&['custom_tool_call_output','function_call_output'].includes(p.type))||['function_call_result','tool/result','tool/return'].includes(r.type)){
   const id=r.callId||r.call_id||p.callId||p.call_id||p.message?.toolCallId||p.id||r.id;const list=calls.get(id)||[];const value=r.output??p.output??p.result??p.message??p;let status=resultStatus(value);if(p.message?.isError===false)status='succeeded';if(status==='unknown'&&value?.content)status=resultStatus(textOf(value.content));for(const op of list){op.resultObserved=true;op.status=r.status==='failed'?'failed':status;op.resultSource={file:session.file,line:offset};}continue;
  }
  parseUnknown++;
 }
 return {messages,changes,cwd,state,unhandledRecords:parseUnknown};
}
export function readSession(session,{cacheDir=path.join(os.homedir(),'.tools/ai-agent-monitor/data/session-cache'),refresh=false}={}){
 const st=fs.statSync(session.file),cacheKey=createHash('sha256').update(session.key).digest('hex'),cache=path.join(cacheDir,cacheKey+'.json');
 try{const old=json(fs.readFileSync(cache,'utf8'));if(!refresh&&old?.version===CACHE_VERSION&&old.file===session.file&&old.size===st.size&&old.mtime===st.mtimeMs)return {...old.result,cacheHit:true};}catch{}
 const {lines,coverage}=readLog(session),records=[];let malformed=0;
 for(let i=0;i<lines.length;i++){if(!lines[i].trim())continue;const r=json(lines[i]);if(r&&typeof r==='object'){r.__line=coverage.omittedPrefix?`tail+${i+1}`:i+1;records.push(r);}else malformed++;}
 const parsed=parseRecords(records,session),result={session:{...session,cwd:parsed.cwd},messages:parsed.messages,changes:parsed.changes,state:parsed.state,coverage:{...coverage,complete:coverage.complete&&malformed===0,malformed,readAt:new Date().toISOString(),messageLimit:100},cacheHit:false};
 try{fs.mkdirSync(cacheDir,{recursive:true});const temp=cache+'.'+process.pid+'.tmp';fs.writeFileSync(temp,JSON.stringify({version:CACHE_VERSION,file:session.file,size:st.size,mtime:st.mtimeMs,result}));fs.renameSync(temp,cache);}catch(e){result.coverage.cacheError=e.message;}
 return result;
}
export function resolveSession(sessions,key){const exact=sessions.filter(s=>s.key===key||s.id===key);if(exact.length===1)return exact[0];const matches=sessions.filter(s=>s.key.includes(key));if(matches.length!==1)throw Error(matches.length?'会话ID不唯一，请使用来源:完整ID':'未找到会话');return matches[0];}
