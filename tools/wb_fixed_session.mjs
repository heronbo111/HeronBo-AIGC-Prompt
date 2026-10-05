// wb_fixed_session.mjs —— 工作台「固定会话」管理（纯新增、自包含，不依赖 doubao_cdp.mjs）
//
// 背景：豆包工作单实例 + 自定义协议，CDP 新开 chat 默认「恢复当前活动会话」（镜像），
//   纯后台无法可靠开出干净独立会话；且「新工作任务」的空会话在发出第一条消息前 URL 无 sid。
//   本模块把一个「工作台专用固定会话」作为部署产物：部署/首次启动时建立（自动；环境不允许
//   则引导用户点一次「新工作任务」后 adopt，可绑定空白窗口），此后 ask / 视频全部锁定它；
//   每次校验存活、失效自动重建；发送前 verify 护栏，杜绝消息误发到用户当前对话。
//
// 锚点：<样本库根>/.heronbo/workbench.json ；引导暂存：同目录 pending.json
//
// 用法：
//   node wb_fixed_session.mjs init    --root "<样本库根>" [--port 9333]
//   node wb_fixed_session.mjs adopt   --root "..."   # 点「新工作任务」后采纳（含空白窗口）
//   node wb_fixed_session.mjs attach  --root "..."   # 锁定输出 {ok,sid,targetId,ws}
//   node wb_fixed_session.mjs verify  --root "..." [--sid s] [--target id]
//   node wb_fixed_session.mjs reset   --root "..." [--then-init]
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function arg(name, def = '') {
  const a = process.argv;
  const i = a.findIndex((x) => x === '--' + name);
  return i >= 0 && i + 1 < a.length ? a[i + 1] : def;
}
const CMD = process.argv[2] && !process.argv[2].startsWith('--') ? process.argv[2] : 'attach';
const ROOT = arg('root');
const WANT_PORT = Number(arg('port', '0'));
const emit = (o) => process.stdout.write(JSON.stringify(o, null, 1) + '\n');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const HERON_DIR = ROOT ? path.join(ROOT, '.heronbo') : '';
const ANCHOR = HERON_DIR ? path.join(HERON_DIR, 'workbench.json') : '';
const PENDING = HERON_DIR ? path.join(HERON_DIR, 'pending.json') : '';
const CDP_STATE = path.join(os.tmpdir(), 'heronbo_doubao_cdp.json');
const SCHEMA = 'heronbo.fixed-session/1';

function loadJson(p) { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; } }
function saveJson(p, d) { fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(d, null, 1), 'utf8'); }
const loadAnchor = () => loadJson(ANCHOR);
const saveAnchor = (d) => saveJson(ANCHOR, d);
const dropFile = (p) => { try { fs.unlinkSync(p); } catch {} };

class Cdp {
  constructor(wsUrl) { this.wsUrl = wsUrl; this.n = 0; this.pend = new Map(); }
  connect() {
    return new Promise((res, rej) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.onopen = () => res();
      this.ws.onmessage = (e) => {
        const d = JSON.parse(e.data);
        if (d.id && this.pend.has(d.id)) {
          const { r, j } = this.pend.get(d.id); this.pend.delete(d.id);
          d.error ? j(new Error(d.error.message)) : r(d.result);
        }
      };
      this.ws.onerror = () => rej(new Error('ws error'));
    });
  }
  send(method, params = {}, timeout = 15000) {
    const id = ++this.n;
    return new Promise((r, j) => {
      const to = setTimeout(() => { this.pend.delete(id); j(new Error('超时 ' + method)); }, timeout);
      this.pend.set(id, { r: (x) => { clearTimeout(to); r(x); }, j: (x) => { clearTimeout(to); j(x); } });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

const sidOf = (u) => { const m = String(u || '').match(/\/chat\/([^/?#]+)/); return m ? m[1] : ''; };
const isBlankChatUrl = (u) => /(doubaowork-chat|work-chat)\/chat\/?$/.test(String(u || ''));
function candidatePorts() {
  const out = [];
  if (WANT_PORT) out.push(WANT_PORT);
  const st = loadJson(CDP_STATE);
  if (st && st.port) out.push(Number(st.port));
  for (let i = 0; i < 10; i++) out.push(9333 + i);
  return [...new Set(out)];
}
async function livePort() {
  for (const p of candidatePorts()) {
    try { const v = await (await fetch(`http://127.0.0.1:${p}/json/version`)).json();
      if (v && v.webSocketDebuggerUrl) return p; } catch {}
  }
  return 0;
}
async function listPages() {
  const p = await livePort();
  if (!p) return [];
  const l = await (await fetch(`http://127.0.0.1:${p}/json/list`)).json();
  return l.filter((x) => x.type === 'page');
}
async function browserCdp() {
  const p = await livePort();
  if (!p) return null;
  const v = await (await fetch(`http://127.0.0.1:${p}/json/version`)).json();
  const c = new Cdp(v.webSocketDebuggerUrl); await c.connect(); return c;
}
async function closeTarget(id) {
  const b = await browserCdp();
  if (!b) return;
  try { await b.send('Target.closeTarget', { targetId: id }); } catch {} b.close();
}
async function createWindow() {
  const b = await browserCdp();
  if (!b) throw new Error('连不上 CDP（豆包工作没开调试端口）');
  try { const r = await b.send('Target.createTarget',
    { url: 'doubaowork://doubaowork-chat/chat', newWindow: true });
    return r.targetId; } finally { b.close(); }
}
async function waitSid(id, sec = 20) {
  for (let i = 0; i < sec; i++) {
    await sleep(1000);
    const t = (await listPages()).find((x) => (x.targetId || x.id) === id);
    if (t) { const s = sidOf(t.url); if (s) return s; }
  }
  return '';
}
async function existingSids() {
  return new Set((await listPages()).map((x) => sidOf(x.url)).filter(Boolean));
}
// 只读检查一个页面：是否有输入框、消息条数、正文长度（判断是否干净空会话）
const INSPECT_EXPR = `(()=>{
  const hasComposer=!!document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"],[data-testid="chat_input_input"],[data-testid$="input_textarea"]');
  const msgCount=document.querySelectorAll('[data-testid*="message"],article,[class*="message_item"]').length;
  return {hasComposer,msgCount,bodyLen:(document.body.innerText||'').length};
})()`;
async function inspectPage(t) {
  if (!t.webSocketDebuggerUrl) return null;
  const c = new Cdp(t.webSocketDebuggerUrl);
  try { await c.connect(); await c.send('Runtime.enable');
    const r = await c.send('Runtime.evaluate',
      { expression: INSPECT_EXPR, returnByValue: true, awaitPromise: true });
    return r.result ? r.result.value : null;
  } catch { return null; } finally { c.close(); }
}
const nowIso = () => new Date().toISOString();

// 自动建立：成功得独立会话；环境不允许 → 写 pending、need_manual
async function tryAutoCreate() {
  const before = await existingSids();
  let nid = '';
  try { nid = await createWindow(); }
  catch (e) { return { ok: false, need_manual: true, why: e.message }; }
  const sid = await waitSid(nid, 20);
  if (sid && !before.has(sid)) {
    const t = (await listPages()).find((x) => sidOf(x.url) === sid);
    const anchor = { schema: SCHEMA, sid, targetId: t ? (t.targetId || t.id) : nid,
      port: await livePort(), createdAt: nowIso(), notes: '工作台固定会话' };
    saveAnchor(anchor); dropFile(PENDING);
    return { ok: true, ...anchor };
  }
  await closeTarget(nid);
  saveJson(PENDING, { beforeSids: [...before], at: nowIso() });
  return { ok: false, need_manual: true,
    hint: '请点左上角「新工作任务」开一个干净空对话，然后运行 adopt', gotSid: sid || '' };
}

async function cmdInit() {
  if (!ROOT) return emit({ ok: false, error: '需要 --root' });
  const a = loadAnchor();
  if (a && (a.sid || a.targetId)) {
    const alive = (await listPages()).some((x) =>
      (a.sid && sidOf(x.url) === a.sid) || (a.targetId && (x.targetId || x.id) === a.targetId));
    if (alive) return emit({ ok: true, reused: true, ...a });
  }
  emit(await tryAutoCreate());
}

async function cmdAdopt() {
  if (!ROOT) return emit({ ok: false, error: '需要 --root' });
  const pend = loadJson(PENDING);
  const before = new Set(pend && Array.isArray(pend.beforeSids) ? pend.beforeSids : []);
  const pages = await listPages();

  // 1) 新出现、已生成 sid 的会话
  const fresh = pages.filter((x) => { const s = sidOf(x.url); return s && !before.has(s); });
  if (fresh.length) {
    const t = fresh[fresh.length - 1]; const sid = sidOf(t.url);
    const anchor = { schema: SCHEMA, sid, targetId: t.targetId || t.id,
      port: await livePort(), createdAt: nowIso(), notes: '工作台固定会话（手动采纳）' };
    saveAnchor(anchor); dropFile(PENDING);
    return emit({ ok: true, adopted: true, ...anchor });
  }
  // 2) 空白干净会话窗口（无 sid、有输入框、无历史消息）
  for (const t of pages.filter((x) => !sidOf(x.url) && isBlankChatUrl(x.url))) {
    const info = await inspectPage(t);
    if (info && info.hasComposer && info.msgCount === 0) {
      const anchor = { schema: SCHEMA, sid: '', pendingSid: true, targetId: t.targetId || t.id,
        port: await livePort(), createdAt: nowIso(), notes: '工作台固定会话（空白窗采纳）' };
      saveAnchor(anchor); dropFile(PENDING);
      return emit({ ok: true, adopted: true, blank: true, ...anchor });
    }
  }
  emit({ ok: false, error: '没发现干净的新会话：请点「新工作任务」开一个空白空对话（能看到输入框）' });
}

async function cmdAttach() {
  if (!ROOT) return emit({ ok: false, error: '需要 --root' });
  const a = loadAnchor();
  const pages = await listPages();
  if (a) {
    let t = a.sid ? pages.find((x) => sidOf(x.url) === a.sid) : null;
    if (!t && a.targetId) t = pages.find((x) => (x.targetId || x.id) === a.targetId);
    if (t) {
      const s = sidOf(t.url);
      if (a.pendingSid && s) { a.sid = s; delete a.pendingSid; saveAnchor(a); }
      return emit({ ok: true, sid: s || a.sid || '', targetId: t.targetId || t.id,
        ws: t.webSocketDebuggerUrl });
    }
  }
  const r = await tryAutoCreate();
  emit(r.ok ? { ok: true, rebuilt: true, sid: r.sid, targetId: r.targetId, ws: '' }
    : { ok: false, ...r });
}

async function cmdVerify() {
  if (!ROOT) return emit({ safe: false, error: '需要 --root' });
  const gotSid = arg('sid'), gotTarget = arg('target');
  const a = loadAnchor(); const pages = await listPages();
  let safe = false;
  if (a) {
    if (a.sid && gotSid) safe = gotSid === a.sid;
    else if (a.targetId && gotTarget) {
      const t = pages.find((x) => (x.targetId || x.id) === gotTarget);
      safe = a.sid ? !!t && sidOf(t.url) === a.sid : !!t;
    }
  }
  emit({ safe, anchorSid: a ? a.sid || '(空白)' : '', gotSid: gotSid || gotTarget || '' });
}

async function cmdReset() {
  if (!ROOT) return emit({ ok: false, error: '需要 --root' });
  const a = loadAnchor();
  if (a) {
    const pages = await listPages();
    const t = (a.sid && pages.find((x) => sidOf(x.url) === a.sid))
      || (a.targetId && pages.find((x) => (x.targetId || x.id) === a.targetId));
    if (t) await closeTarget(t.targetId || t.id);
  }
  dropFile(ANCHOR); dropFile(PENDING);
  emit(process.argv.includes('--then-init') ? await tryAutoCreate() : { ok: true, reset: true });
}

(async () => {
  if (!await livePort()) return emit({ ok: false, error: '连不上豆包 CDP（未开调试端口？）' });
  switch (CMD) {
    case 'init': return await cmdInit();
    case 'adopt': return await cmdAdopt();
    case 'attach': return await cmdAttach();
    case 'verify': return await cmdVerify();
    case 'reset': return await cmdReset();
    case 'inspect': {
      const out = [];
      for (const t of await listPages()) out.push({ id: (t.targetId || t.id).slice(0, 10),
        url: t.url, blankUrl: isBlankChatUrl(t.url), info: await inspectPage(t) });
      return emit(out);
    }
    default: return emit({ ok: false, error: '未知命令 ' + CMD });
  }
})().catch((e) => emit({ ok: false, fatal: e.message }));
