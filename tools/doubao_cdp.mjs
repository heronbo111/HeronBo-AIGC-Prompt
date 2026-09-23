#!/usr/bin/env node
/**
 * 豆包工作（DoubaoWork 桌面端）CDP 桥 —— 让工作台能"叫豆包干活"
 *
 * 为什么是 CDP 而不是 CLI（2026-09-23 实测）：
 *   豆包 / 豆包工作都**没有命令行**（只有 Electron 桌面端：`E:\DoubaoWork\app\DoubaoWork.exe`，
 *   Chrome/147）。但它是 Chromium，**带 `--remote-debugging-port` 起就能被 CDP 接管**——
 *   本机实测 `--remote-debugging-port=9333` 起得来（`/json/version` 正常返回 protocol 1.3）。
 *   同一套路本机早有用例：gpt-image-bridge 用 CDP 驱动 Edge 操作 ChatGPT 网页。
 *
 * 干活链路：
 *   ① 连上 `doubaowork://doubaowork-chat/chat` 这个页面
 *   ② 找到输入框（自动发现 textarea / contenteditable，取"可见 + 靠下 + 最大"那个）
 *   ③ `Input.insertText` 灌入 prompt → 回车（不行再点发送按钮）
 *   ④ **结果从磁盘读**：豆包把每轮写进
 *      `<workspace>\.sessions\<会话id>\agents\<agentid>\system\trajectory.jsonl`
 *      （每行 `{"role":"user|assistant|tool","content":...,"tool_calls":...}`）
 *      → 读新行当进度（工具行形如 `Read "C:\...\文件"`），最后一条 assistant 正文当答复
 *
 * ⚠️ 前提与边界：
 *   · 豆包工作**必须用调试端口启动**才能被接管；已经在跑（没有端口）时要先退出它。
 *     本脚本默认**不替用户关**他正在用的客户端（`restart` 才关，且要 `--yes`）。
 *   · 这套是"代操作企业客户端"，发消息＝真在用它干活；工作台侧应把它当一次正式调用对待。
 *
 * 用法：
 *   node doubao_cdp.mjs status                         # 客户端在不在、CDP 通不通、会话目录在哪
 *   node doubao_cdp.mjs probe                          # 列 CDP 目标 + 输入框候选（一次调参用）
 *   node doubao_cdp.mjs launch                         # 带调试端口起客户端（已在跑则报错）
 *   node doubao_cdp.mjs ask --prompt-file p.txt        # 干活：发这条 prompt，把答复打到 stdout
 *   node doubao_cdp.mjs ask --prompt "你好" --idle 8 --timeout 600
 *   node doubao_cdp.mjs restart --yes                  # 温和关掉再用调试端口起（会打断他当前会话）
 *
 * 选项：
 *   --exe <path>       DoubaoWork.exe（默认自动找 E:\ / F:\ / Program Files）
 *   --port <n>         CDP 端口（默认 9333；环境变量 HERONBO_DOUBAO_PORT）
 *   --sessions <dir>   会话根目录（默认按 LOCALAPPDATA 推；环境变量 HERONBO_DOUBAO_SESSIONS）
 *   --idle <sec>       静默多少秒算"答完了"（默认 8）
 *   --timeout <sec>    总超时（默认 600，与工作台默认一致）
 *   --no-launch        没连上也不许自己起客户端
 *
 * 退出码：0 成功 ｜ 2 参数/环境不对 ｜ 3 客户端在跑但没有调试端口（需先退出）｜ 4 超时 ｜ 5 输入框没找到
 */
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';

const argv = process.argv.slice(2);
const action = argv[0] && !argv[0].startsWith('--') ? argv[0] : 'status';
const opt = (n, d = null) => {
  const i = argv.indexOf('--' + n);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : d;
};
const has = (n) => argv.includes('--' + n);
const log = (...a) => console.error(...a);          // 进度一律走 stderr（工作台把 `· ` 行当动作流）
const step = (...a) => log('· ' + a.map((x) => String(x)).join(' '));

const PORT_ENV = opt('port', process.env.HERONBO_DOUBAO_PORT || '');
let PORT = Number(PORT_ENV || 9333);            // 可能在 ensureAttached 里被改成别的空闲口
const PORT_RANGE = 12;                           // 往后再试这么多个口（别的机器上 9333 可能被占）
const STATE_FILE = path.join(os.tmpdir(), 'heronbo_doubao_cdp.json');
const IDLE = Number(opt('idle', 8));
const TIMEOUT = Number(opt('timeout', 600));
const NO_LAUNCH = has('no-launch');
const PROMPT_FILE = opt('prompt-file');
const PROMPT = opt('prompt');

const EXE_CANDIDATES = [
  process.env.HERONBO_DOUBAO_EXE,
  'E:\\DoubaoWork\\app\\DoubaoWork.exe',
  'F:\\DoubaoWork\\app\\DoubaoWork.exe',
  path.join(process.env['ProgramFiles'] || 'C:\\Program Files', 'DoubaoWork', 'app', 'DoubaoWork.exe'),
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'DoubaoWork', 'app', 'DoubaoWork.exe'),
  path.join(os.homedir(), 'AppData', 'Local', 'DoubaoWork', 'app', 'DoubaoWork.exe'),
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'DoubaoWork', 'DoubaoWork.exe'),
  path.join(os.homedir(), 'AppData', 'Roaming', 'DoubaoWork', 'app', 'DoubaoWork.exe'),
  'C:\Program Files\DoubaoWork\app\DoubaoWork.exe',
].filter(Boolean);
const EXE = opt('exe') || EXE_CANDIDATES.find((p) => { try { return fs.existsSync(p); } catch { return false; } });

function sessionsRoot() {
  const env = opt('sessions') || process.env.HERONBO_DOUBAO_SESSIONS;
  if (env) return env;
  const la = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  const ud = path.join(la, 'DoubaoWork', 'User Data');
  const rel = ['.doubaowork', 'agent_mode', 'workspace', '.sessions'];
  const cands = [];
  // **先把每个 profile 目录都试一遍**（换机/企业版/多开时它不一定叫 Default）
  try {
    for (const d of fs.readdirSync(ud)) cands.push(path.join(ud, d, ...rel));
  } catch {}
  cands.push(path.join(ud, 'Default', ...rel));
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch {} }
  return cands[cands.length - 1];
}
const SESSIONS = sessionsRoot();

// ---------- 基础工具 ----------
function getJson(p) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port: PORT, path: p, timeout: 4000 }, (res) => {
      let b = '';
      res.on('data', (c) => { b += c; });
      res.on('end', () => { try { resolve(JSON.parse(b)); } catch { resolve(null); } });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}
const cdpReady = async () => !!(await getJson('/json/version'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function doubaoRunning() {
  try {
    const out = execSync('tasklist /FI "IMAGENAME eq DoubaoWork.exe" /FO CSV /NH',
                         { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
    return /DoubaoWork\.exe/i.test(out);
  } catch { return false; }
}

/** 极简 CDP 客户端：直接连"页面"那条 WebSocket（不做 session 路由，够用且少一层错）。 */
class Cdp {
  constructor(wsUrl) { this.wsUrl = wsUrl; this.id = 0; this.waiting = new Map(); this.ws = null; }
  connect() {
    return new Promise((resolve, reject) => {
      const WS = globalThis.WebSocket;
      if (typeof WS === 'undefined') {
        reject(new Error('这个 node 没有全局 WebSocket（要 Node ≥21；或装 ws 模块）'));
        return;
      }
      const ws = new WS(this.wsUrl);
      const t = setTimeout(() => { try { ws.close(); } catch {} ; reject(new Error('连 CDP 超时')); }, 8000);
      ws.onopen = () => { clearTimeout(t); this.ws = ws; resolve(); };
      ws.onerror = (e) => { clearTimeout(t); reject(new Error('CDP 连接失败：' + (e && e.message ? e.message : 'error'))); };
      ws.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch { return; }
        if (m.id && this.waiting.has(m.id)) {
          const { res, rej } = this.waiting.get(m.id);
          this.waiting.delete(m.id);
          m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
        }
      };
    });
  }
  send(method, params = {}, timeoutMs = 15000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.waiting.delete(id); reject(new Error(method + ' 超时')); }, timeoutMs);
      this.waiting.set(id, {
        res: (r) => { clearTimeout(t); resolve(r); },
        rej: (e) => { clearTimeout(t); reject(e); },
      });
      try { this.ws.send(JSON.stringify({ id, method, params })); }
      catch (e) { clearTimeout(t); reject(e); }
    });
  }
  async evalJs(expr, timeoutMs = 15000) {
    const r = await this.send('Runtime.evaluate',
      { expression: expr, returnByValue: true, awaitPromise: true }, timeoutMs);
    if (r && r.exceptionDetails) throw new Error('页面脚本报错：' + JSON.stringify(r.exceptionDetails).slice(0, 200));
    return r && r.result ? r.result.value : undefined;
  }
  close() { try { this.ws && this.ws.close(); } catch {} }
}

// ---------- 输入框自动发现 ----------
const FIND_COMPOSER = `(() => {
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 60 && r.height > 16 && r.bottom > 0 && r.top < innerHeight; };
  const all = [...document.querySelectorAll('textarea, [contenteditable="true"], [contenteditable=""]')];
  const cands = all.filter(vis).map((el) => {
    const r = el.getBoundingClientRect();
    return { tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 80),
             ph: el.getAttribute('placeholder') || '', x: Math.round(r.x), y: Math.round(r.y),
             w: Math.round(r.width), h: Math.round(r.height), area: Math.round(r.width * r.height) };
  }).sort((a, b) => (b.y + b.h) - (a.y + a.h) || b.area - a.area);
  return cands.slice(0, 6);
})()`;
const FOCUS_COMPOSER = `(() => {
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 60 && r.height > 16 && r.bottom > 0 && r.top < innerHeight; };
  const all = [...document.querySelectorAll('textarea, [contenteditable="true"], [contenteditable=""]')].filter(vis);
  if (!all.length) return null;
  all.sort((a, b) => { const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (rb.y + rb.height) - (ra.y + ra.height) || (rb.width * rb.height) - (ra.width * ra.height); });
  const el = all[0]; el.focus();
  return { tag: el.tagName.toLowerCase(), cls: String(el.className).slice(0, 80),
           len: (el.value !== undefined ? el.value : el.innerText || '').length };
})()`;
const COMPOSER_TEXT = `(() => {
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 60 && r.height > 16 && r.bottom > 0 && r.top < innerHeight; };
  const all = [...document.querySelectorAll('textarea, [contenteditable="true"], [contenteditable=""]')].filter(vis);
  if (!all.length) return '';
  all.sort((a, b) => { const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (rb.y + rb.height) - (ra.y + ra.height); });
  const el = all[0];
  return String(el.value !== undefined ? el.value : el.innerText || '');
})()`;
const CLICK_SEND = `(() => {
  const bs = [...document.querySelectorAll('button, [role="button"], div')].filter((b) => {
    const t = (b.getAttribute('aria-label') || b.getAttribute('title') || b.innerText || '');
    const c = String(b.className || '');
    return /发送|submit|send/i.test(t + ' ' + c) && b.getBoundingClientRect().width > 10;
  });
  if (!bs.length) return false;
  const b = bs[bs.length - 1];
  b.click();
  return true;
})()`;

async function pickChatTarget() {
  const list = await getJson('/json/list');
  if (!Array.isArray(list)) return null;
  const pages = list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
  const chat = pages.find((t) => /doubaowork-chat|doubaowork-launcher\/chat/i.test(t.url || ''))
            || pages.find((t) => String(t.title || '').includes('豆包工作'))
            || pages[0];
  return chat || null;
}

function stateRead() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; }
}
function stateWrite(d) {
  try { fs.writeFileSync(STATE_FILE, JSON.stringify(d, null, 1)); } catch {}
}
/** 端口占用探测：能 listen 就说明空闲，立刻关掉把口还回去 */
function portFree(port) {
  return new Promise((resolve) => {
    const srv = require('node:net').createServer();
    srv.once('error', () => resolve(false));
    srv.once('listening', () => srv.close(() => resolve(true)));
    srv.listen(port, '127.0.0.1');
  });
}
async function pickPort() {
  if (PORT_ENV) return Number(PORT_ENV);
  for (let i = 0; i < PORT_RANGE; i++) {
    const p = 9333 + i;
    if (await portFree(p)) return p;
  }
  return 9333;
}
/** 这台机器上现在有没有"能连的 CDP"：先看记下来的口，再扫一遍候选口 */
async function findLivePort() {
  const saved = Number(stateRead().port || 0);
  if (saved) {
    PORT = saved;
    if (await cdpReady()) return saved;
  }
  for (let i = 0; i < PORT_RANGE; i++) {
    PORT = 9333 + i;
    if (await cdpReady()) { stateWrite({ port: PORT }); return PORT; }
  }
  return 0;
}

async function launchApp() {
  if (!EXE) return { ok: false, why: '没找到 DoubaoWork.exe（用 --exe 指定，或设 HERONBO_DOUBAO_EXE）' };
  PORT = await pickPort();
  step('正在带调试端口启动豆包工作：端口 ' + PORT);
  const args = ['--remote-debugging-port=' + PORT, '--remote-allow-origins=*'];
  spawn(EXE, args, { detached: true, stdio: 'ignore' }).unref();
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    if (await cdpReady()) return { ok: true };
  }
  return { ok: false, why: '起了但调试端口没通（客户端可能屏蔽了该开关）' };
}

/** 等到"能连上 CDP"；连不上时按需拉起，且在"已在跑但没端口"时明确报错而不是瞎点。 */
async function ensureAttached({ launch = true } = {}) {
  if (await findLivePort()) return { ok: true };
  if (doubaoRunning() && !launch) return { ok: false, code: 3, why: '豆包工作在跑，但不是调试端口起的' };
  if (doubaoRunning() && launch) {
    return { ok: false, code: 3,
             why: '豆包工作正在运行，但它不是用调试端口启动的——我连不上。请先退出豆包工作（或在下面选“重启它”），我再用调试端口把它拉起来（你当前那段对话会留在历史里）' };
  }
  if (!launch) return { ok: false, code: 3, why: '豆包工作没在跑' };
  const r = await launchApp();
  if (!r.ok) return { ok: false, code: 2, why: r.why };
  stateWrite({ port: PORT, exe: EXE });
  return { ok: true };
}

// ---------- 会话轨迹（结果与进度的真源） ----------
function newestTrajectory() {
  let best = null;
  const walk = (dir, depth) => {
    let ents; try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of ents) {
      const p = path.join(dir, e.name);
      if (e.isDirectory() && depth < 6) walk(p, depth + 1);
      else if (e.isFile() && e.name === 'trajectory.jsonl') {
        let st; try { st = fs.statSync(p); } catch { continue; }
        if (!best || st.mtimeMs > best.mtimeMs) best = { path: p, mtimeMs: st.mtimeMs, size: st.size };
      }
    }
  };
  walk(SESSIONS, 0);
  return best;
}
function readLines(file, fromByte) {
  let fd; try { fd = fs.openSync(file, 'r'); } catch { return { lines: [], size: 0 }; }
  try {
    const size = fs.fstatSync(fd).size;
    if (fromByte >= size) return { lines: [], size };
    const buf = Buffer.alloc(size - fromByte);
    fs.readSync(fd, buf, 0, buf.length, fromByte);
    const lines = buf.toString('utf8').split('\n').filter((x) => x.trim());
    return { lines, size };
  } finally { try { fs.closeSync(fd); } catch {} }
}
const clip = (s, n = 110) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, n);

/** 一行轨迹 → 进度文案（返回 null 表示这行不当作进度） */
function progressOf(o) {
  const role = o.role;
  if (role === 'assistant') {
    const calls = parseToolCalls(o.tool_calls);
    if (calls.length) {
      const c = calls[calls.length - 1];
      const arg = c.args ? clip(typeof c.args === 'string' ? c.args : JSON.stringify(c.args), 90) : '';
      return `${c.name}${arg ? ' ' + arg : ''}`;
    }
    const t = clip(o.content, 120);
    return t ? '想：' + t : null;
  }
  if (role === 'tool') return '回：' + clip(o.content, 120);
  return null;                                   // user/system 行不报进度
}
/** 豆包的 tool_calls 是**字符串**（Python repr 风格的单引号列表），不能直接 JSON.parse */
function parseToolCalls(raw) {
  if (!raw) return [];
  if (Array.isArray(raw)) raw = JSON.stringify(raw);
  const out = [];
  const s = String(raw);
  const re = /'name':\s*'([^']+)'(?:[\s\S]{0,400}?'file_path':\s*'([^']*)'|[\s\S]{0,400}?"file_path":\s*"([^"]*)")?/g;
  let m;
  while ((m = re.exec(s)) !== null) out.push({ name: m[1], args: m[2] || m[3] || '' });
  if (!out.length) {
    const re2 = /"name"\s*:\s*"([^"]+)"/g;
    while ((m = re2.exec(s)) !== null) out.push({ name: m[1], args: '' });
  }
  return out;
}
/** 去掉豆包给界面用的 ```html type="renderer" 渲染块（那不是给工作台用的答复文本） */
function stripRenderBlock(t) {
  return String(t || '').replace(/```html\s+type="renderer"[\s\S]*?```/g, '').trim();
}

// ---------- 动作 ----------
async function actStatus() {
  const attach = await cdpReady();
  const running = doubaoRunning();
  const info = attach ? await getJson('/json/version') : null;
  const tr = newestTrajectory();
  const out = {
    ok: attach, running, cdp: attach, port: PORT, exe: EXE || '',
    browser: info ? info.Browser : '', sessions: SESSIONS,
    sessionsDirExists: fs.existsSync(SESSIONS),
    newestTrajectory: tr ? tr.path : '',
    newestAgeMin: tr ? Math.round((Date.now() - tr.mtimeMs) / 60000) : null,
    hint: attach ? '可以直接干活'
          : (running ? '在跑但没有调试端口 → 需先退出它（或 restart）' : '没在跑 → 我可以带调试端口拉起来'),
  };
  console.log(JSON.stringify(out, null, 2));
  return attach ? 0 : (running ? 3 : 1);
}

async function actProbe() {
  const at = await ensureAttached({ launch: !NO_LAUNCH });
  if (!at.ok) { log('✗ ' + at.why); return at.code || 2; }
  const t = await pickChatTarget();
  if (!t) { log('✗ 没有可用的页面目标'); return 5; }
  step('页面目标：' + clip(t.title, 40) + ' ｜ ' + t.url);
  const cdp = new Cdp(t.webSocketDebuggerUrl);
  await cdp.connect();
  try {
    await cdp.send('Runtime.enable');
    const cands = await cdp.evalJs(FIND_COMPOSER);
    log('输入框候选（可见、按靠下排序）：');
    (cands || []).forEach((c, i) => log(`  ${i + 1}. <${c.tag}> class="${c.cls}" ph="${clip(c.ph, 30)}" ${c.w}x${c.h} @(${c.x},${c.y})`));
    if (!cands || !cands.length) log('  （没找到——可能停在登录页/主页，不在聊天页）');
  } finally { cdp.close(); }
  return 0;
}

async function actAsk() {
  let prompt = PROMPT;
  if (PROMPT_FILE) {
    try { prompt = fs.readFileSync(PROMPT_FILE, 'utf8'); } catch (e) { log('✗ 读不到 --prompt-file：' + e.message); return 2; }
  }
  if (!prompt || !prompt.trim()) { log('✗ 没有 prompt（用 --prompt 或 --prompt-file）'); return 2; }

  const at = await ensureAttached({ launch: !NO_LAUNCH });
  if (!at.ok) { log('✗ ' + at.why); return at.code || 2; }

  const t0 = Date.now();
  const before = newestTrajectory();
  const t = await pickChatTarget();
  if (!t) { log('✗ 没有可用的页面目标'); return 5; }
  step('接管页面：' + clip(t.title, 40));

  const cdp = new Cdp(t.webSocketDebuggerUrl);
  await cdp.connect();
  let answer = '';
  try {
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable').catch(() => {});
    await cdp.send('Page.bringToFront').catch(() => {});

    const foc = await cdp.evalJs(FOCUS_COMPOSER);
    if (!foc) { log('✗ 没找到输入框（可能停在登录页/主页——先在豆包工作里进到对话页再试）'); return 5; }
    step('输入框：<' + foc.tag + '> ' + clip(foc.cls, 40));

    await cdp.send('Input.insertText', { text: prompt }, 30000);
    await sleep(300);
    let typed = await cdp.evalJs(COMPOSER_TEXT);
    if (!String(typed || '').trim()) {
      // 有些编辑器不吃 insertText：退回"直接塞值 + 触发 input 事件"
      await cdp.evalJs(`(() => { const el = document.activeElement;
        if (!el) return false;
        if (el.value !== undefined) { el.value = ${JSON.stringify(prompt)}; }
        else { el.innerText = ${JSON.stringify(prompt)}; }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true; })()`);
      typed = await cdp.evalJs(COMPOSER_TEXT);
    }
    step('已填入 ' + String(typed || '').length + ' 字');
    if (!String(typed || '').trim()) { log('✗ 文字塞不进输入框'); return 5; }

    // 提交：回车 → 6 秒后看输入框是否清空 → 没清就点发送按钮
    const key = (type) => cdp.send('Input.dispatchKeyEvent',
      { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: type === 'char' ? '\r' : undefined });
    await key('keyDown'); await key('char'); await key('keyUp');
    let sent = false;
    for (let i = 0; i < 6; i++) {
      await sleep(1000);
      const now = String(await cdp.evalJs(COMPOSER_TEXT) || '').trim();
      if (!now) { sent = true; break; }
    }
    if (!sent) {
      step('回车没提交，改点发送按钮');
      const clicked = await cdp.evalJs(CLICK_SEND);
      if (clicked) { await sleep(1500); sent = String(await cdp.evalJs(COMPOSER_TEXT) || '').trim() === ''; }
    }
    if (!sent) { log('✗ 没能把消息发出去（没找到发送按钮，或页面变了）'); return 5; }
    step('已发送，开始等它干活…');

    // 等轨迹文件出现新内容：先等"新文件/新行出现"，再流式读
    const marker = String(prompt).trim().slice(0, 30);
    let cur = before ? before.path : null;
    let offset = before ? before.size : 0;
    let lastGrow = Date.now();
    let sawMine = false;
    while (Date.now() - t0 < TIMEOUT * 1000) {
      await sleep(1500);
      const newest = newestTrajectory();
      if (newest && newest.path !== cur) {           // 开了新会话
        cur = newest.path; offset = 0;
        if (!sawMine) step('新会话：' + path.basename(path.dirname(path.dirname(path.dirname(cur)))));
      }
      if (!cur) continue;
      const { lines, size } = readLines(cur, offset);
      if (size > offset) { offset = size; lastGrow = Date.now(); }
      for (const ln of lines) {
        let o; try { o = JSON.parse(ln); } catch { continue; }
        if (o.role === 'user') {
          if (!sawMine && String(o.content || '').includes(marker)) {
            sawMine = true;
            step('它收到指令了，开始干');
          }
          continue;
        }
        const p = progressOf(o);
        if (p) step(p);
        if (o.role === 'assistant' && String(o.content || '').trim()) {
          const txt = stripRenderBlock(o.content);
          if (txt) answer = txt;
        }
      }
      if (sawMine && answer && Date.now() - lastGrow > IDLE * 1000) break;
    }
  } finally { cdp.close(); }

  if (!answer) { log('✗ 到时间没拿到答复（看上面的进度判断它卡在哪一步）'); return 4; }
  const sid = cur ? path.basename(path.dirname(path.dirname(path.dirname(cur)))) : '';
  if (sid) log('· 会话 ' + sid);
  step('答完了（用时 %ds）', Math.round((Date.now() - t0) / 1000));
  process.stdout.write(answer + '\n');             // stdout = 纯答复（工作台 text 模式直接取它）
  return 0;
}

function killApp() {
  step('正在温和结束豆包工作（你会话里的历史都在，只是进程重启）');
  try { execSync('taskkill /IM DoubaoWork.exe /T', { stdio: 'ignore' }); } catch {}
}

async function main() {
  if (action === 'status') return actStatus();
  if (action === 'probe') return actProbe();
  if (action === 'ask') return actAsk();
  if (action === 'launch') {
    if (await cdpReady()) { log('· 调试端口已在：' + PORT); return 0; }
    if (doubaoRunning()) { log('✗ 豆包工作已在跑（没有调试端口）：先退出它，或用 restart --yes'); return 3; }
    const r = await launchApp();
    log(r.ok ? '· 起来了，端口 ' + PORT : '✗ ' + r.why);
    return r.ok ? 0 : 2;
  }
  if (action === 'restart') {
    if (!has('yes')) { log('✗ restart 会关掉你正在用的豆包工作，要显式加 --yes'); return 2; }
    killApp();
    for (let i = 0; i < 20 && doubaoRunning(); i++) await sleep(1000);
    const r = await launchApp();
    log(r.ok ? '· 已用调试端口重启（端口 ' + PORT + '）' : '✗ ' + r.why);
    return r.ok ? 0 : 2;
  }
  log('用法：status | probe | launch | ask --prompt-file <f> | restart --yes');
  return 2;
}

main().then((code) => process.exit(code)).catch((e) => { log('✗ 出错了：' + (e && e.stack ? e.stack : e)); process.exit(1); });
