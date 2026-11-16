const path = require('node:path');

/**
 * 直接从豆包工作客户端里抓"某段会话最新生成的那张图/那条视频"。
 *
 * 为什么不用桥接的 attachSessionByTarget：那条路径在"不是自己开的标签页"上
 * 经常读不到内容（连接能建、eval 却拿不到消息）。这里用最笨但可靠的方式：
 * 自己连 CDP、自己读 DOM、自己在 Node 侧下载。
 */
function cdpEval(wsUrl, expression, timeoutMs = 8000) {
  return new Promise((resolve) => {
    let ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch {
      resolve(null);
      return;
    }
    const timer = setTimeout(() => {
      try { ws.close(); } catch { /* ignore */ }
      resolve(null);
    }, timeoutMs);
    ws.addEventListener('open', () => {
      ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { returnByValue: true, awaitPromise: true, expression } }));
    });
    ws.addEventListener('message', (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      if (msg.id !== 1) return;
      clearTimeout(timer);
      try { ws.close(); } catch { /* ignore */ }
      resolve(msg.result && msg.result.result ? msg.result.result.value : null);
    });
    ws.addEventListener('error', () => {
      clearTimeout(timer);
      resolve(null);
    });
  });
}

const MEDIA_EXPR = `(() => {
  const deep = (root, out) => { try { for (const el of root.querySelectorAll('*')) { out.push(el); if (el.shadowRoot) deep(el.shadowRoot, out); } } catch (e) {} return out; };
  const all = deep(document.body, []);
  const imgs = [];
  const vids = [];
  for (const el of all) {
    if (el.tagName === 'IMG') {
      const src = el.currentSrc || el.src || '';
      const w = el.naturalWidth || 0;
      const h = el.naturalHeight || 0;
      if (src && /^(blob:|https?:)/.test(src) && !/avatar|emoji|icon|logo/i.test(src) && w >= 256) imgs.push({ src, w, h });
    } else if (el.tagName === 'VIDEO') {
      const src = el.currentSrc || el.src || '';
      if (src) vids.push({ src, w: el.videoWidth || 0, h: el.videoHeight || 0, duration: el.duration || 0 });
    }
  }
  const uniq = (arr) => { const m = new Map(); for (const x of arr) if (x.src && !m.has(x.src)) m.set(x.src, x); return [...m.values()]; };
  return { images: uniq(imgs), videos: uniq(vids), href: location.href };
})()`;

async function listPages(port) {
  const res = await fetch(`http://127.0.0.1:${port}/json/list`);
  const list = await res.json();
  return (Array.isArray(list) ? list : []).filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
}

function pickPages(pages, sid) {
  return sid
    ? pages.filter((p) => String(p.url || '').indexOf('chat/' + sid) >= 0)
    : pages.filter((p) => /chat\/\d+/.test(String(p.url || '')));
}

/**
 * 生成前先拍一张"基线"：把当前所有会话页里已有的媒体 src 记下来。
 * 之后只要出现基线里没有的 src，就是这次新生成的，跟会话 id 是否已知无关。
 */
async function baselineSrcs({ port = 9333, sid = '' } = {}) {
  const pages = pickPages(await listPages(port), sid);
  const seen = new Set();
  for (const page of pages) {
    const state = await cdpEval(page.webSocketDebuggerUrl, MEDIA_EXPR).catch(() => null);
    if (!state) continue;
    for (const item of [...(state.images || []), ...(state.videos || [])]) if (item.src) seen.add(item.src);
  }
  return seen;
}

async function grabLatest({ port = 9333, sid = '', kind = 'image', exclude = null } = {}) {
  const pages = pickPages(await listPages(port), sid);
  const found = [];
  for (const page of pages) {
    const state = await cdpEval(page.webSocketDebuggerUrl, MEDIA_EXPR).catch(() => null);
    if (!state) continue;
    const raw = (kind === 'video' ? state.videos : state.images) || [];
    const list = exclude ? raw.filter((item) => item.src && !exclude.has(item.src)) : raw;
    if (list.length) found.push({ page, list, href: state.href || '' });
  }
  if (!found.length) return [];
  // 同一段会话可能开了多个标签页：取图片最多的那一页（渲染最完整）。
  found.sort((a, b) => b.list.length - a.list.length);
  const list = found[0].list.slice(-4);
  const chatSid = (String(found[0].page.url || '').match(/chat\/(\d+)/) || [])[1] || '';
  const out = [];
  for (const item of list) {
    try {
      const res = await fetch(item.src);
      if (!res.ok) continue;
      const buffer = Buffer.from(await res.arrayBuffer());
      const mime = String(res.headers.get('content-type') || '').split(';')[0] || (kind === 'video' ? 'video/mp4' : 'image/png');
      out.push({ buffer, mime, w: item.w || 0, h: item.h || 0, duration: item.duration || 0, url: item.src, chatSid });
    } catch { /* 单张失败跳过 */ }
  }
  return out;
}

module.exports = { grabLatest, baselineSrcs };
