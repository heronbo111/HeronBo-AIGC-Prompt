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
 *   ③ `Input.insertText` 灌入 prompt → 回车（不行再点发送按钮），用"界面上多出一条我说的话"确认发出
 *   ④ **结果从界面读**（⚠️ 2026-09-23 实测更正）：这一版豆包工作**不往
 *      `trajectory.jsonl` 写 assistant 行**（只写用户那条），所以答复只能从聊天区 DOM 取——
 *      `[data-testid="receive_message"]` 的最后一条，出现"消耗 N 点"或连续数秒不再变长＝写完了。
 *      轨迹文件仍读，但只当辅助（有些版本/模式下它会写工具调用）。
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
import net from 'node:net';
import { fileURLToPath } from 'node:url';

// ⚠️ 本文件是 **ESM**（.mjs），模块作用域里**没有 __dirname**。2026-09-30 新机搭桥卡死就是这个：
// 豆包工作装在固定候选表之外的位置时，`EXE` 解析会落到 cfgDoubaoworkExe()，它一碰 __dirname
// 就抛 `ReferenceError: __dirname is not defined in ES module scope`——**整个模块崩掉**，
// 连 status / 搭桥验证都跑不起来（本机没暴露：E:\DoubaoWork\app\ 命中固定候选，短路了）。
// ESM 里取本文件所在目录用 fileURLToPath(import.meta.url)。
const HERE = path.dirname(fileURLToPath(import.meta.url));

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
// 视频生成（action=video）相关参数
const VSTAGE = opt('stage', 'prepare');
const VIMAGE = opt('image');
const VAUDIO = opt('audio');
const VVIDEO = opt('video');
const VMODEL = opt('model');
const VRATIO = opt('ratio');
const VDURATION = opt('duration');
const VSID = opt('sid');
const VOUT = opt('out');

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

// 本机配置：agent_bridge.local.json 的 "doubaowork" 字段（exe 完整路径）。
// 2026-09-30 加：新机豆包工作装在自由位置时固定候选全会落空 → 死环（启动器找不到它，
// 豆包又不能重启自己）。豆包自己知道装在哪，让它把路径写进 local.json 即可破环。
function cfgDoubaoworkExe() {
  const la = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  const cands = [
    path.join(HERE, 'agent_bridge.local.json'),
    path.join(HERE, 'dist', 'agent_bridge.local.json'),   // 打包后 exe 旁（cfg_dir 的真身）
    path.join(la, 'HeronBoScoreTool', 'agent_bridge.local.json'),
  ];
  for (const p of cands) {
    try {
      const j = JSON.parse(fs.readFileSync(p, 'utf8'));
      if (j && typeof j.doubaowork === 'string' && j.doubaowork.trim()) return j.doubaowork.trim();
    } catch {}
  }
  return null;
}

// 注册表兜底：官方卸载键的 DisplayIcon 通常就是 exe 完整路径（安装位置自由时的最后防线）
function findExeViaRegistry() {
  const keys = [
    'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
  ];
  for (const k of keys) {
    let out = '';
    try {
      out = execSync(`reg query "${k}" /s /v DisplayIcon`,
                     { encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'ignore'] });
    } catch { continue; }
    for (const line of String(out).split(/\r?\n/)) {
      const m = line.match(/REG_SZ\s+"?(.+DoubaoWork\.exe)/i);
      if (!m) continue;
      const p = m[1].trim().replace(/,0\s*$/, '').replace(/^"|"$/g, '');
      try { if (fs.existsSync(p)) return p; } catch {}
    }
  }
  return null;
}

const EXE = opt('exe')
  || EXE_CANDIDATES.find((p) => { try { return fs.existsSync(p); } catch { return false; } })
  || cfgDoubaoworkExe()
  || findExeViaRegistry()
  || null;

const SESS_REL = ['.doubaowork', 'agent_mode', 'workspace', '.sessions'];

function sessionsRoot() {
  const env = opt('sessions') || process.env.HERONBO_DOUBAO_SESSIONS;
  if (env) return env;
  const la = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  const ud = path.join(la, 'DoubaoWork', 'User Data');
  const cands = [];
  // **每个 profile 目录都试一遍**（换机/企业版/多开时它不一定叫 Default）
  try {
    for (const d of fs.readdirSync(ud)) cands.push(path.join(ud, d, ...SESS_REL));
  } catch {}
  cands.push(path.join(ud, 'Default', ...SESS_REL));
  const live = cands.filter((c) => { try { return fs.existsSync(c); } catch { return false; } });
  // ⚠️ 不能"取第一个存在的"：目录按字母序 Default 在前，但**真正在用的往往是 Profile 2**
  // （2026-09-23 实测：Default 里躺着 9 月 2 日的旧会话，新会话全在 Profile 2 → 取答复永远取空）。
  // 判定标准＝哪个 profile 的轨迹文件最新。
  let best = '', bestMs = -1;
  for (const c of live) {
    const t = newestTrajectoryIn(c);
    if (t && t.mtimeMs > bestMs) { bestMs = t.mtimeMs; best = c; }
  }
  return best || live[0] || cands[cands.length - 1];
}
/** 给定会话 id，找出哪个 profile 下真有它（用于"当前这段对话"精确定位） */
function sessionDirFor(sid) {
  if (!sid) return null;
  const la = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  const ud = path.join(la, 'DoubaoWork', 'User Data');
  const cands = [];
  try { for (const d of fs.readdirSync(ud)) cands.push(path.join(ud, d, ...SESS_REL, String(sid))); } catch {}
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch {} }
  return null;
}
let SESSIONS = sessionsRoot();

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
    if (r && r.exceptionDetails) {
      const edd = r.exceptionDetails;
      const descr = (edd.exception && edd.exception.description) || edd.text || JSON.stringify(edd);
      throw new Error('页面脚本报错：' + String(descr).slice(0, 700) + ' || @line' + edd.lineNumber + ' col' + edd.columnNumber);
    }
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
// 2026-09-28 新版豆包工作发送按钮：[data-testid="chat_input_send_button"]（右下角圆形 ↑，
// 无文字标签）。优先点它；找不到再退回"发送/send"文字匹配。返回 {ok,how}。
const CLICK_SEND = `(() => {
  const r = (el) => el.getBoundingClientRect();
  const usable = (b) => { const bb = r(b); return !b.disabled && bb.width >= 20 && bb.height >= 20; };
  const t = document.querySelector('[data-testid="chat_input_send_button"]');
  if (t && usable(t)) { t.click(); return { ok: true, how: 'testid' }; }
  const bs = [...document.querySelectorAll('button, [role="button"], div')].filter((b) => {
    const x = (b.getAttribute('aria-label') || b.getAttribute('title') || b.innerText || '');
    const c = String(b.className || '');
    return /发送|submit|send/i.test(x + ' ' + c) && usable(b);
  });
  if (!bs.length) return { ok: false };
  bs[bs.length - 1].click();
  return { ok: true, how: 'text' };
})()`;

// 填字：直接走 tiptap EditorView（view.dispatch），让 ProseMirror state 正确更新——
// 只往 DOM 塞文字（insertText/innerText）时 state 不更新，发送按钮不会出现、也发不出去。
// 用法：FILL_VIA_EDITOR + '(' + JSON.stringify(text) + ')'
const FILL_VIA_EDITOR = `(text => {
  const r = (el) => el.getBoundingClientRect();
  const vis = (el) => { const b = r(el); return b.width > 60 && b.height > 16 && b.bottom > 0 && b.top < innerHeight; };
  const eds = [...document.querySelectorAll('textarea,[contenteditable="true"],[contenteditable=""]')].filter(vis);
  eds.sort((a, b) => r(b).bottom - r(a).bottom);
  const ed = eds[0]; if (!ed) return { ok: false, why: 'no editor' };
  const E = ed.editor;
  if (!E || !E.view) return { ok: false, why: 'no tiptap editor' };
  const view = E.view;
  const raw = String(text).replace(/\\r\\n/g, '\\n');
  for (let k = 0; k < 3; k++) {
    try { view.focus();
      if (E.chain && E.commands && E.commands.insertContent) {
        // 官方 API、传纯字符串让 tiptap 自己建节点（手动构造 doc/HTML 会在 React 渲染期报错）
        E.chain().focus().clearContent({ emit: false }).insertContent(raw).run();
      } else {
        // 兜底：手动 dispatch（换行退化为空格，保证能发出）
        const schema = view.state.schema, flat = raw.replace(/\\n/g, ' ');
        view.dispatch(view.state.tr.replaceWith(0, view.state.doc.content.size,
          schema.nodes.doc.create(null, schema.nodes.paragraph.create(null, schema.text(flat))).content));
      }
      const len = view.state.doc.textContent.length;
      if (len > 0) return { ok: true, len };
    } catch (e) { if (k === 2) return { ok: false, why: String((e && e.message) || e).slice(0, 90) }; }
  }
  return { ok: false, why: 'empty after fill' };
})`;

/** 聊天区快照：消息条数 + 最后一条问答 + "它写完了没"。
 *  ⚠️ 这一版豆包工作**不往 trajectory.jsonl 写 assistant 行**（只写用户那条），
 *  所以答复只能从界面读——2026-09-23 实测确认（界面上有"好。"，文件里没有）。 */
const MSG_STATE = `(() => {
  const rows = (sel) => [...document.querySelectorAll(sel)];
  const txt = (el) => {
    const c = el.querySelector('[data-testid="message_text_content"]') || el;
    return String(c.innerText || '').trim();
  };
  const sends = rows('[data-testid="send_message"]');
  const recvs = rows('[data-testid="receive_message"]');
  const lastRecvEl = recvs.length ? recvs[recvs.length - 1] : null;
  const bar = lastRecvEl ? String(lastRecvEl.innerText || '') : '';
  // 运行中最可靠信号：输入区“中断”按钮(chat_input_local_break_button)在思考/工具调用/输出全程都在，
  // 任务真正结束才消失。文字正则在“工具调用间隙、无思考提示”时会漏，故以该按钮为准。
  const running = !!document.querySelector('[data-testid="chat_input_local_break_button"]');
  return {
    sends: sends.length,
    recvs: recvs.length,
    lastSend: sends.length ? txt(sends[sends.length - 1]) : '',
    lastRecv: lastRecvEl ? txt(lastRecvEl) : '',
    fullRecv: bar,
    done: /消耗\\s*[\\d.]+\\s*点/.test(bar),
    running,
    busy: running || /停止生成|生成中|正在生成|思考中|正在思考|请稍候|高峰期|优先通道|排队|正在准备|加载中/.test(bar),
  };
})()`;
const PAGE_SID = `(() => { const m = location.href.match(/chat\\/(\\d+)/); return m ? m[1] : ''; })()`;

// 定位左侧"新工作任务"应点击的坐标（文字SPAN→closest语义按钮→否则整行祖先）
const FIND_NEW_TARGET = `(() => {
  const r = (el) => el.getBoundingClientRect();
  let span = null;
  [...document.querySelectorAll('*')].forEach((e) => {
    if ((e.innerText || '').trim() === '新工作任务' && r(e).width > 30 && !span) span = e;
  });
  if (!span) return null;
  let el = span.closest('button,a,[role="button"]');
  if (!el) { let p = span;
    for (let i = 0; i < 4; i++) { p = p.parentElement; if (!p) break;
      if (r(p).width > 120 && r(p).height >= 30 && r(p).height < 70) { el = p; break; } } }
  el = el || span;
  const b = r(el);
  return { cx: Math.round(b.x + b.width / 2), cy: Math.round(b.y + b.height / 2),
           w: Math.round(b.width), h: Math.round(b.height) };
})()`;

// 当前是否"空白新会话"：URL 无 sid 且输入框无文字
const IS_FRESH = `(() => {
  const r = (el) => el.getBoundingClientRect();
  const vis = (el) => { const b = r(el); return b.width > 60 && b.height > 16; };
  const sidM = location.href.match(/chat\\/(\\d+)/);
  const eds = [...document.querySelectorAll('textarea,[contenteditable="true"],[contenteditable=""]')].filter(vis);
  eds.sort((a, b) => r(b).bottom - r(a).bottom);
  const ed = eds[0];
  const t = ed ? String(ed.value !== undefined ? ed.value : ed.innerText || '') : '';
  return { sid: sidM ? sidM[1] : '', text: t, fresh: !sidM && !t.trim() };
})()`;

// 后台静默新建空白会话：页面内 JS 点击「新工作任务」并轮询进入空白页（不抢焦、不依赖真实鼠标）
const NEW_CHAT_JS = `(async () => {
  const r = (el) => el.getBoundingClientRect();
  const isFresh = () => {
    const sidM = location.href.match(/chat\\/(\\d+)/);
    const ed = document.querySelector('[data-testid="chat_input_input"]');
    const t = ed ? String(ed.innerText || '') : '';
    return !sidM && !t.trim();
  };
  if (isFresh()) return { ok: true, already: true };
  let span = null;
  [...document.querySelectorAll('*')].forEach((e) => {
    if ((e.innerText || '').trim() === '新工作任务' && r(e).width > 30 && !span) span = e;
  });
  if (!span) return { ok: false, why: 'no entry' };
  let el = span.closest('button,a,[role="button"]');
  if (!el) { let p = span;
    for (let i = 0; i < 4; i++) { p = p.parentElement; if (!p) break;
      if (r(p).width > 120 && r(p).height >= 30 && r(p).height < 70) { el = p; break; } } }
  el = el || span;
  el.click();
  for (let i = 0; i < 24; i++) {
    await new Promise((rr) => setTimeout(rr, 300));
    if (isFresh()) return { ok: true, i };
  }
  const b = r(el);
  return { ok: false, why: 'not fresh after js click', cx: Math.round(b.x + b.width / 2), cy: Math.round(b.y + b.height / 2) };
})()`;

// 后台静默发送：页面内轮询等发送按钮启用再 JS 点击（不抢焦）；testid 优先，文字兜底
const CLICK_SEND_WAIT = `(async () => {
  const r = (el) => el.getBoundingClientRect();
  for (let i = 0; i < 24; i++) {
    const t = document.querySelector('[data-testid="chat_input_send_button"]');
    if (t && !t.disabled && r(t).width >= 20) { t.click(); return { ok: true, how: 'testid', i }; }
    await new Promise((rr) => setTimeout(rr, 250));
  }
  const usable = (b) => { const bb = r(b); return !b.disabled && bb.width >= 20 && bb.height >= 20; };
  const bs = [...document.querySelectorAll('button,[role="button"],div')].filter((b) => {
    const x = b.getAttribute('aria-label') || b.getAttribute('title') || b.innerText || '';
    return /发送|submit|send/i.test(x + ' ' + String(b.className || '')) && usable(b);
  });
  if (bs.length) { bs[bs.length - 1].click(); return { ok: true, how: 'text' }; }
  return { ok: false };
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

// 所有"聊天页"target（含空白 chat 与带 sid 的会话；可能不止一个窗口）
async function listChatTargets() {
  const list = await getJson('/json/list');
  if (!Array.isArray(list)) return [];
  return list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl
    && /(doubaowork-chat\/chat|doubaowork-launcher\/chat|\/chat($|\/))/i.test(t.url || ''));
}
// 连接一个 target 并返回 cdp（已 Runtime.enable）
async function attachTarget(t) {
  const c = new Cdp(t.webSocketDebuggerUrl);
  await c.connect();
  await c.send('Runtime.enable');
  return c;
}
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
/** 发送后定位"真正承载本次消息"的 target：扫描所有聊天页，lastSend 含 prompt 指纹即命中。
 *  无论新会话是同窗口路由还是另开窗口都能锁定。返回 {cdp(保持连接,调用方关),sid,state}；找不到 null。 */
async function locateSentTarget(marker, waitMs = 22000) {
  const key = norm(marker).slice(0, 12);
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    const targets = await listChatTargets();
    for (const t of targets) {
      let c;
      try { c = await attachTarget(t); } catch { continue; }
      let state = null, sid = '';
      try {
        state = await c.evalJs(MSG_STATE);
        sid = String(await c.evalJs(PAGE_SID) || '');
      } catch { c.close(); continue; }
      if (state && norm(state.lastSend).includes(key)) return { cdp: c, sid, state: state || {} };
      c.close();
    }
    await sleep(700);
  }
  return null;
}
/** 在**后台标签页**打开空白会话（独立 target/webContents，不弹窗、不抢焦点）：与其他会话隔离，
 *  别的会话提前回消息也不会把它导航/顶掉。返回已连接的 cdp（含 .targetId）。
 *  Electron 默认会节流/挂起后台页面（点击发送零请求），这里用「焦点模拟 + 强制 active」对抗，
 *  另由 launchApp 的抗节流启动开关双保险。 */
async function createIsolatedChat(opts = {}) {
  const asWindow = !!(opts && opts.asWindow);
  const silent = !!(opts && opts.silent);
  const ver = await getJson('/json/version');
  if (!ver || !ver.webSocketDebuggerUrl) throw new Error('拿不到 browser 级 CDP 连接');
  const b = new Cdp(ver.webSocketDebuggerUrl);
  await b.connect();
  let newId = '';
  try {
    const r = await b.send('Target.createTarget',
      asWindow
        ? { url: 'doubaowork://doubaowork-chat/chat', newWindow: true }
        : { url: 'doubaowork://doubaowork-chat/chat', newWindow: false, background: true }, 15000);
    newId = r.targetId;
  } finally { b.close(); }
  if (!newId) throw new Error('Target.createTarget 没返回 targetId');
  let t = null;
  for (let i = 0; i < 30; i++) {
    await sleep(400);
    const list = await getJson('/json/list');
    t = (list || []).find((x) => (x.targetId || x.id) === newId && x.webSocketDebuggerUrl);
    if (t) break;
  }
  if (!t) throw new Error('新建的会话页没出现在 target 列表');
  const cdp = new Cdp(t.webSocketDebuggerUrl);
  await cdp.connect();
  await cdp.send('Runtime.enable');
  await cdp.send('Page.enable').catch(() => {});
  // 无论窗口/标签：模拟焦点 + 强制 active，后台/最小化下不被节流、tiptap 可聚焦、DOM click 可发送
  await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});
  await cdp.send('Page.setWebLifecycleState', { state: 'active' }).catch(() => {});
  if (asWindow && !silent) {
    // 非静默：置前获得真实焦点
    await cdp.send('Page.bringToFront').catch(() => {});
  }
  if (asWindow && silent) {
    // 静默：不置前、移到屏外（normal + 正常尺寸）。⚠️ 不能最小化——最小化窗口会被 Electron/Windows
    // 冻结（occluded/background），豆包不处理、Runtime.evaluate 也不返回（2026-09-29 实测，7分22秒）。
    // 离屏 normal 窗口用户看不见、但保持 active 正常渲染；配合 setFocusEmulation / setWebLifecycleState。
    for (let i = 0; i < 8; i++) {
      try {
        const w = await cdp.send('Browser.getWindowForTarget', { targetId: newId });
        await cdp.send('Browser.setWindowBounds',
          { windowId: w.windowId, bounds: { windowState: 'normal', left: -3200, top: -3200, width: 1000, height: 720 } });
        break;
      } catch { await sleep(250); }
    }
  }
  // 等输入框**真正渲染**：FOCUS_COMPOSER 非空。不能用 IS_FRESH——无编辑器时它会误报 fresh。
  let ready = false;
  for (let i = 0; i < 30; i++) {
    const c = await cdp.evalJs(FOCUS_COMPOSER).catch(() => null);
    if (c) { ready = true; break; }
    await sleep(400);
  }
  if (!ready) throw new Error('后台会话输入框没渲染出来');
  // 再充分等待，让网络/IPC/鉴权等初始化全部完成（否则点击发送会被吞、按钮卡 disabled）。
  await sleep(2500);
  cdp.targetId = newId;
  return cdp;
}
/** 关闭指定会话窗口（Target.closeTarget） */
async function closeTarget(targetId) {
  if (!targetId) return;
  const ver = await getJson('/json/version');
  if (!ver || !ver.webSocketDebuggerUrl) return;
  const b = new Cdp(ver.webSocketDebuggerUrl);
  await b.connect();
  try { await b.send('Target.closeTarget', { targetId }, 8000); } catch {}
  finally { b.close(); }
}

// ========== 视频生成（普通对话，三档可选；prepare / confirm / cancel） ==========
// 上传附件：直接对隐藏 input[type=file] 用 DOM.setFileInputFiles（绕过系统文件选择框）
async function uploadAttachments(cdp, files) {
  await cdp.send('DOM.enable').catch(() => {});
  for (const f of files.map((x) => path.resolve(x))) {
    let nodeId = 0;
    try {
      const s = await cdp.send('DOM.performSearch', { query: 'input[type="file"]' }, 10000);
      if (s && s.resultCount > 0) {
        const r = await cdp.send('DOM.getSearchResults',
          { searchId: s.searchId, fromIndex: 0, toIndex: 1 });
        nodeId = r.nodeIds[0];
      }
    } catch {}
    if (!nodeId) throw new Error('没找到文件上传 input[type=file]');
    await cdp.send('DOM.setFileInputFiles', { files: [f], nodeId }, 20000);
    step('已选择上传：' + path.basename(f));
    await sleep(2800);                          // 等上传完成 / 缩略图出现
  }
}
// 从台词里挑出"必须注音"的词：阿拉伯数字、字母数字混写、常见缩写。
// 为什么自动挑而不是写死：2026-09-30 用户打回——台词"28 考研"被读成"二十八考研"，
// 原因是我们只把"四六级/580"写死在指令里，**新台词的易错词根本没进去**。
// rules 第 15/258 条本来就要求"易错词逐个注音"，这里把它接成默认动作（rules 72）。
// 注意：这四个正则只配 .test() 用，**不能带 /g**——/g 的 lastIndex 会在多次
// pronFor() 调用间残留，第二次起 test() 从上次的匹配点之后找 → 时灵时不灵
// （2026-09-30 复审 ZCode 提交 fa09b2d 时抓出来的静默 bug）。
const PRON_HINTS = [
  [/\d+\s*考研/, '「28 考研」这类"数字+考研"必须按**逐位读法**念：28 读 èr-shí-bā（2028 的 28），不是"二十八"'],
  [/\d+\s*级/, '「四六级」读 sì-liù-jí，绝不读成"四立级/四留级"'],
  [/\d{2,}/, '多位数按**逐位**读（如 580 读 wǔ-bā-líng），不读"五百八十"'],
  [/[A-Za-z]{2,}/, '英文缩写/字母按**字母拼读**（如 CET-4 读 C-E-T-four），不臆造中文谐音'],
];
/** 从提示词正文里抠出「台词改成：」那一句（工作台与 skill 的输出口径一致） */
function extractSpokenLine(prompt) {
  const m = String(prompt || '').match(/台词改成[：:]\s*([^\n]+)/);
  return m ? m[1].trim() : '';
}
/** 台词里有数字/缩写就返回注音要求，没有就返回 ''（不给豆包塞无关读音指令） */
function pronFor(prompt) {
  const line = extractSpokenLine(prompt);
  if (!line) return '';
  const hits = [];
  for (const [re, note] of PRON_HINTS) {
    if (re.test(line) && !hits.includes(note)) hits.push(note);
  }
  return hits.join('；');
}

// 构造视频请求（路径2：图生接口无音频位 → 给本地路径，画面/配音分头生成再对齐合成；prepare 只回计划、不生成）
function buildVideoRequest(o) {
  const L = [];
  // 短片（≤6s）不分段：2026-09-30 实测 4s 的活也被切成两段+锚点衔接，纯浪费还引入拼接风险
  const shortJob = Number(o.duration) <= 6;
  const formName = o.video ? '视频参考' : (o.image ? '图生' : '文生');
  const lockSrc = o.video
    ? '先读取参考视频，锁定人物外观、服装、场景与镜头，并参考其口型、动作与眼神节奏'
    : (o.image ? '先读取首帧图锁定人物外观' : '');
  const segStart = o.video ? '参考视频对应片段（抽其首帧并参考运动/口型）'
    : (o.image ? '首帧图' : '提示词');
  L.push('我要做一条【' + formName + '·口播带货】视频。关键约束：视频生成接口没有“参考视频+音色”一次出整条口播的输入位，所以请按“画面与配音分头生成、再逐段对齐、合成一条成片”的流水线执行。素材你直接读本地文件即可，我不需要在对话里上传附件。');
  L.push('');
  L.push('【本地素材（请直接用绝对路径读取）】');
  if (o.video) L.push('- 参考视频（人物形象/动作/口型节奏/场景的示范，静音；务必识别为“视频参考”，不要当成文生）：' + o.video);
  if (o.image) L.push('- 首帧/形象参考图：' + o.image);
  if (o.audio) L.push('- 音色参考（只克隆声线与说话状态，不要念它原本内容）：' + o.audio);
  L.push('');
  L.push('【独立执行参数（以此为准）】');
  L.push('- 画面模型：' + o.model);
  L.push('- 画面比例：' + o.ratio);
  L.push('- 成片总时长：约 ' + o.duration + ' 秒（带货快口播；'
    + (shortJob ? '一次性生成，不切分' : '分段画面与配音都对齐到该总时长') + '）');
  L.push('- 形式：' + (o.video
      ? '视频参考：以参考视频锁定人物外观与口型/动作节奏，按新台词生成画面（不是文生）'
      : (o.image ? '图生，分段生成画面' : '文生，分段生成画面')));
  L.push('');
  L.push('【确认后请按此流水线执行】');
  if (shortJob) {
    L.push('1. ' + lockSrc + '；总时长只有 ' + o.duration + ' 秒——**不要分段**，一次性生成整条画面：以'
      + segStart + '为起点（接口若支持首尾帧，把参考视频对应片段的末帧一并作为锚点），保证人物、服装、场景、光线一致，并满足提示词中的动作、眼神与口型要求。');
  } else {
    L.push('1. ' + lockSrc + '；按台词语气拐点把画面分成 2-3 段（每段 4-8 秒、贴合口播、含余量且合计不超过总时长；具体切点、每段秒数与对应台词分句请在确认清单里给出时间轴）。逐段生成画面：第1段以' + segStart + '为起点，后续每段以上一段末尾姿态/画面衔接，保证人物、服装、场景、光线一致，并满足提示词中该段对应的动作、眼神与口型要求。');
  }
  L.push('2. 用音色参考 + 台词全文生成与画面等长的配音（可按同一切点分段生成再拼接），带货快口播语速，读音准确'
    + (o.pron ? ('（本条台词的读音要求：' + o.pron + '）') : '') + '。');
  L.push('3. 把配音与画面' + (shortJob ? '' : '逐段') + '对齐口型和节奏，合成【一条】' + o.ratio + '、约 ' + o.duration + ' 秒的成片'
    + (shortJob ? '' : '：段间承接自然、无跳切/黑帧，响度统一') + '；无字幕、水印、Logo，无 BGM。');
  L.push('4. 交付口径：**不要输出自检/质检报告**（分辨率、帧率、LUFS 响度、口型逐项核对那类都省掉），'
    + '也不要把中间分段画面单独发我；' + (shortJob ? '生成完直接把成片发我' : '全部合成完只发最终成片')
    + '。上传/转码等服务偶发异常自己重试一次即可，不要停下来写解释。');
  L.push('');
  L.push('【现在停在计划阶段，不要生成、不消耗额度】请只回一份“执行计划确认清单”：');
  L.push('- 分段时间轴：每段起止秒、对应台词分句、画面动作要点；');
  L.push('- 每段输入（参考视频/首帧/上一帧）与配音方式；合成方式与最终规格（模型/比例/总时长）；');
  L.push('- 预计消耗（点数/积分）：分列画面、配音与合计，并给出重试余量建议。');
  L.push('等我明确回复“确认”后再开始执行。');
  L.push('');
  L.push('【回话纪律（重要，直接影响成片质量）】');
  L.push('- 清单里**只写这份任务真正要执行的内容**：时间轴、分段输入、配音方式、合成方式、最终规格、预计消耗。');
  L.push('- **不要复述本指令里的约束与说明文字**（例如“不要分段”“一次性生成”“无字幕水印”“以你给的…为准”、'
    + '“本次台词里某组读音不在本条中”这类）——它们是执行要求，不是要交付的内容，写进清单只是噪音。'
    + '本次台词用不到的要求，直接不提。');
  L.push('- 不要输出质检/自检结论，也不要解释你怎么理解任务。');
  L.push('');
  L.push('——以下是可移植的视频提示词正文（已含画幅与“按台词估算时长”的依据，请原样遵循，不要删改其中的时长/画幅依据）——');
  L.push('（这份正文是最终下发给视频模型的指令：不要改动、不要加注释、不要在前后附加任何说明文字。）');
  L.push(o.prompt);
  return L.join('\n');
}
// 按 sid 找已存在的会话 target 并连接（后台、抗节流）
async function attachSessionBySid(sid, waitMs = 15000, opts = {}) {
  const dl = Date.now() + waitMs;
  while (Date.now() < dl) {
    const list = await getJson('/json/list');
    const t = (list || []).find((x) => x.type === 'page'
      && (x.url || '').includes('/chat/' + sid) && x.webSocketDebuggerUrl);
    if (t) {
      const c = new Cdp(t.webSocketDebuggerUrl);
      await c.connect();
      await c.send('Runtime.enable');
      await c.send('Page.enable').catch(() => {});
      if (!(opts && opts.silent)) await c.send('Page.bringToFront').catch(() => {});
      await c.send('Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});
      await c.send('Page.setWebLifecycleState', { state: 'active' }).catch(() => {});
      c.targetId = t.targetId || t.id;
      return c;
    }
    await sleep(700);
  }
  return null;
}
// 按 targetId 复用已存在的视频会话（静默、不置前；找不到返回 null）
async function attachSessionByTarget(targetId) {
  if (!targetId) return null;
  const list = await getJson('/json/list');
  const t = (list || []).find((x) => (x.targetId || x.id) === targetId && x.webSocketDebuggerUrl);
  if (!t) return null;
  const cdp = new Cdp(t.webSocketDebuggerUrl);
  await cdp.connect();
  await cdp.send('Runtime.enable');
  await cdp.send('Page.enable').catch(() => {});
  await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});
  await cdp.send('Page.setWebLifecycleState', { state: 'active' }).catch(() => {});
  cdp.targetId = t.targetId || t.id;
  return cdp;
}

// 按 sid **重开**一段历史对话——固定会话的关键拼图（2026-10-02 实测）：
// App 重启 / 会话窗口被关后，target 必然失效；doubaowork:// 协议支持带 /chat/<sid> 直接定位，
// Target.createTarget(background 标签) 就能把**同一段对话**原样拉回来，不再新建、侧栏不再堆积。
async function openSessionBySid(sid) {
  if (!sid) return null;
  const ver = await getJson('/json/version');
  if (!ver || !ver.webSocketDebuggerUrl) return null;
  const b = new Cdp(ver.webSocketDebuggerUrl);
  let newId = '';
  try {
    await b.connect();
    const r = await b.send('Target.createTarget',
      { url: 'doubaowork://doubaowork-chat/chat/' + sid, newWindow: false, background: true }, 15000);
    newId = r.targetId;
  } catch { return null; } finally { try { b.close(); } catch {} }
  if (!newId) return null;
  for (let i = 0; i < 30; i++) {
    await sleep(400);
    const c = await attachSessionByTarget(newId).catch(() => null);
    if (c) return c;
  }
  return null;
}

/* 统一获取固定会话（kind: 'ask' 出提示词 / 'video' 视频）。
   五级回退，目标都是**回到同一段对话**而不是开新的：
   ① state 里锁定的 target 还活着 → 直接用（最快）
   ② 会话正以标签页开着 → 按 sid 找回
   ③ 都不在 → **按 sid 重开历史会话**（后台标签，不弹窗）——跨重启的连续性靠这一级
   ④ 回收同类旧会话（没记过 sid 时的兜底；找到顺手把 sid 也记下）
   ⑤ 实在没有才新建隐藏离屏窗（sid 要等第一句话发出去才有，由调用方回填 state） */
async function acquireSession(kind) {
  const keyT = kind === 'video' ? 'videoTarget' : 'askTarget';
  const keyS = kind === 'video' ? 'videoSid' : 'askSid';
  const st0 = stateRead();
  if (st0[keyT]) {
    const c = await attachSessionByTarget(st0[keyT]).catch(() => null);
    if (c) return c;
  }
  if (st0[keyS]) {
    const c = await attachSessionBySid(st0[keyS], 4000, { silent: true }).catch(() => null);
    if (c) { stateWrite(Object.assign(stateRead(), { [keyT]: c.targetId })); return c; }
  }
  if (st0[keyS]) {
    const c = await openSessionBySid(st0[keyS]).catch(() => null);
    if (c) { stateWrite(Object.assign(stateRead(), { [keyT]: c.targetId })); return c; }
  }
  const list = await getJson('/json/list');
  const RE_KW = kind === 'video'
    ? /视频参考|执行计划|口播|视频/
    : /提示词|口播|视频|参考|prompt/i;
  // 只回收自动化产生的正式 chat 会话：排除主对话（标题「开发与调试」）、排除空白/非 chat 页
  const cands = (list || []).filter((x) => x.type === 'page' && x.webSocketDebuggerUrl
    && /doubaowork-chat\/chat\/\d+/.test(x.url || '')
    && !/开发与调试/.test(x.title || '')
    && RE_KW.test(x.title || ''));
  if (cands.length) {
    const t = cands[cands.length - 1];     // 列表顺序≈创建顺序，取最近一个
    const tid = t.targetId || t.id;
    const c = await attachSessionByTarget(tid).catch(() => null);
    if (c) {
      const m = String(t.url || '').match(/chat\/(\d+)/);
      const patch = { [keyT]: tid };
      if (m) patch[keyS] = m[1];           // 顺手把 sid 也记住，下次重启就能按 sid 重开
      stateWrite(Object.assign(stateRead(), patch));
      return c;
    }
  }
  const c = await createIsolatedChat({ asWindow: true, silent: true });
  stateWrite(Object.assign(stateRead(), { [key]: c.targetId }));
  return c;
}

// 最后一条回复里的视频状态
const VIDEO_STATE = `(() => {
  const recvs = [...document.querySelectorAll('[data-testid="receive_message"]')];
  const last = recvs[recvs.length - 1];
  const running = !!document.querySelector('[data-testid="chat_input_local_break_button"]');
  if (!last) return { has: false, running };
  const v = last.querySelector('video');
  const text = (last.querySelector('[data-testid="message_text_content"]') || last).innerText.trim();
  if (v) return { has: true, src: v.currentSrc || v.src, ready: v.readyState, dur: v.duration };
  return { has: false, running, text: text.slice(-220) };
})()`;
// 页面内 fetch 视频 → 分块 base64 读回写盘（兼容 blob: 与 http）
async function downloadMedia(cdp, srcUrl, outPath) {
  const meta = await cdp.evalJs(`(async () => {
    const r = await fetch(${JSON.stringify(srcUrl)});
    if (!r.ok) return { err: 'HTTP ' + r.status };
    const b = new Uint8Array(await r.arrayBuffer());
    let s = ''; for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    window.__dlv = btoa(s);
    return { bytes: b.length, b64: window.__dlv.length };
  })()`, 90000);
  if (!meta || meta.err) throw new Error('抓取视频失败：' + (meta && meta.err || '?'));
  const fd = fs.openSync(outPath, 'w');
  const CH = 120000;
  try {
    for (let off = 0; off < meta.b64; off += CH) {
      const piece = await cdp.evalJs(`window.__dlv.slice(${off},${off + CH})`);
      fs.writeSync(fd, Buffer.from(piece, 'base64'));
    }
  } finally { fs.closeSync(fd); }
  await cdp.evalJs('delete window.__dlv').catch(() => {});
  return meta.bytes;
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
    const srv = net.createServer();
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
  if (!EXE) return { ok: false, why: '没找到 DoubaoWork.exe——破环三选一：①agent_bridge.local.json 加 "doubaowork":"exe完整路径"；②setx HERONBO_DOUBAO_EXE "exe完整路径"；③--exe 参数' };
  PORT = await pickPort();
  step('正在带调试端口启动豆包工作：端口 ' + PORT);
  // 抗节流/挂起开关：让后台、遮挡、最小化的页面不被 Electron 降速或冻结（配合后台标签页方案）。
  // --disable-notifications 去系统通知弹窗；--window-position 离屏让初始窗口不挡屏（CDP 再最小化兜底）。
  const args = ['--remote-debugging-port=' + PORT, '--remote-allow-origins=*',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows', '--disable-features=CalculateNativeWinOcclusion',
    '--disable-hang-monitor', '--disable-notifications',
    '--window-position=-32000,-32000', '--window-size=1000,700'];
  spawn(EXE, args, { detached: true, stdio: 'ignore' }).unref();
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    if (await cdpReady()) { await hideLaunchedWindow().catch(() => {}); return { ok: true }; }
  }
  return { ok: false, why: '起了但调试端口没通（客户端可能屏蔽了该开关）' };
}

/** 把"我们刚拉起"的客户端窗口移出视野并最小化（绝不动用户已在看的窗口）：
 *  命令行已给离屏坐标，这里再用 CDP 强制最小化兜底。配合抗节流开关，后台仍正常工作。 */
async function hideLaunchedWindow() {
  const ver = await getJson('/json/version');
  if (!ver || !ver.webSocketDebuggerUrl) return;
  const list = await getJson('/json/list');
  const page = (list || []).find((x) => x.type === 'page' && x.webSocketDebuggerUrl);
  if (!page) return;
  const b = new Cdp(ver.webSocketDebuggerUrl);
  await b.connect();
  try {
    const tid = page.targetId || page.id;
    const w = await b.send('Browser.getWindowForTarget', { targetId: tid }).catch(() => null);
    if (w && w.windowId !== undefined) {
      await b.send('Browser.setWindowBounds',
        { windowId: w.windowId, bounds: { windowState: 'minimized' } }).catch(() => {});
    }
  } finally { b.close(); }
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

/** ask 前确保在一个"空白新会话"（工作台一键、不污染旧会话、也避免自己给自己发）。
 *  已在空白页就直接用；否则点「新工作任务」并轮询进入空白页；没成则退回当前会话。 */
async function ensureFreshChat(cdp) {
  // 优先页面内 JS 静默点击（不抢焦、不动 OS 焦点）
  const r = await cdp.evalJs(NEW_CHAT_JS, 20000);
  if (r && r.ok) { step(r.already ? '已在空白新会话' : '已进入空白新会话'); return true; }
  // 兜底：CDP 合成鼠标事件（后台页面也能接收，无需 bringToFront）
  const tg = await cdp.evalJs(FIND_NEW_TARGET);
  if (!tg) { step('没找到「新工作任务」入口，将在当前会话发送'); return false; }
  step('JS 点击没成（' + ((r && r.why) || '?') + '），改用合成鼠标…');
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: tg.cx, y: tg.cy });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: tg.cx, y: tg.cy, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: tg.cx, y: tg.cy, button: 'left', clickCount: 1 });
  for (let i = 0; i < 14; i++) {
    await sleep(500);
    const st = await cdp.evalJs(IS_FRESH);
    if (st && st.fresh) { step('已进入空白新会话'); return true; }
  }
  step('新建会话没成功，退回当前会话发送');
  return false;
}

// ---------- 会话轨迹（结果与进度的真源） ----------
function newestTrajectoryIn(root) {
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
  walk(root, 0);
  return best;
}
function newestTrajectory() { return newestTrajectoryIn(SESSIONS); }
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
  let cdp = null;
  // 固定会话：复用锁定的 ask 会话（→回收同类→才新建隐藏窗），不再每次新建/弹新窗
  try {
    step('正在接入固定会话（不弹窗）…');
    cdp = await acquireSession('ask');
    step('固定会话已就绪');
  } catch (e) {
    log('✗ 固定会话接不上：' + ((e && e.message) || e));
    return 5;
  }
  let answer = '';
  let sid = '';
  let readCdp = null;
  try {
    const foc = await cdp.evalJs(FOCUS_COMPOSER);
    if (!foc) { log('✗ 没找到输入框（可能停在登录页/主页——先在豆包工作里进到对话页再试）'); return 5; }
    step('输入框：<' + foc.tag + '> ' + clip(foc.cls, 40));

    const st0 = await cdp.evalJs(MSG_STATE) || { sends: 0, recvs: 0 };
    const before = newestTrajectory();

    // 填字：优先走 tiptap EditorView（state 正确更新、发送按钮才会启用）；没成再降级 insertText/塞值
    const fill = await cdp.evalJs(FILL_VIA_EDITOR + '(' + JSON.stringify(prompt) + ')', 30000);
    if (!fill || !fill.ok) {
      step('tiptap 写入没成（' + ((fill && fill.why) || '?') + '），降级 insertText');
      await cdp.send('Input.insertText', { text: prompt }, 30000).catch(() => {});
      await sleep(300);
      let typed0 = await cdp.evalJs(COMPOSER_TEXT);
      if (!String(typed0 || '').trim()) {
        // 有些编辑器不吃 insertText：退回"直接塞值 + 触发 input 事件"
        await cdp.evalJs(`(() => { const el = document.activeElement;
          if (!el) return false;
          if (el.value !== undefined) { el.value = ${JSON.stringify(prompt)}; }
          else { el.innerText = ${JSON.stringify(prompt)}; }
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true; })()`);
      }
    }
    await sleep(200);
    const typed = await cdp.evalJs(COMPOSER_TEXT);
    step('已填入 ' + String(typed || '').length + ' 字');
    if (!String(typed || '').trim()) { log('✗ 文字塞不进输入框'); return 5; }

    // 提交：点发送按钮，发送后**直接用 locateSentTarget 轮询锁定**（它内部容错、会等消息真正出现，
    // 高峰期排队、以及 chat→local_xxx→正式 sid 两段路由都能覆盖）；按钮没成或锁不到再用回车兜底。
    const marker = String(prompt).trim().replace(/\s+/g, ' ').slice(0, 24);
    const pressEnter = async () => {
      const key = (type) => cdp.send('Input.dispatchKeyEvent',
        { type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
          text: type === 'char' ? '\r' : undefined });
      await cdp.evalJs(FOCUS_COMPOSER);          // 回车前重新聚焦
      await key('keyDown'); await key('char'); await key('keyUp');
    };

    // 提交：点发送按钮。后台页已开焦点模拟 + active（见 createIsolatedChat），无需拉前台、不抢焦点。
    await sleep(400);
    const sr = await cdp.evalJs(CLICK_SEND_WAIT, 20000);
    if (sr && sr.ok) step('点了发送按钮（' + sr.how + '）');
    else { step('没点到发送按钮，改用回车'); await pressEnter(); }

    // 新建的空白会话首条消息后会导航到正式 /chat/<sid>、renderer 被替换、旧 ws 失效：
    // 循环重连直到落到正式 sid；复用会话（已有 sid、不导航）第一轮即跳过。
    const _tid = cdp.targetId;
    for (let k = 0; k < 24; k++) {
      const href = await cdp.evalJs('location.href').catch(() => null);
      if (href && /\/chat\/\d+/.test(String(href))) break;
      await sleep(600);
      try { cdp.close(); } catch {}
      const rc = await attachSessionByTarget(_tid).catch(() => null);
      if (rc) cdp = rc;
    }

    if (cdp.targetId) {
      // ★ 独立窗口：webContents/target id 全程不变，**只锁定这一个连接**等消息落地，绝不扫描其它窗口。
      // 成功判据：本窗口 sends 增加，或最后一条自己发的消息含本次 marker（覆盖高峰期前 2~3 秒 sends=0 的排队）。
      let sent = false;
      // 点一次后耐心等：消息正常 2~4 秒才 sends=1（点击→提交→气泡渲染），高峰期排队更久；
      // 这段窗口绝不能再点发送按钮——重复点击会干扰/取消首次提交（2026-09-28 实测反而发不出去）。
      for (let i = 0; i < 30; i++) {
        const s = await cdp.evalJs(MSG_STATE).catch(() => null);
        if (s && (s.sends > st0.sends || (s.lastSend && s.lastSend.includes(marker.slice(0, 10))))) { sent = true; break; }
        await sleep(1000);
      }
      if (!sent) { log('✗ 没能把消息发出去（高峰期排队过久或页面变了，可稍后重试）'); return 5; }
      readCdp = cdp;                                        // 同一个连接，后续只从它读答复
      sid = String(await cdp.evalJs('location.href') || '').split('/').pop();
      step('已锁定这个后台会话');
    } else {
      // 兜底旧路径（当前窗口新建，可能跨 target 路由）：才用 locateSentTarget 多 target 扫描锁定。
      let located = await locateSentTarget(marker, 20000);
      if (!located) { step('还没确认发出，回车再等一轮…'); await pressEnter().catch(() => {}); located = await locateSentTarget(marker, 18000); }
      if (!located) { log('✗ 没能把消息发出去（高峰期排队过久或页面变了，可稍后重试）'); return 5; }
      readCdp = located.cdp;
      sid = located.sid || sid;
      step('已锁定任务会话' + (sid ? '：' + sid : ''));
    }
    // 固定会话：拿到真实 sid 就记进 state——App 重启后靠它按 sid 重开同一段对话（ask 通道）
    if (sid && /^\d+$/.test(String(sid))) {
      stateWrite(Object.assign(stateRead(), { askSid: String(sid) }));
    }
    // 认准会话落哪个 profile（多 profile 时 Default 常是旧壳）；仅在拿到数字 sid 时。
    if (sid && /^\d+$/.test(String(sid))) {
      const sd = sessionDirFor(sid);
      if (sd) {
        const root = path.dirname(sd);                 // <…>\workspace\.sessions
        if (root && root !== SESSIONS) { SESSIONS = root; step('会话目录：' + clip(root, 80)); }
      }
    }
    step('等它答复…');

    // 取答复：**以界面为准**（这一版不写 trajectory 的 assistant 行）。
    // 判定写完：出现"消耗 N 点"，或**实质答复**（非排队/思考提示）连续 STABLE_MS 不再变长。
    const STABLE_MS = Math.min(6000, Math.max(2500, (IDLE || 8) * 400));   // --idle 调"静了多久算写完"
    // 高峰期排队 / 正在思考 等"还没真正开始答"的提示：不计入答复，也不能触发写完
    const TRANSIENT = /^(高峰期[\s\S]*优先通道[\s\S]*|正在思考|思考中|排队中?|正在准备|请稍候|正在生成|生成中|加载中)[。.\s]*$/;
    let last = '';
    let lastGrow = 0;      // 首次拿到**实质**答复文本的时刻（0＝还没拿到）
    let lastProgress = 0;
    let lastHeartbeat = 0; // 排队/思考期心跳：每 10 秒报一次，动作流不空白
    // 答复内容逐段进动作流（粒度＝叙述段整条 + 每个工具步骤单独一条）
    let reportedLen = 0;      // 已解析到整条消息 innerText 的偏移
    let pendingSeg = '';      // 还没遇到换行的尾部（半段），下轮拼接
    const emitSeg = (rawSeg) => {
      let seg = String(rawSeg || '').replace(/\u00a0/g, ' ').replace(/[·•]\s*/g, '').trim();
      seg = seg.replace(/^[\s\-—–|]+/, '').trim();
      if (!seg || seg.length < 2) return;
      if (seg.length > 300) seg = seg.slice(0, 300) + '…';   // 长正文只留信号，全文看结果区
      step(seg);                                             // kind 由 server 端按关键词判
    };
    while (Date.now() - t0 < TIMEOUT * 1000) {
      await sleep(1200);
      let s = null;
      try { s = await readCdp.evalJs(MSG_STATE); } catch { /* target 重绘时偶发，下一轮再读 */ }
      if (!s) continue;
      const rawRecv = String(s.lastRecv || '').trim();
      const got = TRANSIENT.test(rawRecv) ? '' : rawRecv;   // 排队/思考提示当空，不污染答复
      if (s.busy) lastGrow = Date.now();                    // 还在忙：持续延后"写完"基线，绝不误判
      // 还没拿到实质答复：每 10 秒报心跳（带上排队/思考提示），让动作流看到它还活着
      if (!last && Date.now() - lastHeartbeat >= 10000) {
        lastHeartbeat = Date.now();
        const waited = Math.round((Date.now() - t0) / 1000);
        const hint = rawRecv ? rawRecv.replace(/[。.\s]+$/, '') : '等豆包答复';
        step(hint + '…已等待 ' + waited + 's');
      }
      if (got && !last) step('它开始答了');
      if (got && got !== last) {
        last = got;
        if (!lastGrow) lastGrow = Date.now();
        if (got.length - lastProgress >= 150) {      // 别刷屏：每长 150 字报一次
          lastProgress = got.length;
          step('正在写…（' + got.length + ' 字）');
        }
      }
      // ── 答复新增内容：按换行切出完整段（叙述段整条、工具步骤单独），半段留下轮 ──
      const full = String(s.fullRecv || '');
      if (full.length >= reportedLen) {
        const combined = pendingSeg + full.slice(reportedLen);
        reportedLen = full.length;
        const parts = combined.split('\n');
        pendingSeg = parts.pop() || '';
        for (const p of parts) emitSeg(p);
      } else { reportedLen = full.length; pendingSeg = ''; }   // 消息被重渲染（异常）→ 重置防错位
      // 写完：**不忙** + 实质文本静够 STABLE_MS；或出现“消耗 N 点”
      if (got && got === last && lastGrow && !s.busy && Date.now() - lastGrow > STABLE_MS) break;
      if (got && s.done) break;
    }
    emitSeg(pendingSeg);    // 收尾：把最后没换行的段落/结论补进动作流
    pendingSeg = '';
    if (last) answer = stripRenderBlock(String(last).replace(/^消耗\s*[\d.]+\s*点\s*/m, ''));
  } finally {
    // 固定会话保留（下次复用、上下文连续）：只断 CDP 连接、不关窗
    try { readCdp && readCdp.close(); } catch {}
    try { if (readCdp !== cdp) cdp.close(); } catch {}
  }

  if (!answer) { log('✗ 到时间没拿到答复（看上面的进度判断它卡在哪一步）'); return 4; }
  if (sid) log('· 会话 ' + sid);
  const late = Date.now() - t0 >= TIMEOUT * 1000;
  step(late ? '答复可能还没写完（已超时，先取现有的）'
            : '答完了（用时 ' + Math.round((Date.now() - t0) / 1000) + 's）');
  process.stdout.write(answer + '\n');             // stdout = 纯答复（工作台 text 模式直接取它）
  return 0;
}

// ---------- 视频生成动作（prepare / confirm / cancel） ----------
async function actVideo() {
  let prompt = PROMPT;
  if (PROMPT_FILE) {
    try { prompt = fs.readFileSync(PROMPT_FILE, 'utf8'); }
    catch (e) { log('✗ 读不到 --prompt-file：' + e.message); return 2; }
  }
  const at = await ensureAttached({ launch: !NO_LAUNCH });
  if (!at.ok) { log('✗ ' + at.why); return at.code || 2; }

  // cancel：按 sid 关闭会话
  if (VSTAGE === 'cancel') {
    const c = await attachSessionBySid(VSID, 8000, { silent: true });
    // 同一上下文：cancel 也保留会话（仅断 CDP、不关窗、不清 target），下次 prepare 直接复用
    if (c) c.close();
    console.log(JSON.stringify({ state: 'cancelled', sid: VSID }));
    return 0;
  }

  // confirm：回"确认" → 轮询成片 → 取回视频
  if (VSTAGE === 'confirm') {
    if (!VSID) { log('✗ confirm 需要 --sid'); return 2; }
    const c = await attachSessionBySid(VSID, 15000, { silent: true });
    if (!c) { log('✗ 找不到会话 ' + VSID + '（可能已被关闭）'); return 4; }
    try {
      const shortJob = Number(VDURATION) <= 6;   // 与 buildVideoRequest 的分段口径一致
      const confirmMsg = shortJob
        ? '确认，开始执行：一次性生成整条画面 → 用音色生成配音 → 对齐合成【一条】' + VRATIO + '、约' + VDURATION + '秒的成片。完成后直接把最终成片发给我（不要自检/质检报告，不用交付中间分段）。'
        : '确认，按你的执行计划开始执行：分段生成画面 → 用音色生成配音 → 逐段对齐口型、合成【一条】' + VRATIO + '、约' + VDURATION + '秒的成片。全部完成后，只把最终合成的那条成片发给我（中间的分段画面不用单独交付，也不要自检/质检报告）。';
      const f = await c.evalJs(FILL_VIA_EDITOR + '(' + JSON.stringify(confirmMsg) + ')');
      if (!f || !f.ok) await c.send('Input.insertText', { text: confirmMsg });
      const sr = await c.evalJs(CLICK_SEND_WAIT, 20000);
      step('已回确认，等待流水线执行（' + (sr && sr.ok ? '已提交' : '提交异常') + '）…');
      let src = '', candSrc = '', stable = 0, lastBeat = 0;
      const minDur = Math.max(8, Number(VDURATION) - 4);   // 最终成片时长门槛，过滤 6–8s 中间分段
      const deadline = Date.now() + Math.min(TIMEOUT, 1500) * 1000;
      while (Date.now() < deadline) {
        await sleep(4000);
        const vs = await c.evalJs(VIDEO_STATE).catch(() => null);
        if (vs) {
          if (vs.running) { candSrc = ''; stable = 0; }
          else if (vs.has && vs.ready >= 2 && vs.dur >= minDur) {
            if (vs.src === candSrc) stable++;
            else { candSrc = vs.src; stable = 0; }
            if (stable >= 2) { src = candSrc; break; }
          }
          if (Date.now() - lastBeat > 15000) { lastBeat = Date.now();
            step('流水线执行中…' + ((vs.text && vs.text.slice(-40)) || (vs.running ? '生成中' : ''))); }
        }
      }
      if (!src) { log('✗ 超时未拿到成片'); return 4; }
      const out = VOUT || path.join(os.homedir(), 'Desktop', 'heronbo_video_' + VSID + '.mp4');
      fs.mkdirSync(path.dirname(out), { recursive: true });
      step('正在取回视频…');
      const n = await downloadMedia(c, src, out);
      console.log(JSON.stringify({ state: 'done', sid: VSID, video: out, bytes: n }));
    } finally {
      // 同一上下文：成片后保留会话（仅断 CDP、不关窗、不清 target），下次 prepare 复用、上下文连续
      c.close();
    }
    return 0;
  }

  // prepare（默认）：建会话 → 传素材 → 发请求 → 读确认清单（不生成），保留会话等 confirm/cancel
  if (!prompt || !prompt.trim()) { log('✗ 缺视频提示词'); return 2; }
  if (!VMODEL || !VRATIO || !VDURATION) { log('✗ prepare 需要 --model --ratio --duration'); return 2; }
  let c = null;
  try {
    // 同一上下文：统一走固定会话（复用锁定 target → 回收同类 → 才新建隐藏窗），不弹新窗
    const _before = stateRead().videoTarget;
    c = await acquireSession('video');
    step(stateRead().videoTarget === _before ? '复用同一视频会话（上下文连续、加载快）'
                                            : '已锁定视频会话（之后都复用、不弹新窗）');
    // 路径2：不上传附件，直接给本地绝对路径让豆包自己读；提示词原样带入（保留画幅/时长等可移植依据，不剥离）
    const req = buildVideoRequest({ prompt, model: VMODEL, ratio: VRATIO, duration: VDURATION,
      image: VIMAGE ? path.resolve(VIMAGE) : '', audio: VAUDIO ? path.resolve(VAUDIO) : '',
      video: VVIDEO ? path.resolve(VVIDEO) : '', pron: pronFor(prompt) });
    const fill = await c.evalJs(FILL_VIA_EDITOR + '(' + JSON.stringify(req) + ')', 30000);
    if (!fill || !fill.ok) await c.send('Input.insertText', { text: req });
    await sleep(300);
    await c.evalJs(CLICK_SEND_WAIT, 20000);
    step('已发请求，读取确认清单…');
    const TID = c.targetId;
    try { c.close(); } catch {}     // 断开"导航前"的旧连接：首条消息后空白 chat 会导航到正式 /chat/sid、
    // renderer/document 被替换，旧 ws 停在导航前文档、MSG_STATE 永远读不到回复（2026-09-29 实测两次）。
    let text = '', idleSince = 0, sid = '';
    const readDeadline = Date.now() + Math.min(TIMEOUT, 600) * 1000;
    while (Date.now() < readDeadline) {
      await sleep(2500);
      // 每轮按 targetId 从 /json/list 重新解析最新 ws（targetId 不变、ws 指向导航后的新 renderer）
      const rc = await attachSessionByTarget(TID);
      if (!rc) continue;
      let s = null;
      try {
        s = await rc.evalJs(MSG_STATE);
        sid = String(await rc.evalJs('location.href') || '').split('/').pop();
        // 固定会话：拿到真实 sid 就记进 state——App 重启后靠它按 sid 重开同一段对话
        if (/^\d+$/.test(sid)) stateWrite(Object.assign(stateRead(), { videoSid: sid }));
      } catch { rc.close(); continue; }
      rc.close();
      if (!s) continue;
      const t = String(s.lastRecv || '').trim();
      // 无论是否在跑，都持续保留最新可见正文（running 期间也更新，避免临近超时抓空）
      if (t && t !== text) text = t;
      if (s.running) { idleSince = 0; continue; }
      // running 持续消失 6 秒（抗工具调用间隙）→ 真正结束
      if (!idleSince) idleSince = Date.now();
      if (Date.now() - idleSince >= 6000) break;
    }
    // 清掉末尾可能残留的单行状态提示
    text = (text.replace(/\n[^\n]*(正在思考|思考中|正在执行|执行代码|生成中|正在生成|读取中|分析中|排队|请稍候|加载中)[^\n]*$/, '').trim() || text);
    const out = { state: 'await_confirm', sid, confirmText: text,
      model: VMODEL, ratio: VRATIO, duration: Number(VDURATION),
      image: VIMAGE ? path.resolve(VIMAGE) : '', audio: VAUDIO ? path.resolve(VAUDIO) : '',
      video: VVIDEO ? path.resolve(VVIDEO) : '',
      form: VVIDEO ? '视频参考' : (VIMAGE ? '图生' : '文生') };
    try { c.close(); } catch {}         // 仅断开 CDP，保留会话 target，等 confirm/cancel
    const jsonOut = JSON.stringify(out, null, 1);
    if (opt('result-file')) { try { fs.writeFileSync(opt('result-file'), jsonOut, 'utf8'); } catch (e) { log('写结果文件失败：' + e.message); } }
    process.stdout.write(jsonOut + '\n');
    return 0;
  } catch (e) {
    log('✗ prepare 出错：' + ((e && e.message) || e));
    try { if (c && c.targetId) await closeTarget(c.targetId); } catch {}
    c && c.close();
    return 1;
  }
}

function killApp() {
  // ⚠️ 必须 **/F 强制 + 等到进程数归零**：只发关闭请求时单实例锁还在，
  // 再拉起来的新进程会被合并到旧实例上 → 调试端口根本不会开（2026-09-23 实测踩到）。
  step('正在结束豆包工作（你会话里的历史都在，只是进程重启）');
  try { execSync('taskkill /IM DoubaoWork.exe /F /T', { stdio: 'ignore' }); } catch {}
  for (let i = 0; i < 20; i++) {
    if (!doubaoRunning()) return true;
    execSync('cmd /c ping -n 2 127.0.0.1 >nul', { stdio: 'ignore' });
  }
  return !doubaoRunning();
}

async function main() {
  if (action === 'status') return actStatus();
  if (action === 'probe') return actProbe();
  if (action === 'ask') return actAsk();
  if (action === 'video') return actVideo();
  if (action === 'launch') {
    if (await cdpReady()) { log('· 调试端口已在：' + PORT); return 0; }
    if (doubaoRunning()) { log('✗ 豆包工作已在跑（没有调试端口）：先退出它，或用 restart --yes'); return 3; }
    const r = await launchApp();
    log(r.ok ? '· 起来了，端口 ' + PORT : '✗ ' + r.why);
    return r.ok ? 0 : 2;
  }
  if (action === 'restart') {
    if (!has('yes')) { log('✗ restart 会关掉你正在用的豆包工作，要显式加 --yes'); return 2; }
    const gone = killApp();
    if (!gone) { log('✗ 等了 40 秒它还在跑（可能被别的进程看护着）——手动退出它再试'); return 3; }
    step('已完全退出，正在带端口重起…');
    const r = await launchApp();
    log(r.ok ? '· 已用调试端口重启（端口 ' + PORT + '）' : '✗ ' + r.why);
    return r.ok ? 0 : 2;
  }
  log('用法：status | probe | launch | ask --prompt-file <f> | restart --yes');
  return 2;
}

main().then((code) => process.exit(code)).catch((e) => { log('✗ 出错了：' + (e && e.stack ? e.stack : e)); process.exit(1); });
