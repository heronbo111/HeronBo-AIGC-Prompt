'use strict';

/**
 * 内置豆包画布的 CDP 读取/选中适配器。
 *
 * 这里只做三件事：
 * 1. 找到聊天主页面 target（内置画布就渲染在这个 document 里，不是 iframe）；
 * 2. 用 Runtime.evaluate 读 React Flow 的 DOM 快照；
 * 3. 把快照交给 node-model.js 归一成 CanonicalNode。
 *
 * 不做：生成提交、上传、复制粘贴、层级调节。那些动作需要先在 node-model 的
 * 动作计划里过一遍，再由执行器逐条翻译成 CDP 输入事件。
 */

const { normalizeBuiltinNode } = require('./node-model.js');

const DEFAULT_PORT = Number(process.env.DOUBAO_CDP_PORT) || 9333;
const CHAT_NEEDLE = 'doubaowork-chat';
let activeConversationId = '';

/** 让后续 CDP 操作优先落到指定会话页；多开标签页时避免串到别的会话。 */
function setActiveConversation(conversationId) {
  activeConversationId = String(conversationId || '').trim();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function listTargets(port = DEFAULT_PORT) {
  const response = await fetch(`http://127.0.0.1:${port}/json/list`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`CDP /json/list ${response.status}`);
  return response.json();
}

async function pickChatTarget(port = DEFAULT_PORT) {
  const targets = await listTargets(port);
  const pages = targets.filter((target) => target.type === 'page' && target.webSocketDebuggerUrl);
  const chat = (activeConversationId
    ? pages.find((target) => String(target.url || '').includes(CHAT_NEEDLE) && String(target.url || '').includes(activeConversationId))
    : null)
    || pages.find((target) => String(target.url || '').includes(CHAT_NEEDLE));
  if (!chat) {
    throw new Error(`没有找到 ${CHAT_NEEDLE} 页面；当前 targets: ${pages.map((page) => page.url).join(' | ') || '空'}`);
  }
  const sameChat = pages.filter((target) => String(target.url || '').includes(CHAT_NEEDLE)
    && (!activeConversationId || String(target.url || '').includes(activeConversationId)));
  if (sameChat.length <= 1) return chat;
  // 同一会话可能同时存在多个 CDP page：新开/恢复失败的页面会是 41 字空壳，
  // 而真正挂着画布的页面 panelWidth≈968 且存在 .react-flow__viewport。
  // 只看 URL 会稳定选到空壳，表现为 no-flow；这里按页面实际内容打分选最健康的一个。
  const scored = await Promise.all(sameChat.map(async (target) => {
    let score = 0;
    let info = null;
    try {
      const connection = await connect(target.webSocketDebuggerUrl);
      const raw = await evaluate(connection, `(() => {
        const panel = document.querySelector('[data-testid="right-panel-container"]');
        return JSON.stringify({
          ready: document.readyState,
          bodyLen: document.body ? document.body.innerText.length : 0,
          panelWidth: panel ? Math.round(panel.getBoundingClientRect().width) : 0,
          flows: document.querySelectorAll('.react-flow__viewport').length,
          rf: document.querySelectorAll('[data-testid="rf__wrapper"]').length,
        });
      })()`);
      connection.close();
      info = JSON.parse(raw || '{}');
      if (info.ready === 'complete') score += 10;
      if (info.panelWidth > 0) score += 10;
      if (info.panelWidth >= 620) score += 50;
      if (info.bodyLen > 1000) score += 20;
      if (info.flows > 0) score += 100;
      score += Math.min(5, Number(info.flows) || 0) * 10;
    } catch (error) {
      score = -1;
    }
    return { target, score, info };
  }));
  scored.sort((a, b) => b.score - a.score);
  return (scored[0] && scored[0].target) || chat;
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let nextId = 1;
    const timer = setTimeout(() => reject(new Error('CDP WebSocket 连接超时')), 5000);

    function send(method, params = {}) {
      return new Promise((resolveCommand, rejectCommand) => {
        const id = nextId++;
        pending.set(id, { resolve: resolveCommand, reject: rejectCommand });
        ws.send(JSON.stringify({ id, method, params }));
      });
    }

    ws.addEventListener('open', () => {
      clearTimeout(timer);
      resolve({ ws, send, close: () => ws.close() });
    });
    ws.addEventListener('error', (event) => {
      clearTimeout(timer);
      reject(new Error(`CDP WebSocket 错误: ${event.message || 'unknown'}`));
    });
    ws.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!message.id || !pending.has(message.id)) return;
      const waiter = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error.message || 'CDP 命令失败'));
      else waiter.resolve(message.result);
    });
    ws.addEventListener('close', () => {
      for (const waiter of pending.values()) waiter.reject(new Error('CDP 连接已关闭'));
      pending.clear();
    });
  });
}

async function withCdp(fn, port = DEFAULT_PORT) {
  const target = await pickChatTarget(port);
  const connection = await connect(target.webSocketDebuggerUrl);
  try {
    return await fn(connection, target);
  } finally {
    connection.close();
  }
}

async function evaluate(connection, expression) {
  const result = await connection.send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    const detail = result.exceptionDetails.exception || result.exceptionDetails;
    throw new Error(detail.description || detail.text || 'Runtime.evaluate 抛错');
  }
  return result.result ? result.result.value : undefined;
}

/** 读内置画布的原始 DOM 快照。返回 JSON 字符串，避免 CDP 不能按值返回大对象。 */
const SNAPSHOT_EXPR = `(() => {
  const panel = document.querySelector('[data-testid="right-panel-container"]');
  const flow = document.querySelector('[data-testid="rf__wrapper"]') || (panel && panel.querySelector('.react-flow'));
  if (!flow) return JSON.stringify({ ok: false, error: 'no-flow', panel: Boolean(panel) });
  const panelRect = panel ? panel.getBoundingClientRect() : null;
  const flowRect = flow.getBoundingClientRect();
  if (!flowRect.width || !flowRect.height) {
    return JSON.stringify({ ok: false, error: 'canvas-hidden', panelRect, flowRect });
  }
  const viewport = flow.querySelector('.react-flow__viewport');
  const viewportTransform = viewport ? (viewport.style.transform || '') : '';
  const nodes = Array.from(flow.querySelectorAll('.react-flow__node.react-flow__node-canvas-node')).map((node) => {
    const rect = node.getBoundingClientRect();
    const resourceEl = node.querySelector('[data-canvas-open-resource-id]');
    const contentEl = node.querySelector('[data-canvas-node-content]') || resourceEl;
    const resourceId = resourceEl ? (resourceEl.getAttribute('data-canvas-open-resource-id') || '') : '';
    const generationState = resourceEl ? (resourceEl.getAttribute('data-canvas-generation-state') || '') : '';
    const generationVersion = resourceEl ? Number(resourceEl.getAttribute('data-canvas-generation-version')) || 0 : 0;
    const style = node.style || {};
    const frameW = parseFloat(style.width) || rect.width;
    const frameH = parseFloat(style.height) || rect.height;
    const transform = style.transform || '';
    const match = transform.match(/translate\\(\\s*(-?[\\d.]+)px,\\s*(-?[\\d.]+)px/);
    const flowPosition = match ? { x: Number(match[1]), y: Number(match[2]) } : { x: 0, y: 0 };
    const titleEl = node.querySelector('[title="双击编辑标题"]');
    const title = titleEl ? (titleEl.textContent || '').trim().slice(0, 60) : '';
    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 2000);
    const media = Array.from(node.querySelectorAll('img, video, audio')).map((element) => {
      const tag = element.tagName.toLowerCase();
      const mediaRect = element.getBoundingClientRect();
      return {
        tag,
        src: element.currentSrc || element.src || '',
        poster: tag === 'video' ? (element.poster || '') : '',
        width: Math.round(mediaRect.width) || element.naturalWidth || element.videoWidth || 0,
        height: Math.round(mediaRect.height) || element.naturalHeight || element.videoHeight || 0,
        duration: tag === 'video' || tag === 'audio' ? Number(element.duration) || 0 : 0,
      };
    });
    return {
      id: node.getAttribute('data-id') || (node.getAttribute('data-testid') || '').replace(/^rf__node-/, ''),
      nodeType: (node.className || '').split(' ').filter(Boolean).slice(0, 4).join(' '),
      resourceId,
      generationState,
      generationVersion,
      contentText: contentEl ? (contentEl.getAttribute('data-canvas-open-resource-state') || '') : '',
      frameSize: { w: Math.round(frameW), h: Math.round(frameH) },
      flowPosition,
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      transform,
      zIndex: style.zIndex || '',
      selected: node.classList.contains('selected'),
      title,
      text,
      media,
    };
  });
  return JSON.stringify({
    ok: true,
    href: location.href,
    title: document.title,
    panelRect: panelRect ? { x: Math.round(panelRect.x), y: Math.round(panelRect.y), w: Math.round(panelRect.width), h: Math.round(panelRect.height) } : null,
    flowRect: { x: Math.round(flowRect.x), y: Math.round(flowRect.y), w: Math.round(flowRect.width), h: Math.round(flowRect.height) },
    viewportTransform,
    zoom: parseFloat((viewportTransform.match(/scale\\(([\\d.]+)\\)/) || [])[1]) || 1,
    nodes,
    capturedAt: Date.now(),
  });
})()`;

async function readBuiltinContext(port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, 'JSON.stringify({ href: location.href, title: document.title })');
    if (typeof raw !== 'string') throw new Error('上下文没有返回 JSON 字符串');
    try { return JSON.parse(raw); } catch { return { href: '', title: '' }; }
  }, port);
}

async function readBuiltinState(port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    if (typeof raw !== 'string') throw new Error('快照没有返回 JSON 字符串');
    const state = JSON.parse(raw);
    if (!state.ok) {
      const error = new Error(state.error || '内置画布不可读');
      error.code = state.error || 'bridge-read-failed';
      error.state = state;
      throw error;
    }
    return state;
  }, port);
}

async function readBuiltinModel(port = DEFAULT_PORT, context = {}) {
  const state = await readBuiltinState(port);
  const nodes = [];
  const errors = [];
  for (const snapshot of state.nodes) {
    const normalized = normalizeBuiltinNode(snapshot, context);
    if (normalized.ok) nodes.push(normalized.node);
    else errors.push({ id: snapshot.id, errors: normalized.errors });
  }
  return { ...state, model: { nodes, errors } };
}

/**
 * 通过右侧工作台自己的 tab service 打开画布。
 *
 * 这条链路不依赖 CanvasService 的 Symbol token，也不依赖 chatViewService：
 * 右侧工作台有一个 RightPanelTabService，它的 open({ pluginId, payload })
 * 会直接按插件注册表创建 / 复用 tab。已实测：pluginId='infinite-canvas'
 * + payload.infiniteCanvas.canvasId 能把内置画布召回到右工作台。
 */
const OPEN_BY_TAB_SERVICE_EXPR = `(async () => {
  const panel = document.querySelector('[data-testid="right-panel-container"]');
  if (!panel) return JSON.stringify({ ok: false, error: 'no-right-panel' });
  const key = Object.keys(panel).find((k) => k.startsWith('__reactFiber$'));
  if (!key) return JSON.stringify({ ok: false, error: 'no-right-panel-fiber' });
  let svc = window.__RIGHT_PANEL_SERVICE__;
  let f = panel[key];
  for (let i = 0; f && i < 40 && !svc; i++, f = f.return) {
    const props = f.memoizedProps;
    if (props && props.service && typeof props.service.open === 'function') svc = props.service;
  }
  if (!svc) return JSON.stringify({ ok: false, error: 'tab-service-unavailable' });
  let snapshot = null;
  try { snapshot = svc.getSnapshot(); } catch (error) {}
  const scopeId = __SCOPE_ID__ || (snapshot && snapshot.activeScopeId) || '';
  if (!scopeId) return JSON.stringify({ ok: false, error: 'active-scope-missing' });
  try {
    if (typeof svc.setVisible === 'function') {
      try { svc.setVisible(true, { preferredMode: 'docked' }); } catch (error) {}
    }
    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const canvasId = __CANVAS_ID__;
    const instanceId = 'infinite-canvas:' + canvasId;
    try {
      if (typeof svc.setActiveScope === 'function') await svc.setActiveScope(scopeId);
    } catch (error) {}
    const payload = {
      instanceId,
      pluginIdentifier: 'infinite-canvas',
      title: __TITLE__,
      // 关键：不要传 icon:null。s2 的 eL 校验只接受 icon 为 undefined 或 string。
      canvasPayload: {
        canvasId,
        conversationId: __CONVERSATION_ID__,
        title: __TITLE__,
        canvasLayoutState: 'docked',
      },
    };
    const openPromise = Promise.resolve(svc.open({
      pluginId: 'canvas',
      scopeId,
      activate: true,
      reuse: 'update',
      instanceKey: JSON.stringify(['infinite-canvas', instanceId]),
      payload,
      title: __TITLE__,
    }));
    const openResult = await Promise.race([
      openPromise,
      new Promise((resolve) => setTimeout(() => resolve({ result: false, reason: 'open-timeout-3s' }), 3000)),
    ]);
    // tab 的实际创建是异步的；open 的 result 可能先返回 false，但 tab 随后出现。
    // 以快照里是否真的存在带 canvasId 的 canvas tab 为准，而不是只信 result。
    await wait(1400);
    let after = null;
    try { after = svc.getSnapshot(); } catch (error) {}
    const tabs = Object.values((after && after.tabs) || {}).filter((tab) => {
      if (!tab || tab.pluginId !== 'canvas') return false;
      return String(tab.instanceKey || '').includes(canvasId);
    });
    const tab = tabs[0] || null;
    if (tab && typeof svc.setActiveScope === 'function') {
      try { await svc.setActiveScope(scopeId); } catch (error) {}
    }
    if (tab && typeof svc.activate === 'function') {
      try { await svc.activate(tab.id); } catch (error) {}
      await wait(900);
    }
    return JSON.stringify({
      ok: Boolean(tab),
      scopeId,
      beforeTabs: snapshot && snapshot.tabs ? Object.keys(snapshot.tabs).length : 0,
      result: openResult || null,
      tabId: tab ? tab.id : null,
      tabPhase: tab ? tab.phase : null,
      tabs: tabs.map((item) => ({ id: item.id, phase: item.phase, instanceKey: item.instanceKey })),
      error: tab ? undefined : 'tab-not-created',
    });
  } catch (error) {
    return JSON.stringify({ ok: false, error: 'tab-service-open-failed', detail: String(error) });
  }
})()`;

/**
 * 对话产物 → 画布：用户在会话里点过的那条画布产物。
 *
 * 这是官方 UI 的等价路径：顶部「对话摘要」打开对话产物浮层，浮层里的
 * 「豆包无限画布项目交接」条目被点击后，由客户端自己的 CanvasService
 * 打开画布。它不依赖 tab service 的插件注册表，也不依赖 Symbol token。
 */
/**
 * 激活已经存在的画布 tab：画布 tab 在快照里、但另一个会话的 scope 正处于
 * activeScopeId 时，setVisible / open 都可能看不见它。必须先把右侧面板切回
 * 该 tab 的 scope，再 activate(tabId)，最后等一帧让 React Flow 重新测量。
 */
const ACTIVATE_EXISTING_CANVAS_EXPR = `(async () => {
  const panel = document.querySelector('[data-testid="right-panel-container"]');
  if (!panel) return JSON.stringify({ ok: false, error: 'no-right-panel' });
  const key = Object.keys(panel).find((k) => k.startsWith('__reactFiber$'));
  if (!key) return JSON.stringify({ ok: false, error: 'no-right-panel-fiber' });
  let svc = window.__RIGHT_PANEL_SERVICE__;
  let f = panel[key];
  for (let i = 0; f && i < 40 && !svc; i++, f = f.return) {
    const props = f.memoizedProps;
    if (props && props.service && typeof props.service.open === 'function') svc = props.service;
  }
  if (!svc) return JSON.stringify({ ok: false, error: 'tab-service-unavailable' });
  const snapshot = svc.getSnapshot();
  const tabs = Object.values(snapshot.tabs || {}).filter((tab) => tab && tab.pluginId === 'canvas');
  const tab = tabs.find((item) => String(item.instanceKey || '').includes(__CANVAS_ID__)) || null;
  if (!tab) return JSON.stringify({ ok: false, error: 'no-existing-canvas-tab', activeScopeId: snapshot.activeScopeId });
  let scopeResult = null;
  try {
    if (typeof svc.setActiveScope === 'function') scopeResult = await svc.setActiveScope(tab.scopeId);
  } catch (error) { scopeResult = { error: String(error) }; }
  let activateResult = null;
  try {
    activateResult = await svc.activate(tab.id);
  } catch (error) { activateResult = { error: String(error) }; }
  await new Promise((resolve) => setTimeout(resolve, 700));
  let after = null;
  try { after = svc.getSnapshot(); } catch (error) {}
  return JSON.stringify({
    ok: true,
    via: 'activate',
    scopeId: tab.scopeId,
    tabId: tab.id,
    title: tab.title || '',
    scopeResult,
    activateResult,
    activeScopeId: after && after.activeScopeId,
    phase: after && after.tabs && after.tabs[tab.id] ? after.tabs[tab.id].phase : null,
  });
})()`;

const OPEN_BY_ARTIFACT_EXPR = `(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const docTitle = String((document.title || '').split(' - ')[0] || '').trim();
  const preferred = String(__TITLE__ || '').trim();
  const needles = [preferred, docTitle].filter((value, index, array) => value && array.indexOf(value) === index);
  if (!needles.length) return JSON.stringify({ ok: false, error: 'artifact-title-missing' });
  function candidates(needle) {
    const out = [];
    for (const el of document.querySelectorAll('div, button, a, [role="button"]')) {
      if ((el.textContent || '').replace(/\\s+/g, ' ').trim() !== needle) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 50 || r.height < 16 || r.width > 420) continue;
      if (r.x < 500 || r.y < 50 || r.y > 400) continue;
      out.push({ el, area: r.width * r.height, rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } });
    }
    return out.sort((a, b) => a.area - b.area);
  }
  let list = [];
  let usedNeedle = needles[0];
  for (const needle of needles) {
    list = candidates(needle);
    if (list.length) { usedNeedle = needle; break; }
  }
  if (!list.length) {
    const summary = Array.from(document.querySelectorAll('button')).find((b) => {
      const aria = b.getAttribute('aria-label') || '';
      return aria === '对话摘要' || aria === '收起摘要';
    });
    // 「收起摘要」说明浮层已经开着，不用再点；否则点开。
    if (summary && summary.getAttribute('aria-label') === '对话摘要') summary.click();
    for (let attempt = 0; attempt < 30 && !list.length; attempt += 1) {
      await wait(300);
      for (const needle of needles) {
        list = candidates(needle);
        if (list.length) { usedNeedle = needle; break; }
      }
    }
  }
  if (!list.length) return JSON.stringify({ ok: false, error: 'artifact-not-found', needle: needles[0], docTitle });
  const item = list[0];
  const clickable = item.el.closest('button, a, [role="button"], [tabindex]') || item.el;
  try {
    clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  } catch (error) {
    try { clickable.click(); } catch (inner) { return JSON.stringify({ ok: false, error: 'artifact-click-failed', detail: String(inner) }); }
  }
  return JSON.stringify({ ok: true, needle: usedNeedle, rect: item.rect, tag: item.el.tagName.toLowerCase() });
})()`;

function jsString(value) {
  return JSON.stringify(String(value == null ? '' : value));
}

async function openBuiltinCanvas(options = {}, port = DEFAULT_PORT) {
  const canvasId = String(options.canvasId || '').trim();
  const conversationId = String(options.conversationId || '').trim();
  if (!canvasId) throw new Error('openBuiltinCanvas 缺少 canvasId');
  if (!conversationId) throw new Error('openBuiltinCanvas 缺少 conversationId');
  const scopeId = String(options.scopeId || conversationId);
  const title = String(options.title || '').trim();
  let navigation = null;
  if (options.autoNavigate !== false) {
    try {
      navigation = await withCdp(async (connection) => {
        const currentRaw = await evaluate(connection, 'JSON.stringify({ href: location.href, title: document.title })');
        const current = JSON.parse(currentRaw);
        if (String(current.href || '').includes(conversationId)) {
          return { ok: true, navigated: false, href: current.href, title: current.title };
        }
        const target = `chrome://doubaowork-chat/chat/${conversationId}`;
        await connection.send('Page.navigate', { url: target });
        await sleep(2600);
        const afterRaw = await evaluate(connection, 'JSON.stringify({ href: location.href, title: document.title })');
        return { ok: true, navigated: true, target, after: JSON.parse(afterRaw) };
      }, port);
    } catch (error) {
      navigation = { ok: false, error: String(error) };
    }
  }
  const activateExpression = ACTIVATE_EXISTING_CANVAS_EXPR.replace('__CANVAS_ID__', jsString(canvasId));
  let activated = null;
  try {
    activated = await withCdp(async (connection) => {
      const raw = await evaluate(connection, activateExpression);
      if (typeof raw !== 'string') throw new Error('激活画布没有返回 JSON 字符串');
      return JSON.parse(raw);
    }, port);
  } catch (error) {
    activated = { ok: false, error: String(error) };
  }
  if (activated && activated.ok) {
    return { ...activated, via: 'activate', navigation };
  }
  const expression = OPEN_BY_TAB_SERVICE_EXPR
    .replace('__SCOPE_ID__', jsString(scopeId))
    .replace('__CANVAS_ID__', jsString(canvasId))
    .replace('__CONVERSATION_ID__', jsString(conversationId))
    .replace('__TITLE__', jsString(title));
  let tabResult = null;
  let tabError = null;
  try {
    tabResult = await withCdp(async (connection) => {
      const raw = await evaluate(connection, expression);
      if (typeof raw !== 'string') throw new Error('打开画布没有返回 JSON 字符串');
      return JSON.parse(raw);
    }, port);
  } catch (error) {
    tabError = error;
  }
  if (tabResult && tabResult.ok) {
    return { ...tabResult, via: 'tab-service', navigation };
  }
  const artifactExpression = OPEN_BY_ARTIFACT_EXPR
    .replace('__TITLE__', jsString(title));
  let artifact = null;
  let artifactError = null;
  try {
    artifact = await withCdp(async (connection) => {
      const raw = await evaluate(connection, artifactExpression);
      if (typeof raw !== 'string') throw new Error('打开画布产物没有返回 JSON 字符串');
      return JSON.parse(raw);
    }, port);
  } catch (error) {
    artifactError = error;
  }
  if (artifact && artifact.ok) {
    return { ...artifact, via: 'artifact', tabService: tabResult || null, navigation };
  }
  const error = new Error((artifact && artifact.error) || (tabResult && tabResult.error) || '打开内置画布失败');
  error.code = (artifact && artifact.error) || (tabResult && tabResult.error) || 'bridge-open-failed';
  error.detail = (artifact && artifact.detail) || (tabResult && tabResult.detail) || '';
  error.open = { tabResult, artifact, tabError: tabError && String(tabError), artifactError: artifactError && String(artifactError) };
  throw error;
}

/**
 * 右侧工作台被收成图标条时，点「全屏显示」把画布撑开。
 * 只做布局恢复，不动节点。
 */
async function expandBuiltinPanel(port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const result = await evaluate(connection, `(() => {
      const panel = document.querySelector('[data-testid="right-panel-container"]');
      const button = document.querySelector('[data-testid="right-panel-fullpage-toggle"]');
      if (!button) return { ok: false, reason: 'no-fullpage-button', panelWidth: panel ? Math.round(panel.getBoundingClientRect().width) : 0 };
      const before = panel ? Math.round(panel.getBoundingClientRect().width) : 0;
      button.click();
      return { ok: true, before, tag: button.tagName };
    })()`);
    await sleep(700);
    return result;
  }, port);
}

/**
 * 读取；如果画布不在场，就先召唤再读。
 * 返回结构：{ opened: boolean, open: object|null, state, model }。
 */
async function ensureBuiltinCanvas(options = {}, port = DEFAULT_PORT) {
  if (options.conversationId) setActiveConversation(options.conversationId);
  let state = null;
  let expanded = false;
  // 客户端可能在别的会话页前台；先按会话 id 把 CDP 目标页导航到正确会话，
  // 否则会对着另一个会话的画布建节点、填提示词，结果全跑到看不见的地方。
  try {
    const context = await readBuiltinContext(port);
    if (options.conversationId && context && context.href && !String(context.href).includes(options.conversationId)) {
      await openBuiltinCanvas(options, port).catch(() => null);
    }
  } catch (contextError) { /* 页面还没就绪时继续按原路径召回 */ }
  try {
    state = await readBuiltinState(port);
  } catch (error) {
    if (error.code !== 'no-flow' && error.code !== 'canvas-hidden') throw error;
  }
  let open = null;
  if (!state) {
    // 右侧工作台三种状态：0=完全关闭、~220=收起成图标条、~968=正常停靠。
    // 0/220 都先点「打开侧边工作台」恢复停靠；没有这个按钮再退回「全屏显示」。
    try {
      const measurePanelWidth = () => withCdp(async (connection) => {
        const raw = await evaluate(connection, `(() => {
          const panel = document.querySelector('[data-testid="right-panel-container"]');
          return panel ? Math.round(panel.getBoundingClientRect().width) : 0;
        })()`);
        return Number(raw) || 0;
      }, port);
      let panelWidth = await measurePanelWidth();
      if (panelWidth < 320) {
        const clickedDock = await withCdp(async (connection) => {
          const raw = await evaluate(connection, `(() => {
            const button = document.querySelector('button[aria-label="打开侧边工作台"]');
            if (!button) return false;
            button.click();
            return true;
          })()`);
          return Boolean(raw);
        }, port).catch(() => false);
        if (clickedDock) {
          await sleep(900);
          panelWidth = await measurePanelWidth();
        }
        if (panelWidth > 0 && panelWidth < 320) {
          try { await expandBuiltinPanel(port); } catch (expandError) { /* 面板可能已被用户展开 */ }
        }
      }
    } catch (widthError) { /* 面板不存在时继续按原路径召回 */ }
    // 重载后摘要浮层/产物列表是分批渲染的；首次找不到时隔一会儿再走一遍。
    // activate 可能撞上 tab 还在恢复、右侧面板 scope 尚未切回，单次重试不够。
    let openError = null;
    for (let attempt = 0; attempt < 5 && !open; attempt += 1) {
      try {
        open = await openBuiltinCanvas(options, port);
      } catch (error) {
        openError = error;
        if (error.code !== 'artifact-not-found' && error.code !== 'canvas-open-timeout') throw error;
        await sleep(1800);
      }
    }
    if (!open && openError) throw openError;
    // 冷启动/客户端刚重启时，内置画布要 6~8 秒才挂上 React Flow；
    // 旧版只等 5 秒（10×500ms），会把「还在加载」误判成 canvas-open-timeout。
    for (let attempt = 0; attempt < 40 && !state; attempt += 1) {
      await sleep(500);
      try {
        state = await readBuiltinState(port);
      } catch (error) {
        if (error.code !== 'no-flow' && error.code !== 'canvas-hidden') throw error;
        if (error.code === 'canvas-hidden' && !expanded) {
          expanded = true;
          try { await expandBuiltinPanel(port); } catch (expandError) { /* 面板可能已被用户展开 */ }
        }
      }
    }
    if (!state) {
      const error = new Error('已调用右侧工作台 open，但内置画布仍未出现');
      error.code = 'canvas-open-timeout';
      error.open = open;
      throw error;
    }
  }
  // 220px 图标条里 flow 也存在，但生成条/工具栏不可用；等停靠动画结束（通常 <2s）。
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (!state || !state.flowRect || state.flowRect.w >= 320) break;
    await sleep(400);
    try {
      state = await readBuiltinState(port);
    } catch (error) {
      if (error.code !== 'no-flow' && error.code !== 'canvas-hidden') throw error;
    }
  }
  const model = await readBuiltinModel(port, options.context || {});
  return { opened: Boolean(open), open, state, model };
}

function clickAt(connection, x, y, { button = 'left', count = 1 } = {}) {
  return (async () => {
    await connection.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', clickCount: 0 });
    await sleep(40);
    for (let index = 1; index <= Math.max(1, count); index += 1) {
      await connection.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button, clickCount: index });
      await sleep(60);
      await connection.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button, clickCount: index });
      if (index < count) await sleep(30);
    }
    await sleep(420);
  })();
}

/**
 * 把内置画布右工作台截成一张图，供本地前端做「套壳」实时镜像。
 * 只截右侧面板（包含 tab / 内置工具栏 / React Flow），这样镜像里的坐标
 * 可以直接按 panelRect 归一化回真实屏幕坐标。
 */
async function captureBuiltinScreenshot(options = {}, port = DEFAULT_PORT) {
  const quality = Math.min(90, Math.max(30, Number(options.quality) || 72));
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    if (!state.panelRect || !state.panelRect.w || !state.panelRect.h) throw new Error('panel-rect-unavailable');
    const shot = await connection.send('Page.captureScreenshot', {
      format: 'jpeg',
      quality,
      fromSurface: true,
      clip: {
        x: state.panelRect.x,
        y: state.panelRect.y,
        width: state.panelRect.w,
        height: state.panelRect.h,
        scale: 1,
      },
    });
    return {
      ok: true,
      dataUrl: 'data:image/jpeg;base64,' + shot.data,
      panelRect: state.panelRect,
      flowRect: state.flowRect,
      nodeCount: state.nodes.length,
      href: state.href,
      title: state.title,
      capturedAt: Date.now(),
    };
  }, port);
}

function panelPoint(state, nx, ny) {
  const panel = state.panelRect;
  if (!panel || !panel.w || !panel.h) throw new Error('panel-rect-unavailable');
  const xRatio = Number(nx);
  const yRatio = Number(ny);
  if (!Number.isFinite(xRatio) || !Number.isFinite(yRatio) || xRatio < 0 || xRatio > 1 || yRatio < 0 || yRatio > 1) {
    throw new Error('浏览器窗口坐标必须是 0~1 的归一化值');
  }
  return {
    x: Math.round(panel.x + xRatio * panel.w),
    y: Math.round(panel.y + yRatio * panel.h),
  };
}

async function clickBuiltinNormalized(nx, ny, options = {}, port = DEFAULT_PORT) {
  const button = options.button === 'right' ? 'right' : 'left';
  const count = Math.min(2, Math.max(1, Number(options.count) || 1));
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const point = panelPoint(state, nx, ny);
    await clickAt(connection, point.x, point.y, { button, count });
    return { ok: true, button, count, point, panelRect: state.panelRect };
  }, port);
}

async function wheelBuiltinNormalized(nx, ny, deltaY = 120, port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const point = panelPoint(state, nx, ny);
    await connection.send('Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x: point.x,
      y: point.y,
      deltaX: 0,
      deltaY: Number(deltaY) || 0,
      button: 'none',
      clickCount: 0,
    });
    await sleep(200);
    return { ok: true, point, panelRect: state.panelRect };
  }, port);
}

async function dragBuiltinNormalized(from, to, port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const start = panelPoint(state, from && from.nx, from && from.ny);
    const end = panelPoint(state, to && to.nx, to && to.ny);
    await connection.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: start.x, y: start.y, button: 'none', clickCount: 0 });
    await sleep(40);
    await connection.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: start.x, y: start.y, button: 'left', clickCount: 1 });
    await sleep(60);
    for (let step = 1; step <= 8; step += 1) {
      const x = start.x + ((end.x - start.x) * step) / 8;
      const y = start.y + ((end.y - start.y) * step) / 8;
      await connection.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'left', clickCount: 1 });
      await sleep(30);
    }
    await connection.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: end.x, y: end.y, button: 'left', clickCount: 1 });
    await sleep(420);
    return { ok: true, from: start, to: end, panelRect: state.panelRect };
  }, port);
}

async function keyBuiltinNormalized(event = {}, port = DEFAULT_PORT) {
  const type = event.type === 'keyUp' ? 'keyUp' : 'keyDown';
  const key = String(event.key || '');
  if (!key) throw new Error('缺少 key');
  const text = typeof event.text === 'string' ? event.text : '';
  const modifiers = (event.altKey ? 1 : 0) + (event.ctrlKey ? 2 : 0) + (event.metaKey ? 4 : 0) + (event.shiftKey ? 8 : 0);
  const virtualKeyCode = Number(event.windowsVirtualKeyCode) || virtualKeyCodeFor(key, String(event.code || ''));
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const params = {
      type,
      key,
      code: String(event.code || ''),
      modifiers,
      windowsVirtualKeyCode: virtualKeyCode || undefined,
      nativeVirtualKeyCode: virtualKeyCode || undefined,
    };
    if (text) { params.text = text; params.unmodifiedText = text; }
    await connection.send('Input.dispatchKeyEvent', params);
    if (type === 'keyDown') {
      await sleep(40);
      await connection.send('Input.dispatchKeyEvent', { ...params, type: 'keyUp', text: undefined, unmodifiedText: undefined });
    }
    return { ok: true, key, type, modifiers, virtualKeyCode };
  }, port);
}

function virtualKeyCodeFor(key, code) {
  const named = {
    Backspace: 8, Tab: 9, Enter: 13, Shift: 16, Control: 17, Alt: 18,
    Escape: 27, Space: 32, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
    Delete: 46, Home: 36, End: 35, PageUp: 33, PageDown: 34,
  };
  if (named[key] !== undefined) return named[key];
  if (key === ' ') return 32;
  const letter = code.match(/^Key([A-Z])$/);
  if (letter) return letter[1].charCodeAt(0);
  const digit = code.match(/^Digit(\d)$/);
  if (digit) return 48 + Number(digit[1]);
  if (/^F\d{1,2}$/.test(key)) return 111 + Number(key.slice(1));
  return 0;
}

async function selectBuiltinNode(nodeId, port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const node = state.nodes.find((item) => item.id === nodeId);
    if (!node) throw new Error(`节点不存在: ${nodeId}`);
    const x = node.rect.x + node.rect.w / 2;
    const y = node.rect.y + node.rect.h / 2;
    await clickAt(connection, x, y);
    const afterRaw = await evaluate(connection, SNAPSHOT_EXPR);
    const after = JSON.parse(afterRaw);
    const selected = after.ok ? after.nodes.find((item) => item.selected) : null;
    return {
      ok: Boolean(selected && selected.id === nodeId),
      requested: nodeId,
      selected: selected ? selected.id : null,
      state: after,
    };
  }, port);
}

/**
 * 删除内置节点。先点选目标；如果点中的是别的节点（重叠场景），拒绝删除。
 */
async function deleteBuiltinNode(nodeId, port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const node = state.nodes.find((item) => item.id === nodeId);
    if (!node) throw new Error(`节点不存在: ${nodeId}`);
    await clickAt(connection, node.rect.x + node.rect.w / 2, node.rect.y + node.rect.h / 2);
    const selectedRaw = await evaluate(connection, SNAPSHOT_EXPR);
    const selectedState = JSON.parse(selectedRaw);
    const selected = selectedState.ok ? selectedState.nodes.find((item) => item.selected) : null;
    if (!selected || selected.id !== nodeId) {
      return {
        ok: false,
        reason: 'not-selected',
        requested: nodeId,
        selected: selected ? selected.id : null,
        state: selectedState,
      };
    }
    await connection.send('Input.dispatchKeyEvent', {
      type: 'keyDown',
      key: 'Delete',
      code: 'Delete',
      windowsVirtualKeyCode: 46,
      nativeVirtualKeyCode: 46,
    });
    await sleep(60);
    await connection.send('Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: 'Delete',
      code: 'Delete',
      windowsVirtualKeyCode: 46,
      nativeVirtualKeyCode: 46,
    });
    await sleep(500);
    const afterRaw = await evaluate(connection, SNAPSHOT_EXPR);
    const after = JSON.parse(afterRaw);
    return {
      ok: Boolean(after.ok && !after.nodes.some((item) => item.id === nodeId)),
      requested: nodeId,
      beforeCount: state.nodes.length,
      afterCount: after.ok ? after.nodes.length : null,
      state: after,
    };
  }, port);
}
async function moveBuiltinNode(nodeId, dx, dy, port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const node = state.nodes.find((item) => item.id === nodeId);
    if (!node) throw new Error(`节点不存在: ${nodeId}`);
    const fromX = node.rect.x + node.rect.w / 2;
    const fromY = node.rect.y + node.rect.h / 2;
    const toX = fromX + Number(dx || 0);
    const toY = fromY + Number(dy || 0);
    await connection.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: fromX, y: fromY, button: 'none', clickCount: 0 });
    await sleep(40);
    await connection.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: fromX, y: fromY, button: 'left', clickCount: 1 });
    await sleep(80);
    for (let step = 1; step <= 6; step += 1) {
      const x = fromX + ((toX - fromX) * step) / 6;
      const y = fromY + ((toY - fromY) * step) / 6;
      await connection.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'left', clickCount: 1 });
      await sleep(35);
    }
    await connection.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: toX, y: toY, button: 'left', clickCount: 1 });
    await sleep(450);
    const afterRaw = await evaluate(connection, SNAPSHOT_EXPR);
    const after = JSON.parse(afterRaw);
    const moved = after.ok ? after.nodes.find((item) => item.id === nodeId) : null;
    return {
      ok: Boolean(moved && (moved.flowPosition.x !== node.flowPosition.x || moved.flowPosition.y !== node.flowPosition.y)),
      requested: nodeId,
      before: node.flowPosition,
      after: moved ? moved.flowPosition : null,
      state: after,
    };
  }, port);
}

async function deselectBuiltin(port = DEFAULT_PORT) {
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const flow = state.flowRect;
    const candidates = [
      { x: flow.x + 24, y: flow.y + 24 },
      { x: flow.x + flow.w - 24, y: flow.y + 24 },
      { x: flow.x + 24, y: flow.y + flow.h - 24 },
      { x: flow.x + flow.w - 24, y: flow.y + flow.h - 24 },
    ];
    const point = candidates.find((candidate) => !state.nodes.some((node) => (
      candidate.x >= node.rect.x && candidate.x <= node.rect.x + node.rect.w
      && candidate.y >= node.rect.y && candidate.y <= node.rect.y + node.rect.h
    )));
    if (!point) throw new Error('找不到空白点，无法取消选中');
    await clickAt(connection, point.x, point.y);
    const afterRaw = await evaluate(connection, SNAPSHOT_EXPR);
    const after = JSON.parse(afterRaw);
    return {
      ok: Boolean(after.ok && !after.nodes.some((node) => node.selected)),
      state: after,
    };
  }, port);
}

async function probeCdp(port = DEFAULT_PORT) {
  const response = await fetch(`http://127.0.0.1:${port}/json/version`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`CDP /json/version ${response.status}`);
  return response.json();
}

/** 本地画布前台 → 内置画布后台执行器：点底部“视频生成/图片生成”，生成待填节点。 */
async function createBuiltinGenerationNode(kind = 'video', port = DEFAULT_PORT) {
  const aria = kind === 'video' ? '视频生成' : '图片生成';
  const result = await withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    const before = (state.nodes || []).map((node) => node.id);
    const expr = `(() => {
      const btn = [...document.querySelectorAll('button,[role="button"]')]
        .find((el) => (el.getAttribute('aria-label') || '') === ${JSON.stringify(aria)});
      if (!btn) return { ok: false, reason: '找不到按钮：' + ${JSON.stringify(aria)} };
      const rect = btn.getBoundingClientRect();
      btn.click();
      return { ok: true, rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height } };
    })()`;
    const clicked = await evaluate(connection, expr);
    if (!clicked || !clicked.ok) throw new Error((clicked && clicked.reason) || '点击生成按钮失败');
    await new Promise((resolve) => setTimeout(resolve, 900));
    const afterRaw = await evaluate(connection, SNAPSHOT_EXPR);
    const after = JSON.parse(afterRaw);
    const created = (after.nodes || []).filter((node) => !before.includes(node.id));
    return { ok: true, clicked, before, after, created };
  }, port);
  // 新建出来的节点只是选中状态，生成条要双击节点才展开；这里顺手打开，
  // 否则后面的写提示词/同步参数会找不到 .ProseMirror.tiptap。
  if (result && result.created && result.created[0]) {
    try { await openBuiltinComposer(result.created[0].id, port); } catch { /* 生成条可能由调用方稍后打开 */ }
  }
  return result;
}

/**
 * 双击生成节点，展开它底部的生成条。内置画布的生成条不是常驻 DOM：
 * 节点只是选中时，[data-canvas-generation-composer] 是 0 高度的空壳。
 */
async function openBuiltinComposer(itemId, port = DEFAULT_PORT) {
  const id = String(itemId || '');
  if (!id) throw new Error('缺少生成节点 id');
  return withCdp(async (connection) => {
    const raw = await evaluate(connection, SNAPSHOT_EXPR);
    const state = JSON.parse(raw);
    if (!state.ok) throw new Error(state.error || '内置画布不可读');
    let node = (state.nodes || []).find((entry) => String(entry.id) === id);
    if (!node) throw new Error('内置画布上找不到生成节点：' + id);
    // 探针/缩放操作可能把画布缩到 3%，或者节点被平移到视口外。
    // 直接改 viewport 的 transform 把目标节点带回视口中央，比滚轮可靠。
    const flow = state.flowRect;
    const outside = node.rect.x < flow.x || node.rect.y < flow.y
      || node.rect.x + node.rect.w > flow.x + flow.w || node.rect.y + node.rect.h > flow.y + flow.h;
    if (node.rect.w < 120 || outside) {
      const targetScale = 0.22;
      const frameW = Math.max(320, Math.min(2048, Number(node.frameSize && node.frameSize.w) || 1024));
      const frameH = Math.max(320, Math.min(2048, Number(node.frameSize && node.frameSize.h) || 1024));
      const tx = Math.round(flow.w / 2 - (node.flowPosition.x + frameW / 2) * targetScale);
      const ty = Math.round(flow.h / 2 - (node.flowPosition.y + frameH / 2) * targetScale);
      const focused = await evaluate(connection, `(() => {
        const viewport = document.querySelector('[data-testid="rf__wrapper"] .react-flow__viewport')
          || document.querySelector('.react-flow__viewport');
        if (!viewport) return false;
        viewport.style.transform = ${JSON.stringify(`translate(${tx}px, ${ty}px) scale(${targetScale})`)};
        return true;
      })()`);
      if (focused) {
        await sleep(350);
        const next = JSON.parse(await evaluate(connection, SNAPSHOT_EXPR));
        node = (next.nodes || []).find((entry) => String(entry.id) === id) || node;
      }
    }
    const x = node.rect.x + node.rect.w / 2;
    const y = node.rect.y + node.rect.h / 2;
    await clickAt(connection, x, y, { count: 2 });
    await sleep(900);
    const composer = JSON.parse(await evaluate(connection, `(() => {
      const composer = [...document.querySelectorAll('[data-canvas-generation-composer]')]
        .find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
      if (!composer) return JSON.stringify({ ok: false, reason: 'composer-missing' });
      const rect = composer.getBoundingClientRect();
      return JSON.stringify({ ok: rect.height > 20, reason: rect.height > 20 ? '' : 'composer-collapsed', rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) } });
    })()`));
    if (!composer.ok) {
      // 缩放过小时 CDP 双击会打偏；DOM 事件不依赖坐标，直接命中节点本身。
      const domOpened = await evaluate(connection, `(() => {
        const node = [...document.querySelectorAll('.react-flow__node')]
          .find((el) => (el.getAttribute('data-id') || '') === ${JSON.stringify(id)});
        if (!node) return false;
        node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window, detail: 1 }));
        node.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true, view: window, detail: 2 }));
        return true;
      })()`);
      await sleep(900);
      const domComposer = JSON.parse(await evaluate(connection, `(() => {
        const el = [...document.querySelectorAll('[data-canvas-generation-composer]')]
          .find((entry) => (entry.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
        if (!el) return JSON.stringify({ ok: false, reason: 'composer-missing' });
        const rect = el.getBoundingClientRect();
        return JSON.stringify({ ok: rect.height > 20, reason: rect.height > 20 ? '' : 'composer-collapsed', rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) } });
      })()`));
      if (domComposer.ok) return { ok: true, itemId: id, composer: domComposer, opened: true, via: 'dom' };
      // 部分节点第一次双击只选中，再点一次才会展开。
      await clickAt(connection, x, y, { count: 1 });
      await sleep(700);
      const again = JSON.parse(await evaluate(connection, `(() => {
        const el = [...document.querySelectorAll('[data-canvas-generation-composer]')]
          .find((entry) => (entry.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
        if (!el) return JSON.stringify({ ok: false, reason: 'composer-missing' });
        const rect = el.getBoundingClientRect();
        return JSON.stringify({ ok: rect.height > 20, reason: rect.height > 20 ? '' : 'composer-collapsed', rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) } });
      })()`));
      if (!again.ok) throw new Error('生成条没有展开：' + (again.reason || ''));
      return { ok: true, itemId: id, composer: again, opened: true };
    }
    return { ok: true, itemId: id, composer, opened: true };
  }, port);
}

/** 往指定节点的生成条写提示词。与 fillBuiltinPrompt 的区别是：不依赖“最后聚焦的编辑器”。 */
async function fillBuiltinPromptForItem(itemId, text = '', port = DEFAULT_PORT) {
  const id = String(itemId || '');
  const safe = String(text || '');
  if (!id) throw new Error('缺少生成节点 id');
  if (!safe.trim()) throw new Error('提示词不能为空');
  await openBuiltinComposer(id, port).catch(() => null);
  return withCdp(async (connection) => {
    const expr = `(() => {
      const composer = [...document.querySelectorAll('[data-canvas-generation-composer]')]
        .find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
      if (!composer) return { ok: false, reason: '找不到生成条：' + ${JSON.stringify(id)} };
      const editor = composer.querySelector('.ProseMirror.tiptap') || composer.querySelector('[contenteditable="true"]');
      if (!editor) return { ok: false, reason: '生成条里找不到输入编辑器' };
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(editor);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('selectAll', false, null);
      const ok = document.execCommand('insertText', false, ${JSON.stringify(safe)});
      return { ok, text: (editor.innerText || '').trim().slice(0, 800) };
    })()`;
    const filled = await evaluate(connection, expr);
    if (!filled || !filled.ok) throw new Error((filled && filled.reason) || '写提示词失败');
    const state = JSON.parse(await evaluate(connection, SNAPSHOT_EXPR));
    return { ok: true, itemId: id, filled, state };
  }, port);
}

/** 往刚创建的生成节点/底部生成条写提示词；不点击生成，避免擅自耗点。 */
async function fillBuiltinPrompt(text = '', port = DEFAULT_PORT) {
  const safe = String(text || '');
  if (!safe.trim()) throw new Error('提示词不能为空');
  return withCdp(async (connection) => {
    const expr = `(() => {
      // 聊天输入框也是 .ProseMirror.tiptap，必须限定在右侧工作台内，
      // 并优先取聚焦的/最后一个编辑器（底部生成条），否则会把提示词写进聊天框。
      const panel = document.querySelector('[data-testid="right-panel-container"]');
      const editable = Array.from((panel || document).querySelectorAll('.ProseMirror.tiptap')).filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      const editor = editable.find((el) => el.classList.contains('ProseMirror-focused'))
        || editable[editable.length - 1]
        || null;
      if (!editor) return { ok: false, reason: '找不到生成输入框（右侧工作台内没有编辑器）' };
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(editor);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      const ok = document.execCommand('insertText', false, ${JSON.stringify(safe)});
      return { ok, text: (editor.innerText || '').trim().slice(0, 500) };
    })()`;
    const filled = await evaluate(connection, expr);
    if (!filled || !filled.ok) throw new Error((filled && filled.reason) || '写提示词失败');
    const state = JSON.parse(await evaluate(connection, SNAPSHOT_EXPR));
    return { ok: true, filled, state };
  }, port);
}

const BUILTIN_VIDEO_MODELS = {
  seedance_2_5: 'Seedance 2.5',
  'seedance_2.5': 'Seedance 2.5',
  seedance_2_0: 'Seedance 2.0',
  'seedance_2.0': 'Seedance 2.0',
  seedance_2_0_fast: 'Seedance 2.0 Fast',
  'seedance_2.0_fast': 'Seedance 2.0 Fast',
  seedance_2_0_mini: 'Seedance 2.0 Mini',
  'seedance_2.0_mini': 'Seedance 2.0 Mini',
  '2.5': 'Seedance 2.5',
  '2.0': 'Seedance 2.0',
  '2.0 fast': 'Seedance 2.0 Fast',
  '2.0 mini': 'Seedance 2.0 Mini',
};

const BUILTIN_IMAGE_MODELS = {
  seedream_5_0_flash: 'Seedream 5.0 Flash',
  'seedream_5.0_flash': 'Seedream 5.0 Flash',
  seedream_5_0_pro: 'Seedream 5.0 Pro',
  'seedream_5.0_pro': 'Seedream 5.0 Pro',
  seedream_4_5: 'Seedream 4.5',
  'seedream_4.5': 'Seedream 4.5',
  seedream_4_0: 'Seedream 4.0',
  'seedream_4.0': 'Seedream 4.0',
  '5.0 flash': 'Seedream 5.0 Flash',
  '5.0 pro': 'Seedream 5.0 Pro',
  '4.5': 'Seedream 4.5',
  '4.0': 'Seedream 4.0',
};

function builtinModelLabel(model, kind = 'video') {
  const value = String(model || '').trim();
  if (!value) return '';
  const table = kind === 'image' ? BUILTIN_IMAGE_MODELS : BUILTIN_VIDEO_MODELS;
  const mapped = table[value] || table[value.toLowerCase()];
  if (mapped) return mapped;
  const labels = kind === 'image'
    ? ['Seedream 5.0 Flash', 'Seedream 5.0 Pro', 'Seedream 4.5', 'Seedream 4.0']
    : ['Seedance 2.5', 'Seedance 2.0', 'Seedance 2.0 Fast', 'Seedance 2.0 Mini'];
  const exact = labels.find((label) => label.toLowerCase() === value.toLowerCase());
  if (exact) return exact;
  throw new Error(`不支持的${kind === 'image' ? '图片' : '视频'}模型：${value}；可用：${labels.join(' / ')}`);
}

/**
 * 同步内置画布生成条上的模型 / 比例 / 时长。
 *
 * 内置画布不是普通 DOM 表单：模型菜单和「自动 · 10s」都是 Radix 菜单，
 * 必须用 CDP Input.dispatchMouseEvent 点在按钮中心，再点菜单项；
 * 比例和时长共用一个菜单，时长是 role=slider 的 Radix 滑块。
 * 这里不提交、不耗点，只改参数。
 */
async function configureBuiltinGeneration(itemId, options = {}, port = DEFAULT_PORT) {
  const id = String(itemId || '');
  if (!id) throw new Error('缺少生成节点 id');
  const kind = options.kind === 'image' ? 'image' : 'video';
  const modelLabel = options.model ? builtinModelLabel(options.model, kind) : '';
  const ratio = options.ratio ? String(options.ratio).trim() : '';
  const duration = Number(options.duration) || 0;
  if (kind === 'video' && duration && (duration < 4 || duration > 15)) {
    throw new Error(`内置画布当前时长只支持 4–15 秒，收到 ${duration} 秒`);
  }
  // 生成条可能因为焦点丢失/点击空白而收起；同步参数前先确保它是展开的。
  await openBuiltinComposer(id, port).catch(() => null);

  return withCdp(async (connection) => {
    const evalV = (expression) => evaluate(connection, expression);
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const clickAt = async (x, y) => {
      await connection.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
      await connection.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    };
    const key = async (keyName, code, keyCode) => {
      await connection.send('Input.dispatchKeyEvent', { type: 'keyDown', key: keyName, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
      await connection.send('Input.dispatchKeyEvent', { type: 'keyUp', key: keyName, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
    };
    const esc = async () => {
      await key('Escape', 'Escape', 27);
      await sleep(250);
    };
    const composerButton = async (needle) => evalV(`(() => {
      const c = [...document.querySelectorAll('[data-canvas-generation-composer]')]
        .find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
      if (!c) return null;
      const b = [...c.querySelectorAll('button')].find((el) => (el.innerText || '').includes(${JSON.stringify(needle)}));
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), text: (b.innerText || '').trim() };
    })()`);
    const menuVisible = async (menuKind = 'model') => evalV(menuKind === 'ratio' ? `(() => {
      const menu = [...document.querySelectorAll('[role="menu"]')]
        .find((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && el.querySelectorAll('button').length > 0; });
      return Boolean(menu);
    })()` : `(() => {
      const item = [...document.querySelectorAll('[role="menuitem"]')].some((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
      return item;
    })()`);
    const focusComposerButton = async (needle) => evalV(`(() => {
      const c = [...document.querySelectorAll('[data-canvas-generation-composer]')]
        .find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
      if (!c) return false;
      const b = [...c.querySelectorAll('button')].find((el) => (el.innerText || '').includes(${JSON.stringify(needle)}));
      if (!b) return false;
      b.focus();
      return true;
    })()`);
    const openMenu = async (needle, menuKind = 'model') => {
      // Escape 在没有菜单时会冒泡到画布，把生成条一起关掉；只在确有菜单时才按。
      const anyMenu = await evalV(`(() => {
        return [...document.querySelectorAll('[role="menu"]')].some((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
      })()`);
      if (anyMenu) {
        await esc();
        await sleep(250);
      }
      const button = await composerButton(needle);
      if (!button) throw new Error(`生成条上找不到按钮：${needle}`);
      await clickAt(button.x, button.y);
      await sleep(700);
      if (await menuVisible(menuKind)) return;
      // Radix 菜单偶尔第一次点击只吃掉焦点；先重试中心点击，再退回键盘触发。
      await clickAt(button.x, button.y);
      await sleep(900);
      if (await menuVisible(menuKind)) return;
      if (await focusComposerButton(needle)) {
        await key('Enter', 'Enter', 13);
        await sleep(700);
        if (await menuVisible(menuKind)) return;
        await key('Space', ' ', 32);
        await sleep(700);
        if (await menuVisible(menuKind)) return;
      }
      throw new Error(`菜单没有打开：${needle}`);
    };
    const pickModel = async () => {
      await openMenu('模型', 'model');
      const option = await evalV(`(() => {
        const items = [...document.querySelectorAll('[role="menuitem"]')]
          .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
        const hit = items.find((el) => ((el.innerText || '').trim().split('\\n')[0] || '').trim() === ${JSON.stringify(modelLabel)});
        if (!hit) return { ok: false, options: items.map((el) => ((el.innerText || '').trim().split('\\n')[0] || '').trim()) };
        const r = hit.getBoundingClientRect();
        return { ok: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
      })()`);
      if (!option || !option.ok) throw new Error(`模型菜单没有「${modelLabel}」；当前可选：${(option && option.options || []).join(' / ') || '未读到菜单'}`);
      await clickAt(option.x, option.y);
      await sleep(500);
    };
    const pickRatio = async () => {
      await openMenu(kind === 'image' ? '比例' : ' · ', 'ratio');
      const option = await evalV(`(() => {
        const menu = [...document.querySelectorAll('[role="menu"]')]
          .find((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
        if (!menu) return { ok: false, reason: '比例/时长菜单没有打开' };
        const hit = [...menu.querySelectorAll('button')].find((el) => (el.innerText || '').trim() === ${JSON.stringify(ratio)});
        if (!hit) return { ok: false, options: [...menu.querySelectorAll('button')].map((el) => (el.innerText || '').trim()).filter(Boolean) };
        const r = hit.getBoundingClientRect();
        return { ok: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
      })()`);
      if (!option || !option.ok) throw new Error(`比例菜单没有「${ratio}」；当前可选：${(option && option.options || []).join(' / ') || '未读到菜单'}`);
      await clickAt(option.x, option.y);
      await sleep(450);
    };
    const setDuration = async () => {
      await openMenu(' · ', 'ratio');
      const slider = await evalV(`(() => {
        const menu = [...document.querySelectorAll('[role="menu"]')]
          .find((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
        if (!menu) return { ok: false, reason: '比例/时长菜单没有打开' };
        const s = menu.querySelector('[role="slider"]');
        if (!s) return { ok: false, reason: '时长滑块没有出现' };
        const r = s.getBoundingClientRect();
        return { ok: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), min: Number(s.getAttribute('aria-valuemin')), now: Number(s.getAttribute('aria-valuenow')) };
      })()`);
      if (!slider || !slider.ok) throw new Error((slider && slider.reason) || '找不到时长滑块');
      await clickAt(slider.x, slider.y);
      await sleep(250);
      await key('Home', 'Home', 36);
      // 内置滑块 min=4s，步长 1 秒；Home 后按右键补齐。
      for (let index = 0; index < Math.max(0, duration - 4); index += 1) {
        await key('ArrowRight', 'ArrowRight', 39);
      }
      await sleep(350);
      await esc();
    };

    if (modelLabel) await pickModel();
    if (ratio) await pickRatio();
    if (kind === 'video' && duration) await setDuration();

    const final = await evalV(`(() => {
      const c = [...document.querySelectorAll('[data-canvas-generation-composer]')]
        .find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
      if (!c) return null;
      return {
        text: (c.innerText || '').trim(),
        buttons: [...c.querySelectorAll('button')].map((el) => (el.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean),
      };
    })()`);
    return { ok: true, kind, model: modelLabel, ratio, duration, quality: options.quality || '', qualityNote: options.quality ? '内置生成条未暴露清晰度按钮；当前模型档位决定清晰度' : '', composer: final };
  }, port);
}

/**
 * 读取某个生成节点的底部生成条，不点击任何按钮。
 * 这是「提交前检查」：确认发送按钮存在、是否可用、当前模型/时长标签。
 */
async function readBuiltinComposer(itemId, port = DEFAULT_PORT) {
  const id = String(itemId || '');
  if (!id) throw new Error('缺少生成节点 id');
  return withCdp(async (connection) => {
    const expr = `(() => {
      const composers = [...document.querySelectorAll('[data-canvas-generation-composer]')];
      const composer = composers.find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)})
        || composers.find((el) => el.querySelector('.ProseMirror.tiptap') && (el.querySelector('.ProseMirror.tiptap').innerText || '').trim());
      if (!composer) return { ok: false, reason: '找不到生成条：' + ${JSON.stringify(id)} };
      const editor = composer.querySelector('.ProseMirror.tiptap');
      const visibleButtons = [...composer.querySelectorAll('button')].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width >= 24 && r.height >= 24;
      });
      const send = composer.querySelector('[data-testid="chat_input_send_button"]')
        || [...composer.querySelectorAll('button')].find((el) => (el.getAttribute('aria-label') || '') === '发送')
        || visibleButtons[visibleButtons.length - 1]
        || null;
      const buttons = [...composer.querySelectorAll('button')].map((el) => {
        const r = el.getBoundingClientRect();
        return {
          testid: el.getAttribute('data-testid') || '',
          text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
          disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
          x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
        };
      });
      const labels = buttons.map((b) => b.text).filter(Boolean);
      const modelLabel = labels.find((label) => /^模型/.test(label)) || '';
      const paramLabel = labels.find((label) => /^比例/.test(label)) || '';
      const timeLabel = paramLabel || labels.find((label) => /(\\d+:\\d+|自动).*(s|秒)|(\\d+)s/i.test(label)) || '';
      const model = modelLabel.replace(/^模型\\s*/, '').trim();
      const ratio = (timeLabel.match(/(\\d+:\\d+)/) || [])[1] || (timeLabel.includes('自动') ? '自动' : '');
      const duration = Number((timeLabel.match(/(\\d+)\\s*s/i) || [])[1]) || 0;
      const composerRect = composer.getBoundingClientRect();
      return {
        ok: true,
        itemId: composer.getAttribute('data-canvas-generation-item-id') || '',
        kind: composer.getAttribute('data-canvas-generation-composer') || '',
        status: composer.getAttribute('data-canvas-instruction-status') || '',
        expanded: composerRect.height > 20 && Boolean(editor),
        composerRect: { x: Math.round(composerRect.x), y: Math.round(composerRect.y), w: Math.round(composerRect.width), h: Math.round(composerRect.height) },
        text: editor ? (editor.innerText || '').trim().slice(0, 500) : '',
        focused: editor ? editor.classList.contains('ProseMirror-focused') : false,
        buttons,
        labels,
        model,
        ratio,
        duration,
        send: send ? (() => {
          const r = send.getBoundingClientRect();
          return { found: true, disabled: send.disabled === true || send.getAttribute('aria-disabled') === 'true', x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
        })() : { found: false },
      };
    })()`;
    const composer = await evaluate(connection, expr);
    if (!composer || !composer.ok) throw new Error((composer && composer.reason) || '读取生成条失败');
    return composer;
  }, port);
}

/** 提交内置画布上的生成；只在用户明确确认消耗后调用。 */
async function startBuiltinGeneration(itemId, port = DEFAULT_PORT) {
  const id = String(itemId || '');
  if (!id) throw new Error('缺少生成节点 id');
  return withCdp(async (connection) => {
    const expr = `(() => {
      const composers = [...document.querySelectorAll('[data-canvas-generation-composer]')];
      const composer = composers.find((el) => (el.getAttribute('data-canvas-generation-item-id') || '') === ${JSON.stringify(id)});
      if (!composer) return { ok: false, reason: '找不到生成条：' + ${JSON.stringify(id)} };
      const editor = composer.querySelector('.ProseMirror.tiptap');
      if (!editor || !(editor.innerText || '').trim()) return { ok: false, reason: '提示词为空，拒绝提交' };
      const visible = [...composer.querySelectorAll('button')].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width >= 24 && r.height >= 24;
      });
      const send = composer.querySelector('[data-testid="chat_input_send_button"]') || visible[visible.length - 1] || null;
      if (!send) return { ok: false, reason: '找不到发送按钮' };
      const disabled = send.disabled === true || send.getAttribute('aria-disabled') === 'true';
      if (disabled) return { ok: false, reason: '发送按钮当前禁用' };
      const r = send.getBoundingClientRect();
      return { ok: true, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), sendTestid: send.getAttribute('data-testid') || '' };
    })()`;
    const clicked = await evaluate(connection, expr);
    if (!clicked || !clicked.ok) throw new Error((clicked && clicked.reason) || '提交生成失败');
    await clickAt(connection, clicked.x + clicked.w / 2, clicked.y + clicked.h / 2, { count: 1 });
    return clicked;
  }, port);
}

/** 读取单个生成节点的状态；结果媒体仍在 media 数组里。 */
async function readBuiltinGeneration(itemId, port = DEFAULT_PORT) {
  const state = await readBuiltinState(port);
  const node = (state.nodes || []).find((entry) => String(entry.id) === String(itemId));
  if (!node) throw new Error('内置画布上找不到生成节点：' + itemId);
  return { state, node };
}

/** 轮询内置画布节点；不下载、不落库，只等待状态。 */
async function waitBuiltinGeneration(itemId, { port = DEFAULT_PORT, timeoutMs = 480000, intervalMs = 5000 } = {}) {
  const deadline = Date.now() + Math.max(10000, Number(timeoutMs) || 480000);
  let last = null;
  while (Date.now() < deadline) {
    last = await readBuiltinGeneration(itemId, port);
    const status = String(last.node.generationState || '').toLowerCase();
    if (/done|success|completed|finished|error|failed/.test(status)) return last;
    if ((last.node.media || []).some((item) => item && item.src)) return last;
    await new Promise((resolve) => setTimeout(resolve, Math.max(1000, Number(intervalMs) || 5000)));
  }
  return last || await readBuiltinGeneration(itemId, port);
}

/**
 * 下载内置画布节点里的媒体。
 * https 资源直接在 Node 侧取；blob: 资源只能在页面里 fetch，再转 base64 回传。
 */
async function downloadBuiltinMedia(src, port = DEFAULT_PORT) {
  const url = String(src || '');
  if (!url) throw new Error('媒体地址为空');
  if (/^https?:/i.test(url)) {
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (response.ok) {
        const buffer = Buffer.from(await response.arrayBuffer());
        const mime = String(response.headers.get('content-type') || '').split(';')[0] || 'application/octet-stream';
        return { buffer, mime, url, via: 'node-fetch' };
      }
    } catch { /* 需要登录态时退回页面内 fetch */ }
  }
  const raw = await withCdp(async (connection) => evaluate(connection, `(async () => {
    const response = await fetch(${JSON.stringify(url)}, { credentials: 'include' });
    if (!response.ok) return JSON.stringify({ ok: false, status: response.status });
    const buffer = await response.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const chunk = 0x8000;
    for (let index = 0; index < bytes.length; index += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunk));
    }
    return JSON.stringify({ ok: true, mime: response.headers.get('content-type') || '', b64: btoa(binary) });
  })()`), port);
  let parsed;
  try { parsed = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { parsed = null; }
  if (!parsed || !parsed.ok || !parsed.b64) throw new Error('页面内下载媒体失败' + (parsed && parsed.status ? `（HTTP ${parsed.status}）` : ''));
  return {
    buffer: Buffer.from(parsed.b64, 'base64'),
    mime: String(parsed.mime || '').split(';')[0] || 'application/octet-stream',
    url,
    via: 'page-fetch',
  };
}

module.exports = {
  DEFAULT_PORT,
  listTargets,
  pickChatTarget,
  setActiveConversation,
  openBuiltinCanvas,
  ensureBuiltinCanvas,
  expandBuiltinPanel,
  readBuiltinState,
  readBuiltinContext,
  readBuiltinModel,
  selectBuiltinNode,
  moveBuiltinNode,
  deleteBuiltinNode,
  deselectBuiltin,
  captureBuiltinScreenshot,
  clickBuiltinNormalized,
  wheelBuiltinNormalized,
  dragBuiltinNormalized,
  keyBuiltinNormalized,
  createBuiltinGenerationNode,
  openBuiltinComposer,
  fillBuiltinPrompt,
  fillBuiltinPromptForItem,
  configureBuiltinGeneration,
  readBuiltinComposer,
  startBuiltinGeneration,
  readBuiltinGeneration,
  waitBuiltinGeneration,
  downloadBuiltinMedia,
  probeCdp,
  SNAPSHOT_EXPR,
  OPEN_BY_TAB_SERVICE_EXPR,
  ACTIVATE_EXISTING_CANVAS_EXPR,
  OPEN_BY_ARTIFACT_EXPR,
};
