/* 豆包画布 · 本地版
 * 零依赖前端：无限画布、节点、连线、任务轮询、素材本地化。
 * 关键性能策略（对应参考版 2.9.6 的「卡顿根治」）：
 *   - 视口裁剪：视野外的卡片直接 display:none，不参与布局/绘制
 *   - 媒体懒加载：图片 loading=lazy，视频 preload=none，裁剪时暂停播放
 *   - 拖动只改 transform，连线只更新受影响的 path
 *   - 持久化防抖，平移/缩放不触发整画布重绘
 */
'use strict';

const TOKEN = window.__CANVAS_TOKEN__ || '';
/* 挂到工作台同端口时页面在 /canvas/，API 与媒体也必须带这个前缀；
   单独开在 9190 根路径时为空，行为不变。 */
const CANVAS_BASE = String(window.__CANVAS_BASE__ || '');
const BRIDGE_MODE = new URLSearchParams(location.search).get('bridge') || '';
try {
  if (BRIDGE_MODE) localStorage.setItem('heronbo:onboarded', '1');
} catch (e) { /* ignore */ }
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const KIND_META = {
  image: { icon: '◨', label: '图片', title: '图片生成', color: '#4b7cff' },
  video: { icon: '▶', label: '视频', title: '视频生成', color: '#e0724f' },
  upload: { icon: '↑', label: '素材', title: '本地素材', color: '#2fa47a' },
  text: { icon: 'T', label: '文本', title: '文本', color: '#8a6bff' },
  subject: { icon: '◍', label: '主体', title: '主体', color: '#7a5cf0' },
};
const RATIOS = ['1:1', '4:3', '3:4', '16:9', '9:16', '21:9'];
const IMAGE_QUALITIES = ['1K', '2K', '4K'];
const COUNTS = [1, 2, 4];

// 豆包内置模型线（Seed 三条：Seedream 图片 / Seedance 视频 / Seed-Audio 音频）。
// 生成走豆包工作客户端，模型名会写进结构化生成指令。
const IMAGE_MODELS = [
  { id: 'seedream_5.0_flash', label: 'Seedream 5.0 Flash（默认）', note: '文生图 / 图生图 / 编辑 / 多图融合' },
  { id: 'seedream_5.0_pro', label: 'Seedream 5.0 Pro', note: '会员专属，复杂构图与精细度更强' },
  { id: 'seedream_4.5', label: 'Seedream 4.5', note: '上一代稳定版' },
  { id: 'seedream_4.0', label: 'Seedream 4.0', note: '内置画布在用的旧档位' },
];

// 内置画布生成条的时长滑块实测为 4–15s（2.5 也一样）；这里按后台执行器的真实上限收口。
const VIDEO_MODELS = [
  { id: 'seedance_2.5', label: 'Seedance 2.5', min: 4, max: 15, qualities: ['480P', '720P', '1080P'], note: '旗舰档；内置画布时长上限 15s' },
  { id: 'seedance_2.0', label: 'Seedance 2.0', min: 4, max: 15, qualities: ['720P', '1080P'], note: '标准版，质量优先' },
  { id: 'seedance_2.0_fast', label: 'Seedance 2.0 Fast', min: 4, max: 15, qualities: ['720P'], note: '快速出片，默认走量版本' },
  { id: 'seedance_2.0_mini', label: 'Seedance 2.0 Mini', min: 4, max: 15, qualities: ['720P'], note: '日常生成，内置画布默认档位' },
];

// 本地前台唯一工作台 → 内置画布执行器。这里的会话必须和豆包客户端当前打开的画布一致，
// 否则会在另一个会话里建节点，本地这边永远拿不到结果。
const BUILTIN_TARGET = {
  conversationId: '38450384705095170',
  canvasId: 'cws_60302341085214210',
  title: '豆包无限画布项目交接',
};

const NODE_MIN_W = 220;
const NODE_MAX_W = 560;
const GRID = 24;
const MAX_ASSETS_SHOWN = 8;

const S = {
  token: TOKEN,
  config: null,
  paths: null,
  version: '',
  health: null,
  desktop: null,
  onboardChecked: false,
  canvases: [],
  subjects: [],
  doc: null,
  nodeById: new Map(),
  els: new Map(),
  edgeEls: new Map(),
  sel: new Set(),
  selEdge: null,
  composerHidden: false,
  composerTimer: 0,          // 面板跟随节点定位的节流定时器
  tool: 'select',            // 'select' 移动节点/框选 | 'hand' 拖动画布
  snapLines: [],
  view: { x: 0, y: 0, scale: 1 },
  gateway: { ok: null, models: [], error: '', baseUrl: '' },
  tasks: new Map(),          // nodeId -> task view（只用于 UI 提示）
  history: { past: [], future: [] },
  typing: null,              // {nodeId} 提示词编辑会话，用于撤销快照只压一次
  drag: null,
  link: null,
  marquee: null,
  mention: null,
  addPoint: null,
  lightbox: null,
  saveTimer: 0,
  viewTimer: 0,
  saveState: 'saved',
  pollTimer: 0,
  minimapRaf: 0,
  cullRaf: 0,
  moveRaf: 0,
  perf: false,
  suppressClick: false,
};

/* ── 小工具 ─────────────────────────────────────────────── */

const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
const uid = (prefix) => prefix + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
const escapeHtml = (text) => String(text == null ? '' : text).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

function debounce(fn, wait) {
  let timer = 0;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  wrapped.flush = (...args) => { clearTimeout(timer); fn(...args); };
  return wrapped;
}

function fmtBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

function fmtDuration(seconds) {
  const n = Number(seconds) || 0;
  if (!n) return '';
  const m = Math.floor(n / 60);
  const s = Math.round(n % 60);
  return m ? `${m}:${String(s).padStart(2, '0')}` : `0:${String(s).padStart(2, '0')}`;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), kind === 'bad' ? 5200 : 2600);
}

function mediaUrl(assetId) {
  return CANVAS_BASE + '/api/media/' + encodeURIComponent(assetId);
}

/* ── API ────────────────────────────────────────────────── */

async function api(path, { method = 'GET', body, headers = {}, raw = null } = {}) {
  const options = {
    method,
    headers: { 'x-canvas-token': S.token, ...headers },
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  } else if (raw !== null) {
    options.body = raw;
  }
  const response = await fetch(CANVAS_BASE + path, options);
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text.slice(0, 300) }; }
  if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
  return data;
}

/** 把生成面板贴到选中节点旁边：优先下方，其次上方，都放不下就贴右侧/左侧，并按压扁。 */
function positionComposer() {
  const el = $('#inspector');
  if (!el) return;
  const app = $('#app');
  if (app.classList.contains('inspector-hidden')) return;
  let anchors = [];
  if (S.sel.size === 1) {
    const node = S.nodeById.get([...S.sel][0]);
    if (node) anchors = [node];
  } else if (S.selEdge) {
    const edge = S.doc.edges.find((entry) => entry.id === S.selEdge);
    if (edge) anchors = [S.nodeById.get(edge.from), S.nodeById.get(edge.to)].filter(Boolean);
  }
  if (!anchors.length) return;

  const rect = viewportRect();
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of anchors) {
    const h = S.els.get(node.id)?.offsetHeight || 240;
    minX = Math.min(minX, S.view.x + node.x * S.view.scale);
    minY = Math.min(minY, S.view.y + node.y * S.view.scale);
    maxX = Math.max(maxX, S.view.x + (node.x + node.w) * S.view.scale);
    maxY = Math.max(maxY, S.view.y + (node.y + h) * S.view.scale);
  }

  const margin = 12;
  const topLimit = 56;
  const width = el.offsetWidth || 520;
  const below = Math.floor(rect.height - maxY - 10 - margin);
  const above = Math.floor(minY - 10 - topLimit);
  const right = Math.floor(rect.width - maxX - 10 - margin);
  const leftSpace = Math.floor(minX - 10 - margin - 48);

  let mode = 'below';
  if (below >= 220) mode = 'below';
  else if (above >= 220) mode = 'above';
  else if (right >= 320) mode = 'right';
  else if (leftSpace >= 320) mode = 'left';
  else mode = below >= above ? 'below' : 'above';

  let maxH;
  if (mode === 'below') maxH = Math.max(160, below);
  else if (mode === 'above') maxH = Math.max(160, above);
  else maxH = Math.max(200, Math.floor(rect.height - topLimit - margin));
  el.style.maxHeight = Math.min(560, maxH) + 'px';

  const height = el.offsetHeight || 320;
  let left;
  let top;
  if (mode === 'right') {
    left = maxX + 10;
    top = clamp(minY, topLimit, Math.max(topLimit, rect.height - height - margin));
  } else if (mode === 'left') {
    left = minX - 10 - width;
    top = clamp(minY, topLimit, Math.max(topLimit, rect.height - height - margin));
  } else if (mode === 'above') {
    left = (minX + maxX) / 2 - width / 2;
    top = Math.max(topLimit, minY - 10 - height);
  } else {
    left = (minX + maxX) / 2 - width / 2;
    top = Math.min(maxY + 10, Math.max(topLimit, rect.height - height - margin));
  }
  left = clamp(left, margin + 48, Math.max(margin + 48, rect.width - width - margin));
  el.style.left = Math.round(left) + 'px';
  el.style.top = Math.round(top) + 'px';
}

function scheduleComposerFollow() {
  // 不用 rAF：部分嵌入式浏览器会节流 rAF，面板会停在旧位置。
  if (S.composerTimer) return;
  S.composerTimer = setTimeout(() => {
    S.composerTimer = 0;
    positionComposer();
  }, 16);
}

/* ── 导演工具箱 / 资产库 ───────────────────────────────── */

let pickHandler = null;

/** 取某个节点上能当作参考的素材（音频按 kind 取；图片允许用视频节点兜底）。 */
function pickableAsset(node, kind) {
  if (!node) return null;
  const assets = node.assets || [];
  if (kind === 'audio') return assets.find((asset) => asset.kind === 'audio') || null;
  // 「形象」只认真图片：拿视频当形象会让封面空白、生成时还会被当参考视频送出去。
  const direct = assets.find((asset) => asset.kind === 'image');
  if (direct) return direct;
  // 主体节点本身不存 assets，形象/声音在主体库里；直接合成一个够用的引用对象。
  if (node.kind === 'subject') {
    const subject = S.subjects.find((entry) => entry.id === node.subjectId);
    if (!subject) return null;
    if (kind === 'audio') {
      return subject.audioId ? { id: subject.audioId, kind: 'audio', name: subject.name + ' 音色' } : null;
    }
    const imageId = subject.coverId || (subject.assetIds || [])[0];
    return imageId ? { id: imageId, kind: 'image', name: subject.name } : null;
  }
  return null;
}

/**
 * 从画布选择：进入点选模式，直接在画布上点节点（即梦那种手感）。
 * 列表弹层保留作备选（节点太多/太小的时候用）。
 */
function openCanvasPicker(kind, title, onPick) {
  cancelCanvasPick();
  // 从「主体库」弹层里点进来的：先把弹层收起来（不然蒙层挡住画布点不到东西），选完再自动弹回去。
  const subjectModalOpen = !$('#subjectModal').classList.contains('hidden');
  if (subjectModalOpen) {
    closeSubjectsModal();
    S.pickReopenSubjects = true;
  }
  S.pick = { kind, title, onPick };
  document.body.classList.add('picking');
  const label = kind === 'audio' ? '带音频的节点' : '带图片/视频的节点';
  $('#pickTitle').textContent = title;
  const bar = $('#pickBar');
  if (bar) {
    const text = $('#pickBarText');
    if (text) text.textContent = `${title}：点画布上${label}（亮起的才是可选）`;
    bar.classList.remove('hidden');
  }
  markPickable();
}

function markPickable() {
  const pick = S.pick;
  if (!pick) return;
  for (const [id, el] of S.els) {
    el.classList.toggle('pickable', Boolean(pickableAsset(S.nodeById.get(id), pick.kind)));
  }
}

function cancelCanvasPick({ reopenSubjects = true } = {}) {
  if (S.pick) S.pick = null;
  document.body.classList.remove('picking');
  $('#pickBar')?.classList.add('hidden');
  $('#pickMask')?.classList.add('hidden');
  $('#pickModal')?.classList.add('hidden');
  for (const el of S.els.values()) el.classList.remove('pickable');
  if (reopenSubjects && S.pickReopenSubjects) {
    S.pickReopenSubjects = false;
    if ($('#subjectModal').classList.contains('hidden')) openSubjectsModal();
  }
}

/** 点选模式下点了某个节点：命中就回调，未命中只提示不退出。 */
function pickFromNode(node) {
  const pick = S.pick;
  if (!pick) return false;
  const asset = pickableAsset(node, pick.kind);
  if (!asset) {
    toast(`「${node.title}」里没有可用的${pick.kind === 'audio' ? '音频' : '图片'}`, 'bad');
    return true;
  }
  const handler = pick.onPick;
  cancelCanvasPick();
  if (handler) handler(asset, node);
  return true;
}

/** 列表弹层（备选路线）。 */
function openPickerList(kind, title, onPick) {
  // 走列表时不立刻把主体库弹回来：等真正选完（或关掉列表）再恢复，避免两个弹层叠着抢焦点。
  const reopenSubjects = S.pickReopenSubjects;
  cancelCanvasPick({ reopenSubjects: false });
  const restore = () => {
    if (!reopenSubjects) return;
    S.pickReopenSubjects = false;
    if ($('#subjectModal').classList.contains('hidden')) openSubjectsModal();
  };
  S.pickListRestore = restore;
  const assets = [];
  const seen = new Set();
  for (const node of (S.doc ? S.doc.nodes : [])) {
    for (const asset of node.assets || []) {
      if (asset.kind !== kind || seen.has(asset.id)) continue;
      seen.add(asset.id);
      assets.push(asset);
    }
    if (node.kind === 'subject') {
      const extra = pickableAsset(node, kind);
      if (extra && !seen.has(extra.id)) { seen.add(extra.id); assets.push(extra); }
    }
  }
  pickHandler = onPick;
  $('#pickTitle').textContent = title;
  const grid = $('#pickGrid');
  grid.innerHTML = assets.length
    ? assets.map((asset) => `<button class="asset-card" data-asset="${asset.id}" title="${escapeHtml(asset.name || '')}">
        ${asset.kind === 'video'
          ? `<video src="${mediaUrl(asset.id)}" preload="metadata" muted></video>`
          : asset.kind === 'audio'
            ? '<span class="asset-ph">♪</span>'
            : `<img src="${mediaUrl(asset.id)}" loading="lazy" alt="">`}
        <span class="asset-meta">${escapeHtml(asset.name || asset.id.slice(0, 8))}</span>
      </button>`).join('')
    : `<div class="ref-empty">画布上还没有${kind === 'audio' ? '音频' : '图片'}素材。</div>`;
  grid.querySelectorAll('.asset-card').forEach((card) => card.addEventListener('click', () => {
    const asset = assets.find((entry) => entry.id === card.dataset.asset);
    const handler = pickHandler;
    closeCanvasPicker();
    if (asset && handler) handler(asset);
  }));
  $('#pickMask').classList.remove('hidden');
  $('#pickModal').classList.remove('hidden');
}

function closeCanvasPicker() {
  $('#pickMask').classList.add('hidden');
  $('#pickModal').classList.add('hidden');
  pickHandler = null;
  const restore = S.pickListRestore;
  S.pickListRestore = null;
  if (restore) restore();
}

async function saveSubjectFields(subject, patch, node) {
  try {
    const res = await api('/api/subjects/' + subject.id, { method: 'PATCH', body: patch });
    if (res && res.subject) Object.assign(subject, res.subject);
  } catch (error) {
    toast(error.message, 'bad');
    return;
  }
  if (node) {
    node.title = subject.name;
    const titleEl = $('.node-title', S.els.get(node.id));
    if (titleEl) titleEl.textContent = node.title;
    renderNodeBody(node);
    renderNodeStatus(node);
  }
  updateMeta();
}

async function createSubjectNode(point) {
  const rect = viewportRect();
  const target = point || screenToWorld(rect.width / 2 - 150, rect.height / 2 - 230);
  try {
    const { subject } = await api('/api/subjects', { method: 'POST', body: { name: '新主体', assetIds: [], draft: true } });
    S.subjects.unshift(subject);
    createNode('subject', target, { subjectId: subject.id, title: subject.name });
    setTimeout(() => $('#subjectNodeName')?.focus(), 80);
  } catch (error) {
    toast('创建主体失败：' + error.message, 'bad');
  }
}

function renderSubjectInspector(body, node) {
  const subject = S.subjects.find((entry) => entry.id === node.subjectId);
  if (!subject) {
    body.innerHTML = '<div class="inspector-empty">这个主体已经不在主体库里了。<div style="margin-top:12px"><button class="btn" id="rebuildSubject">重新建一个主体节点</button></div></div>';
    $('#rebuildSubject', body).addEventListener('click', () => createSubjectNode({ x: node.x, y: node.y }));
    return;
  }
  const images = subject.assetIds || [];
  body.innerHTML = `
    <div class="insp-head"><h3>${escapeHtml(subject.name)}</h3><span class="insp-kind">主体</span></div>
    <div class="insp-sub">形象 ${images.length} 张${subject.audioId ? ' · 参考音频 1 条' : ' · 暂无音频'}；把它连到图片 / 视频节点，生成时就会带上这个主体。</div>

    <div class="field">
      <label>名称</label>
      <input class="set-input" id="subjectNodeName" maxlength="40" value="${escapeHtml(subject.name)}">
    </div>
    <div class="field">
      <label>描述 <span class="hint">会写进提示词</span></label>
      <input class="set-input" id="subjectNodeDesc" maxlength="200" placeholder="例如：晓艳老师，黑色史努比 T 恤" value="${escapeHtml(subject.note || '')}">
    </div>

    <div class="field">
      <label>形象参考图 <span class="hint">1~6 张</span></label>
      <div class="subject-thumbs">
        ${images.map((id) => `<span class="subject-thumb"><img src="${mediaUrl(id)}" alt=""><button data-rm-image="${id}" title="移除">×</button></span>`).join('')}
        <button class="subject-add" id="subjectAddLocal">＋ 本地</button>
        <button class="subject-add" id="subjectAddCanvas">＋ 画布</button>
        <button class="subject-add" id="subjectAddAssets">＋ 资产库</button>
      </div>
    </div>

    <div class="field">
      <label>参考音频 <span class="hint">可选</span></label>
      ${subject.audioId
        ? `<div class="subject-audio"><audio controls preload="metadata" src="${mediaUrl(subject.audioId)}"></audio><button class="btn danger" id="subjectRmAudio">移除</button></div>`
        : '<div class="ref-empty">还没有音频，可从本地或画布添加；不传也能生成。</div>'}
      <div class="insp-actions">
        <button class="btn" id="subjectAddAudioLocal">＋ 本地音频</button>
        <button class="btn" id="subjectAddAudioCanvas">＋ 画布音频</button>
      </div>
    </div>

    <div class="insp-actions">
      <button class="btn danger" id="delNode">删除节点</button>
      <button class="btn" id="mkImage">用它做图片</button>
      <button class="btn primary" id="mkVideo">用它做视频</button>
    </div>`;

  const sync = (patch) => saveSubjectFields(subject, patch, node);
  const addImage = (assetId) => {
    if (!assetId) return;
    const next = [...new Set([...(subject.assetIds || []), assetId])].slice(0, 6);
    subject.assetIds = next;
    subject.coverId = next[0] || '';
    sync({ assetIds: next }).then(() => renderInspector());
  };

  const nameInput = $('#subjectNodeName', body);
  nameInput.addEventListener('change', () => {
    subject.name = nameInput.value.trim() || '新主体';
    sync({ name: subject.name });
  });
  const descInput = $('#subjectNodeDesc', body);
  descInput.addEventListener('change', () => {
    subject.note = descInput.value.trim();
    sync({ note: subject.note });
  });

  body.querySelectorAll('[data-rm-image]').forEach((button) => button.addEventListener('click', () => {
    const next = (subject.assetIds || []).filter((id) => id !== button.dataset.rmImage);
    subject.assetIds = next;
    subject.coverId = next[0] || '';
    sync({ assetIds: next }).then(() => renderInspector());
  }));
  $('#subjectAddLocal', body).addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.multiple = true;
    input.addEventListener('change', async () => {
      const files = [...input.files].slice(0, 6);
      for (const file of files) {
        try {
          const dims = await probeMedia(file);
          const asset = await uploadOne(file, dims);
          if (asset) addImage(asset.id);
        } catch (error) { toast('上传失败：' + error.message, 'bad'); }
      }
    });
    input.click();
  });
  $('#subjectAddCanvas', body).addEventListener('click', () => openCanvasPicker('image', '从画布选择形象', (asset) => addImage(asset.id)));
  $('#subjectAddAssets', body).addEventListener('click', () => openAssetLibrary((asset) => addImage(asset.id)));
  $('#subjectAddAudioLocal', body).addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'audio/*';
    input.addEventListener('change', async () => {
      const file = input.files[0];
      if (!file) return;
      try {
        const dims = await probeMedia(file);
        const asset = await uploadOne(file, dims);
        if (asset) { subject.audioId = asset.id; await sync({ audioId: asset.id }); renderInspector(); }
      } catch (error) { toast('上传失败：' + error.message, 'bad'); }
    });
    input.click();
  });
  $('#subjectAddAudioCanvas', body).addEventListener('click', () => openCanvasPicker('audio', '从画布选择参考音频', async (asset) => {
    subject.audioId = asset.id;
    await sync({ audioId: asset.id });
    renderInspector();
  }));
  $('#subjectRmAudio', body)?.addEventListener('click', async () => {
    subject.audioId = '';
    await sync({ audioId: '' });
    renderInspector();
  });

  const spawn = (kind) => {
    const child = createNode(kind, { x: node.x + node.w + 80, y: node.y });
    addEdge(node.id, child.id, { silent: true });
    toast('已创建下游节点，写提示词后 Ctrl+Enter 生成');
  };
  $('#mkImage', body).addEventListener('click', () => spawn('image'));
  $('#mkVideo', body).addEventListener('click', () => spawn('video'));
  $('#delNode', body).addEventListener('click', () => deleteNode(node.id));
}

let pendingSubjectAssets = [];
let pendingSubjectAudio = null;

function openSubjectsModal() {
  renderSubjectGrid();
  $('#subjectMask').classList.remove('hidden');
  $('#subjectModal').classList.remove('hidden');
  setTimeout(() => $('#subjectName')?.focus(), 60);
}

function closeSubjectsModal() {
  $('#subjectMask').classList.add('hidden');
  $('#subjectModal').classList.add('hidden');
}

function renderSubjectGrid() {
  const grid = $('#subjectGrid');
  if (!grid) return;
  if (!S.subjects.length) {
    grid.innerHTML = '<div class="ref-empty">还没有主体。上面填名字、选 1~6 张参考图，保存即可。</div>';
    return;
  }
  grid.innerHTML = S.subjects.map((subject) => `<div class="subject-card" data-id="${subject.id}">
    <img src="${mediaUrl(subject.coverId)}" alt="">
    <div class="subject-meta"><b>${escapeHtml(subject.name)}${subject.audioId ? ' ♪' : ''}</b><span>${subject.assetIds.length} 张形象${subject.audioId ? ' · 带参考音频' : ''}</span></div>
    <div class="subject-actions"><button data-rename>重命名</button><button data-del>删除</button></div>
  </div>`).join('');

  $$('.subject-card', grid).forEach((card) => {
    const subject = S.subjects.find((entry) => entry.id === card.dataset.id);
    if (!subject) return;
    card.querySelector('[data-rename]').addEventListener('click', () => {
      const label = card.querySelector('.subject-meta b');
      const input = document.createElement('input');
      input.className = 'set-input';
      input.value = subject.name;
      label.replaceWith(input);
      input.focus();
      input.select();
      let done = false;
      const commit = async () => {
        if (done) return;
        done = true;
        const name = input.value.trim();
        if (name && name !== subject.name) {
          await api('/api/subjects/' + subject.id, { method: 'PATCH', body: { name } }).catch(() => null);
          subject.name = name;
        }
        renderSubjectGrid();
        renderInspector();
      };
      input.addEventListener('blur', commit, { once: true });
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') input.blur();
        if (event.key === 'Escape') { input.value = subject.name; input.blur(); }
      });
    });
    card.querySelector('[data-del]').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      if (!button.classList.contains('confirm')) {
        button.classList.add('confirm');
        button.textContent = '确认删除';
        setTimeout(() => { if (button.isConnected) { button.classList.remove('confirm'); button.textContent = '删除'; } }, 3000);
        return;
      }
      await api('/api/subjects/' + subject.id, { method: 'DELETE' }).catch(() => null);
      S.subjects = S.subjects.filter((entry) => entry.id !== subject.id);
      renderSubjectGrid();
      renderInspector();
    });
  });
}

function renderPendingSubjectAssets() {
  const preview = $('#subjectPreview');
  if (!preview) return;
  preview.innerHTML = pendingSubjectAssets.map((asset) => '<img src="' + mediaUrl(asset.id) + '" alt="">').join('');
  const box = $('#subjectAudioPreview');
  if (box) {
    box.innerHTML = pendingSubjectAudio
      ? '<audio controls preload="metadata" src="' + mediaUrl(pendingSubjectAudio.id) + '"></audio><span class="audio-name">' + escapeHtml(pendingSubjectAudio.name || '') + '</span>'
      : '';
  }
}

async function handleSubjectFiles(files) {
  const list = [...files].filter((file) => file.type.startsWith('image/')).slice(0, 6);
  if (!list.length) return;
  const preview = $('#subjectPreview');
  preview.innerHTML = '<span class="ref-empty">上传中…</span>';
  const uploaded = [];
  for (const file of list) {
    try {
      const dims = await probeMedia(file);
      uploaded.push(await uploadOne(file, dims));
    } catch { /* 单张失败就跳过 */ }
  }
  pendingSubjectAssets = uploaded;
  renderPendingSubjectAssets();
}

async function handleSubjectAudio(files) {
  const file = [...files].find((entry) => entry.type.startsWith('audio/'));
  if (!file) return;
  const box = $('#subjectAudioPreview');
  box.innerHTML = '<span class="ref-empty">上传中…</span>';
  try {
    const dims = await probeMedia(file);
    const asset = await uploadOne(file, dims);
    pendingSubjectAudio = asset;
    renderPendingSubjectAssets();
  } catch (error) {
    box.innerHTML = `<span class="ref-empty">${escapeHtml(error.message)}</span>`;
  }
}

async function saveSubjectDraft() {
  const name = ($('#subjectName')?.value || '').trim();
  if (!pendingSubjectAssets.length) { toast('先选 1~6 张形象参考图', 'bad'); return; }
  try {
    const { subject } = await api('/api/subjects', {
      method: 'POST',
      body: { name, assetIds: pendingSubjectAssets.map((asset) => asset.id), audioId: pendingSubjectAudio ? pendingSubjectAudio.id : '' },
    });
    S.subjects.unshift(subject);
    pendingSubjectAssets = [];
    pendingSubjectAudio = null;
    if ($('#subjectName')) $('#subjectName').value = '';
    if ($('#subjectPreview')) $('#subjectPreview').innerHTML = '';
    if ($('#subjectAudioPreview')) $('#subjectAudioPreview').innerHTML = '';
    renderSubjectGrid();
    renderInspector();
    toast('主体已保存', 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  }
}

const KIT_GROUPS = [
  { title: '相机 / 机位', items: ['低角度仰拍', '俯拍全景', '贴近特写', '过肩镜头', '广角空镜', '长焦压缩'] },
  { title: '运镜', items: ['镜头缓慢推近', '镜头缓慢拉远', '环绕拍摄', '横向跟拍', '垂直摇镜', '手持轻晃'] },
  { title: '配色', items: ['冷调蓝灰', '暖调橘黄', '胶片颗粒', '低饱和莫兰迪', '高对比霓虹', '黑白'] },
  { title: '风格', items: ['电影感', '日系清透', '赛博霓虹', '水墨写意', '写实纪实', '黏土定格'] },
];

function openDirectorKit() {
  const body = $('#kitBody');
  body.innerHTML = KIT_GROUPS.map((group) => `<section class="kit-group">
    <h4>${group.title}</h4>
    <div class="chips">${group.items.map((item) => `<button class="chip" data-kit-item="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join('')}</div>
  </section>`).join('');
  $$('[data-kit-item]', body).forEach((button) => button.addEventListener('click', () => {
    const node = S.sel.size === 1 ? S.nodeById.get([...S.sel][0]) : null;
    if (!node) { toast('先在画布上选中一个节点', 'bad'); return; }
    if (node.kind !== 'image' && node.kind !== 'video') { toast('这个节点没有提示词', 'bad'); return; }
    node.prompt = node.prompt ? `${node.prompt.replace(/[，,]s*$/, '')}，${button.dataset.kitItem}` : button.dataset.kitItem;
    node.updatedAt = Date.now();
    markDirty();
    renderInspector();
    renderNodeStatus(node);
    toast(`已追加：${button.dataset.kitItem}`, 'ok');
  }));
  $('#kitMask').classList.remove('hidden');
  $('#kitModal').classList.remove('hidden');
}

function closeDirectorKit() {
  $('#kitMask').classList.add('hidden');
  $('#kitModal').classList.add('hidden');
}

async function openAssetLibrary(onPick = null) {
  $('#assetMask').classList.remove('hidden');
  $('#assetModal').classList.remove('hidden');
  const grid = $('#assetGrid');
  grid.innerHTML = '<div class="ref-empty">正在读取资产…</div>';
  try {
    const { assets } = await api('/api/media');
    const list = Array.isArray(assets) ? assets : [];
    if (!list.length) {
      grid.innerHTML = '<div class="ref-empty">还没有素材。把图片 / 视频 / 音频拖进画布，或在上传节点里添加。</div>';
      return;
    }
    grid.innerHTML = list.map((asset) => `<button class="asset-card" data-asset="${asset.id}" title="${escapeHtml(asset.name || '')}">
      ${asset.kind === 'video'
        ? `<video src="${mediaUrl(asset.id)}" preload="metadata" muted></video>`
        : asset.kind === 'audio'
          ? '<span class="asset-ph">♪</span>'
          : `<img src="${mediaUrl(asset.id)}" loading="lazy" alt="">`}
      <span class="asset-meta">${escapeHtml(asset.name || asset.id.slice(0, 8))}</span>
    </button>`).join('');
    $$('.asset-card', grid).forEach((card) => card.addEventListener('click', () => {
      const asset = list.find((entry) => entry.id === card.dataset.asset);
      if (!asset) return;
      if (onPick) { closeAssetLibrary(); onPick(asset); return; }
      const selected = S.sel.size === 1 ? S.nodeById.get([...S.sel][0]) : null;
      if (selected && selected.kind === 'upload') {
        selected.assets = [asset];
        selected.updatedAt = Date.now();
        renderNodeBody(selected);
        renderNodeStatus(selected);
        renderInspector();
        markDirty();
        closeAssetLibrary();
        toast('已替换当前素材节点', 'ok');
        return;
      }
      const rect = viewportRect();
      const point = screenToWorld(rect.width / 2 - 150, rect.height / 2 - 230);
      createNode('upload', point, { assets: [asset] });
      closeAssetLibrary();
      toast('已从资产库放入画布', 'ok');
    }));
  } catch (error) {
    grid.innerHTML = `<div class="ref-empty">读取失败：${escapeHtml(error.message)}</div>`;
  }
}

function closeAssetLibrary() {
  $('#assetMask').classList.add('hidden');
  $('#assetModal').classList.add('hidden');
}

/* ── 启动 ───────────────────────────────────────────────── */

async function boot() {
  try {
    const [configRes, listRes, healthRes] = await Promise.all([
      api('/api/config'),
      api('/api/canvases'),
      api('/api/health').catch(() => null),
    ]);
    S.config = configRes.config;
    S.paths = configRes.paths;
    S.health = healthRes;
    S.version = (healthRes && healthRes.version) || '';
    loadSkills();
    api('/api/pricing').then((res) => { S.pricing = res.pricing; }).catch(() => {});
    renderAgentMode();
    if (window.heronbo) S.desktop = await window.heronbo.info().catch(() => null);
    applyTheme(S.config.theme);
    applyPerf(S.config.performanceMode);
    updateBrandLine();
    S.canvases = listRes.canvases || [];
    S.subjects = (await api('/api/subjects').catch(() => ({ subjects: [] }))).subjects || [];

    let canvasId = localStorage.getItem('doubao-canvas:last');
    if (!S.canvases.some((item) => item.id === canvasId)) canvasId = S.canvases[0] && S.canvases[0].id;
    if (!canvasId) {
      const created = await api('/api/canvases', { method: 'POST', body: { title: '我的画布' } });
      canvasId = created.canvas.id;
      S.canvases = [{ id: canvasId, title: created.canvas.title, nodeCount: 0, edgeCount: 0, updatedAt: Date.now() }];
    }
    await openCanvas(canvasId);
    renderSidebar();
    refreshGateway();
    refreshStats();
    bindGlobalEvents();
    S.pollTimer = setInterval(pollTasks, 1400);
    setInterval(refreshGateway, 45000);
    setInterval(refreshStats, 60000);
  } catch (error) {
    showBootError(error);
  }
}

function showBootError(error) {
  const el = $('#bootError');
  el.className = 'boot-error';
  el.innerHTML = `<div><strong style="font-size:16px;color:var(--ink)">画布没能启动</strong><br><br>
    ${escapeHtml(error.message || error)}<br><br>
    确认服务在运行：<code>node server.js</code><br>然后刷新这个页面。</div>`;
}

/* ── 画布加载 / 保存 ─────────────────────────────────────── */

async function openCanvas(id) {
  if (S.doc) await saveCanvasNow();
  cancelCanvasPick({ reopenSubjects: false });
  const { canvas } = await api('/api/canvases/' + id);
  S.doc = canvas;
  S.view = { ...canvas.viewport };
  $('#canvasTitle').value = canvas.title;
  document.title = canvas.title + ' · HeronBo 豆包画布';
  S.nodeById = new Map(canvas.nodes.map((node) => [node.id, node]));
  S.sel.clear();
  S.selEdge = null;
  S.history.past.length = 0;
  S.history.future.length = 0;
  S.typing = null;
  S.saveState = 'saved';
  localStorage.setItem('doubao-canvas:last', id);
  buildAllNodes();
  applyView(true);
  renderEdges('structure');
  renderSidebar();
  renderInspector();
  updateMeta();
  updateEmpty();
  if (S.gateway) refreshGateway();
}

function currentCanvasSummary() {
  return {
    id: S.doc.id,
    title: S.doc.title,
    nodeCount: S.doc.nodes.length,
    edgeCount: S.doc.edges.length,
    updatedAt: Date.now(),
  };
}

function markDirty() {
  S.saveState = 'saving';
  updateMeta();
  saveSoon();
}

const saveSoon = debounce(() => { saveCanvasNow(); }, 550);
const saveViewSoon = debounce(() => { saveCanvasNow({ viewportOnly: true }); }, 900);

async function saveCanvasNow({ viewportOnly = false } = {}) {
  if (!S.doc) return;
  const payload = viewportOnly
    ? { viewport: S.view }
    : { title: S.doc.title, nodes: S.doc.nodes, edges: S.doc.edges, viewport: S.view };
  try {
    await api('/api/canvases/' + S.doc.id, { method: 'PUT', body: payload });
    S.saveState = 'saved';
    updateMeta();
    const summary = S.canvases.find((item) => item.id === S.doc.id);
    Object.assign(summary || {}, currentCanvasSummary());
    renderSidebar();
  } catch (error) {
    S.saveState = 'error';
    updateMeta();
    toast('保存失败：' + error.message, 'bad');
  }
}

function updateMeta() {
  if (!S.doc) return;
  const state = S.saveState === 'saving' ? '保存中…' : S.saveState === 'error' ? '保存失败' : '已保存';
  $('#canvasMeta').textContent = `${S.doc.nodes.length} 节点 · ${S.doc.edges.length} 连线 · ${state}`;
}

function updateEmpty() {
  const show = Boolean(S.doc) && S.doc.nodes.length === 0;
  $('#emptyState').classList.toggle('hidden', !show);
}

/* ── 侧边栏 ─────────────────────────────────────────────── */

function renderSidebar() {
  const list = $('#canvasList');
  list.innerHTML = '';
  for (const item of S.canvases) {
    const row = document.createElement('div');
    row.className = 'canvas-item' + (S.doc && item.id === S.doc.id ? ' active' : '');
    row.dataset.id = item.id;
    row.innerHTML = `
      <div class="ci-body">
        <span class="ci-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</span>
        <span class="ci-meta">${item.nodeCount || 0} 个节点</span>
      </div>
      <button class="ci-edit" title="重命名画布">✎</button>
      <button class="ci-x" title="删除画布">×</button>`;
    row.addEventListener('click', (event) => {
      if (event.target.closest('.ci-x, .ci-edit')) return;
      if (item.id !== (S.doc && S.doc.id)) openCanvas(item.id);
    });
    row.addEventListener('dblclick', (event) => {
      if (event.target.closest('.ci-x, .ci-edit')) return;
      renameCanvasInline(row, item);
    });
    row.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      event.stopPropagation();
      openCanvasContextMenu(item, event.clientX, event.clientY);
    });
    row.querySelector('.ci-edit').addEventListener('click', (event) => {
      event.stopPropagation();
      renameCanvasInline(row, item);
    });
    row.querySelector('.ci-x').addEventListener('click', async (event) => {
      const btn = event.currentTarget;
      if (!btn.classList.contains('confirm')) {
        btn.classList.add('confirm');
        btn.textContent = '确认删除';
        setTimeout(() => {
          if (btn.isConnected) { btn.classList.remove('confirm'); btn.textContent = '×'; }
        }, 3000);
        return;
      }
      await deleteCanvas(item);
    });
    list.appendChild(row);
  }
}

function renameCanvasInline(row, item) {
  const titleEl = row.querySelector('.ci-title');
  const input = document.createElement('input');
  input.className = 'set-input';
  input.style.cssText = 'font-size:11.5px;padding:2px 5px';
  input.value = item.title;
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = async () => {
    const title = input.value.trim() || item.title;
    item.title = title;
    if (S.doc.id === item.id) S.doc.title = title;
    $('#canvasTitle').value = S.doc.id === item.id ? title : $('#canvasTitle').value;
    await api('/api/canvases/' + item.id, { method: 'PUT', body: { title, nodes: S.doc.id === item.id ? S.doc.nodes : undefined, edges: S.doc.id === item.id ? S.doc.edges : undefined, viewport: S.doc.id === item.id ? S.view : undefined } }).catch(() => {});
    renderSidebar();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
    if (event.key === 'Escape') { event.preventDefault(); renderSidebar(); }
  });
  input.addEventListener('blur', commit, { once: true });
}

async function deleteCanvas(item) {
  const deletingCurrent = S.doc && S.doc.id === item.id;
  await api('/api/canvases/' + item.id, { method: 'DELETE' });
  S.canvases = S.canvases.filter((entry) => entry.id !== item.id);
  if (deletingCurrent) S.doc = null;
  if (!S.canvases.length) {
    const created = await api('/api/canvases', { method: 'POST', body: { title: '我的画布' } });
    S.canvases = [{ id: created.canvas.id, title: created.canvas.title, nodeCount: 0, edgeCount: 0, updatedAt: Date.now() }];
    await openCanvas(created.canvas.id);
    renderSidebar();
    toast('画布已删除，已自动新建一张空白画布');
    return;
  }
  if (deletingCurrent) await openCanvas(S.canvases[0].id);
  renderSidebar();
  toast('画布已删除');
}

async function duplicateCanvas(item) {
  const { canvas } = await api('/api/canvases/' + item.id);
  const created = await api('/api/canvases', { method: 'POST', body: { title: (item.title + ' 副本').slice(0, 60) } });
  await api('/api/canvases/' + created.canvas.id, {
    method: 'PUT',
    body: { nodes: canvas.nodes, edges: canvas.edges, viewport: canvas.viewport },
  });
  S.canvases.unshift({
    id: created.canvas.id,
    title: created.canvas.title,
    nodeCount: canvas.nodes.length,
    edgeCount: canvas.edges.length,
    updatedAt: Date.now(),
  });
  await openCanvas(created.canvas.id);
  renderSidebar();
  toast('已复制画布');
}

async function newCanvas() {
  const created = await api('/api/canvases', { method: 'POST', body: { title: '新画布' } });
  S.canvases.unshift({ id: created.canvas.id, title: created.canvas.title, nodeCount: 0, edgeCount: 0, updatedAt: Date.now() });
  await openCanvas(created.canvas.id);
  toast('已新建画布');
}

/* ── 节点 DOM ───────────────────────────────────────────── */

function buildAllNodes() {
  const container = $('#nodes');
  container.innerHTML = '';
  S.els.clear();
  // 先把上一张画布的连线元素摘掉：edgeEls 清空后就再也拿不到它们了。
  for (const path of S.edgeEls.values()) path.remove();
  S.edgeEls.clear();
  for (const node of S.doc.nodes) buildNodeEl(node);
  updateEmpty();
  if (S.pick) markPickable();
}

function buildNodeEl(node) {
  const el = document.createElement('article');
  el.className = 'node';
  el.dataset.id = node.id;
  el.innerHTML = `
    <span class="port in" data-port="in" title="输入：接收上游节点"></span>
    <span class="port out" data-port="out" title="输出：拖到下游节点的左侧圆点"></span>
    <div class="node-head">
      <span class="node-icon ${node.kind}">${KIND_META[node.kind].icon}</span>
      <span class="node-title"></span>
      <button class="node-close" title="删除节点">×</button>
    </div>
    <div class="node-media"></div>
    <div class="node-foot">
      <div class="node-status"><span></span></div>
      <button class="node-run" title="生成（Ctrl+Enter）">▷</button>
    </div>
    <div class="node-resize" title="拖动调整宽度"></div>`;

  const titleEl = $('.node-title', el);
  titleEl.textContent = node.title;
  titleEl.addEventListener('dblclick', (event) => { event.stopPropagation(); editNodeTitle(node, titleEl); });

  applyNodeGeometry(el, node);
  renderNodeBody(node, el);
  renderNodeStatus(node, el);

  el.addEventListener('pointerdown', onNodePointerDown);
  $('.node-close', el).addEventListener('pointerdown', (event) => event.stopPropagation());
  $('.node-close', el).addEventListener('click', (event) => { event.stopPropagation(); deleteNode(node.id); });
  $('.node-run', el).addEventListener('pointerdown', (event) => event.stopPropagation());
  $('.node-run', el).addEventListener('click', (event) => { event.stopPropagation(); runNode(node); });
  $('.node-resize', el).addEventListener('pointerdown', (event) => onNodeResizeStart(event, node));

  const outPort = $('.port.out', el);
  outPort.addEventListener('pointerdown', (event) => onPortDown(event, node));
  const inPort = $('.port.in', el);
  inPort.addEventListener('pointerdown', (event) => event.stopPropagation());

  $('#nodes').appendChild(el);
  S.els.set(node.id, el);
  return el;
}

function applyNodeGeometry(el, node) {
  el.style.transform = `translate(${node.x}px, ${node.y}px)`;
  el.style.width = node.w + 'px';
}

function renderNodeBody(node, el = S.els.get(node.id), force = false) {
  if (!el) return;
  const body = $('.node-media', el);
  const subjectOfNode = node.kind === 'subject' ? S.subjects.find((entry) => entry.id === node.subjectId) : null;
  const subjectSig = subjectOfNode ? [subjectOfNode.name, subjectOfNode.assetIds.join('.'), subjectOfNode.audioId || ''].join('~') : '';
  const signature = node.kind + '|' + node.assets.map((asset) => asset.id).join(',') + '|' + (node.text || '').slice(0, 40) + '|' + subjectSig;
  if (!force && body.dataset.sig === signature) return;
  body.dataset.sig = signature;

  if (node.kind === 'subject') {
    body.innerHTML = `<div class="subject-body">
      ${subjectOfNode && subjectOfNode.coverId
        ? `<img src="${mediaUrl(subjectOfNode.coverId)}" loading="lazy" alt="">`
        : '<div class="subject-ph">◍</div>'}
      <div class="subject-line">
        <b>${escapeHtml(subjectOfNode ? subjectOfNode.name : '主体已失效')}</b>
        <span>${subjectOfNode ? subjectOfNode.assetIds.length : 0} 张形象${subjectOfNode && subjectOfNode.audioId ? ' · 带音频' : ''}</span>
      </div>
    </div>`;
    return;
  }
  if (node.kind === 'text') {
    body.innerHTML = `<div class="text-body ${node.text.trim() ? '' : 'empty'}">${escapeHtml(node.text.trim() || '双击节点，在右侧面板写文案；下游节点可以用 @ 引用这段文字。')}</div>`;
    return;
  }
  if (!node.assets.length) {
    const hint = node.kind === 'upload'
      ? '<b>把图片 / 视频拖进来</b><br>或者双击画布新建素材节点'
      : node.kind === 'video'
        ? '<b>视频节点</b><br>在右侧写提示词，可连一张上游图片做参考'
        : '<b>图片节点</b><br>在右侧写提示词，Ctrl+Enter 开始生成';
    body.innerHTML = `<div class="node-placeholder">${hint}</div>`;
    return;
  }

  const assets = node.assets.slice(0, MAX_ASSETS_SHOWN);
  if (assets.length === 1 && assets[0].kind === 'audio') {
    const asset = assets[0];
    body.innerHTML = `<div class="audio-body">
      <audio controls preload="metadata" src="${mediaUrl(asset.id)}"></audio>
      <span class="audio-name">${escapeHtml(asset.name || '音频素材')}${asset.duration ? ' · ' + fmtDuration(asset.duration) : ''}</span>
    </div>`;
    return;
  }
  const grid = document.createElement('div');
  grid.className = 'media-grid' + (assets.length === 1 ? ' one' : '');
  assets.forEach((asset, index) => {
    const item = document.createElement('div');
    item.className = 'media-item';
    if (asset.kind === 'video') {
      if (asset.coverId) {
        item.innerHTML = `<img src="${mediaUrl(asset.coverId)}" loading="lazy" decoding="async" draggable="false" alt="">
          <span class="play-badge"><i>▶</i></span>`;
      } else {
        item.innerHTML = `<video src="${mediaUrl(asset.id)}" preload="none" muted playsinline loop></video>
          <span class="play-badge"><i>▶</i></span>`;
      }
    } else {
      item.innerHTML = `<img src="${mediaUrl(asset.id)}" loading="lazy" decoding="async" draggable="false" alt="">`;
    }
    item.insertAdjacentHTML('beforeend', `
      <span class="media-index">${index + 1}${asset.kind === 'video' && asset.duration ? ' · ' + fmtDuration(asset.duration) : (asset.w && asset.h ? ' · ' + asset.w + '×' + asset.h : '')}</span>
      <span class="media-tools">
        <button data-dl title="下载">⤓</button>
        <button data-rm title="从节点移除">×</button>
      </span>`);
    item.querySelector('[data-dl]').addEventListener('click', (event) => {
      event.stopPropagation();
      downloadAsset(asset.id);
    });
    item.querySelector('[data-rm]').addEventListener('click', (event) => {
      event.stopPropagation();
      pushHistory();
      node.assets = node.assets.filter((entry) => entry.id !== asset.id);
      renderNodeBody(node, el, true);
      markDirty();
      renderInspector();
    });
    grid.appendChild(item);
  });
  body.innerHTML = '';
  body.appendChild(grid);
}

/** 前端侧的同一套点数口径（服务端 /api/pricing 会给实际表）。 */
function estimateCost(node) {
  const pricing = S.pricing;
  if (!pricing) return null;
  const params = nodeParams(node);
  const table = pricing[node.kind === 'video' ? 'video' : 'image'];
  if (!table) return null;
  if (node.kind === 'video') {
    const row = (table.perSecond || {})[params.model] || {};
    const rate = typeof row === 'number' ? row : Number(row[params.quality]) || 0;
    const seconds = Math.max(1, Number(params.duration) || 5);
    return { points: Math.round(rate * seconds * 10) / 10, detail: rate ? `${rate} 点/秒 × ${seconds} 秒` : '' };
  }
  const base = (table.perSheet || {})[params.quality] || 0;
  const factor = (table.modelFactor || {})[params.model] || 1;
  const sheets = Math.max(1, Number(params.count) || 1);
  return { points: Math.round(base * factor * sheets * 10) / 10, detail: base ? `${base} 点/张 × ${factor} × ${sheets} 张` : '' };
}

function refreshCostLine(node) {
  const el = document.getElementById('costLine');
  if (!el) return;
  const est = estimateCost(node);
  if (!est || !est.points) { el.textContent = ''; el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.innerHTML = `预计消耗 <b>≈ ${est.points} 点</b>${est.detail ? `<span class="cost-detail">${escapeHtml(est.detail)}</span>` : ''}`;
  el.title = (S.pricing && S.pricing.note) || '预估，实际以豆包账单为准';
}

function renderNodeStatus(node, el = S.els.get(node.id)) {
  if (!el) return;
  const statusEl = $('.node-status', el);
  const runBtn = $('.node-run', el);
  const label = $('span', statusEl);
  statusEl.className = 'node-status';
  runBtn.className = 'node-run';
  runBtn.textContent = '▷';
  runBtn.disabled = false;

  if (node.kind === 'subject') {
    const subject = S.subjects.find((entry) => entry.id === node.subjectId);
    label.textContent = subject
      ? subject.assetIds.length + ' 张形象' + (subject.audioId ? ' · 带音频' : '')
      : '主体已失效';
    runBtn.classList.add('hidden');
    return;
  }
  if (node.kind === 'upload') {
    label.textContent = node.assets.length ? `本地素材 · ${node.assets.length} 项` : '等待上传';
    runBtn.classList.add('hidden');
    return;
  }
  runBtn.classList.remove('hidden');
  if (node.kind === 'text') {
    label.textContent = node.text.trim() ? `文本 · ${node.text.trim().length} 字` : '空白文本';
    runBtn.classList.add('hidden');
    return;
  }

  if (['queued', 'running', 'preparing', 'downloading', 'submitting'].includes(node.status)) {
    statusEl.classList.add('running');
    label.textContent = node.statusText || '生成中…';
    runBtn.textContent = '◌';
    runBtn.classList.add('running');
    runBtn.disabled = true;
  } else if (node.status === 'prepared') {
    statusEl.classList.add('done');
    label.textContent = '已准备 · 待确认';
    runBtn.textContent = '▷';
  } else if (node.status === 'error') {
    statusEl.classList.add('error');
    label.textContent = node.statusText || '生成失败';
    runBtn.textContent = '↻';
    runBtn.title = '重试';
  } else if (node.status === 'done' || node.assets.length) {
    statusEl.classList.add('done');
    label.textContent = `完成 · ${node.assets.length} ${node.kind === 'video' ? '个视频' : '张图片'}`;
    runBtn.textContent = '↻';
    runBtn.title = '再生成一次';
  } else {
    const p = nodeParams(node);
    const paramText = node.kind === 'video'
      ? `${node.ratio} · ${p.duration} 秒 · ${p.quality}`
      : `${node.ratio} · ${p.quality}${p.count > 1 ? ' · ' + p.count + ' 张' : ''}`;
    label.textContent = `待生成 · ${paramText}`;
    runBtn.textContent = '▷';
  }
}

function editNodeTitle(node, titleEl) {
  const input = document.createElement('input');
  input.className = 'node-title-input';
  input.value = node.title;
  input.maxLength = 60;
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = () => {
    const title = input.value.trim() || KIND_META[node.kind].title;
    if (title !== node.title) { pushHistory(); node.title = title; markDirty(); }
    input.replaceWith(titleEl);
    titleEl.textContent = node.title;
    renderInspector();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
    if (event.key === 'Escape') { event.preventDefault(); input.value = node.title; commit(); }
  });
  input.addEventListener('blur', commit, { once: true });
}

/* ── 视口 / 变换 / 裁剪 ─────────────────────────────────── */

function applyView(smooth = false) {
  scheduleComposerFollow();
  const world = $('#world');
  if (smooth) {
    world.classList.add('smooth');
    setTimeout(() => world.classList.remove('smooth'), 220);
  }
  world.style.transform = `translate(${S.view.x}px, ${S.view.y}px) scale(${S.view.scale})`;
  const grid = $('#grid');
  const size = GRID * S.view.scale;
  grid.style.backgroundSize = `${size}px ${size}px`;
  grid.style.backgroundPosition = `${S.view.x}px ${S.view.y}px`;
  $('#zoomLabel').textContent = Math.round(S.view.scale * 100) + '%';
  scheduleCull();
  scheduleMinimap();
  saveViewSoon();
}

function screenToWorld(sx, sy) {
  return { x: (sx - S.view.x) / S.view.scale, y: (sy - S.view.y) / S.view.scale };
}

function viewportRect() {
  const rect = $('#viewport').getBoundingClientRect();
  return rect;
}

function zoomAt(sx, sy, factor) {
  const next = clamp(S.view.scale * factor, 0.15, 2.5);
  if (Math.abs(next - S.view.scale) < 0.0001) return;
  const worldPoint = screenToWorld(sx, sy);
  S.view.scale = next;
  S.view.x = sx - worldPoint.x * next;
  S.view.y = sy - worldPoint.y * next;
  applyView();
}

function scheduleCull() {
  if (S.cullRaf) return;
  S.cullRaf = requestAnimationFrame(() => {
    S.cullRaf = 0;
    cullNodes();
  });
}

function cullNodes() {
  const rect = viewportRect();
  const margin = 480;
  const left = -S.view.x / S.view.scale - margin;
  const top = -S.view.y / S.view.scale - margin;
  const right = left + rect.width / S.view.scale + margin * 2;
  const bottom = top + rect.height / S.view.scale + margin * 2;
  for (const node of S.doc.nodes) {
    const el = S.els.get(node.id);
    if (!el) continue;
    const visible = node.x + node.w > left && node.x < right && node.y + 300 > top && node.y < bottom;
    el.classList.toggle('culled', !visible);
    if (!visible) {
      const video = el.querySelector('video');
      if (video && !video.paused) video.pause();
    }
  }
}

/* ── 连线 ───────────────────────────────────────────────── */

function portPoints(fromNode, toNode) {
  const y1 = fromNode.y + 30;
  const y2 = toNode.y + 30;
  return {
    x1: fromNode.x + fromNode.w,
    y1,
    x2: toNode.x,
    y2,
  };
}

function wirePath(fromNode, toNode) {
  const p = portPoints(fromNode, toNode);
  const dx = Math.max(46, Math.abs(p.x2 - p.x1) * 0.42);
  return `M ${p.x1} ${p.y1} C ${p.x1 + dx} ${p.y1}, ${p.x2 - dx} ${p.y2}, ${p.x2} ${p.y2}`;
}

function renderEdges(mode = 'structure', movedIds = null) {
  const svg = $('#edges');
  if (mode === 'structure') {
    const existing = new Set(S.doc.edges.map((edge) => edge.id));
    for (const [id, path] of S.edgeEls) {
      if (!existing.has(id)) { path.remove(); S.edgeEls.delete(id); }
    }
    for (const edge of S.doc.edges) {
      let path = S.edgeEls.get(edge.id);
      const fromNode = S.nodeById.get(edge.from);
      const toNode = S.nodeById.get(edge.to);
      if (!fromNode || !toNode) continue;
      if (!path) {
        path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('class', 'wire');
        path.dataset.id = edge.id;
        path.addEventListener('pointerdown', (event) => {
          event.stopPropagation();
          selectEdge(edge.id);
        });
        svg.appendChild(path);
        S.edgeEls.set(edge.id, path);
      }
      path.setAttribute('d', wirePath(fromNode, toNode));
      path.classList.toggle('selected', S.selEdge === edge.id);
    }
    return;
  }

  const affected = movedIds ? new Set(movedIds) : null;
  for (const edge of S.doc.edges) {
    if (affected && !affected.has(edge.from) && !affected.has(edge.to)) continue;
    const path = S.edgeEls.get(edge.id);
    const fromNode = S.nodeById.get(edge.from);
    const toNode = S.nodeById.get(edge.to);
    if (path && fromNode && toNode) path.setAttribute('d', wirePath(fromNode, toNode));
  }
}

function addEdge(fromId, toId, { silent = false } = {}) {
  if (!fromId || !toId || fromId === toId) return null;
  if (!S.nodeById.has(fromId) || !S.nodeById.has(toId)) return null;
  if (S.doc.edges.some((edge) => edge.from === fromId && edge.to === toId)) return null;
  pushHistory();
  const edge = { id: uid('e'), from: fromId, to: toId };
  S.doc.edges.push(edge);
  renderEdges('structure');
  markDirty();
  if (!silent && S.sel.size === 1 && S.sel.has(toId)) renderInspector();
  return edge;
}

function selectEdge(id) {
  S.composerHidden = false;
  S.sel.clear();
  S.selEdge = id;
  syncSelection();
  renderEdges('structure');
  renderInspector();
}

/* ── 选择 / 历史 ────────────────────────────────────────── */

function selectOnly(id) {
  S.composerHidden = false;
  S.sel.clear();
  if (id) S.sel.add(id);
  S.selEdge = null;
  syncSelection();
  renderEdges('structure');
  renderInspector();
}

function toggleSelect(id) {
  S.composerHidden = false;
  if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
  S.selEdge = null;
  syncSelection();
  renderEdges('structure');
  renderInspector();
}

function clearSelection() {
  if (!S.sel.size && !S.selEdge) return;
  S.sel.clear();
  S.selEdge = null;
  syncSelection();
  renderEdges('structure');
  renderInspector();
}

function syncSelection() {
  for (const [id, el] of S.els) el.classList.toggle('selected', S.sel.has(id));
}

function snapshot() {
  return {
    nodes: JSON.parse(JSON.stringify(S.doc.nodes)),
    edges: JSON.parse(JSON.stringify(S.doc.edges)),
  };
}

function pushHistory() {
  if (!S.doc) return;
  S.history.past.push(snapshot());
  if (S.history.past.length > 60) S.history.past.shift();
  S.history.future.length = 0;
}

function restoreSnapshot(state) {
  S.doc.nodes = state.nodes;
  S.doc.edges = state.edges;
  S.nodeById = new Map(S.doc.nodes.map((node) => [node.id, node]));
  S.sel.clear();
  S.selEdge = null;
  buildAllNodes();
  renderEdges('structure');
  syncSelection();
  renderInspector();
  updateMeta();
  markDirty();
}

function undo() {
  if (!S.history.past.length) return toast('没有可撤销的操作');
  S.history.future.push(snapshot());
  restoreSnapshot(S.history.past.pop());
}

function redo() {
  if (!S.history.future.length) return toast('没有可重做的操作');
  S.history.past.push(snapshot());
  restoreSnapshot(S.history.future.pop());
}

/* ── 指针交互：平移 / 框选 / 拖节点 / 连线 ─────────────── */

function bindGlobalEvents() {
  const viewport = $('#viewport');

  viewport.addEventListener('wheel', (event) => {
    event.preventDefault();
    const rect = viewportRect();
    if (event.ctrlKey || event.metaKey) {
      zoomAt(event.clientX - rect.left, event.clientY - rect.top, event.deltaY < 0 ? 1.08 : 0.925);
    } else {
      S.view.x -= event.deltaX;
      S.view.y -= event.deltaY;
      applyView();
    }
  }, { passive: false });

  viewport.addEventListener('pointerdown', onViewportPointerDown);
  viewport.addEventListener('dblclick', (event) => {
    if (event.target.closest('.node, .zoombar, .minimap, .empty-state')) return;
    const rect = viewportRect();
    S.addPoint = screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
    showAddMenu(event.clientX, event.clientY);
  });

  viewport.addEventListener('contextmenu', (event) => {
    if (event.target.closest('.zoombar, .minimap')) return;
    event.preventDefault();
    const edgeEl = event.target.closest('#edges path.wire');
    if (edgeEl) {
      selectEdge(edgeEl.dataset.id);
      openContextMenu(event.clientX, event.clientY, [
        { label: '删除这条连线', icon: '×', danger: true, action: () => deleteSelected() },
      ]);
      return;
    }
    const nodeEl = event.target.closest('.node');
    if (nodeEl) {
      const node = S.nodeById.get(nodeEl.dataset.id);
      if (!node) return;
      selectOnly(node.id);
      openNodeContextMenu(node, event.clientX, event.clientY);
      return;
    }
    const rect = viewportRect();
    const point = screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
    S.addPoint = point;
    openContextMenu(event.clientX, event.clientY, [
      { label: '新建图片节点', icon: '◨', color: '#4b7cff', action: () => createNode('image', point) },
      { label: '新建视频节点', icon: '▶', color: '#e0724f', action: () => createNode('video', point) },
      { label: '上传本地素材', icon: '↑', color: '#12a150', action: () => pickUpload(point) },
      { label: '新建文本节点', icon: 'T', color: '#8a6bff', action: () => createNode('text', point) },
      { label: '新建主体', icon: '◍', color: '#7a5cf0', action: () => createSubjectNode(point) },
      { label: '载入示例画布', icon: '✨', color: '#dfa740', action: loadExampleCanvas },
    ]);
  });

  window.addEventListener('pointermove', onGlobalPointerMove);
  window.addEventListener('pointerup', onGlobalPointerUp);

  // 双击节点空白处选中并在检查器里写提示词
  viewport.addEventListener('click', (event) => {
    if (S.suppressClick) { S.suppressClick = false; return; }
    if (event.target.closest('.node, #edges path, .zoombar, .minimap, .empty-state')) return;
    if (event.target === viewport || event.target.id === 'grid') clearSelection();
  });

  // 拖文件进来
  let dragDepth = 0;
  viewport.addEventListener('dragenter', (event) => {
    if (!event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
    event.preventDefault();
    dragDepth += 1;
    $('#dropOverlay').classList.remove('hidden');
  });
  viewport.addEventListener('dragover', (event) => {
    if (event.dataTransfer && [...event.dataTransfer.types].includes('Files')) event.preventDefault();
  });
  viewport.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) $('#dropOverlay').classList.add('hidden');
  });
  viewport.addEventListener('drop', async (event) => {
    event.preventDefault();
    dragDepth = 0;
    $('#dropOverlay').classList.add('hidden');
    const files = [...(event.dataTransfer.files || [])];
    if (!files.length) return;
    const rect = viewportRect();
    const point = screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
    await uploadFiles(files, point);
  });

  document.addEventListener('paste', async (event) => {
    if (event.target.closest('input, textarea')) return;
    const items = [...(event.clipboardData ? event.clipboardData.items : [])];
    const fileItem = items.find((item) => item.kind === 'file' && item.type.startsWith('image/'));
    if (!fileItem) return;
    const file = fileItem.getAsFile();
    if (!file) return;
    const rect = viewportRect();
    const point = screenToWorld(rect.width / 2, rect.height / 2);
    await uploadFiles([file], point);
  });

  $('#nodes').addEventListener('click', (event) => {
    const nodeEl = event.target.closest('.node');
    if (nodeEl && !event.target.closest('button, input, .media-item, .port')) {
      const id = nodeEl.dataset.id;
      if (!event.shiftKey) selectOnly(id);
    }
  });

  $('#canvasTitle').addEventListener('input', (event) => {
    if (!S.doc) return;
    S.doc.title = event.target.value.slice(0, 60) || '未命名画布';
    markDirty();
    const item = S.canvases.find((entry) => entry.id === S.doc.id);
    if (item) item.title = S.doc.title;
  });

  $$('[data-new]').forEach((button) => button.addEventListener('click', () => {
    const rect = viewportRect();
    const point = screenToWorld(rect.width / 2, rect.height / 2);
    if (button.dataset.new === 'upload') pickUpload();
    else createNode(button.dataset.new, { x: point.x - 150, y: point.y - 230 });
  }));

  $('#addMenu').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', (event) => {
    if (event.target.closest && event.target.closest('#addMenu, #ctxMenu')) return;
    if (Date.now() - (S.menuAt || 0) < 250) return;
    if (Date.now() - (S.ctxAt || 0) < 250) return;
    hideAddMenu();
    hideContextMenu();
  });
  window.addEventListener('blur', () => { if (S.link) cancelLink(); });
  window.addEventListener('beforeunload', () => {
    if (!S.doc) return;
    try {
      fetch(CANVAS_BASE + '/api/canvases/' + S.doc.id, {
        method: 'PUT',
        keepalive: true,
        headers: { 'Content-Type': 'application/json', 'x-canvas-token': S.token },
        body: JSON.stringify({ title: S.doc.title, nodes: S.doc.nodes, edges: S.doc.edges, viewport: S.view }),
      });
    } catch { /* ignore */ }
  });
}

/* ── 工具模式：选择（移动节点）/ 抓手（移动画布） ────────── */

function isPanMode() {
  return S.tool === 'hand';
}

function setTool(tool) {
  S.tool = tool === 'hand' ? 'hand' : 'select';
  const btn = $('#toolBtn');
  if (btn) {
    btn.classList.toggle('hand', S.tool === 'hand');
    btn.classList.toggle('on', S.tool === 'select');
    btn.title = S.tool === 'hand'
      ? '抓手工具（H）：拖动画布；V 回到选择工具'
      : '选择工具（V）：移动节点、拖空白框选；H 切抓手';
  }
  syncToolCursor();
}

function syncToolCursor() {
  const vp = $('#viewport');
  if (vp) vp.classList.toggle('tool-hand', S.tool === 'hand');
}


/* ── 磁吸对齐 ───────────────────────────────────────────── */

function nodeHeight(node) {
  const el = S.els.get(node.id);
  return el ? el.offsetHeight : 220;
}

/**
 * 拖动节点时的磁吸：把被拖节点的左/中/右、上/中/下，去贴别的节点的同类线；
 * 命中 6px（屏幕像素）以内就吸过去，并回传参考线。
 */
function snapDrag(ids, dx, dy) {
  const moving = ids.map((id) => S.nodeById.get(id)).filter(Boolean);
  if (!moving.length) return { dx, dy, lines: [] };
  const movingSet = new Set(ids);
  let left = Infinity;
  let right = -Infinity;
  let top = Infinity;
  let bottom = -Infinity;
  for (const node of moving) {
    left = Math.min(left, node.x + dx);
    right = Math.max(right, node.x + dx + node.w);
    top = Math.min(top, node.y + dy);
    bottom = Math.max(bottom, node.y + dy + nodeHeight(node));
  }
  const boxX = [left, (left + right) / 2, right];
  const boxY = [top, (top + bottom) / 2, bottom];
  const xs = [];
  const ys = [];
  for (const node of S.doc.nodes) {
    if (movingSet.has(node.id)) continue;
    xs.push(node.x, node.x + node.w / 2, node.x + node.w);
    const h = nodeHeight(node);
    ys.push(node.y, node.y + h / 2, node.y + h);
  }
  const threshold = 6 / S.view.scale;
  let bestX = null;
  let bestY = null;
  for (const edge of boxX) {
    for (const target of xs) {
      const delta = target - edge;
      if (Math.abs(delta) <= threshold && (!bestX || Math.abs(delta) < Math.abs(bestX.delta))) bestX = { delta, target };
    }
  }
  for (const edge of boxY) {
    for (const target of ys) {
      const delta = target - edge;
      if (Math.abs(delta) <= threshold && (!bestY || Math.abs(delta) < Math.abs(bestY.delta))) bestY = { delta, target };
    }
  }
  const lines = [];
  if (bestX) lines.push({ axis: 'v', at: bestX.target });
  if (bestY) lines.push({ axis: 'h', at: bestY.target });
  return { dx: dx + (bestX ? bestX.delta : 0), dy: dy + (bestY ? bestY.delta : 0), lines };
}

function drawGuides(lines) {
  const layer = $('#guides');
  if (!layer) return;
  if (!lines || !lines.length) {
    if (layer.childElementCount) layer.innerHTML = '';
    return;
  }
  layer.innerHTML = lines.map((line) => (line.axis === 'v'
    ? `<i class="v" style="left:${Math.round(S.view.x + line.at * S.view.scale)}px"></i>`
    : `<i class="h" style="top:${Math.round(S.view.y + line.at * S.view.scale)}px"></i>`)).join('');
}

function onViewportPointerDown(event) {
  if (event.button !== 0 && event.button !== 1) return;
  const middle = event.button === 1;
  const pan = isPanMode() || middle;
  if (event.target.closest('.minimap, .add-menu, .mention-menu, .empty-state')) return;
  // 选择工具下点在节点上是"移动节点"；抓手工具下拖哪里都是平移
  if (!pan && event.target.closest('.node')) return;
  if (event.target.closest('#edges path')) return;
  const rect = viewportRect();

  if (!pan) {
    // 选择工具：拖空白 = 框选（不按 Shift 会先清掉原选择，按住 Shift 累加）
    if (!event.shiftKey) clearSelection();
    S.marquee = {
      startX: event.clientX,
      startY: event.clientY,
      rect,
      base: new Set(event.shiftKey ? S.sel : []),
    };
    const el = $('#marquee');
    el.classList.remove('hidden');
    el.style.cssText = `left:${event.clientX - rect.left}px;top:${event.clientY - rect.top}px;width:0;height:0`;
    $('#viewport').classList.add('marquee');
    return;
  }

  // 抓手工具 / 中键：平移画布
  clearSelection();
  S.drag = {
    type: 'pan',
    startX: event.clientX,
    startY: event.clientY,
    viewX: S.view.x,
    viewY: S.view.y,
  };
  $('#viewport').classList.add('panning');
}

function onNodePointerDown(event) {
  if (event.button !== 0) return;
  // 点选模式优先：点节点＝选中这个节点上的素材，不做拖动/多选。
  if (S.pick) {
    const node = S.nodeById.get(this.dataset.id);
    if (node) {
      event.preventDefault();
      event.stopPropagation();
      pickFromNode(node);
    }
    return;
  }
  if (isPanMode()) return;   // 抓手模式：让事件冒泡到画布去平移
  if (event.target.closest('.port, .node-resize, .node-close, .node-run, button, input, textarea')) return;
  const id = this.dataset.id;
  const node = S.nodeById.get(id);
  if (!node) return;

  if (event.shiftKey) {
    toggleSelect(id);
  }
  // 点未选中的节点时先不选中：直接拖就只移动节点，不弹详情面板；
  // 只有"按下-抬起没移动"（单击）才在 pointerup 里选中并展开面板。
  const pendingSelect = !event.shiftKey && !S.sel.has(id) ? id : '';

  pushHistory();
  const ids = S.sel.has(id) ? [...S.sel] : [id];
  S.drag = {
    type: 'nodes',
    startX: event.clientX,
    startY: event.clientY,
    ids,
    pendingSelect,
    origin: new Map(ids.map((nodeId) => {
      const target = S.nodeById.get(nodeId);
      return [nodeId, { x: target.x, y: target.y }];
    })),
    moved: false,
  };
  for (const nodeId of ids) S.els.get(nodeId)?.classList.add('dragging');
  event.preventDefault();
}

function onNodeResizeStart(event, node) {
  event.stopPropagation();
  if (event.button !== 0) return;
  pushHistory();
  S.drag = {
    type: 'resize',
    id: node.id,
    startX: event.clientX,
    startW: node.w,
    node,
  };
  event.preventDefault();
}

function onPortDown(event, node) {
  event.stopPropagation();
  if (event.button !== 0) return;
  const rect = viewportRect();
  const point = screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('class', 'wire temp');
  path.setAttribute('d', `M ${node.x + node.w} ${node.y + 30} L ${point.x} ${point.y}`);
  $('#edges').appendChild(path);
  S.link = { from: node.id, path };
  document.body.classList.add('linking');
  event.preventDefault();
}

function hitTestDrop(clientX, clientY) {
  const target = document.elementFromPoint(clientX, clientY);
  if (!target || !target.closest) return { node: null, port: null };
  const nodeEl = target.closest('.node');
  return {
    node: nodeEl ? S.nodeById.get(nodeEl.dataset.id) || null : null,
    port: target.closest('.port'),
  };
}

function updateLinkTarget(clientX, clientY, fromId) {
  const hit = hitTestDrop(clientX, clientY);
  const targetId = hit.node && hit.node.id !== fromId ? hit.node.id : '';
  for (const [id, el] of S.els) el.classList.toggle('drop-target', id === targetId);
}

function clearLinkTarget() {
  for (const el of S.els.values()) el.classList.remove('drop-target');
  document.body.classList.remove('linking');
}

function onGlobalPointerMove(event) {
  if (S.drag) {
    if (S.drag.type === 'pan') {
      S.view.x = S.drag.viewX + (event.clientX - S.drag.startX);
      S.view.y = S.drag.viewY + (event.clientY - S.drag.startY);
      applyView();
      return;
    }
    if (S.drag.type === 'nodes') {
      // 3px 死区：手抖几像素不算拖动，否则"单击节点"会被判成拖动、详情面板不弹出来。
      if (!S.drag.moved
        && Math.abs(event.clientX - S.drag.startX) < 3
        && Math.abs(event.clientY - S.drag.startY) < 3) return;
      let dx = (event.clientX - S.drag.startX) / S.view.scale;
      let dy = (event.clientY - S.drag.startY) / S.view.scale;
      const lockX = event.shiftKey && Math.abs(dx) < Math.abs(dy);
      const lockY = event.shiftKey && Math.abs(dy) <= Math.abs(dx);
      if (event.shiftKey) {
        drawGuides([]);
      } else {
        const snapped = snapDrag(S.drag.ids, dx, dy);
        dx = snapped.dx;
        dy = snapped.dy;
        drawGuides(snapped.lines);
      }
      for (const nodeId of S.drag.ids) {
        const node = S.nodeById.get(nodeId);
        const origin = S.drag.origin.get(nodeId);
        if (!node || !origin) continue;
        node.x = Math.round(origin.x + (lockX ? 0 : dx));
        node.y = Math.round(origin.y + (lockY ? 0 : dy));
        applyNodeGeometry(S.els.get(nodeId), node);
      }
      S.drag.moved = true;
      if (!S.moveRaf) {
        S.moveRaf = requestAnimationFrame(() => {
          S.moveRaf = 0;
          renderEdges('move', S.drag ? S.drag.ids : null);
          scheduleMinimap();
          scheduleComposerFollow();
        });
      }
      return;
    }
    if (S.drag.type === 'resize') {
      const width = clamp(S.drag.startW + (event.clientX - S.drag.startX) / S.view.scale, NODE_MIN_W, NODE_MAX_W);
      S.drag.node.w = Math.round(width);
      applyNodeGeometry(S.els.get(S.drag.node.id), S.drag.node);
      renderEdges('move', [S.drag.node.id]);
      scheduleMinimap();
      return;
    }
  }

  if (S.link) {
    const rect = viewportRect();
    const point = screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
    const from = S.nodeById.get(S.link.from);
    if (from) {
      S.link.path.setAttribute('d', `M ${from.x + from.w} ${from.y + 30} L ${point.x} ${point.y}`);
    }
    updateLinkTarget(event.clientX, event.clientY, S.link.from);
    return;
  }

  if (S.marquee) {
    const { rect, startX, startY } = S.marquee;
    const left = Math.min(startX, event.clientX) - rect.left;
    const top = Math.min(startY, event.clientY) - rect.top;
    const width = Math.abs(event.clientX - startX);
    const height = Math.abs(event.clientY - startY);
    const el = $('#marquee');
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    el.style.width = width + 'px';
    el.style.height = height + 'px';
  }
}

function onGlobalPointerUp(event) {
  if (S.drag) {
    const drag = S.drag;
    S.drag = null;
    $('#viewport').classList.remove('panning');
    drawGuides([]);
    if (drag.type === 'nodes') {
      for (const nodeId of drag.ids) S.els.get(nodeId)?.classList.remove('dragging');
      if (drag.moved) {
        for (const nodeId of drag.ids) {
          const node = S.nodeById.get(nodeId);
          if (node) node.updatedAt = Date.now();
        }
        markDirty();
        renderInspector();
      } else {
        S.history.past.pop();
        // 单击（没有拖动）才选中并展开详情面板；拖动只移动节点，不打断当前面板。
        if (drag.pendingSelect) selectOnly(drag.pendingSelect);
      }
      renderEdges('structure');
    } else if (drag.type === 'resize') {
      markDirty();
      renderEdges('structure');
    } else if (drag.type === 'pan') {
      saveViewSoon();
    }
  }

  if (S.link) {
    const fromId = S.link.from;
    const path = S.link.path;
    S.link = null;
    path.remove();
    clearLinkTarget();
    const hit = hitTestDrop(event.clientX, event.clientY);
    if (hit.node && hit.node.id !== fromId) {
      // 落到目标节点的任意位置都算连线 —— 不必精确戳中那个 12px 的小圆点。
      addEdge(fromId, hit.node.id);
    } else if (hit.node && hit.node.id === fromId) {
      toast('不能连到自己');
    } else {
      // 真正落在空白处：打开新建菜单，选完会自动连到刚拖出来的这个节点。
      const rect = viewportRect();
      if (event.clientX > rect.left && event.clientX < rect.right && event.clientY > rect.top && event.clientY < rect.bottom) {
        const point = screenToWorld(event.clientX - rect.left, event.clientY - rect.top);
        S.addPoint = { x: point.x - 150, y: point.y - 100 };
        showAddMenu(event.clientX, event.clientY, fromId);
      }
    }
    S.suppressClick = true;
  }

  if (S.marquee) {
    const { rect, base, startX, startY } = S.marquee;
    const left = (Math.min(startX, event.clientX) - rect.left - S.view.x) / S.view.scale;
    const top = (Math.min(startY, event.clientY) - rect.top - S.view.y) / S.view.scale;
    const right = (Math.max(startX, event.clientX) - rect.left - S.view.x) / S.view.scale;
    const bottom = (Math.max(startY, event.clientY) - rect.top - S.view.y) / S.view.scale;
    S.sel = new Set(base);
    for (const node of S.doc.nodes) {
      const intersects = node.x < right && node.x + node.w > left && node.y < bottom && node.y + 300 > top;
      if (intersects) S.sel.add(node.id);
    }
    S.marquee = null;
    $('#marquee').classList.add('hidden');
    $('#viewport').classList.remove('marquee');
    syncSelection();
    renderInspector();
    S.suppressClick = true;
  }
}

/* ── 新建节点 / 添加菜单 ────────────────────────────────── */

function defaultTitle(kind) {
  const base = KIND_META[kind].title;
  const count = S.doc.nodes.filter((node) => node.kind === kind).length;
  return count ? `${base} ${count + 1}` : base;
}

function freeSpot(x, y, width = 300) {
  const overlaps = (px, py) => S.doc.nodes.some((node) => (
    px < node.x + node.w + 24 && px + width + 24 > node.x
    && py < node.y + 300 && py + 260 + 24 > node.y
  ));
  let dx = 0;
  let dy = 0;
  for (let i = 0; i < 40; i += 1) {
    if (!overlaps(x + dx, y + dy)) return { x: Math.round(x + dx), y: Math.round(y + dy) };
    dx += 42;
    if (i % 4 === 3) { dx = 0; dy += 72; }
  }
  return { x: Math.round(x), y: Math.round(y) };
}

function createNode(kind, point, patch = {}) {
  if (!S.doc) return null;
  pushHistory();
  const spot = freeSpot(point.x, point.y);
  const node = {
    id: uid('n'),
    kind,
    title: defaultTitle(kind),
    x: spot.x,
    y: spot.y,
    w: 300,
    prompt: '',
    text: '',
    ratio: kind === 'video' ? '9:16' : '1:1',
    status: 'idle',
    statusText: '',
    assets: [],
    createdAt: Date.now(),
    updatedAt: Date.now(),
    ...patch,
  };
  S.doc.nodes.push(node);
  S.nodeById.set(node.id, node);
  buildNodeEl(node);
  renderEdges('structure');
  selectOnly(node.id);
  updateMeta();
  updateEmpty();
  markDirty();
  scheduleMinimap();
  return node;
}

function showAddMenu(clientX, clientY, fromId = null, point = null) {
  const menu = $('#addMenu');
  const anchorPoint = point || S.addPoint;
  menu.innerHTML = '';
  for (const kind of ['image', 'video', 'upload', 'text']) {
    const button = document.createElement('button');
    button.innerHTML = `<span class="mi" style="background:${KIND_META[kind].color}">${KIND_META[kind].icon}</span>
      <span>${kind === 'upload' ? '上传本地素材' : KIND_META[kind].title + '节点'}</span>`;
    button.addEventListener('click', async () => {
      hideAddMenu();
      if (kind === 'upload') {
        pickUpload(anchorPoint);
        return;
      }
      const node = createNode(kind, anchorPoint || { x: 0, y: 0 });
      if (fromId && node) addEdge(fromId, node.id);
    });
    menu.appendChild(button);
  }
  menu.style.left = Math.min(clientX, window.innerWidth - 210) + 'px';
  menu.style.top = Math.min(clientY, window.innerHeight - 220) + 'px';
  S.menuAt = Date.now();
  menu.classList.remove('hidden');
}

function hideAddMenu() {
  $('#addMenu').classList.add('hidden');
}

function openContextMenu(clientX, clientY, items) {
  const menu = $('#ctxMenu');
  menu.innerHTML = '';
  for (const item of items) {
    if (item.label === '-') {
      const line = document.createElement('div');
      line.className = 'ctx-label';
      line.textContent = item.text || '';
      menu.appendChild(line);
      continue;
    }
    const button = document.createElement('button');
    if (item.danger) button.classList.add('danger');
    button.innerHTML = `<span class="mi" style="background:${item.color || 'transparent'}">${item.icon || ''}</span><span>${escapeHtml(item.label)}</span>`;
    button.addEventListener('click', async () => {
      hideContextMenu();
      try { await item.action(); } catch (error) { toast(error.message, 'bad'); }
    });
    menu.appendChild(button);
  }
  menu.style.left = Math.min(clientX, window.innerWidth - 220) + 'px';
  menu.style.top = Math.min(clientY, window.innerHeight - 300) + 'px';
  S.ctxAt = Date.now();
  menu.classList.remove('hidden');
}

function hideContextMenu() {
  $('#ctxMenu').classList.add('hidden');
}

function duplicateNode(node) {
  pushHistory();
  const copy = JSON.parse(JSON.stringify(node));
  copy.id = uid('n');
  copy.title = (node.title + ' 副本').slice(0, 60);
  copy.x += 36;
  copy.y += 36;
  copy.createdAt = Date.now();
  copy.updatedAt = Date.now();
  delete copy.taskId;
  S.doc.nodes.push(copy);
  S.nodeById.set(copy.id, copy);
  buildNodeEl(copy);
  renderEdges('structure');
  selectOnly(copy.id);
  updateMeta();
  updateEmpty();
  markDirty();
  scheduleMinimap();
}

function openNodeContextMenu(node, clientX, clientY) {
  const items = [
    {
      label: '重命名',
      icon: '✎',
      action: () => {
        const el = S.els.get(node.id);
        const titleEl = el && el.querySelector('.node-title');
        if (titleEl) editNodeTitle(node, titleEl);
      },
    },
    { label: '复制节点', icon: '⧉', action: () => duplicateNode(node) },
  ];
  if (node.assets.length) {
    items.push({
      label: '下载全部结果',
      icon: '⤓',
      action: () => node.assets.forEach((asset, index) => setTimeout(() => downloadAsset(asset.id), index * 160)),
    });
  }
  items.push({ label: '删除节点', icon: '×', danger: true, action: () => deleteNode(node.id) });
  openContextMenu(clientX, clientY, items);
}

function openCanvasContextMenu(item, clientX, clientY) {
  openContextMenu(clientX, clientY, [
    {
      label: '重命名画布',
      icon: '✎',
      action: () => {
        const row = document.querySelector(`.canvas-item[data-id="${item.id}"]`);
        if (row) renameCanvasInline(row, item);
      },
    },
    { label: '复制画布', icon: '⧉', action: () => duplicateCanvas(item) },
    {
      label: '清空画布',
      icon: '⌫',
      danger: true,
      action: async () => {
        if (item.id !== S.doc.id) await openCanvas(item.id);
        if (!S.doc.nodes.length) return;
        if (!confirm('清空这张画布上的所有节点和连线？')) return;
        pushHistory();
        S.doc.nodes = [];
        S.doc.edges = [];
        S.nodeById = new Map();
        S.sel.clear();
        S.selEdge = null;
        buildAllNodes();
        renderEdges('structure');
        renderInspector();
        updateMeta();
        updateEmpty();
        markDirty();
      },
    },
    { label: '删除画布', icon: '×', danger: true, action: () => deleteCanvas(item) },
  ]);
}

function pickUpload(point = null, accept = 'image/*,video/*,audio/*') {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = accept;
  input.multiple = true;
  input.addEventListener('change', async () => {
    const files = [...input.files];
    if (!files.length) return;
    let target = point;
    if (!target) {
      const rect = viewportRect();
      target = screenToWorld(rect.width / 2 - 150, rect.height / 2 - 230);
    }
    await uploadFiles(files, target);
  });
  input.click();
}

async function uploadFiles(files, point) {
  let offset = 0;
  for (const file of files) {
    const isImage = file.type.startsWith('image/');
    const isVideo = file.type.startsWith('video/');
    const isAudio = file.type.startsWith('audio/');
    if (!isImage && !isVideo && !isAudio) { toast(`跳过 ${file.name}：只支持图片/视频/音频`, 'bad'); continue; }
    try {
      toast(`上传 ${file.name}…`);
      const dims = await probeMedia(file);
      const asset = await uploadOne(file, dims);
      const node = createNode('upload', { x: point.x + offset, y: point.y + offset }, {
        title: file.name.replace(/\.[^.]+$/, '').slice(0, 40) || '本地素材',
        assets: [asset],
      });
      offset += 36;
      if (!node) break;
    } catch (error) {
      toast('上传失败：' + error.message, 'bad');
    }
  }
  refreshStats();
}

function probeMedia(file) {
  return new Promise((resolve) => {
    if (file.type.startsWith('audio/')) {
      const url = URL.createObjectURL(file);
      const audio = document.createElement('audio');
      audio.preload = 'metadata';
      audio.onloadedmetadata = () => { resolve({ w: 0, h: 0, duration: audio.duration }); URL.revokeObjectURL(url); };
      audio.onerror = () => { resolve({ w: 0, h: 0, duration: 0 }); URL.revokeObjectURL(url); };
      audio.src = url;
      return;
    }
    if (file.type.startsWith('image/')) {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { resolve({ w: img.naturalWidth, h: img.naturalHeight, duration: 0 }); URL.revokeObjectURL(url); };
      img.onerror = () => { resolve({ w: 0, h: 0, duration: 0 }); URL.revokeObjectURL(url); };
      img.src = url;
      return;
    }
    const url = URL.createObjectURL(file);
    const video = document.createElement('video');
    video.preload = 'metadata';
    video.onloadedmetadata = () => { resolve({ w: video.videoWidth, h: video.videoHeight, duration: video.duration }); URL.revokeObjectURL(url); };
    video.onerror = () => { resolve({ w: 0, h: 0, duration: 0 }); URL.revokeObjectURL(url); };
    video.src = url;
  });
}

async function uploadOne(file, dims) {
  const response = await fetch(CANVAS_BASE + '/api/upload', {
    method: 'POST',
    headers: {
      'x-canvas-token': S.token,
      'x-filename': encodeURIComponent(file.name),
      'x-width': String(dims.w || 0),
      'x-height': String(dims.h || 0),
      'x-duration': String(dims.duration || 0),
      'Content-Type': file.type || 'application/octet-stream',
    },
    body: file,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data.asset;
}

/* ── 检查器 ─────────────────────────────────────────────── */

function renderInspector() {
  const body = $('#inspectorBody');
  if (!S.doc) return;
  renderAgentCtx();

  // 生成面板贴着画布底部居中，只在选中节点 / 连线时浮出来（和即梦画布一致）。
  const hasSelection = Boolean(S.selEdge) || S.sel.size > 0;
  const showComposer = hasSelection && !S.composerHidden;
  $('#app').classList.toggle('inspector-hidden', !showComposer);
  if (!showComposer) { body.innerHTML = ''; return; }
  positionComposer();
  scheduleComposerFollow();

  if (S.selEdge) {
    const edge = S.doc.edges.find((entry) => entry.id === S.selEdge);
    if (!edge) { S.selEdge = null; return renderInspector(); }
    const from = S.nodeById.get(edge.from);
    const to = S.nodeById.get(edge.to);
    body.innerHTML = `
      <div class="insp-head"><h3>连线</h3><span class="insp-kind">引用关系</span></div>
      <div class="insp-sub">${escapeHtml(from ? from.title : '?')} → ${escapeHtml(to ? to.title : '?')}</div>
      <div class="ref-list">
        <div class="ref-item"><span class="ref-ph">↑</span><span class="ref-name">${escapeHtml(from ? from.title : '?')}</span><span class="ref-tag">上游</span></div>
        <div class="ref-item"><span class="ref-ph">↓</span><span class="ref-name">${escapeHtml(to ? to.title : '?')}</span><span class="ref-tag">下游</span></div>
      </div>
      <div class="insp-actions"><button class="btn danger" data-act="del-edge">删除连线</button></div>`;
    $('[data-act="del-edge"]').addEventListener('click', () => deleteSelected());
    return;
  }

  if (S.sel.size > 1) {
    body.innerHTML = `
      <div class="insp-head"><h3>已选择 ${S.sel.size} 个节点</h3></div>
      <div class="insp-sub">拖动任意一个节点会带着整批一起移动。</div>
      <div class="insp-actions">
        <button class="btn danger" data-act="del-many">删除这 ${S.sel.size} 个节点</button>
      </div>`;
    $('[data-act="del-many"]').addEventListener('click', () => deleteSelected());
    return;
  }

  const node = S.sel.size === 1 ? S.nodeById.get([...S.sel][0]) : null;
  if (!node) {
    body.innerHTML = `
      <div class="inspector-empty">
        <div class="ie-mark">⌁</div>
        <div>选中一个节点开始<br>双击画布空白处可以新建节点</div>
        <div style="margin-top:18px">
          <button class="btn" data-act="new-image">＋ 图片节点</button>
        </div>
      </div>`;
    $('[data-act="new-image"]').addEventListener('click', () => {
      const rect = viewportRect();
      const point = screenToWorld(rect.width / 2 - 150, rect.height / 2 - 230);
      createNode('image', point);
    });
    return;
  }

  if (node.kind === 'subject') return renderSubjectInspector(body, node);
  if (node.kind === 'upload') return renderUploadInspector(body, node);
  if (node.kind === 'text') return renderTextInspector(body, node);
  return renderGenerateInspector(body, node);
}

/** 节点生成参数：比例之外，清晰度 / 张数 / 时长都跟着节点存。 */
function videoModel(id) {
  return VIDEO_MODELS.find((model) => model.id === id) || VIDEO_MODELS[0];
}

function nodeParams(node) {
  if (!node.params || typeof node.params !== 'object') node.params = {};
  const p = node.params;
  if (node.kind === 'video') {
    if (!VIDEO_MODELS.some((model) => model.id === p.model)) p.model = VIDEO_MODELS[0].id;
    const cap = videoModel(p.model);
    if (!cap.qualities.includes(p.quality)) p.quality = cap.qualities.includes('720P') ? '720P' : cap.qualities[0];
    const seconds = Math.round(Number(p.duration) || Math.min(cap.max, Math.max(cap.min, 5)));
    p.duration = Math.min(cap.max, Math.max(cap.min, seconds));
  } else {
    if (!IMAGE_MODELS.some((model) => model.id === p.model)) p.model = IMAGE_MODELS[0].id;
    if (!IMAGE_QUALITIES.includes(p.quality)) p.quality = '2K';
    if (!COUNTS.includes(Number(p.count))) p.count = 1;
  }
  return p;
}

function renderGenerateInspector(body, node) {
  const upstream = incomingSources(node.id);
  const upstreamText = upstream.filter((source) => source.kind === 'text');
  const upstreamSubjects = upstream.filter((source) => source.kind === 'subject');
  const upstreamMedia = upstream.filter((source) => source.kind !== 'text' && source.kind !== 'subject');
  const imageRefs = upstreamMedia.map((source) => ({ source, asset: (source.assets || []).find((asset) => asset.kind === 'image') })).filter((entry) => entry.asset);
  const task = S.tasks.get(node.id);
  const busy = ['queued', 'running', 'preparing', 'downloading', 'submitting'].includes(node.status);
  const prepared = node.status === 'prepared';
  const params = nodeParams(node);

  // 口播 / 数字人形态提示：给了音色参考，模型只能做口型合成，不会给你真实动作。
  const voicedSubjects = [
    ...upstreamSubjects.map((source) => S.subjects.find((entry) => entry.id === source.subjectId)),
    ...(node.subjectIds || []).map((id) => S.subjects.find((entry) => entry.id === id)),
  ].filter((subject) => subject && subject.audioId);
  const audioMedia = upstreamMedia.filter((source) => (source.assets || []).some((asset) => asset.kind === 'audio'));
  const videoMedia = upstreamMedia.filter((source) => (source.assets || []).some((asset) => asset.kind === 'video'));
  const hasVoice = voicedSubjects.length > 0 || audioMedia.length > 0;
  let voiceNotice = '';
  if (node.kind === 'video' && hasVoice) {
    const isFast = params.model !== 'seedance_2.5';
    const isSquare = node.ratio === '1:1';
    const notes = ['口播形态：带音色参考时，客户端把「形象图 + 音色 + 台词」合成成说话镜头。口播也能有动作——把“坐着 / 固定机位 / 只比划”换成“起身、走动、递书、镜头跟随”，8 秒只写 2~3 个动作，别塞满逐句手势。'];
    if (isFast && videoMedia.length) {
      notes.push('当前档位装不下「参考视频 + 音频」（输入合计上限 15s），容易失败或退化成拼接感口播——建议换 Seedance 2.5。');
    } else if (isFast && Number(params.duration) > 12) {
      notes.push(`当前档位输入上限 15s，${params.duration}s 输出再加音色采样容易超限，建议换 Seedance 2.5。`);
    }
    if (isSquare) notes.push('方视频（1:1）最像数字人口播；竖屏 9:16 / 横屏 16:9 更像真实拍摄。');
    notes.push('音色采样建议 6–10 秒纯人声；台词要逐字准，优先用整条配音驱动而不是让模型自读。');
    voiceNotice = `<div class="model-note">
      <span>${notes.map((line) => escapeHtml(line)).join(' ')}</span>
      ${isSquare ? '<button class="link-btn" id="fixRatio">改成竖屏 9:16</button>' : ''}
      ${isFast ? '<button class="link-btn" id="switchTo25">换 Seedance 2.5</button>' : ''}
    </div>`;
  }

  body.innerHTML = `
    <div class="insp-head">
      <h3>${escapeHtml(node.title)}</h3>
      <span class="insp-kind">${KIND_META[node.kind].label}</span>
    </div>
    <div class="insp-sub">${node.kind === 'video' ? '豆包视频生成 · 通常 1~5 分钟' : '豆包图片生成 · 通常 10~40 秒'}</div>
    ${voiceNotice}

    <div class="field">
      <label>提示词 <span class="hint"><kbd>@</kbd> 引用上游节点</span></label>
      <div class="prompt-wrap">
        <textarea class="prompt-input" id="inspPrompt" placeholder="${node.kind === 'video' ? '主体 + 动作 + 场景 + 镜头运动 + 风格' : '主体 + 动作 + 场景 + 风格；有参考图时描述要保留/改变的部分'}">${escapeHtml(node.prompt)}</textarea>
      </div>
      <div class="prompt-meta">
        <span>Ctrl+Enter 开始生成</span>
        <span class="prompt-meta-right">
          <button class="link-btn" id="skillPolish" title="用内置提示词 skill 改写这条提示词">✨ 用 skill 优化</button>
          <span id="promptCount">${node.prompt.length} 字</span>
        </span>
      </div>
    </div>

    ${`<div class="field inline">
      <label>模型</label>
      <select class="param-select" id="modelSelect">
        ${(node.kind === 'video' ? VIDEO_MODELS : IMAGE_MODELS).map((model) => `<option value="${model.id}"${model.id === params.model ? ' selected' : ''}>${model.label}</option>`).join('')}
      </select>
    </div>`}

    <div class="field inline">
      <label>比例</label>
      <div class="chips" id="ratioChips">
        ${RATIOS.map((ratio) => `<button class="chip ${ratio === node.ratio ? 'on' : ''}" data-ratio="${ratio}">${ratio}</button>`).join('')}
      </div>
    </div>

    <div class="field inline" title="本地计价与预估用；内置画布生成条目前不暴露清晰度，实际以豆包档位为准">
      <label>${node.kind === 'video' ? '分辨率（预估）' : '清晰度（预估）'}</label>
      <div class="chips" id="qualityChips">
        ${(node.kind === 'video' ? videoModel(params.model).qualities : IMAGE_QUALITIES).map((quality) => `<button class="chip ${quality === params.quality ? 'on' : ''}" data-quality="${quality}">${quality}</button>`).join('')}
      </div>
    </div>

    ${node.kind === 'image' ? `<div class="field inline">
      <label>张数 <span class="hint">内置单次 1 张</span></label>
      <div class="chips" id="countChips">
        ${COUNTS.map((count) => `<button class="chip ${count === params.count ? 'on' : ''}" data-count="${count}" ${count > 1 ? 'disabled title="多张暂未接入内置画布"' : ''}>${count} 张</button>`).join('')}
      </div>
    </div>` : `<div class="field inline">
      <label>时长</label>
      <input class="param-range" type="range" id="durationRange"
        min="${videoModel(params.model).min}" max="${videoModel(params.model).max}" step="1" value="${params.duration}">
      <b class="param-value" id="durationValue">${params.duration} 秒</b>
    </div>`}

    <div class="field">
      <label>参考（来自上游连线） <span class="hint">${imageRefs.length || upstreamMedia.length || upstreamSubjects.length ? '图片 / 视频 / 音频 / 主体' : '无'}</span></label>
      <div class="ref-list">
        ${upstream.length === 0 ? '<div class="ref-empty">还没有上游节点。从别的节点右侧圆点拖一条线过来，或者在上游节点上生成结果。</div>' : ''}
        ${upstreamSubjects.map((source) => {
          const subject = S.subjects.find((entry) => entry.id === source.subjectId);
          if (!subject) return '';
          const faces = (subject.assetIds || []).length;
          const cover = subject.coverId ? mediaUrl(subject.coverId) : '';
          const voice = subject.audioId
            ? (node.kind === 'video' ? ' + 音色' : '（音色仅视频用）')
            : '';
          return `<div class="ref-item">
            ${cover ? `<img src="${cover}" loading="lazy" alt="">` : `<span class="ref-ph">${KIND_META.subject.icon}</span>`}
            <span class="ref-name">${escapeHtml(subject.name)}</span>
            <span class="ref-tag">主体 · ${faces} 张形象${voice}</span>
          </div>`;
        }).join('')}
        ${upstreamMedia.length
          ? upstreamMedia.map((source) => {
            const assets = source.assets || [];
            const asset = assets.find((entry) => entry.kind === 'image')
              || assets.find((entry) => entry.kind === 'video')
              || assets.find((entry) => entry.kind === 'audio');
            const kindLabel = asset ? ({ image: '图片', video: '视频', audio: '音频' }[asset.kind] || '素材') : '无可用素材';
            const cover = asset && asset.kind === 'video' && asset.coverId ? mediaUrl(asset.coverId) : (asset && asset.kind !== 'audio' ? mediaUrl(asset.id) : '');
            return `<div class="ref-item">
              ${cover ? `<img src="${cover}" loading="lazy" alt="">` : `<span class="ref-ph">${KIND_META[source.kind].icon}</span>`}
              <span class="ref-name">${escapeHtml(source.title)}</span>
              <span class="ref-tag">${kindLabel}</span>
            </div>`;
          }).join('')
          : ''}
        ${upstreamText.length ? `<div class="ref-empty">上游文本：${upstreamText.map((source) => escapeHtml(source.title)).join('、')}（${upstreamText.length} 个，生成时会拼进提示词）</div>` : ''}
      </div>
    </div>

    <div class="field inline">
      <label>主体</label>
      <div class="chips" id="subjectChips">
        ${S.subjects.filter((subject) => (subject.assetIds || []).length).map((subject) => `<button class="chip ${(node.subjectIds || []).includes(subject.id) ? 'on' : ''}" data-subject="${subject.id}">${escapeHtml(subject.name)}</button>`).join('')}
        <button class="chip" id="manageSubjects">＋ 管理主体</button>
      </div>
    </div>

    <div class="task-line ${node.status === 'error' ? 'error' : node.status === 'done' ? 'done' : (busy || prepared ? '' : 'hidden')}" id="taskLine">
      ${node.status === 'error'
        ? escapeHtml(node.statusText || '生成失败')
        : node.status === 'done'
          ? escapeHtml(node.statusText || '完成')
          : prepared
            ? '<span style="color:#d97706;font-size:12px;">●</span><span>' + escapeHtml(node.statusText || '已准备，等待确认') + '</span>'
            : (busy ? '<i class="spin"></i><span>' + escapeHtml(node.statusText || '生成中…') + '</span>' : '')}
    </div>

    <div class="cost-line hidden" id="costLine"></div>
    <div class="insp-actions">
      <button class="btn danger" id="delNode">删除节点</button>
      ${busy ? '<button class="btn" id="cancelBtn">取消等待</button>' : ''}
      ${prepared ? '<button class="btn" id="cancelPrepared">先不提交</button><button class="btn primary" id="confirmBuiltin">确认提交</button>' : ''}
      ${node.status === 'error' ? '<button class="btn" id="collectBtn">从豆包取回结果</button>' : ''}
      ${node.assets.length ? '<button class="btn" id="dlAll">下载全部</button>' : ''}
      <button class="btn primary" id="runBtn" ${busy ? 'disabled' : ''}>${busy ? '生成中…' : prepared ? '重新准备' : (node.assets.length ? '再生成一次' : '开始生成')}</button>
    </div>

    ${node.assets.length ? `<div class="insp-section">
      <h4>本节点结果（${node.assets.length}）</h4>
      <div class="asset-strip">${node.assets.slice(0, 9).map((asset) => `
        <div class="asset-thumb" data-asset="${asset.id}">
          ${asset.kind === 'video'
            ? (asset.coverId ? `<img src="${mediaUrl(asset.coverId)}" loading="lazy" alt="">` : `<video src="${mediaUrl(asset.id)}" preload="none" muted></video>`)
            : `<img src="${mediaUrl(asset.id)}" loading="lazy" alt="">`}
          <button title="下载">⤓</button>
        </div>`).join('')}</div>
    </div>` : ''}
  `;

  const textarea = $('#inspPrompt', body);
  textarea.addEventListener('focus', () => beginTyping(node));
  textarea.addEventListener('blur', () => { S.typing = null; hideMention(); saveSoon(); });
  textarea.addEventListener('input', () => {
    node.prompt = textarea.value;
    node.updatedAt = Date.now();
    $('#promptCount').textContent = `${node.prompt.length} 字`;
    handleMentionInput(textarea, node);
    markDirty();
    renderNodeStatus(node);
  });
  textarea.addEventListener('keydown', (event) => onPromptKeyDown(event, textarea, node));
  textarea.addEventListener('scroll', hideMention);
  $('#skillPolish', body)?.addEventListener('click', () => polishNodePrompt(node));
  refreshCostLine(node);
  $('#fixRatio', body)?.addEventListener('click', () => {
    pushHistory();
    node.ratio = '9:16';
    node.updatedAt = Date.now();
    markDirty();
    renderInspector();
    toast('已改成竖屏 9:16', 'ok');
  });
  $('#switchTo25', body)?.addEventListener('click', () => {
    pushHistory();
    const cap = videoModel('seedance_2.5');
    params.model = 'seedance_2.5';
    params.duration = Math.min(cap.max, Math.max(cap.min, Number(params.duration) || cap.min));
    if (!cap.qualities.includes(params.quality)) params.quality = cap.qualities[cap.qualities.length - 1];
    node.updatedAt = Date.now();
    markDirty();
    renderInspector();
    toast('已换成 Seedance 2.5（≤30s，支持视频+音频整段）', 'ok');
  });

  $$('#ratioChips .chip', body).forEach((chip) => chip.addEventListener('click', () => {
    pushHistory();
    node.ratio = chip.dataset.ratio;
    node.updatedAt = Date.now();
    $$('#ratioChips .chip', body).forEach((entry) => entry.classList.toggle('on', entry === chip));
    markDirty();
    renderNodeStatus(node);
  }));

  const bindParams = (selector, key, cast) => {
    $$(selector + ' .chip', body).forEach((chip) => chip.addEventListener('click', () => {
      pushHistory();
      params[key] = cast ? cast(chip.dataset[key]) : chip.dataset[key];
      node.updatedAt = Date.now();
      $$(selector + ' .chip', body).forEach((entry) => entry.classList.toggle('on', entry === chip));
      markDirty();
      renderNodeStatus(node);
      refreshCostLine(node);
    }));
  };
  $$('#subjectChips [data-subject]', body).forEach((chip) => chip.addEventListener('click', () => {
    pushHistory();
    const id = chip.dataset.subject;
    const list = new Set(node.subjectIds || []);
    if (list.has(id)) list.delete(id); else list.add(id);
    node.subjectIds = [...list];
    chip.classList.toggle('on', list.has(id));
    node.updatedAt = Date.now();
    markDirty();
  }));
  $('#manageSubjects', body)?.addEventListener('click', openSubjectsModal);
  bindParams('#qualityChips', 'quality');
  if (node.kind === 'image') {
    bindParams('#countChips', 'count', Number);
  } else {
    const range = $('#durationRange', body);
    const valueEl = $('#durationValue', body);
    if (range) {
      range.addEventListener('input', () => {
        params.duration = Number(range.value);
        if (valueEl) valueEl.textContent = params.duration + ' 秒';
        node.updatedAt = Date.now();
        markDirty();
        renderNodeStatus(node);
        refreshCostLine(node);
      });
      range.addEventListener('change', () => pushHistory());
    }
    const select = $('#modelSelect', body);
    if (select) {
      select.addEventListener('change', () => {
        pushHistory();
        params.model = select.value;
        const cap = videoModel(params.model);
        params.duration = Math.min(cap.max, Math.max(cap.min, params.duration));
        if (range) {
          range.min = String(cap.min);
          range.max = String(cap.max);
          range.value = String(params.duration);
        }
        if (valueEl) valueEl.textContent = params.duration + ' 秒';
        node.updatedAt = Date.now();
        markDirty();
        renderNodeStatus(node);
        // 分辨率候选和时长上限都跟着模型换，整块重画最稳。
        renderInspector();
      });
    }
  }

  const runBtn = $('#runBtn', body);
  runBtn.addEventListener('click', () => runNode(node));
  const cancelBtn = $('#cancelBtn', body);
  if (cancelBtn) cancelBtn.addEventListener('click', () => cancelNode(node));
  $('#confirmBuiltin', body)?.addEventListener('click', () => submitBuiltinNode(node));
  $('#cancelPrepared', body)?.addEventListener('click', () => {
    node.status = 'idle';
    node.statusText = '已取消确认，未提交';
    node.updatedAt = Date.now();
    document.getElementById('bridgeConfirmMask')?.remove();
    renderInspector();
    renderNodeStatus(node);
    markDirty();
  });
  $('#collectBtn', body)?.addEventListener('click', () => collectResult(node));
  const dlAll = $('#dlAll', body);
  if (dlAll) dlAll.addEventListener('click', () => node.assets.forEach((asset, index) => setTimeout(() => downloadAsset(asset.id), index * 180)));
  $('#delNode', body).addEventListener('click', () => deleteNode(node.id));

  $$('.asset-thumb', body).forEach((thumb) => {
    const assetId = thumb.dataset.asset;
    const index = node.assets.findIndex((asset) => asset.id === assetId);
    thumb.addEventListener('click', (event) => {
      if (event.target.closest('button')) return;
      openLightbox(node, index);
    });
    thumb.querySelector('button').addEventListener('click', (event) => {
      event.stopPropagation();
      downloadAsset(assetId);
    });
  });
}

function renderUploadInspector(body, node) {
  const asset = node.assets[0];
  body.innerHTML = `
    <div class="insp-head"><h3>${escapeHtml(node.title)}</h3><span class="insp-kind">本地素材</span></div>
    <div class="insp-sub">素材只存在本机，不会上传到云端；生成时才会把参考图发给豆包。</div>
    ${asset ? `<div class="upload-row">
      <div class="upload-thumb${asset.kind === 'audio' ? ' audio' : ''}">
        ${asset.kind === 'video'
          ? `<video src="${mediaUrl(asset.id)}" preload="metadata" controls muted></video>`
          : asset.kind === 'audio'
            ? `<audio src="${mediaUrl(asset.id)}" controls preload="metadata"></audio>`
            : `<img src="${mediaUrl(asset.id)}" alt="">`}
      </div>
      <div class="kv">
        <div><span>文件名</span><b>${escapeHtml(asset.name || '—')}</b></div>
        <div><span>尺寸</span><b>${asset.w && asset.h ? asset.w + ' × ' + asset.h : '—'}</b></div>
        <div><span>时长</span><b>${asset.duration ? fmtDuration(asset.duration) : '—'}</b></div>
        <div><span>大小</span><b>${fmtBytes(asset.bytes)}</b></div>
      </div>
    </div>` : '<div class="ref-empty">这个节点没有素材，删掉重新上传吧。</div>'}
    <div class="insp-actions">
      <button class="btn danger" id="delUpload">删除节点</button>
      <button class="btn" id="dlUpload">下载</button>
      <button class="btn" id="mkImage">用它做图片</button>
      <button class="btn primary" id="mkVideo">用它做视频</button>
    </div>`;

  if (asset) {
    $('#dlUpload').addEventListener('click', () => downloadAsset(asset.id));
  }
  const spawn = (kind) => {
    const child = createNode(kind, { x: node.x + node.w + 70, y: node.y });
    addEdge(node.id, child.id);
    toast('已创建下游节点，写提示词后 Ctrl+Enter 生成');
  };
  $('#mkImage').addEventListener('click', () => spawn('image'));
  $('#mkVideo').addEventListener('click', () => spawn('video'));
  $('#delUpload').addEventListener('click', () => deleteNode(node.id));
}

function renderTextInspector(body, node) {
  body.innerHTML = `
    <div class="insp-head"><h3>${escapeHtml(node.title)}</h3><span class="insp-kind">文本</span></div>
    <div class="insp-sub">下游节点用 <kbd>@</kbd> 引用这个节点时，这段文字会拼进提示词。</div>
    <div class="field">
      <label>文案</label>
      <textarea class="prompt-input" id="textBody" style="min-height:170px" placeholder="写一段文案、分镜描述或风格提示…">${escapeHtml(node.text)}</textarea>
    </div>
    <div class="insp-actions">
      <button class="btn danger" id="delText">删除节点</button>
      <button class="btn" id="mkImage">接到图片节点</button>
      <button class="btn primary" id="mkVideo">接到视频节点</button>
    </div>`;

  const textarea = $('#textBody', body);
  textarea.addEventListener('focus', () => beginTyping(node));
  textarea.addEventListener('blur', () => { S.typing = null; saveSoon(); });
  textarea.addEventListener('input', () => {
    node.text = textarea.value;
    node.updatedAt = Date.now();
    markDirty();
    renderNodeBody(node);
    renderNodeStatus(node);
  });
  const spawn = (kind) => {
    const child = createNode(kind, { x: node.x + node.w + 70, y: node.y });
    addEdge(node.id, child.id);
    toast('已创建下游节点，写提示词后 Ctrl+Enter 生成');
  };
  $('#mkImage').addEventListener('click', () => spawn('image'));
  $('#mkVideo').addEventListener('click', () => spawn('video'));
  $('#delText').addEventListener('click', () => deleteNode(node.id));
}

function beginTyping(node) {
  if (S.typing && S.typing.nodeId === node.id) return;
  pushHistory();
  S.typing = { nodeId: node.id };
}

function incomingSources(nodeId) {
  const sources = [];
  const seen = new Set();
  for (const edge of S.doc.edges) {
    if (edge.to !== nodeId) continue;
    const source = S.nodeById.get(edge.from);
    if (source && !seen.has(source.id)) { seen.add(source.id); sources.push(source); }
  }
  return sources;
}

/* ── @ 引用 ─────────────────────────────────────────────── */

function handleMentionInput(textarea, node) {
  const caret = textarea.selectionStart;
  const before = textarea.value.slice(0, caret);
  const match = /@([^\s@]{0,24})$/.exec(before);
  if (!match) { hideMention(); return; }
  const query = match[1].toLowerCase();
  const candidates = S.doc.nodes
    .filter((entry) => entry.id !== node.id)
    .filter((entry) => !query || entry.title.toLowerCase().includes(query))
    .slice(0, 8);
  if (!candidates.length) { hideMention(); return; }
  S.mention = { textarea, node, start: caret - match[0].length, end: caret, candidates };
  const menu = $('#mentionMenu');
  menu.innerHTML = '';
  candidates.forEach((candidate, index) => {
    const asset = (candidate.assets || []).find((entry) => entry.kind === 'image');
    const item = document.createElement('div');
    item.className = 'mention-item' + (index === 0 ? ' on' : '');
    item.innerHTML = `
      ${asset ? `<img class="mm-thumb" src="${mediaUrl(asset.id)}" loading="lazy" alt="">` : `<span class="mm-thumb">${KIND_META[candidate.kind].icon}</span>`}
      <span class="mm-body">
        <span class="mm-title">${escapeHtml(candidate.title)}</span>
        <span class="mm-meta">${KIND_META[candidate.kind].label}</span>
      </span>`;
    item.addEventListener('mousedown', (event) => { event.preventDefault(); applyMention(candidate); });
    menu.appendChild(item);
  });
  S.mention.index = 0;
  const rect = textarea.getBoundingClientRect();
  menu.style.left = Math.min(rect.left + 12, window.innerWidth - 250) + 'px';
  menu.style.top = Math.min(rect.top + Math.min(rect.height, 150), window.innerHeight - 280) + 'px';
  menu.classList.remove('hidden');
}

function hideMention() {
  S.mention = null;
  $('#mentionMenu').classList.add('hidden');
}

function applyMention(candidate) {
  const mention = S.mention;
  if (!mention) return;
  const { textarea, node, start, end } = mention;
  const insert = `@${candidate.title} `;
  const caret = start + insert.length;
  textarea.value = textarea.value.slice(0, start) + insert + textarea.value.slice(end);
  node.prompt = textarea.value;
  node.updatedAt = Date.now();
  addEdge(candidate.id, node.id, { silent: true });
  hideMention();
  markDirty();
  // addEdge 之后刷新检查器，让「参考」列表立刻出现新上游；随后把焦点和光标放回去。
  renderInspector();
  const next = $('#inspPrompt');
  if (next) {
    next.focus();
    next.setSelectionRange(caret, caret);
  }
  const count = $('#promptCount');
  if (count) count.textContent = `${node.prompt.length} 字`;
}

function onPromptKeyDown(event, textarea, node) {
  if (S.mention) {
    const items = $$('.mention-item');
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      S.mention.index = (S.mention.index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items.forEach((item, index) => item.classList.toggle('on', index === S.mention.index));
      return;
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      const target = S.mention && S.mention.candidates ? S.mention.candidates[S.mention.index] : null;
      if (target) applyMention(target);
      return;
    }
    if (event.key === 'Escape') { event.preventDefault(); hideMention(); return; }
  }
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    runNode(node);
  }
  if (event.key === 'Escape') {
    textarea.blur();
  }
}

/* ── 生成 / 任务 ────────────────────────────────────────── */

async function runNode(node) {
  if (!node || (node.kind !== 'image' && node.kind !== 'video')) return;
  if (node.status === 'queued' || node.status === 'running') return;
  const prompt = node.prompt.trim();
  if (!prompt) {
    selectOnly(node.id);
    toast('先在右侧写提示词', 'bad');
    setTimeout(() => $('#inspPrompt')?.focus(), 30);
    return;
  }

  const upstream = incomingSources(node.id);
  const textRefs = upstream.filter((source) => source.kind === 'text' && source.text.trim());
  const mediaRefs = upstream.filter((source) => source.kind !== 'text' && source.kind !== 'subject');
  const subjectRefs = upstream.filter((source) => source.kind === 'subject');
  const refAssetIds = [];
  const refNames = [];      // 与 refAssetIds 一一对应，用来把 @名字 标成"参考素材 N"
  // 同一素材可能被两条线重复引用（比如复制出来的节点）：这里和服务端、tasks 用同一套去重，
  // 否则客户端算出的"参考素材 2"在真正发给豆包的清单里并不存在。
  const refSeen = new Set();
  const refEntries = [];
  const pushRef = (assetId, name, kind) => {
    const id = String(assetId || '');
    if (!id || refSeen.has(id)) return;
    refSeen.add(id);
    refEntries.push({ id, name, kind: kind || 'image' });
  };
  const skipped = [];
  for (const source of mediaRefs) {
    const assets = source.assets || [];
    const asset = assets.find((entry) => entry.kind === 'image')
      || assets.find((entry) => entry.kind === 'video')
      || assets.find((entry) => entry.kind === 'audio');
    if (asset) pushRef(asset.id, source.title, asset.kind);
    else skipped.push(source.title);
  }
  const subjectNames = [];
  for (const source of subjectRefs) {
    const subject = S.subjects.find((entry) => entry.id === source.subjectId);
    if (!subject) continue;
    subjectNames.push(subject.name);
    for (const assetId of subject.assetIds || []) pushRef(assetId, subject.name, 'image');
    // 声音只有做视频时才有意义：图片生成带音频参考会让豆包读进一个"听不了的图"。
    if (node.kind === 'video' && subject.audioId) pushRef(subject.audioId, subject.name + ' 音色', 'audio');
  }
  // 桥接（vendor 的 actMedia）拼参考清单的顺序是 图片 → 视频 → 音频，
  // 这里必须用同一个顺序，否则"@名字（参考素材 N）"里的 N 会和清单对不上。
  const KIND_ORDER = { image: 0, video: 1, audio: 2 };
  refEntries.sort((a, b) => (KIND_ORDER[a.kind] ?? 3) - (KIND_ORDER[b.kind] ?? 3));
  for (const entry of refEntries) {
    refAssetIds.push(entry.id);
    refNames.push(entry.name);
  }
  // 名字里本身可能带空格（「晓艳 老师」），统一按去空格后的 key 建索引。
  const normalizeName = (name) => String(name || '').replace(/\s+/g, '');
  const refIndexByName = new Map();
  for (let i = 0; i < refNames.length; i += 1) {
    const key = normalizeName(refNames[i]);
    if (key && !refIndexByName.has(key)) refIndexByName.set(key, i + 1);
  }
  // @ 引用保留名字，并标上是第几个参考素材，豆包才能把名字和本地路径对上。
  // 豆包模型改写提示词时经常往名字中间塞空格（@晓艳 1），所以按"允许空格"匹配。
  // 不能用正则替换（短名字会命中长名字的前缀、也会重复标注），这里显式扫描：
  // 取 @ 后面的一串 → 去空格后找"最长的已知名字" → 只消耗这个名字实际占的字符。
  const mentionNames = [...refIndexByName.keys()].sort((a, b) => b.length - a.length);
  const annotateMentions = (text) => {
    const src = String(text);
    if (!mentionNames.length) return src;
    let out = '';
    let cursor = 0;
    while (cursor < src.length) {
      const at = src.indexOf('@', cursor);
      if (at < 0) { out += src.slice(cursor); break; }
      out += src.slice(cursor, at);
      let end = at + 1;
      let run = '';
      while (end < src.length && src[end] !== '@' && !/[，。；、！？\n]/.test(src[end]) && run.length <= 60) {
        run += src[end];
        end += 1;
      }
      const normalized = run.replace(/\s+/g, '');
      let matchedKey = '';
      for (const name of mentionNames) {
        if (normalized.startsWith(name) && name.length > matchedKey.length) matchedKey = name;
      }
      if (!matchedKey) { out += '@'; cursor = at + 1; continue; }
      let consumed = 0;
      let letters = 0;
      while (consumed < run.length && letters < matchedKey.length) {
        if (!/\s/.test(run[consumed])) letters += 1;
        consumed += 1;
      }
      if (run.slice(consumed).startsWith('（参考素材')) {
        // 已经标过编号，原样保留（幂等）。
        out += '@' + run;
        cursor = end;
        continue;
      }
      out += `@${matchedKey}（参考素材 ${refIndexByName.get(matchedKey)}）`;
      cursor = at + 1 + consumed;
    }
    return out;
  };

  let finalPrompt = annotateMentions(prompt).replace(/（参考素材\s*\d+）/g, '').replace(/[ \t]{2,}/g, ' ').trim();
  // 内置画布目前只回填文字提示词，参考图上传尚未接通；不追加“以参考图为准”，避免模型无图可依。
  // 生成面板里挂了主体（node.subjectIds）时，服务端会自己补「主体：…」；
  // 但服务端从不补「音色参考」，所以这两行要分开判断，否则边引用 + chip 同时用时音色说明会整行丢掉。
  const chipSubjectIds = Array.isArray(node.subjectIds) ? node.subjectIds : [];
  const chipSubjectNames = chipSubjectIds
    .map((id) => (S.subjects.find((entry) => entry.id === id) || {}).name)
    .filter(Boolean);
  const linkedOnlyNames = subjectNames.filter((name) => !chipSubjectNames.includes(name));
  if (linkedOnlyNames.length) {
    finalPrompt += `\n\n主体：${linkedOnlyNames.join('、')}（严格保持该主体的外观 / 长相 / 服饰一致，见参考图）`;
  }
  if (node.kind === 'video') {
    const voiced = [
      ...subjectRefs.map((source) => S.subjects.find((entry) => entry.id === source.subjectId)),
      ...chipSubjectIds.map((id) => S.subjects.find((entry) => entry.id === id)),
    ].filter((subject) => subject && subject.audioId).map((subject) => subject.name);
    const uniqueVoiced = [...new Set(voiced)];
    if (uniqueVoiced.length) {
      finalPrompt += `\n音色参考：${uniqueVoiced.join('、')}（只克隆声线与说话状态，不要念参考音频原本的内容）`;
    }
  }
  for (const source of textRefs) {
    finalPrompt += `\n\n参考文案（${source.title}）：\n${source.text.trim()}`;
  }

  // 不再直接走旧的 /api/generate（那条链路会在后台另起 actMedia）。
  // 新链路：本地只负责准备，先把提示词/模型/比例/时长同步进内置画布的生成条，
  // 拿到执行器返回的 itemId，再让用户确认后提交。
  node.status = 'preparing';
  node.statusText = '正在写入内置画布…';
  node.updatedAt = Date.now();
  node.bridgePrompt = finalPrompt;
  node.bridgeRefCount = refAssetIds.length;
  const builtinCount = 1;
  if (node.kind === 'image' && Number(nodeParams(node).count) > 1) {
    toast('内置画布一次只出 1 张；已按 1 张准备，多张请复制节点分别生成', 'bad');
  }
  renderNodeStatus(node);
  updateInspectorTask(node);
  markDirty();

  try {
    const prepared = await api('/api/builtin/prepare', {
      method: 'POST',
      body: {
        canvasId: BUILTIN_TARGET.canvasId,
        conversationId: BUILTIN_TARGET.conversationId,
        title: BUILTIN_TARGET.title,
        nodeId: node.id,
        itemId: node.assets.length ? '' : (node.builtinItemId || ''),
        kind: node.kind,
        prompt: finalPrompt,
        model: nodeParams(node).model,
        ratio: node.ratio,
        duration: nodeParams(node).duration,
        quality: nodeParams(node).quality,
        count: builtinCount,
      },
    });
    if (!prepared || !prepared.ok || !prepared.itemId) throw new Error('内置画布没有返回生成节点 id');
    if (node.status !== 'preparing') return; // 准备过程中被取消
    node.builtinItemId = prepared.itemId;
    node.bridgePlan = prepared.plan || null;
    node.bridgeConfigured = prepared.configured || null;
    node.bridgePrompt = finalPrompt;
    node.status = 'prepared';
    node.statusText = `已在内置画布准备好：${(prepared.configured && prepared.configured.model) || (prepared.plan && prepared.plan.modelLabel) || '默认模型'} · ${node.ratio} · ${nodeParams(node).duration}s；等待你确认`;
    node.updatedAt = Date.now();
    renderNodeStatus(node);
    updateInspectorTask(node);
    markDirty();
    if (skipped.length) toast(`已准备；${skipped.join('、')} 没有可用图片，未作为参考`, 'bad');
    openBridgeConfirm(node, prepared);
  } catch (error) {
    node.status = 'error';
    node.statusText = error.message;
    renderNodeStatus(node);
    updateInspectorTask(node);
    markDirty();
    toast(error.message, 'bad');
  }
}

function bridgePlanLine(plan) {
  if (!plan) return '';
  const bits = [];
  if (plan.modelLabel || plan.model) bits.push(plan.modelLabel || plan.model);
  if (plan.ratio) bits.push(plan.ratio);
  if (plan.kind === 'video') bits.push(`${plan.duration}s`);
  else bits.push(`${plan.count} 张 · ${plan.quality}`);
  if (plan.kind === 'video' && plan.quality) bits.push(plan.quality);
  if (plan.points) bits.push(`约 ${plan.points} 点`);
  return bits.join(' · ');
}

function openBridgeConfirm(node, prepared) {
  document.getElementById('bridgeConfirmMask')?.remove();
  const plan = (prepared && prepared.plan) || node.bridgePlan || {};
  const hasRefs = Number(node.bridgeRefCount) > 0;
  const mask = document.createElement('div');
  mask.id = 'bridgeConfirmMask';
  mask.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(16,18,24,.48);display:flex;align-items:center;justify-content:center;padding:24px;';
  const card = document.createElement('div');
  card.style.cssText = 'width:min(520px,100%);background:#fff;border:1px solid rgba(0,0,0,.1);border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.28);padding:22px 24px;font-family:inherit;color:#1d1d1f;';
  card.innerHTML = `
    <div style="font-size:18px;font-weight:650;margin-bottom:6px;">确认提交到内置画布</div>
    <div style="font-size:13px;line-height:1.6;color:#666;margin-bottom:16px;">本地只做前台，真正的生成由豆包内置画布执行。提交后会在内置画布上出现这条节点，并消耗豆包点数。</div>
    <div style="display:grid;gap:9px;font-size:13px;line-height:1.5;background:#f7f7f8;border-radius:10px;padding:13px 14px;margin-bottom:16px;">
      <div style="display:flex;justify-content:space-between;gap:16px;"><span style="color:#888;">入口</span><b style="font-weight:600;text-align:right;">豆包内置画布 · 后台执行器</b></div>
      <div style="display:flex;justify-content:space-between;gap:16px;"><span style="color:#888;">模型</span><b style="font-weight:600;text-align:right;">${escapeHtml(plan.modelLabel || (prepared.configured && prepared.configured.model) || '默认档位')}</b></div>
      <div style="display:flex;justify-content:space-between;gap:16px;"><span style="color:#888;">比例 / 时长</span><b style="font-weight:600;text-align:right;">${escapeHtml(plan.ratio || node.ratio)}${plan.kind === 'video' ? ' · ' + Number(plan.duration || nodeParams(node).duration) + ' 秒' : ` · ${Number(plan.count || 1)} 张`}</b></div>
      <div style="display:flex;justify-content:space-between;gap:16px;"><span style="color:#888;">清晰度（预估）</span><b style="font-weight:600;text-align:right;">${escapeHtml(plan.quality || nodeParams(node).quality || '内置档位')}</b></div>
      <div style="display:flex;justify-content:space-between;gap:16px;"><span style="color:#888;">预计消耗</span><b style="font-weight:650;text-align:right;color:#c2410c;">${plan.points ? '约 ' + plan.points + ' 点' : '以豆包扣点为准'}</b></div>
    </div>
    ${hasRefs ? `<div style="font-size:12px;line-height:1.55;color:#9a3412;background:#fff7ed;border:1px solid #fed7aa;border-radius:9px;padding:10px 12px;margin-bottom:14px;">本节点有 ${node.bridgeRefCount} 个参考素材。当前版本先把文字提示词同步进内置画布，参考图上传还在接入中；这次提交不会带图。</div>` : ''}
    <div style="font-size:12px;line-height:1.55;color:#777;margin-bottom:16px;">内置画布已准备好：提示词已写入生成条，模型 / 比例 / 时长已同步。确认后只点一次发送。</div>
    <div style="display:flex;justify-content:flex-end;gap:10px;">
      <button id="bridgeCancel" style="height:38px;padding:0 16px;border-radius:9px;border:1px solid #ddd;background:#fff;color:#333;font-size:13px;cursor:pointer;">先不提交</button>
      <button id="bridgeConfirm" style="height:38px;padding:0 18px;border-radius:9px;border:0;background:#2563eb;color:#fff;font-size:13px;font-weight:600;cursor:pointer;">确认提交${plan.points ? ' · ' + plan.points + ' 点' : ''}</button>
    </div>
  `;
  mask.appendChild(card);
  document.body.appendChild(mask);
  const close = () => mask.remove();
  $('#bridgeCancel', card).addEventListener('click', () => {
    close();
    node.status = 'idle';
    node.statusText = '已取消确认，未提交';
    renderNodeStatus(node);
    updateInspectorTask(node);
    renderInspector();
    markDirty();
    toast('已取消，没有提交到内置画布', 'ok');
  });
  mask.addEventListener('click', (event) => { if (event.target === mask) $('#bridgeCancel', card).click(); });
  $('#bridgeConfirm', card).addEventListener('click', () => {
    close();
    submitBuiltinNode(node);
  });
  document.addEventListener('keydown', function onEscape(event) {
    if (event.key !== 'Escape') return;
    document.removeEventListener('keydown', onEscape);
    if (document.getElementById('bridgeConfirmMask')) $('#bridgeCancel', card).click();
  }, { once: false });
}

async function submitBuiltinNode(node) {
  if (!node || !S.doc) return;
  node.status = 'queued';
  node.statusText = '提交到内置画布…';
  renderNodeStatus(node);
  updateInspectorTask(node);
  renderInspector();
  markDirty();
  try {
    const { task } = await api('/api/generate', {
      method: 'POST',
      body: {
        engine: 'builtin',
        canvasId: BUILTIN_TARGET.canvasId,
        nodeId: node.id,
        kind: node.kind,
        prompt: node.bridgePrompt || node.prompt,
        ratio: node.ratio,
        params: { ...nodeParams(node), count: 1 },
        conversationId: BUILTIN_TARGET.conversationId,
        conversationTitle: BUILTIN_TARGET.title,
        builtinItemId: node.builtinItemId || '',
      },
    });
    S.tasks.set(node.id, task);
    node.status = task.status === 'queued' ? 'queued' : 'running';
    node.statusText = task.message;
    node.taskId = task.id;
    renderNodeStatus(node);
    updateInspectorTask(node);
    renderInspector();
    markDirty();
  } catch (error) {
    node.status = 'error';
    node.statusText = error.message;
    renderNodeStatus(node);
    updateInspectorTask(node);
    renderInspector();
    markDirty();
    toast(error.message, 'bad');
  }
}

async function collectResult(node) {
  if (!node || !S.doc) return;
  try {
    toast('正在从豆包会话里取回结果…');
    const { assets } = await api('/api/collect', {
      method: 'POST',
      body: { canvasId: S.doc.id, nodeId: node.id, kind: node.kind, sid: node.lastSessionSid || '', match: String(node.prompt || '').replace(/\s+/g, ' ').trim().slice(0, 60) },
    });
    const list = Array.isArray(assets) ? assets : [];
    if (!list.length) { toast('豆包会话里没有找到可取的素材', 'bad'); return; }
    const known = new Set(node.assets.map((asset) => asset.id));
    const added = list.filter((asset) => !known.has(asset.id));
    node.assets = [...added.map((asset) => ({ id: asset.id, kind: asset.kind, w: asset.w, h: asset.h, duration: asset.duration })), ...node.assets];
    node.status = 'done';
    node.statusText = '已从豆包取回 ' + list.length + ' 个结果';
    node.updatedAt = Date.now();
    renderNodeBody(node);
    renderNodeStatus(node);
    renderInspector();
    markDirty();
    toast('已取回 ' + list.length + ' 个结果', 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  }
}

async function cancelNode(node) {
  if (!node.taskId) {
    node.status = 'idle';
    node.statusText = '已取消准备，未提交';
    node.updatedAt = Date.now();
    renderNodeStatus(node);
    updateInspectorTask(node);
    markDirty();
    toast('已取消本地准备，内置画布没有提交', 'ok');
    return;
  }
  try {
    await api('/api/tasks/' + node.taskId + '/cancel', { method: 'POST', body: {} });
    node.status = 'idle';
    node.statusText = '已取消等待';
    node.taskId = null;
    S.tasks.delete(node.id);
    renderNodeStatus(node);
    updateInspectorTask(node);
    markDirty();
  } catch (error) {
    toast('取消失败：' + error.message, 'bad');
  }
}

async function pollTasks() {
  const running = [...S.nodeById.values()].filter((node) => node.taskId && (node.status === 'queued' || node.status === 'running'));
  if (!running.length) return;
  await Promise.all(running.map(async (node) => {
    try {
      const { task } = await api('/api/tasks/' + node.taskId);
      applyTask(node, task);
    } catch (error) {
      if (/不存在|404/.test(error.message)) {
        node.status = 'error';
        node.statusText = '任务丢失（服务可能重启过）';
        node.taskId = null;
        renderNodeStatus(node);
        updateInspectorTask(node);
        markDirty();
      }
    }
  }));
}

function applyTask(node, task) {
  S.tasks.set(node.id, task);
  if (task.builtinItemId) node.builtinItemId = task.builtinItemId;
  if (task.status === 'done') {
    node.status = 'done';
    node.statusText = task.message;
    node.taskId = null;
    node.assets = (task.assets || []).map((asset) => ({ ...asset }));
    node.updatedAt = Date.now();
    renderNodeBody(node, S.els.get(node.id), true);
    renderNodeStatus(node);
    updateInspectorTask(node);
    if (S.sel.size === 1 && S.sel.has(node.id)) renderInspector();
    markDirty();
    refreshStats();
    toast(`${node.title}：${task.message}`, 'ok');
    return;
  }
  if (task.status === 'error') {
    node.status = 'error';
    node.statusText = task.error || task.message || '生成失败';
    node.taskId = null;
    renderNodeStatus(node);
    updateInspectorTask(node);
    if (S.sel.size === 1 && S.sel.has(node.id)) renderInspector();
    markDirty();
    toast(`${node.title} 失败：${node.statusText}`, 'bad');
    return;
  }
  if (task.status === 'cancelled') {
    node.status = 'idle';
    node.statusText = task.message || '已取消';
    node.taskId = null;
    renderNodeStatus(node);
    updateInspectorTask(node);
    return;
  }
  node.status = task.status === 'queued' ? 'queued' : 'running';
  node.statusText = task.message;
  if (task.sessionSid) node.lastSessionSid = task.sessionSid;
  renderNodeStatus(node);
  updateInspectorTask(node);
}

/** 只更新检查器里的任务行/按钮，不重建 textarea（避免打字时被打断）。 */
function updateInspectorTask(node) {
  if (S.sel.size !== 1 || !S.sel.has(node.id)) return;
  if (node.status === 'prepared' || node.status === 'preparing') { renderInspector(); return; }
  const line = $('#taskLine');
  const runBtn = $('#runBtn');
  if (!line || !runBtn) { renderInspector(); return; }
  const busy = ['queued', 'running', 'downloading', 'submitting'].includes(node.status);
  line.className = 'task-line' + (node.status === 'error' ? ' error' : node.status === 'done' ? ' done' : '');
  line.classList.toggle('hidden', !busy && node.status !== 'error' && node.status !== 'done');
  line.innerHTML = busy
    ? `<i class="spin"></i><span>${escapeHtml(node.statusText || '生成中…')}</span>`
    : escapeHtml(node.statusText || '');
  runBtn.disabled = busy;
  runBtn.textContent = busy ? '生成中…' : (node.assets.length ? '再生成一次' : '开始生成');
  let cancelBtn = $('#cancelBtn');
  if (busy && !cancelBtn) {
    cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn';
    cancelBtn.id = 'cancelBtn';
    cancelBtn.textContent = '取消等待';
    runBtn.after(cancelBtn);
    cancelBtn.addEventListener('click', () => cancelNode(node));
  } else if (!busy && cancelBtn) {
    cancelBtn.remove();
  }
}

/* ── 删除 / 下载 / 灯箱 ─────────────────────────────────── */

function deleteSelected() {
  if (!S.doc) return;
  if (S.selEdge) {
    pushHistory();
    S.doc.edges = S.doc.edges.filter((edge) => edge.id !== S.selEdge);
    S.selEdge = null;
    renderEdges('structure');
    renderInspector();
    markDirty();
    return;
  }
  if (!S.sel.size) return;
  pushHistory();
  const ids = new Set(S.sel);
  S.doc.nodes = S.doc.nodes.filter((node) => !ids.has(node.id));
  S.doc.edges = S.doc.edges.filter((edge) => !ids.has(edge.from) && !ids.has(edge.to));
  for (const id of ids) {
    S.els.get(id)?.remove();
    S.els.delete(id);
    S.nodeById.delete(id);
    S.tasks.delete(id);
  }
  S.sel.clear();
  renderEdges('structure');
  renderInspector();
  updateMeta();
  updateEmpty();
  markDirty();
  scheduleMinimap();
}

function deleteNode(id) {
  if (!S.doc) return;
  const node = S.nodeById.get(id);
  if (!node) return;
  pushHistory();
  S.doc.nodes = S.doc.nodes.filter((entry) => entry.id !== id);
  S.doc.edges = S.doc.edges.filter((edge) => edge.from !== id && edge.to !== id);
  S.els.get(id)?.remove();
  S.els.delete(id);
  S.nodeById.delete(id);
  S.tasks.delete(id);
  S.sel.delete(id);
  if (S.selEdge) {
    const still = S.doc.edges.some((edge) => edge.id === S.selEdge);
    if (!still) S.selEdge = null;
  }
  renderEdges('structure');
  renderInspector();
  updateMeta();
  updateEmpty();
  markDirty();
  scheduleMinimap();
}

async function loadExampleCanvas() {
  if (!S.doc) return;
  if (S.doc.nodes.length) {
    const created = await api('/api/canvases', { method: 'POST', body: { title: '示例画布' } });
    S.canvases.unshift({ id: created.canvas.id, title: created.canvas.title, nodeCount: 0, edgeCount: 0, updatedAt: Date.now() });
    await openCanvas(created.canvas.id);
  }
  const now = Date.now();
  const make = (kind, title, x, y, extra) => ({
    id: uid('n'), kind, title, x, y, w: 300,
    prompt: '', text: '', ratio: '1:1', status: 'idle', statusText: '', assets: [],
    createdAt: now, updatedAt: now, ...extra,
  });
  const text = make('text', '风格文案', 60, 120, { text: '暖色调，电影感，35mm 胶片颗粒，柔和侧光' });
  const image = make('image', '首帧', 430, 70, {
    prompt: '一只橘猫趴在窗台上看雨，暖色调，电影感，浅景深',
    ratio: '16:9',
  });
  const video = make('video', '镜头运动', 800, 90, {
    prompt: '镜头慢慢推近，雨滴落在窗玻璃上，慢动作，氛围感',
    ratio: '16:9',
  });
  S.doc.title = S.doc.title === '我的画布' || S.doc.title === '未命名画布' ? '示例画布' : S.doc.title;
  $('#canvasTitle').value = S.doc.title;
  S.doc.nodes = [text, image, video];
  S.doc.edges = [
    { id: uid('e'), from: text.id, to: image.id },
    { id: uid('e'), from: image.id, to: video.id },
  ];
  S.nodeById = new Map(S.doc.nodes.map((node) => [node.id, node]));
  S.sel.clear();
  S.selEdge = null;
  buildAllNodes();
  renderEdges('structure');
  syncSelection();
  selectOnly(image.id);
  fitAll();
  updateEmpty();
  updateMeta();
  markDirty();
  scheduleMinimap();
  toast('示例画布已载入：改提示词后 Ctrl+Enter 就能跑', 'ok');
}

function exportCanvas() {
  if (!S.doc) return;
  const payload = {
    format: 'heronbo-canvas',
    version: 1,
    exportedAt: new Date().toISOString(),
    canvas: {
      title: S.doc.title,
      nodes: S.doc.nodes,
      edges: S.doc.edges,
    },
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${S.doc.title || 'canvas'}.heronbo.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1200);
  toast('已导出画布 JSON（不含素材文件）', 'ok');
}

function importCanvas() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.json,application/json';
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const canvas = parsed.canvas || parsed;
      if (!canvas || !Array.isArray(canvas.nodes)) throw new Error('文件里没有节点数据');
      const created = await api('/api/canvases', { method: 'POST', body: { title: (canvas.title || '导入的画布').slice(0, 60) } });
      await api('/api/canvases/' + created.canvas.id, {
        method: 'PUT',
        body: { nodes: canvas.nodes, edges: Array.isArray(canvas.edges) ? canvas.edges : [] },
      });
      S.canvases.unshift({
        id: created.canvas.id,
        title: canvas.title || '导入的画布',
        nodeCount: canvas.nodes.length,
        edgeCount: Array.isArray(canvas.edges) ? canvas.edges.length : 0,
        updatedAt: Date.now(),
      });
      await openCanvas(created.canvas.id);
      renderSidebar();
      toast('已导入画布。素材不会随 JSON 过来，需要重新生成或上传。', 'ok');
    } catch (error) {
      toast('导入失败：' + error.message, 'bad');
    }
  });
  input.click();
}

async function downloadAsset(assetId) {
  try {
    const result = await api('/api/download', { method: 'POST', body: { assetId } });
    toast('已保存：' + result.path, 'ok');
  } catch (error) {
    toast('下载失败：' + error.message, 'bad');
  }
}

function openLightbox(node, index) {
  const assets = node.assets || [];
  if (!assets.length) return;
  S.lightbox = { nodeId: node.id, index: clamp(index, 0, assets.length - 1) };
  $('#lightbox').classList.remove('hidden');
  renderLightbox();
}

function renderLightbox() {
  if (!S.lightbox) return;
  const node = S.nodeById.get(S.lightbox.nodeId);
  if (!node) return closeLightbox();
  const assets = node.assets || [];
  const asset = assets[S.lightbox.index];
  if (!asset) return closeLightbox();
  $('#lightboxTitle').textContent = `${node.title} · ${S.lightbox.index + 1}/${assets.length}`
    + (asset.w && asset.h ? ` · ${asset.w}×${asset.h}` : '')
    + (asset.kind === 'video' && asset.duration ? ' · ' + fmtDuration(asset.duration) : '');
  const stage = $('#lightboxStage');
  stage.innerHTML = asset.kind === 'video'
    ? `<video src="${mediaUrl(asset.id)}" controls autoplay playsinline></video>`
    : `<img src="${mediaUrl(asset.id)}" alt="">`;
  $('#lightboxNav').innerHTML = assets.map((entry, index) => `
    <button data-i="${index}" title="${entry.kind === 'video' ? '视频' : '图片'} ${index + 1}" style="opacity:${index === S.lightbox.index ? 1 : .55}">
      ${entry.kind === 'video' ? '▶' : '▣'}
    </button>`).join('') + `<span>← → 切换 · Esc 关闭</span>`;
  $$('#lightboxNav button').forEach((button) => button.addEventListener('click', () => {
    S.lightbox.index = Number(button.dataset.i);
    renderLightbox();
  }));
}

function closeLightbox() {
  S.lightbox = null;
  $('#lightbox').classList.add('hidden');
  $('#lightboxStage').innerHTML = '';
}

/* ── 网关 / 统计 ────────────────────────────────────────── */

async function refreshGateway() {
  const pill = $('#gatewayPill');
  pill.querySelector('span').textContent = '检测中';
  pill.querySelector('.dot').className = 'dot wait';
  const card = $('#gatewayCard');
  card.innerHTML = '<div class="gateway-line"><i class="dot wait"></i><span>正在检测豆包连接…</span></div>';
  const launchBtn = $('#launchGatewayBtn');
  try {
    const status = await api('/api/gateway/status' + (S.doc ? '?canvasId=' + encodeURIComponent(S.doc.id) : ''));
    S.gateway = status;
    const mode = status.mode === 'doubaowork';
    const ok = status.ok;
    const authError = status.authError === true;
    const login = status.login || {};
    const loginKnown = login.known === true;
    const loggedIn = loginKnown ? login.loggedIn === true : null;

    if (mode) {
      pill.classList.toggle('bad', !ok);
      pill.querySelector('.dot').className = 'dot ' + (ok ? (loggedIn ? 'ok' : 'wait') : 'bad');
      pill.querySelector('span').textContent = !ok
        ? '豆包工作未启动'
        : loggedIn
          ? '豆包工作已登录'
          : '豆包工作待登录';
      card.innerHTML = `
        <div class="gateway-line"><i class="dot ${ok ? (loggedIn ? 'ok' : 'wait') : 'bad'}"></i><span><b>${!ok ? '未启动' : loggedIn ? '已登录' : '待登录'}</b> · 豆包工作客户端</span></div>
        <div style="margin-top:6px;line-height:1.55">${!ok
          ? '没连上豆包工作客户端：点「启动豆包工作」，会自动带调试端口拉起。'
          : loggedIn
            ? '已通过本机豆包工作客户端生成，不需要网关和 API Key。'
            : '客户端在线，还没登录：点「打开豆包工作」，在客户端里扫码。'}</div>
        ${ok && loggedIn ? `<div style="margin-top:5px;font-size:10.5px;color:var(--muted-2)">生成会话：${status.sessionSid ? '#' + escapeHtml(status.sessionSid) : '首次生成时创建'}（Agent 用另一段会话，互不打扰）<div style="margin-top:6px"><button class="side-action" data-reset-session>换新会话（清上下文）</button></div></div>` : ''}
        ${!ok
          ? '<button class="side-action" data-launch-dw>启动豆包工作</button>'
          : loggedIn === false
            ? '<button class="side-action" data-launch-dw>打开豆包工作登录</button>'
            : ''}`;
      if (!card.dataset.boundReset) {
    card.dataset.boundReset = '1';
    card.addEventListener('click', async (event) => {
      const btn = event.target.closest('[data-reset-session]');
      if (!btn || !S.doc) return;
      event.stopPropagation();
      try {
        await api('/api/gateway/reset-session', { method: 'POST', body: { canvasId: S.doc.id } });
        toast('已换新会话，下次生成会用一段干净的对话', 'ok');
        refreshGateway();
      } catch (error) {
        toast(error.message, 'bad');
      }
    });
  }
  card.querySelector('[data-launch-dw]')?.addEventListener('click', launchDoubaoWork);
      launchBtn.textContent = '启动豆包工作';
      launchBtn.classList.toggle('hidden', ok);
      if (ok && loggedIn === false) watchGatewayLogin();
      updateOnboardingStatus();
      maybeShowOnboarding();
      return;
    }

    const ready = ok && loggedIn !== false;
    pill.classList.toggle('bad', !ready && !authError);
    pill.querySelector('.dot').className = 'dot ' + (ready ? 'ok' : authError ? 'wait' : 'bad');
    pill.querySelector('span').textContent = authError
      ? '网关待配置'
      : !ok
        ? '网关未连接'
        : loggedIn === false
          ? '豆包未登录'
          : loggedIn === true
            ? '豆包工作版已登录'
            : '豆包已连接';

    let detail = '';
    if (!ok) {
      detail = authError
        ? '网关在线，但 API Key 不对：点「配置 API Key」，或选择 doubao2api 目录自动读取 .api_key。'
        : escapeHtml(status.error || '先启动 doubao2api，并用工作版账号登录');
    } else if (loggedIn === false) {
      detail = '网关在线，但豆包还没登录：点下面的「扫码登录豆包」，用手机豆包 App 扫码。';
    } else {
      const page = login.pageUrl ? escapeHtml(login.pageUrl.replace(/^https?:\/\//, '').slice(0, 42)) : '';
      detail = `模型：${escapeHtml((status.models || []).filter((name) => /image|video/.test(name)).join(' / ') || 'doubao')}${page ? '<br>页面：' + page : ''}`;
    }

    card.innerHTML = `
      <div class="gateway-line"><i class="dot ${ready ? 'ok' : loggedIn === false ? 'wait' : 'bad'}"></i><span><b>${!ok ? '连不上' : loggedIn === false ? '待登录' : '已连接'}</b> · ${escapeHtml((status.baseUrl || '').replace(/^https?:\/\//, ''))}</span></div>
      <div style="margin-top:6px;line-height:1.55">${detail}</div>
      ${ok && loggedIn === false ? '<button class="side-action" data-qr-login>扫码登录豆包</button><button class="side-action" data-open-admin style="margin-top:6px">打开网关后台</button>' : ''}
      ${!ok && authError ? '<button class="side-action" data-open-settings>配置 API Key</button>' : ''}`;
    card.querySelector('[data-qr-login]')?.addEventListener('click', openQrLogin);
    card.querySelector('[data-open-admin]')?.addEventListener('click', openGatewayAdmin);
    card.querySelector('[data-open-settings]')?.addEventListener('click', openSettings);
    launchBtn.textContent = '启动 doubao2api';
    launchBtn.classList.toggle('hidden', ok);
    updateOnboardingStatus();
    maybeShowOnboarding();
  } catch (error) {
    pill.querySelector('span').textContent = '豆包未知';
    pill.querySelector('.dot').className = 'dot bad';
    pill.classList.add('bad');
    card.innerHTML = `<div class="gateway-line"><i class="dot bad"></i><span>${escapeHtml(error.message)}</span></div>`;
    launchBtn.textContent = S.config && S.config.doubaoMode === 'external' ? '启动 doubao2api' : '启动豆包工作';
    launchBtn.classList.remove('hidden');
    updateOnboardingStatus();
    maybeShowOnboarding();
  }
}

let gatewayWatchTimer = 0;
let gatewayWatchUntil = 0;

function watchGatewayLogin(durationMs = 120000) {
  gatewayWatchUntil = Date.now() + durationMs;
  if (gatewayWatchTimer) return;
  gatewayWatchTimer = setInterval(async () => {
    if (Date.now() > gatewayWatchUntil) {
      clearInterval(gatewayWatchTimer);
      gatewayWatchTimer = 0;
      return;
    }
    await refreshGateway();
    if (S.gateway && S.gateway.login && S.gateway.login.loggedIn) {
      clearInterval(gatewayWatchTimer);
      gatewayWatchTimer = 0;
      toast('豆包工作已登录', 'ok');
    }
  }, 5000);
}

async function launchDoubaoWork() {
  const result = await api('/api/gateway/launch', { method: 'POST', body: {} })
    .catch((error) => ({ ok: false, message: error.message }));
  toast(result.message || (result.ok ? '已打开豆包工作' : '打开失败'), result.ok ? 'ok' : 'bad');
  setTimeout(refreshGateway, 2500);
  watchGatewayLogin();
}

function openQrLogin() {
  if (S.gateway && S.gateway.mode === 'doubaowork') {
    launchDoubaoWork();
    return;
  }
  openQrModal();
}

async function openGatewayAdmin() {
  try {
    await api('/api/gateway/open-admin', { method: 'POST', body: {} });
    if (S.gateway && S.gateway.mode === 'doubaowork') toast('已打开豆包工作客户端', 'ok');
    else toast('已打开豆包网关登录页，扫码登录后点「检测网关」', 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  }
}

let qrTimer = 0;
let agentBusy = false;

function openAgentPanel() {
  $('#agentPanel').classList.remove('hidden');
  $('#agentBtn').classList.add('on');
  renderAgentCtx();
  renderAgentMode();
  setTimeout(() => $('#agentInput')?.focus(), 60);
}

/** AI 面板顶部的「上下文」提示：让用户看得见这次对话会带上哪些节点。 */
function renderAgentCtx() {  const el = $('#agentCtx');
  if (!el) return;
  const nodes = [...S.sel].map((id) => S.nodeById.get(id)).filter(Boolean);
  if (!nodes.length) {
    el.classList.add('hidden');
    el.innerHTML = '';
    return;
  }
  const preview = nodes.length === 1 && nodes[0].prompt
    ? '：' + String(nodes[0].prompt).replace(/\s+/g, ' ').slice(0, 46) + '…'
    : '';
  el.classList.remove('hidden');
  el.innerHTML = `上下文：<b>${nodes.length}</b> 个选中节点（${nodes.slice(0, 3).map((node) => escapeHtml(node.title)).join('、')}）${escapeHtml(preview)}`
    + '<br>可以直接说「把这个提示词改得更有电影感」';
}

function closeAgentPanel() {
  $('#agentPanel').classList.add('hidden');
  $('#agentBtn').classList.remove('on');
}

/* ── AI 输入框里的 @ 引用 ──────────────────────────────── */

function agentMentionMeta(node) {
  if (node.kind === 'subject') {
    const subject = S.subjects.find((entry) => entry.id === node.subjectId);
    const images = subject ? (subject.assetIds || []).length : 0;
    const voice = subject && subject.audioId ? ' + 音色' : '';
    return `主体 · ${images} 张形象${voice}`;
  }
  const assets = node.assets || [];
  if (!assets.length) return node.kind === 'text' ? '文本' : '还没有素材';
  const count = { image: 0, video: 0, audio: 0 };
  for (const asset of assets) if (count[asset.kind] !== undefined) count[asset.kind] += 1;
  const parts = [];
  if (count.image) parts.push(`${count.image} 张图`);
  if (count.video) parts.push(`${count.video} 段视频`);
  if (count.audio) parts.push(`${count.audio} 条音频`);
  return parts.join(' · ') || `${assets.length} 个素材`;
}

function agentMentionCandidates(query) {
  const text = String(query || '').toLowerCase();
  const list = (S.doc ? S.doc.nodes : [])
    .filter((node) => !text || String(node.title || '').toLowerCase().includes(text))
    .slice()
    .sort((a, b) => {
      const rank = (node) => (node.kind === 'subject' ? 1 : (node.assets || []).length ? 0 : 2);
      return rank(a) - rank(b);
    })
    .slice(0, 8);
  return list;
}

function handleAgentMentionInput(textarea) {
  const caret = textarea.selectionStart;
  const before = textarea.value.slice(0, caret);
  const match = /@([^\s@]{0,24})$/.exec(before);
  if (!match) { hideAgentMention(); return; }
  const candidates = agentMentionCandidates(match[1]);
  if (!candidates.length) { hideAgentMention(); return; }
  S.agentMention = { textarea, start: caret - match[0].length, end: caret, candidates, index: 0 };
  const menu = $('#agentMentionMenu');
  if (!menu) return;
  menu.innerHTML = '';
  candidates.forEach((candidate, index) => {
    const cover = candidate.kind === 'subject'
      ? (() => {
        const subject = S.subjects.find((entry) => entry.id === candidate.subjectId);
        return subject && subject.coverId ? mediaUrl(subject.coverId) : '';
      })()
      : ((candidate.assets || []).find((asset) => asset.kind === 'image') || {}).id;
    const item = document.createElement('div');
    item.className = 'mention-item' + (index === 0 ? ' on' : '');
    item.innerHTML = `
      ${cover && candidate.kind !== 'subject' ? `<img class="mm-thumb" src="${mediaUrl(cover)}" loading="lazy" alt="">` : ''}
      ${candidate.kind === 'subject' && cover ? `<img class="mm-thumb" src="${cover}" loading="lazy" alt="">` : ''}
      ${!cover ? `<span class="mm-thumb">${KIND_META[candidate.kind].icon}</span>` : ''}
      <span class="mm-body">
        <span class="mm-title">${escapeHtml(candidate.title)}</span>
        <span class="mm-meta">${escapeHtml(agentMentionMeta(candidate))}</span>
      </span>`;
    item.addEventListener('mousedown', (event) => { event.preventDefault(); applyAgentMention(candidate); });
    menu.appendChild(item);
  });
  const rect = textarea.getBoundingClientRect();
  menu.style.left = Math.min(rect.left + 12, window.innerWidth - 260) + 'px';
  menu.style.top = Math.max(12, rect.top - 190) + 'px';
  menu.classList.remove('hidden');
}

function hideAgentMention() {
  S.agentMention = null;
  $('#agentMentionMenu')?.classList.add('hidden');
}

/**
 * 从输入文本里解析 @ 到了哪些节点。
 * 不维护"已选列表"，每次都按当前文本重算——否则删掉 @名字 之后提示还挂着。
 */
function collectAgentMentions(text) {
  const src = String(text || '');
  if (src.indexOf('@') < 0 || !S.doc) return [];
  const nodes = S.doc.nodes
    .filter((node) => node.title)
    .slice()
    .sort((a, b) => String(b.title).length - String(a.title).length);
  const ids = new Set();
  let cursor = 0;
  while (cursor < src.length) {
    const at = src.indexOf('@', cursor);
    if (at < 0) break;
    const rest = src.slice(at + 1);
    const hit = nodes.find((node) => rest.startsWith(node.title));
    if (hit) {
      ids.add(hit.id);
      cursor = at + 1 + hit.title.length;
    } else {
      cursor = at + 1;
    }
  }
  return [...ids];
}

function applyAgentMention(node) {
  const mention = S.agentMention;
  if (!mention || !node) return;
  const { textarea, start, end } = mention;
  const insert = `@${node.title} `;
  const caret = start + insert.length;
  textarea.value = textarea.value.slice(0, start) + insert + textarea.value.slice(end);
  textarea.selectionStart = caret;
  textarea.selectionEnd = caret;
  S.agentMentions = collectAgentMentions(textarea.value);
  hideAgentMention();
  textarea.focus();
  updateAgentCtxHint();
}

/** 输入框下面那行小提示：本句已经 @ 了哪些素材。 */
function updateAgentCtxHint() {
  const el = $('#agentMentionChips');
  if (!el) return;
  const names = (S.agentMentions || [])
    .map((id) => S.nodeById.get(id))
    .filter(Boolean)
    .map((node) => node.title);
  if (!names.length) { el.classList.add('hidden'); el.innerHTML = ''; return; }
  el.classList.remove('hidden');
  el.innerHTML = '已引用：' + names.slice(0, 6).map((name) => `<b>@${escapeHtml(name)}</b>`).join('、');
}

function appendAgentMessage(role, html) {
  const log = $('#agentLog');
  const wrap = document.createElement('div');
  wrap.className = 'agent-msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'agent-bubble';
  bubble.innerHTML = html;
  wrap.appendChild(bubble);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

/* ── 提示词 skill ──────────────────────────────────────── */

const SKILL_KEY = 'doubao-canvas:skill';
const AGENT_MODE_KEY = 'doubao-canvas:agent-mode';

function agentMode() {
  try { return localStorage.getItem(AGENT_MODE_KEY) === 'prompt' ? 'prompt' : 'plan'; } catch { return 'plan'; }
}

function renderAgentMode() {
  const mode = agentMode();
  $$('#agentMode button').forEach((btn) => btn.classList.toggle('on', btn.dataset.mode === mode));
  const input = $('#agentInput');
  if (input) {
    input.placeholder = mode === 'prompt'
      ? '想要什么提示词：例如「晓艳老师推这本书的口播镜头」；输入 @ 可引用画布素材'
      : '例如：做 3 个分镜，先首帧再视频，电影感；输入 @ 可引用画布素材';
  }
}

function setAgentMode(mode) {
  const next = mode === 'prompt' ? 'prompt' : 'plan';
  try { localStorage.setItem(AGENT_MODE_KEY, next); } catch { /* ignore */ }
  renderAgentMode();
  toast(next === 'prompt' ? '只出提示词：不会建节点' : '规划节点：会按目标建节点', 'ok');
}

function currentSkillId() {
  try { return localStorage.getItem(SKILL_KEY) || 'auto'; } catch { return 'auto'; }
}

function skillById(id) {
  return (S.skills || []).find((skill) => skill.id === id) || null;
}

function skillName(id) {
  if (!id || id === 'auto') return '自动匹配';
  const skill = skillById(id);
  return skill ? skill.name : id;
}

function renderSkillChips() {
  const box = $('#agentSkills');
  if (!box) return;
  const active = currentSkillId();
  const chips = [{ id: 'auto', name: '自动', icon: '✦', hint: '按需求自动挑最合适的口径' }].concat(S.skills || []);
  box.innerHTML = chips.map((skill) => `<button class="skill-chip${skill.id === active ? ' on' : ''}" data-skill="${escapeHtml(skill.id)}" title="${escapeHtml(skill.hint || '')}">${escapeHtml(skill.icon || '')} ${escapeHtml(skill.name)}</button>`).join('');
  box.querySelectorAll('.skill-chip').forEach((btn) => btn.addEventListener('click', () => {
    try { localStorage.setItem(SKILL_KEY, btn.dataset.skill); } catch { /* ignore */ }
    renderSkillChips();
    toast(`提示词 skill：${skillName(btn.dataset.skill)}`, 'ok');
  }));
}

async function loadSkills() {
  try {
    const res = await api('/api/skills');
    S.skills = Array.isArray(res.skills) ? res.skills : [];
  } catch {
    S.skills = [];
  }
  renderSkillChips();
}

/** 生成面板上的「用 skill 优化」：只改这一条提示词，不动画布结构。 */
async function polishNodePrompt(node) {
  if (!node || (node.kind !== 'image' && node.kind !== 'video')) return;
  if (node.status === 'queued' || node.status === 'running') {
    toast('正在生成，等结束再优化提示词', 'bad');
    return;
  }
  const prompt = String(node.prompt || '').trim();
  if (!prompt) {
    toast('先写几个字，再让 skill 帮你扩写', 'bad');
    $('#inspPrompt')?.focus();
    return;
  }
  const btn = $('#skillPolish');
  if (btn) { btn.disabled = true; btn.textContent = '优化中…'; }
  try {
    const upstream = incomingSources(node.id).map((source) => source.title);
    const res = await api('/api/agent/rewrite', {
      method: 'POST',
      body: {
        canvasId: S.doc.id,
        nodeId: node.id,
        kind: node.kind,
        prompt,
        title: node.title,
        upstream,
        skill: currentSkillId(),
      },
    });
    const better = String((res && res.prompt) || '').trim();
    if (!better) throw new Error('模型没有返回提示词');
    pushHistory();
    node.prompt = better;
    node.updatedAt = Date.now();
    const textarea = $('#inspPrompt');
    if (textarea) textarea.value = better;
    const count = $('#promptCount');
    if (count) count.textContent = `${better.length} 字`;
    node.statusText = `已按「${skillName(res.skill)}」改写提示词`;
    renderNodeStatus(node);
    markDirty();
    toast(`提示词已按「${skillName(res.skill)}」改写（可 Ctrl+Z 撤销）`, 'ok');
  } catch (error) {
    toast('优化失败：' + error.message, 'bad');
  } finally {
    const again = $('#skillPolish');
    if (again) { again.disabled = false; again.textContent = '✨ 用 skill 优化'; }
  }
}

function applyAgentUpdates(updates) {
  const applied = [];
  for (const patch of updates || []) {
    const node = S.nodeById.get(String(patch.nodeId || ''));
    if (!node) continue;
    if (typeof patch.title === 'string' && patch.title) node.title = patch.title;
    if (typeof patch.prompt === 'string' && patch.prompt) node.prompt = patch.prompt;
    if (typeof patch.text === 'string' && patch.text) node.text = patch.text;
    node.updatedAt = Date.now();
    applied.push(node);
  }
  for (const node of applied) {
    const el = S.els.get(node.id);
    if (!el) continue;
    const titleEl = $('.node-title', el);
    if (titleEl) titleEl.textContent = node.title;
    renderNodeBody(node, el, true);
    renderNodeStatus(node, el);
  }
  if (applied.length) {
    renderInspector();
    updateMeta();
    markDirty();
  }
  return applied;
}

function applyAgentPlan(plan) {
  const updateList = Array.isArray(plan.updates) ? plan.updates : [];
  const nodeList = Array.isArray(plan.nodes) ? plan.nodes : [];
  if (!nodeList.length) {
    // 纯改写：不动布局，只把选中节点的提示词换掉。
    if (!updateList.length) return { created: [], updated: [], byKey: new Map() };
    pushHistory();
    const updated = applyAgentUpdates(updateList);
    return { created: [], updated, byKey: new Map() };
  }
  pushHistory();
  const rect = viewportRect();
  const center = screenToWorld(rect.width / 2 - 180, rect.height / 2 - 140);
  let base = center;
  if (S.doc.nodes.length) {
    base = {
      x: Math.max(...S.doc.nodes.map((node) => node.x + node.w)) + 120,
      y: Math.min(...S.doc.nodes.map((node) => node.y)),
    };
  }
  const levels = new Map();
  for (const item of plan.nodes) {
    levels.set(item.key, item.ref ? (levels.get(item.ref) || 0) + 1 : 0);
  }
  const counts = new Map();
  const byKey = new Map();
  const created = [];
  for (const item of plan.nodes) {
    const level = levels.get(item.key) || 0;
    const index = counts.get(level) || 0;
    counts.set(level, index + 1);
    const node = {
      id: uid('n'),
      kind: item.kind,
      title: item.title || KIND_META[item.kind].title,
      x: Math.round(base.x + level * 360),
      y: Math.round(base.y + index * 340),
      w: 300,
      prompt: item.kind === 'text' ? '' : item.prompt,
      text: item.kind === 'text' ? item.text : '',
      ratio: item.ratio || '16:9',
      status: 'idle',
      statusText: '',
      assets: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    S.doc.nodes.push(node);
    S.nodeById.set(node.id, node);
    byKey.set(item.key, node);
    created.push(node);
  }
  for (const item of plan.nodes) {
    if (!item.ref) continue;
    const from = byKey.get(item.ref);
    const to = byKey.get(item.key);
    if (from && to) S.doc.edges.push({ id: uid('e'), from: from.id, to: to.id });
  }
  buildAllNodes();
  renderEdges('structure');
  selectOnly(created[0] ? created[0].id : null);
  fitNodes(created);
  updateEmpty();
  updateMeta();
  markDirty();
  scheduleMinimap();
  const updated = updateList.length ? applyAgentUpdates(updateList) : [];
  return { created, updated, byKey };
}

/** 「只出提示词」模式的气泡：每条提示词一张卡片，可复制或填进选中的节点。 */
function renderAgentPrompts(bubble, result) {
  const prompts = Array.isArray(result.prompts) ? result.prompts : [];
  bubble.innerHTML = `<div class="agent-summary">${escapeHtml(result.summary || `给了 ${prompts.length} 条提示词`)}</div>`
    + `<div class="agent-skill-tag">口径：${escapeHtml(skillName(result.skill))} · 未建节点</div>`
    + prompts.map((item, index) => `<div class="prompt-card">
        <div class="prompt-card-head">
          <b>${escapeHtml(item.title)}</b>
          <span class="prompt-card-kind">${item.kind === 'video' ? '视频' : '图片'}</span>
        </div>
        <div class="prompt-card-body">${escapeHtml(item.prompt)}</div>
        <div class="prompt-card-actions">
          <button class="btn" data-copy="${index}">复制</button>
          <button class="btn primary" data-fill="${index}">填入选中节点</button>
        </div>
      </div>`).join('');
  bubble.querySelectorAll('[data-copy]').forEach((btn) => btn.addEventListener('click', async () => {
    const item = prompts[Number(btn.dataset.copy)];
    if (!item) return;
    try {
      await navigator.clipboard.writeText(item.prompt);
      toast('已复制，可以直接粘到节点里', 'ok');
    } catch {
      toast('复制失败：手动选中正文复制吧', 'bad');
    }
  }));
  bubble.querySelectorAll('[data-fill]').forEach((btn) => btn.addEventListener('click', () => {
    const item = prompts[Number(btn.dataset.fill)];
    if (!item) return;
    const target = [...S.sel].map((id) => S.nodeById.get(id))
      .find((node) => node && (node.kind === 'image' || node.kind === 'video'));
    if (!target) {
      toast('先在画布上选中一个图片 / 视频节点', 'bad');
      return;
    }
    pushHistory();
    target.prompt = item.prompt;
    target.updatedAt = Date.now();
    target.statusText = '已填入 AI 写的提示词';
    renderNodeBody(target);
    renderNodeStatus(target);
    if (S.sel.size === 1) renderInspector();
    markDirty();
    toast(`已填入「${target.title}」`, 'ok');
  }));
}

async function sendAgent() {
  if (agentBusy) return;
  const input = $('#agentInput');
  const goal = input.value.trim();
  if (!goal) { toast('先告诉 Agent 你想做什么', 'bad'); input.focus(); return; }
  // 先把本句 @ 到的节点拍成快照，再清输入框状态（否则请求里永远是空数组）。
  const mentionedIds = collectAgentMentions(input.value);
  input.value = '';
  S.agentMentions = [];
  updateAgentCtxHint();
  appendAgentMessage('user', escapeHtml(goal));
  const bubble = appendAgentMessage('assistant', '<span><i class="spin" style="display:inline-block;vertical-align:-1px;margin-right:6px"></i>正在拆解目标…</span>');
  agentBusy = true;
  $('#agentSend').disabled = true;
  try {
    const result = await api('/api/agent/plan', {
      method: 'POST',
      body: {
        goal,
        canvasId: S.doc.id,
        skill: currentSkillId(),
        mode: agentMode(),
        context: {
          mentioned: mentionedIds,
          nodes: S.doc.nodes.map((node) => ({
            id: node.id,
            title: node.title,
            kind: node.kind,
            prompt: node.prompt ? node.prompt.slice(0, 240) : '',
          })),
          edges: S.doc.edges.map((edge) => ({ from: edge.from, to: edge.to })),
          selected: [...S.sel]
            .map((id) => S.nodeById.get(id))
            .filter(Boolean)
            .map((node) => ({
              id: node.id,
              title: node.title,
              kind: node.kind,
              prompt: node.prompt || '',
              text: node.text || '',
              ratio: node.ratio || '',
              params: node.params || {},
              upstream: incomingSources(node.id).map((source) => source.title),
            })),
        },
      },
    });
    if (result.mode === 'prompt') {
      if (!Array.isArray(result.prompts) || !result.prompts.length) {
        bubble.innerHTML = `<span style="color:var(--red)">Agent 没有给出提示词：${escapeHtml(result.error || '返回为空')}</span>`
          + '<div class="agent-actions"><button class="btn" data-retry>再试一次</button></div>';
        bubble.querySelector('[data-retry]')?.addEventListener('click', () => { input.value = goal; sendAgent(); });
        return;
      }
      renderAgentPrompts(bubble, result);
      return;
    }
    if (!result.plan) {
      const reason = !S.sel.size && /没有给出可用的节点/.test(result.error || '')
        ? '没选中节点：要改写提示词请先在画布上选中一个节点；要新建节点请说清要做什么。'
        : (result.error || '没有计划');
      bubble.innerHTML = `<span style="color:var(--red)">${escapeHtml(reason)}</span>`
        + `<div class="agent-actions"><button class="btn" data-retry>再试一次</button></div>`;
      bubble.querySelector('[data-retry]')?.addEventListener('click', () => {
        input.value = goal;
        sendAgent();
      });
      return;
    }
    const plan = result.plan;
    const { created, updated, byKey } = applyAgentPlan(plan);
    const runnable = created.filter((node) => node.kind !== 'text');
    const suggested = (plan.autoRun || [])
      .map((key) => byKey.get(key))
      .filter((node) => node && node.kind !== 'text');
    const list = plan.nodes.map((item) => {
      const refTitle = item.ref && byKey.get(item.ref) ? byKey.get(item.ref).title : '';
      return `<li>${KIND_META[item.kind].label} · ${escapeHtml(item.title)}${refTitle ? ' ← ' + escapeHtml(refTitle) : ''}</li>`;
    }).join('');
    const changed = updated.map((node) => {
      const preview = String(node.prompt || node.text || '').replace(/\s+/g, ' ').slice(0, 120);
      return `<li>改写 · ${escapeHtml(node.title)}<div class="agent-preview">${escapeHtml(preview)}${preview.length >= 120 ? '…' : ''}</div></li>`;
    }).join('');
    let actions = '';
    if (suggested.length) {
      actions += `<button class="btn primary" data-run="suggested">运行建议的 ${suggested.length} 个节点</button>`;
      if (runnable.length > suggested.length) {
        actions += `<button class="btn" data-run="all">运行全部 ${runnable.length} 个</button>`;
      }
    } else if (runnable.length) {
      actions += `<button class="btn primary" data-run="all">运行这 ${runnable.length} 个节点</button>`;
    }
    if (updated.length === 1 && runnable.length === 0) {
      actions += `<button class="btn primary" data-run-updated>用新提示词生成</button>`;
    }
    actions += '<button class="btn" data-undo>撤销这一步</button>';
    bubble.innerHTML = `<div class="agent-summary">${escapeHtml(plan.summary)}</div>`
      + `<div class="agent-skill-tag">口径：${escapeHtml(skillName(result.skill))}</div>`
      + (list ? `<ul class="agent-list">${list}</ul>` : '')
      + (changed ? `<ul class="agent-list agent-changes">${changed}</ul>` : '')
      + `<div class="agent-actions">${actions}</div>`;
    const runList = (nodes) => {
      if (!nodes.length) return;
      nodes.forEach((node) => runNode(node));
      toast(`已提交 ${nodes.length} 个生成任务`, 'ok');
    };
    bubble.querySelector('[data-run="suggested"]')?.addEventListener('click', () => runList(suggested));
    bubble.querySelector('[data-run="all"]')?.addEventListener('click', () => runList(runnable));
    bubble.querySelector('[data-run-updated]')?.addEventListener('click', () => runList(updated.filter((node) => node.kind === 'image' || node.kind === 'video')));
    bubble.querySelector('[data-undo]')?.addEventListener('click', () => {
      undo();
      const actions = bubble.querySelector('.agent-actions');
      if (actions) actions.remove();
      bubble.classList.add('undone');
      const tag = document.createElement('div');
      tag.className = 'agent-skill-tag';
      tag.textContent = created.length ? '已撤销，这些节点已从画布移除' : '已撤销，提示词已还原';
      bubble.appendChild(tag);
    });
    const parts = [];
    if (created.length) parts.push(`建了 ${created.length} 个节点`);
    if (updated.length) parts.push(`改写了 ${updated.length} 个提示词`);
    toast(parts.length ? `Agent ${parts.join('、')}` : 'Agent 没有改动画布', parts.length ? 'ok' : 'bad');
  } catch (error) {
    const needLogin = /登录|Not logged in|扫码/i.test(error.message);
    bubble.innerHTML = `<span style="color:var(--red)">${escapeHtml(error.message)}</span>`
      + (needLogin
        ? '<div class="agent-actions"><button class="btn primary" data-login>扫码登录豆包</button></div>'
        : '<div class="agent-actions"><button class="btn" data-retry>再试一次</button></div>');
    bubble.querySelector('[data-login]')?.addEventListener('click', openQrLogin);
    bubble.querySelector('[data-retry]')?.addEventListener('click', () => {
      input.value = goal;
      sendAgent();
    });
  } finally {
    agentBusy = false;
    $('#agentSend').disabled = false;
    $('#agentLog').scrollTop = $('#agentLog').scrollHeight;
  }
}

async function openQrModal() {
  $('#qrMask').classList.remove('hidden');
  $('#qrModal').classList.remove('hidden');
  await loadQr();
}

async function loadQr() {
  clearInterval(qrTimer);
  const box = $('#qrBox');
  const hint = $('#qrHint');
  box.textContent = '生成中…';
  hint.className = 'qr-hint';
  hint.textContent = '正在向本机网关申请二维码…';
  try {
    const result = await api('/api/gateway/qr-login', { method: 'POST', body: {} });
    if (result.qr_image_base64) {
      box.innerHTML = `<img src="data:image/png;base64,${result.qr_image_base64}" alt="豆包登录二维码">`;
      hint.textContent = '打开手机豆包 App → 右上角扫一扫，确认登录（二维码 1~2 分钟内有效）';
      startQrPoll();
    } else {
      hint.className = 'qr-hint bad';
      hint.textContent = result.error || result.message || '二维码生成失败';
    }
  } catch (error) {
    hint.className = 'qr-hint bad';
    hint.textContent = error.message;
  }
}

function startQrPoll() {
  clearInterval(qrTimer);
  qrTimer = setInterval(async () => {
    try {
      const status = await api('/api/gateway/qr-login');
      if (status.status === 'success') {
        clearInterval(qrTimer);
        const hint = $('#qrHint');
        hint.className = 'qr-hint ok';
        hint.textContent = '登录成功，正在刷新网关状态…';
        setTimeout(() => { closeQrLogin(); refreshGateway(); }, 1200);
        toast('豆包工作版登录成功', 'ok');
      } else if (status.status === 'failed') {
        clearInterval(qrTimer);
        const hint = $('#qrHint');
        hint.className = 'qr-hint bad';
        hint.textContent = status.error || '扫码失败，请重新生成二维码';
      }
    } catch { /* 网络抖动继续等 */ }
  }, 1800);
}

function closeQrLogin() {
  clearInterval(qrTimer);
  qrTimer = 0;
  $('#qrMask').classList.add('hidden');
  $('#qrModal').classList.add('hidden');
}

async function refreshStats() {
  try {
    const stats = await api('/api/stats');
    const card = $('#storageCard');
    const bytes = stats.media.bytes || 0;
    const wide = Math.min(100, (bytes / (4 * 1024 * 1024 * 1024)) * 100);
    card.innerHTML = `
      <div class="storage-bar"><i style="width:${wide}%"></i></div>
      <span>${stats.media.count} 个素材 · ${fmtBytes(bytes)}<br>全部保存在本机 data\\ 目录</span>`;
  } catch { /* ignore */ }
}

/* ── 设置 / 帮助 ────────────────────────────────────────── */

/** 工作模型下拉：不可选时只显示当前值，别给一个点了没反应的假选项。 */
function agentModelOptions(config) {
  const current = (config && config.agentModel) || '';
  const models = (config && Array.isArray(config.agentModels) && config.agentModels.length)
    ? config.agentModels.slice(0, 60)
    : [current || 'doubao-pro'];
  const list = models.includes(current) || !current ? models : [current, ...models];
  return list
    .map((name) => `<option value="${escapeHtml(name)}" ${name === current ? 'selected' : ''}>${escapeHtml(name)}</option>`)
    .join('');
}

function fmtLedgerPoints(sum) {
  const points = Math.round(((sum && Number(sum.points)) || 0) * 10) / 10;
  const calls = (sum && Number(sum.calls)) || 0;
  return `${points} 点 / ${calls} 次`;
}

function fmtLedgerTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (value) => String(value).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderLedger(box, result) {
  if (!box) return;
  const rows = (result && result.rows) || [];
  const summary = (result && result.summary) || {};
  if (!rows.length) {
    box.innerHTML = '<div class="ledger-empty">还没有记录。生成一次，或让 Agent 规划一次，这里就会出现明细。</div>';
    return;
  }
  const head = `<div class="ledger-sum">
    <span>今日 <b>${fmtLedgerPoints(summary.today)}</b></span>
    <span>近 7 天 <b>${fmtLedgerPoints(summary.week)}</b></span>
    <span>累计 <b>${fmtLedgerPoints(summary.total)}</b></span>
  </div>`;
  const items = rows.slice(0, 12).map((row) => {
    const kind = row.kind === 'video' ? '视频' : row.kind === 'image' ? '图片' : '文本';
    const statusText = row.status === 'done' ? '成功' : row.status === 'error' ? '失败' : row.status === 'cancelled' ? '取消' : String(row.status || '');
    const cost = row.points ? `${row.points} 点${row.pointsEstimated ? '（估）' : ''}` : '不计点';
    const detail = row.kind === 'chat'
      ? `${row.charsIn || 0} 字 → ${row.charsOut || 0} 字`
      : `${row.model || '默认'}${row.quality ? ' · ' + row.quality : ''}${row.kind === 'video' ? ' · ' + (row.duration || 0) + ' 秒' : ''}`;
    return `<div class="ledger-row">
      <span class="ledger-time">${fmtLedgerTime(row.at)}</span>
      <span class="ledger-kind">${kind}</span>
      <span class="ledger-detail">${escapeHtml(detail)}</span>
      <span class="ledger-cost ${row.points ? '' : 'muted'}">${cost}</span>
      <span class="ledger-status ${row.status === 'done' ? 'ok' : 'bad'}">${statusText}</span>
    </div>`;
  }).join('');
  box.innerHTML = head + items + `<div class="ledger-more">最近 ${Math.min(rows.length, 12)} 条 · 完整明细见 data/ledger.jsonl</div>`;
}

function openSettings() {
  const config = S.config;
  const mode = config.doubaoMode || 'doubaowork';
  const body = $('#settingsBody');
  body.innerHTML = `
    <div class="set-row">
      <label>豆包接入方式</label>
      <div class="seg" id="setMode">
        <button data-v="doubaowork" class="${mode === 'doubaowork' ? 'on' : ''}">豆包工作客户端（推荐）</button>
        <button data-v="external" class="${mode === 'external' ? 'on' : ''}">外部网关 doubao2api</button>
      </div>
      <div class="set-hint">推荐方式：画布直接接管本机「豆包工作」客户端，不需要 API Key / 启动脚本。</div>
    </div>
    <div id="dwFields" class="${mode === 'doubaowork' ? '' : 'hidden'}">
      <div class="set-row">
        <label>豆包工作 exe（可选）</label>
        <div class="input-row">
          <input class="set-input mono" id="setDoubaoExe" value="${escapeHtml(config.doubaoworkExe || '')}" placeholder="留空自动查找 D:\\DoubaoWork\\app\\DoubaoWork.exe">
          ${S.desktop ? '<button class="btn" id="browseDoubao">浏览…</button>' : ''}
        </div>
        <div class="set-hint">会在常见安装位置、注册表和磁盘里自动找；找不到再手选。</div>
      </div>
      <div class="set-actions">
        <button class="btn" id="setDwLaunch">启动 / 打开豆包工作</button>
        <button class="btn" id="setDwCheck">检测客户端</button>
      </div>
    </div>
    <div id="extFields" class="${mode === 'external' ? '' : 'hidden'}">
    <div class="set-row">
      <label>豆包网关地址</label>
      <input class="set-input mono" id="setGateway" value="${escapeHtml(config.gateway.baseUrl)}" placeholder="http://127.0.0.1:9090">
      <div class="set-hint">本机 doubao2api 的地址。画布只跟它通信，不直接连豆包。</div>
    </div>
    <div class="set-row">
      <label>API Key</label>
      <input class="set-input mono" id="setApiKey" type="password" placeholder="${config.gateway.apiKeySet ? '已配置 · ' + escapeHtml(config.gateway.apiKeyHint || '') + '（留空保持不变）' : '未配置'}">
      <div class="set-hint">通常会自动读取 doubao2api\\.api_key，不用手填。</div>
    </div>
    <div class="set-row">
      <label>网关启动脚本（可选）</label>
      <div class="input-row">
        <input class="set-input mono" id="setStartScript" value="${escapeHtml(config.gatewayStartScript || '')}" placeholder="D:\\ZCode工作区\\doubao2api\\start.bat">
        ${S.desktop ? '<button class="btn" id="browseScript">浏览…</button>' : ''}
      </div>
      <div class="set-hint">「启动 doubao2api」按钮会调用它；留空则自动找兄弟目录下的 start.bat。</div>
      ${S.desktop ? '<button class="btn" id="chooseGatewayDir" style="margin-top:6px">选择 doubao2api 目录</button>' : ''}
    </div>
    </div>
    <div class="set-row">
      <label>工作模型（Agent 规划 / 提示词）</label>
      <select class="set-input" id="setAgentModel" ${config.agentModelSelectable ? '' : 'disabled'}>
        ${agentModelOptions(config)}
      </select>
      <div class="set-hint">${escapeHtml(config.agentModelSelectable
        ? '画布用这个模型做节点规划与提示词改写；真正的出图 / 出视频仍走豆包生成通道。'
        : (config.agentModelReason || '当前接入方式不支持选模型。'))}</div>
    </div>
    <div class="set-row">
      <label>消耗记录</label>
      <div id="ledgerBox" class="ledger-box">加载中…</div>
      <div class="set-hint">每笔生成与文本调用都落账；生成点数按档位估算，实际以豆包账单为准。</div>
    </div>
    <div class="set-row">
      <label>下载目录</label>
      <div class="input-row">
        <input class="set-input mono" id="setDownloadDir" value="${escapeHtml(config.downloadDir || '')}">
        ${S.desktop ? '<button class="btn" id="browseDownload">浏览…</button>' : ''}
      </div>
    </div>
    <div class="set-row">
      <label>渲染模式</label>
      <div class="seg" id="setPerf">
        <button data-v="auto" class="${config.performanceMode === 'auto' ? 'on' : ''}">自动</button>
        <button data-v="quality" class="${config.performanceMode === 'quality' ? 'on' : ''}">画质优先</button>
        <button data-v="speed" class="${config.performanceMode === 'speed' ? 'on' : ''}">性能优先</button>
      </div>
      <div class="set-hint">性能优先会关掉卡片阴影 / 毛玻璃，节点多、低配电脑更顺。</div>
    </div>
    <div class="set-row">
      <label>主题</label>
      <div class="seg" id="setTheme">
        <button data-v="dark" class="${config.theme === 'dark' ? 'on' : ''}">深色</button>
        <button data-v="light" class="${config.theme === 'light' ? 'on' : ''}">浅色</button>
      </div>
    </div>
    <div class="set-actions">
      <button class="btn primary" id="setSave">保存设置</button>
      <button class="btn ext-only" id="setCheck">检测网关</button>
      <button class="btn ext-only" id="setLaunch">启动 doubao2api</button>
      <button class="btn ext-only" id="setAdmin">扫码登录豆包</button>
      <button class="btn ext-only" id="setOpenAdmin">打开网关后台</button>
      <button class="btn dw-only" id="setDwLaunch2">打开豆包工作</button>
      <button class="btn" id="setOpenDir">打开下载目录</button>
      <button class="btn" id="setOnboard">首次使用向导</button>
      <button class="btn" id="setExport">导出画布 JSON</button>
      <button class="btn" id="setImport">导入画布 JSON</button>
      ${S.desktop ? '<button class="btn" id="setOpenData">打开数据目录</button>' : ''}
      <button class="btn danger" id="setClean">清理未引用素材</button>
    </div>
    <div class="set-status" id="setStatus">数据目录：${escapeHtml(S.paths?.data || '')}</div>
    <div class="set-status" id="setAbout" style="margin-top:8px">
      HeronBo · 豆包画布 ${S.version ? 'v' + escapeHtml(S.version) : ''} · ${S.desktop ? (S.desktop.isPortable ? '桌面版（便携）' : '桌面版') : '浏览器本地版'}
      ${S.desktop ? '<br>Electron ' + escapeHtml(S.desktop.electron) + ' · Chromium ' + escapeHtml(S.desktop.chrome) : ''}
      <br>所有数据只保存在本机，不上传云端。
    </div>`;

  const perf = { value: config.performanceMode };
  const modeSel = { value: mode };
  const applyModeUI = () => {
    const dw = modeSel.value === 'doubaowork';
    $('#dwFields', body)?.classList.toggle('hidden', !dw);
    $('#extFields', body)?.classList.toggle('hidden', dw);
    $$('.ext-only', body).forEach((el) => el.classList.toggle('hidden', dw));
    $$('.dw-only', body).forEach((el) => el.classList.toggle('hidden', !dw));
  };
  $$('#setMode button', body).forEach((button) => button.addEventListener('click', () => {
    modeSel.value = button.dataset.v;
    $$('#setMode button', body).forEach((entry) => entry.classList.toggle('on', entry === button));
    applyModeUI();
  }));
  applyModeUI();
  api('/api/ledger?limit=50')
    .then((result) => renderLedger($('#ledgerBox', body), result))
    .catch((error) => {
      const box = $('#ledgerBox', body);
      if (box) box.textContent = error.message;
    });
  $$('#setPerf button', body).forEach((button) => button.addEventListener('click', () => {
    perf.value = button.dataset.v;
    $$('#setPerf button', body).forEach((entry) => entry.classList.toggle('on', entry === button));
  }));
  const theme = { value: config.theme };
  $$('#setTheme button', body).forEach((button) => button.addEventListener('click', () => {
    theme.value = button.dataset.v;
    $$('#setTheme button', body).forEach((entry) => entry.classList.toggle('on', entry === button));
    applyTheme(theme.value);
  }));

  $('#setCheck', body).addEventListener('click', async () => {
    const status = $('#setStatus');
    status.className = 'set-status';
    status.textContent = '检测中…';
    try {
      const result = await api('/api/gateway/status');
      status.className = 'set-status ' + (result.ok ? 'ok' : 'bad');
      const login = result.login || {};
      const loginText = login.known ? (login.loggedIn ? '豆包工作版已登录' : '豆包未登录（要点上面的登录页扫码）') : '登录状态未知';
      status.textContent = result.ok
        ? `已连接 ${result.baseUrl} · ${result.latency}ms · ${loginText} · 模型：${(result.models || []).join(', ') || '—'}`
        : `连不上：${result.error}`;
      refreshGateway();
    } catch (error) {
      status.className = 'set-status bad';
      status.textContent = error.message;
    }
  });

  $('#setLaunch', body).addEventListener('click', async () => {
    const status = $('#setStatus');
    status.className = 'set-status';
    status.textContent = '正在启动…';
    try {
      const result = await api('/api/gateway/launch', { method: 'POST', body: {} });
      status.className = 'set-status ' + (result.ok ? 'ok' : 'bad');
      status.textContent = result.message + (result.script ? `（${result.script}）` : '');
      setTimeout(refreshGateway, 2500);
    } catch (error) {
      status.className = 'set-status bad';
      status.textContent = error.message;
    }
  });

  $('#setOpenDir', body).addEventListener('click', async () => {
    try { await api('/api/open-download-dir', { method: 'POST', body: {} }); } catch (error) { toast(error.message, 'bad'); }
  });

  $('#setAdmin', body).addEventListener('click', openQrLogin);
  $('#setOpenAdmin', body).addEventListener('click', openGatewayAdmin);
  $('#setOnboard', body).addEventListener('click', () => { closeSettings(); openOnboarding(true); });
  $('#setExport', body).addEventListener('click', exportCanvas);
  $('#setImport', body).addEventListener('click', importCanvas);

  const browseScript = $('#browseScript', body);
  if (browseScript) browseScript.addEventListener('click', async () => {
    const file = await window.heronbo.chooseFile({
      title: '选择 doubao2api 启动脚本',
      filters: [{ name: '批处理', extensions: ['bat', 'cmd'] }, { name: '所有文件', extensions: ['*'] }],
    });
    if (file) $('#setStartScript', body).value = file;
  });
  const chooseGateway = $('#chooseGatewayDir', body);
  if (chooseGateway) chooseGateway.addEventListener('click', async () => {
    const dir = await window.heronbo.chooseDirectory({ title: '选择 doubao2api 目录' });
    if (!dir) return;
    const script = dir.replace(/[\\/]+$/, '') + '\\start.bat';
    $('#setStartScript', body).value = script;
    toast('已填入启动脚本，记得点「保存设置」', 'ok');
  });
  const browseDownload = $('#browseDownload', body);
  if (browseDownload) browseDownload.addEventListener('click', async () => {
    const dir = await window.heronbo.chooseDirectory({ title: '选择下载目录' });
    if (dir) $('#setDownloadDir', body).value = dir;
  });
  const openData = $('#setOpenData', body);
  if (openData) openData.addEventListener('click', async () => {
    try { await window.heronbo.openPath(S.desktop.dataDir); } catch (error) { toast(error.message, 'bad'); }
  });

  const launchDw = () => { closeSettings(); launchDoubaoWork(); };
  $('#setDwLaunch', body)?.addEventListener('click', launchDw);
  $('#setDwLaunch2', body)?.addEventListener('click', launchDw);
  $('#setDwCheck', body)?.addEventListener('click', async () => {
    const status = $('#setStatus', body);
    status.className = 'set-status';
    status.textContent = '检测中…';
    await refreshGateway();
    const login = (S.gateway && S.gateway.login) || {};
    status.className = 'set-status ' + (S.gateway && S.gateway.ok ? (login.loggedIn ? 'ok' : '') : 'bad');
    status.textContent = S.gateway && S.gateway.mode === 'doubaowork'
      ? (S.gateway.ok
        ? (login.loggedIn ? '豆包工作客户端已登录' : '客户端在线，但还没登录')
        : '没连上豆包工作客户端：' + (S.gateway.error || '点「启动 / 打开豆包工作」'))
      : (S.gateway && S.gateway.ok ? '网关已连接' : '连不上网关');
  });
  $('#browseDoubao', body)?.addEventListener('click', async () => {
    const file = await window.heronbo.chooseFile({
      title: '选择 DoubaoWork.exe',
      filters: [{ name: '豆包工作客户端', extensions: ['exe'] }, { name: '所有文件', extensions: ['*'] }],
    });
    if (file) $('#setDoubaoExe', body).value = file;
  });

  $('#setClean', body).addEventListener('click', async () => {
    if (!confirm('删除所有没有被任何画布引用的素材文件？只影响 data\\media，不影响画布里的结果。')) return;
    try {
      const result = await api('/api/media/clean', { method: 'POST', body: {} });
      toast(`已清理 ${result.removed} 个素材`, 'ok');
      refreshStats();
    } catch (error) { toast(error.message, 'bad'); }
  });

  $('#setSave', body).addEventListener('click', async () => {
    const status = $('#setStatus');
    const patch = {
      gateway: { baseUrl: $('#setGateway', body).value.trim() },
      gatewayStartScript: $('#setStartScript', body).value.trim(),
      doubaoMode: modeSel.value,
      doubaoworkExe: ($('#setDoubaoExe', body)?.value || '').trim(),
      downloadDir: $('#setDownloadDir', body).value.trim(),
      performanceMode: perf.value,
      theme: theme.value,
      agentModel: ($('#setAgentModel', body)?.value || '').trim(),
    };
    const key = $('#setApiKey', body).value.trim();
    if (key) patch.gateway.apiKey = key;
    try {
      const result = await api('/api/config', { method: 'PATCH', body: patch });
      S.config = { ...S.config, ...result.config, performanceMode: perf.value, theme: theme.value, gatewayStartScript: patch.gatewayStartScript, downloadDir: patch.downloadDir, doubaoMode: modeSel.value, doubaoworkExe: patch.doubaoworkExe, agentModel: patch.agentModel };
      applyTheme(theme.value);
      applyPerf(perf.value);
      status.className = 'set-status ok';
      status.textContent = '已保存';
      refreshGateway();
      toast('设置已保存', 'ok');
    } catch (error) {
      status.className = 'set-status bad';
      status.textContent = error.message;
    }
  });

  $('#settingsMask').classList.remove('hidden');
  $('#settingsModal').classList.remove('hidden');
}

function closeSettings() {
  $('#settingsMask').classList.add('hidden');
  $('#settingsModal').classList.add('hidden');
}

function openHelp() {
  $('#helpMask').classList.remove('hidden');
  $('#helpModal').classList.remove('hidden');
}

function closeHelp() {
  $('#helpMask').classList.add('hidden');
  $('#helpModal').classList.add('hidden');
}

function applyTheme(theme) {
  const value = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem('doubao-canvas:theme', value); } catch { /* ignore */ }
  const btn = $('#themeToggle');
  if (btn) {
    btn.title = value === 'dark' ? '切换到浅色主题' : '切换到深色主题';
    btn.setAttribute('aria-label', btn.title);
  }
  if (typeof scheduleMinimap === 'function') scheduleMinimap();
}

function updateBrandLine() {
  const parts = ['HeronBo Workbench'];
  if (S.version) parts.push('v' + S.version);
  parts.push(S.desktop ? (S.desktop.isPortable ? '便携版' : '桌面版') : 'Local');
  const el = $('#brandVersion');
  if (el) el.textContent = parts.join(' · ');
}

function maybeShowOnboarding() {
  if (S.onboardChecked) return;
  S.onboardChecked = true;
  try {
    if (localStorage.getItem('heronbo:onboarded') === '1') return;
  } catch { /* ignore */ }
  openOnboarding();
}

function openOnboarding(manual = false) {
  S.onboardChecked = true;
  $('#onboardMask').classList.remove('hidden');
  $('#onboardModal').classList.remove('hidden');
  updateOnboardingStatus();
}

function updateOnboardingStatus() {
  if ($('#onboardModal').classList.contains('hidden')) return;
  const gateway = S.gateway || {};
  const chooser = $('[data-ob-choose]');
  if (chooser) chooser.classList.toggle('hidden', gateway.mode === 'doubaowork');
  const gwEl = $('[data-status="gateway"]');
  const loginEl = $('[data-status="login"]');
  if (gateway.mode === 'doubaowork') {
    if (gwEl) {
      gwEl.className = 'ob-status ' + (gateway.ok ? 'ok' : 'bad');
      gwEl.textContent = gateway.ok
        ? '豆包工作客户端已连接'
        : '未连接：点右边「启动豆包工作」';
    }
    if (loginEl) {
      const login = gateway.login || {};
      loginEl.className = 'ob-status ' + (gateway.ok ? (login.loggedIn ? 'ok' : 'wait') : 'bad');
      loginEl.textContent = !gateway.ok
        ? '先启动客户端'
        : login.loggedIn
          ? '已登录豆包工作'
          : '客户端在线，还没登录';
    }
    const canvasEl = $('[data-status="canvas"]');
    if (canvasEl && S.doc) canvasEl.textContent = `当前画布 ${S.doc.nodes.length} 个节点 · 共 ${S.canvases.length} 张画布`;
    return;
  }
  if (gwEl) {
    if (gateway.ok === undefined) {
      gwEl.className = 'ob-status wait';
      gwEl.textContent = '检测中…';
    } else if (gateway.ok) {
      gwEl.className = 'ob-status ok';
      gwEl.textContent = '已连接 · ' + String(gateway.baseUrl || '').replace(/^https?:\/\//, '');
    } else {
      gwEl.className = 'ob-status ' + (gateway.authError ? 'wait' : 'bad');
      gwEl.textContent = gateway.authError
        ? '网关在线，但缺少 API Key：点下面的「选目录」自动读取 .api_key'
        : '未连接：' + (gateway.error || '先点右边启动');
    }
  }
  const loginElExtern = loginEl;
  if (loginElExtern) {
    const login = gateway.login || {};
    if (!gateway.ok) {
      loginElExtern.className = 'ob-status bad';
      loginElExtern.textContent = '先启动网关';
    } else if (login.known) {
      loginElExtern.className = 'ob-status ' + (login.loggedIn ? 'ok' : 'wait');
      loginElExtern.textContent = login.loggedIn ? '已登录豆包工作版' : '待扫码登录';
    } else {
      loginElExtern.className = 'ob-status wait';
      loginElExtern.textContent = '登录状态未知';
    }
  }
  const canvasEl = $('[data-status="canvas"]');
  if (canvasEl && S.doc) {
    canvasEl.textContent = `当前画布 ${S.doc.nodes.length} 个节点 · 共 ${S.canvases.length} 张画布`;
  }
}

function closeOnboarding() {
  try {
    if ($('#obDontShow').checked) localStorage.setItem('heronbo:onboarded', '1');
    else localStorage.removeItem('heronbo:onboarded');
  } catch { /* ignore */ }
  $('#onboardMask').classList.add('hidden');
  $('#onboardModal').classList.add('hidden');
}

function applyPerf(mode) {
  let perf = mode === 'speed';
  if (mode === 'auto') {
    const cores = navigator.hardwareConcurrency || 8;
    const memory = navigator.deviceMemory || 8;
    perf = cores <= 4 || memory <= 4;
  }
  S.perf = perf;
  $('#app').classList.toggle('perf', perf);
  $('#perfBadge').classList.toggle('hidden', !perf);
}

/* ── 小地图 / 全览 ──────────────────────────────────────── */

function toggleEdges() {
  const svg = $('#edges');
  svg.classList.toggle('hidden');
  const hidden = svg.classList.contains('hidden');
  $('#edgesBtn')?.classList.toggle('on', !hidden);
  return hidden;
}

function toggleMinimap() {
  const el = $('#minimap');
  if (!el) return false;
  el.classList.toggle('hidden');
  const on = !el.classList.contains('hidden');
  $('#minimapBtn')?.classList.toggle('on', on);
  if (on) scheduleMinimap();
  return on;
}

function scheduleMinimap() {
  if (S.minimapRaf) return;
  S.minimapRaf = requestAnimationFrame(() => {
    S.minimapRaf = 0;
    drawMinimap();
  });
}

function drawMinimap() {
  const canvas = $('#minimapCanvas');
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  const cssVar = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const brand = cssVar('--brand', '#009efa');
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = cssVar('--bg-soft', '#191e26');
  ctx.fillRect(0, 0, width, height);
  if (!S.doc || !S.doc.nodes.length) {
    ctx.fillStyle = cssVar('--muted-2', 'rgba(140,150,165,.45)');
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('空画布', width / 2, height / 2);
    return;
  }
  const bounds = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
  for (const node of S.doc.nodes) {
    bounds.left = Math.min(bounds.left, node.x);
    bounds.top = Math.min(bounds.top, node.y);
    bounds.right = Math.max(bounds.right, node.x + node.w);
    bounds.bottom = Math.max(bounds.bottom, node.y + 260);
  }
  const pad = 40;
  bounds.left -= pad; bounds.top -= pad; bounds.right += pad; bounds.bottom += pad;
  const scale = Math.min(width / (bounds.right - bounds.left), height / (bounds.bottom - bounds.top));
  const ox = (width - (bounds.right - bounds.left) * scale) / 2 - bounds.left * scale;
  const oy = (height - (bounds.bottom - bounds.top) * scale) / 2 - bounds.top * scale;
  for (const node of S.doc.nodes) {
    ctx.fillStyle = S.sel.has(node.id) ? brand : cssVar('--muted-2', 'rgba(150,163,184,.55)');
    ctx.fillRect(ox + node.x * scale, oy + node.y * scale, Math.max(2, node.w * scale), Math.max(2, 160 * scale));
  }
  const rect = viewportRect();
  const view = {
    x: ox + (-S.view.x / S.view.scale) * scale,
    y: oy + (-S.view.y / S.view.scale) * scale,
    w: (rect.width / S.view.scale) * scale,
    h: (rect.height / S.view.scale) * scale,
  };
  ctx.strokeStyle = brand;
  ctx.globalAlpha = .85;
  ctx.lineWidth = 1;
  ctx.strokeRect(view.x, view.y, view.w, view.h);
  ctx.globalAlpha = 1;
  canvas._map = { scale, ox, oy };
}

function fitNodes(nodes) {
  if (!nodes || !nodes.length) return;
  const rect = viewportRect();
  const bounds = { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity };
  for (const node of nodes) {
    bounds.left = Math.min(bounds.left, node.x);
    bounds.top = Math.min(bounds.top, node.y);
    bounds.right = Math.max(bounds.right, node.x + node.w);
    bounds.bottom = Math.max(bounds.bottom, node.y + 280);
  }
  const pad = 80;
  const scale = clamp(Math.min((rect.width - pad * 2) / (bounds.right - bounds.left), (rect.height - pad * 2) / (bounds.bottom - bounds.top)), 0.15, 1.4);
  S.view.scale = scale;
  S.view.x = rect.width / 2 - ((bounds.left + bounds.right) / 2) * scale;
  S.view.y = rect.height / 2 - ((bounds.top + bounds.bottom) / 2) * scale;
  applyView(true);
}

function fitAll() {
  if (S.doc) fitNodes(S.doc.nodes);
}

/* ── 快捷键 ─────────────────────────────────────────────── */

function isTypingTarget(target) {
  return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
}

document.addEventListener('keydown', (event) => {
  const typing = isTypingTarget(event.target);
  const mod = event.ctrlKey || event.metaKey;

  if (mod && event.key.toLowerCase() === 'z') {
    event.preventDefault();
    if (event.shiftKey) redo(); else undo();
    return;
  }
  if (mod && event.key.toLowerCase() === 'y') { event.preventDefault(); redo(); return; }
  if (mod && event.key === 'Enter') {
    event.preventDefault();
    const node = S.sel.size === 1 ? S.nodeById.get([...S.sel][0]) : null;
    if (node) runNode(node);
    return;
  }
  if (typing) return;

  // 浏览器里 Ctrl+N / Ctrl+B / Ctrl+0 等会被浏览器自己吃掉（新窗口、书签栏、页面缩放），
  // 所以本地版改用「单键」：N 新建、B 侧栏、I 检查器、F 全览、A 全选、L 连线、0 归位。
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === 'v') { event.preventDefault(); setTool('select'); return; }
  if (key === 'h') { event.preventDefault(); setTool('hand'); return; }
  if (key === 'n') {
    event.preventDefault();
    if (event.shiftKey) { newCanvas(); return; }
    const rect = viewportRect();
    S.addPoint = screenToWorld(rect.width / 2 - 150, rect.height / 2 - 230);
    showAddMenu(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return;
  }
  if (key === 'b') {
    event.preventDefault();
    $('#app').classList.toggle('sidebar-hidden');
    scheduleCull();
    return;
  }
  if (key === 'i') {
    event.preventDefault();
    if (!S.sel.size && !S.selEdge) return;
    const app = $('#app');
    app.classList.toggle('inspector-hidden');
    S.composerHidden = app.classList.contains('inspector-hidden');
    scheduleCull();
    return;
  }
  if (key === 'f') { event.preventDefault(); fitAll(); return; }
  if (key === 'a') {
    event.preventDefault();
    S.sel = new Set(S.doc.nodes.map((node) => node.id));
    syncSelection();
    renderInspector();
    return;
  }
  if (key === 'l') {
    event.preventDefault();
    toast(toggleEdges() ? '已隐藏连线' : '已显示连线');
    return;
  }
  if (key === 'm') {
    event.preventDefault();
    toast(toggleMinimap() ? '已打开小地图' : '已收起小地图');
    return;
  }
  if (key === '0') {
    event.preventDefault();
    const rect = viewportRect();
    S.view.scale = 1;
    S.view.x = rect.width / 2;
    S.view.y = rect.height / 2;
    applyView(true);
    return;
  }
  if (key === '=' || key === '+') {
    event.preventDefault();
    const rect = viewportRect();
    zoomAt(rect.width / 2, rect.height / 2, 1.12);
    return;
  }
  if (key === '-') {
    event.preventDefault();
    const rect = viewportRect();
    zoomAt(rect.width / 2, rect.height / 2, 0.89);
    return;
  }

  if (event.key === 'Delete' || event.key === 'Backspace') {
    if (S.pick) return;   // 点选期间别让键盘顺手删掉节点
    event.preventDefault();
    deleteSelected();
    return;
  }
  if (event.key === 'Escape') {
    hideAddMenu();
    hideContextMenu();
    hideMention();
    if (S.pick) { cancelCanvasPick(); return; }
    if (S.lightbox) { closeLightbox(); return; }
    if (!$('#settingsModal').classList.contains('hidden')) { closeSettings(); return; }
    if (!$('#helpModal').classList.contains('hidden')) { closeHelp(); return; }
    clearSelection();
    return;
  }
  if (event.key === '?') { event.preventDefault(); openHelp(); return; }
  if (event.key.startsWith('Arrow') && S.sel.size) {
    event.preventDefault();
    const step = event.shiftKey ? 10 : 1;
    const delta = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[event.key];
    if (!delta) return;
    if (!document.__nudgeBurst) {
      document.__nudgeBurst = true;
      pushHistory();
      setTimeout(() => { document.__nudgeBurst = false; }, 500);
    }
    for (const id of S.sel) {
      const node = S.nodeById.get(id);
      if (!node) continue;
      node.x += delta[0];
      node.y += delta[1];
      applyNodeGeometry(S.els.get(id), node);
    }
    renderEdges('move', [...S.sel]);
    markDirty();
    scheduleMinimap();
  }
});

/* ── 按钮绑定 ───────────────────────────────────────────── */

function bindButtons() {
  $('#newCanvasBtn').addEventListener('click', newCanvas);
  $('#settingsBtn').addEventListener('click', openSettings);
  $('#helpBtn').addEventListener('click', openHelp);
  $('#helpBtnSide').addEventListener('click', openHelp);
  $('#agentBtn').addEventListener('click', () => {
    if ($('#agentPanel').classList.contains('hidden')) openAgentPanel();
    else closeAgentPanel();
  });
  $('#agentClose').addEventListener('click', closeAgentPanel);
  $$('#agentMode button').forEach((button) => button.addEventListener('click', () => setAgentMode(button.dataset.mode)));
  $('#agentSend').addEventListener('click', sendAgent);
  $('#agentInput').addEventListener('input', (event) => {
    const textarea = event.currentTarget;
    S.agentMentions = collectAgentMentions(textarea.value);
    updateAgentCtxHint();
    handleAgentMentionInput(textarea);
  });
  $('#agentInput').addEventListener('blur', () => hideAgentMention());
  $('#agentInput').addEventListener('scroll', () => hideAgentMention());
  $('#agentInput').addEventListener('keydown', (event) => {
    if (S.agentMention) {
      const items = $$('#agentMentionMenu .mention-item');
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        S.agentMention.index = (S.agentMention.index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items.forEach((item, index) => item.classList.toggle('on', index === S.agentMention.index));
        return;
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault();
        const target = S.agentMention.candidates[S.agentMention.index];
        if (target) applyAgentMention(target);
        return;
      }
      if (event.key === 'Escape') { event.preventDefault(); hideAgentMention(); return; }
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      sendAgent();
    }
    if (event.key === 'Escape') event.currentTarget.blur();
  });
  $('#gatewayPill').addEventListener('click', openSettings);
  $('#launchGatewayBtn').addEventListener('click', async () => {
    const result = await api('/api/gateway/launch', { method: 'POST', body: {} }).catch((error) => ({ ok: false, message: error.message }));
    toast(result.message, result.ok ? 'ok' : 'bad');
    setTimeout(refreshGateway, 2500);
  });
  $('#toggleSidebarBtn').addEventListener('click', () => {
    $('#app').classList.toggle('sidebar-hidden');
    setTimeout(scheduleCull, 200);
  });
  $('#sideClose')?.addEventListener('click', () => {
    $('#app').classList.add('sidebar-hidden');
    setTimeout(scheduleCull, 200);
  });
  $('#themeToggle')?.addEventListener('click', async () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    S.config = { ...(S.config || {}), theme: next };
    await api('/api/config', { method: 'PATCH', body: { theme: next } }).catch(() => null);
  });
  $('#minimapBtn')?.addEventListener('click', toggleMinimap);
  $('#toolBtn')?.addEventListener('click', () => setTool(S.tool === 'hand' ? 'select' : 'hand'));
  setTool(S.tool);
  $('#zoomLabel')?.addEventListener('click', () => {
    const rect = viewportRect();
    S.view.scale = 1;
    S.view.x = rect.width / 2;
    S.view.y = rect.height / 2;
    applyView(true);
  });
  $('#undoBtn').addEventListener('click', undo);
  $('#redoBtn').addEventListener('click', redo);
  $('#edgesBtn').addEventListener('click', () => toggleEdges());
  $('#fitBtn').addEventListener('click', fitAll);
  $('#zoomIn').addEventListener('click', () => {
    const rect = viewportRect();
    zoomAt(rect.width / 2, rect.height / 2, 1.15);
  });
  $('#zoomOut').addEventListener('click', () => {
    const rect = viewportRect();
    zoomAt(rect.width / 2, rect.height / 2, 0.87);
  });
  $('#zoomReset').addEventListener('click', () => {
    const rect = viewportRect();
    S.view.scale = 1;
    S.view.x = rect.width / 2;
    S.view.y = rect.height / 2;
    applyView(true);
  });

  $('#minimap').addEventListener('pointerdown', (event) => {
    const canvas = $('#minimapCanvas');
    const map = canvas._map;
    if (!map) return;
    const rect = canvas.getBoundingClientRect();
    const mx = ((event.clientX - rect.left) / rect.width) * canvas.width;
    const my = ((event.clientY - rect.top) / rect.height) * canvas.height;
    const worldX = (mx - map.ox) / map.scale;
    const worldY = (my - map.oy) / map.scale;
    const view = viewportRect();
    S.view.x = view.width / 2 - worldX * S.view.scale;
    S.view.y = view.height / 2 - worldY * S.view.scale;
    applyView(true);
  });

  $('#settingsMask').addEventListener('click', closeSettings);
  $('#helpMask').addEventListener('click', closeHelp);
  $$('[data-close-settings]').forEach((button) => button.addEventListener('click', closeSettings));
  $$('[data-close-help]').forEach((button) => button.addEventListener('click', closeHelp));
  $('#qrMask').addEventListener('click', closeQrLogin);
  $$('[data-close-qr]').forEach((button) => button.addEventListener('click', closeQrLogin));
  $('#qrRetry').addEventListener('click', loadQr);
  $('#qrAdmin').addEventListener('click', openGatewayAdmin);

  $('#onboardMask').addEventListener('click', closeOnboarding);
  $$('[data-close-onboard]').forEach((button) => button.addEventListener('click', closeOnboarding));
  $$('[data-example]').forEach((button) => button.addEventListener('click', loadExampleCanvas));
  $$('[data-audio]').forEach((button) => button.addEventListener('click', () => pickUpload(null, 'audio/*')));
  $$('[data-kit]').forEach((button) => button.addEventListener('click', openDirectorKit));
  $$('[data-subjects]').forEach((button) => button.addEventListener('click', () => createSubjectNode()));
  $$('[data-close-subjects]').forEach((button) => button.addEventListener('click', closeSubjectsModal));
  $('#subjectMask')?.addEventListener('click', closeSubjectsModal);
  $('#subjectFiles')?.addEventListener('change', (event) => { handleSubjectFiles(event.currentTarget.files); event.currentTarget.value = ''; });
  $('#subjectAudio')?.addEventListener('change', (event) => { handleSubjectAudio(event.currentTarget.files); event.currentTarget.value = ''; });
  $('#subjectSave')?.addEventListener('click', saveSubjectDraft);
  $('#pickMask')?.addEventListener('click', closeCanvasPicker);
  $$('[data-close-pick]').forEach((button) => button.addEventListener('click', closeCanvasPicker));
  $('#pickBarCancel')?.addEventListener('click', cancelCanvasPick);
  $('#pickBarList')?.addEventListener('click', () => {
    const pick = S.pick;
    if (!pick) return;
    openPickerList(pick.kind, pick.title, pick.onPick);
  });
  $('#subjectPickImage')?.addEventListener('click', () => openCanvasPicker('image', '从画布选择形象', (asset) => {
    if (!pendingSubjectAssets.some((entry) => entry.id === asset.id)) pendingSubjectAssets.push(asset);
    renderPendingSubjectAssets();
  }));
  $('#subjectPickAudio')?.addEventListener('click', () => openCanvasPicker('audio', '从画布选择参考音频', (asset) => {
    pendingSubjectAudio = asset;
    renderPendingSubjectAssets();
  }));
  $$('[data-assets]').forEach((button) => button.addEventListener('click', openAssetLibrary));
  $$('[data-close-kit]').forEach((button) => button.addEventListener('click', closeDirectorKit));
  $$('[data-close-assets]').forEach((button) => button.addEventListener('click', closeAssetLibrary));
  $('#kitMask')?.addEventListener('click', closeDirectorKit);
  $('#assetMask')?.addEventListener('click', closeAssetLibrary);
  const obLaunch = $('[data-ob-launch]');
  if (obLaunch) obLaunch.addEventListener('click', async () => {
    const status = $('[data-status="gateway"]');
    status.className = 'ob-status wait';
    status.textContent = '正在启动 doubao2api…';
    const result = await api('/api/gateway/launch', { method: 'POST', body: {} })
      .catch((error) => ({ ok: false, message: error.message }));
    toast(result.message, result.ok ? 'ok' : 'bad');
    setTimeout(async () => { await refreshGateway(); updateOnboardingStatus(); }, 3000);
  });
  const obLogin = $('[data-ob-login]');
  if (obLogin) obLogin.addEventListener('click', openQrLogin);
  const obChoose = $('[data-ob-choose]');
  if (obChoose) obChoose.addEventListener('click', async () => {
    if (!window.heronbo) {
      toast('浏览器版请到「设置 → 网关启动脚本」里手动填写', 'bad');
      return;
    }
    const dir = await window.heronbo.chooseDirectory({ title: '选择 doubao2api 目录' }).catch(() => '');
    if (!dir) return;
    const script = dir.replace(/[\\/]+$/, '') + '\\start.bat';
    await api('/api/config', { method: 'PATCH', body: { gatewayStartScript: script } });
    S.config.gatewayStartScript = script;
    toast('已记住网关目录，正在启动…', 'ok');
    const result = await api('/api/gateway/launch', { method: 'POST', body: {} })
      .catch((error) => ({ ok: false, message: error.message }));
    toast(result.message, result.ok ? 'ok' : 'bad');
    setTimeout(async () => { await refreshGateway(); updateOnboardingStatus(); }, 3000);
  });
  const obCanvas = $('[data-ob-canvas]');
  if (obCanvas) obCanvas.addEventListener('click', () => {
    closeOnboarding();
    const rect = viewportRect();
    createNode('image', screenToWorld(rect.width / 2 - 150, rect.height / 2 - 230));
  });

  $('#lightboxClose').addEventListener('click', closeLightbox);
  $('#lightboxDownload').addEventListener('click', () => {
    if (!S.lightbox) return;
    const node = S.nodeById.get(S.lightbox.nodeId);
    const asset = node && node.assets[S.lightbox.index];
    if (asset) downloadAsset(asset.id);
  });
  $('#lightbox').addEventListener('click', (event) => { if (event.target.id === 'lightbox') closeLightbox(); });

  document.addEventListener('keydown', (event) => {
    if (!S.lightbox) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const node = S.nodeById.get(S.lightbox.nodeId);
      if (!node) return;
      const length = node.assets.length;
      S.lightbox.index = (S.lightbox.index + (event.key === 'ArrowRight' ? 1 : -1) + length) % length;
      renderLightbox();
      event.preventDefault();
    }
  });

}

/* ── 内置画布映射（本地画布前端调用 /api/bridge/*） ─────── */

const BRIDGE = {
  open: false,
  nodes: [],
  state: null,
  context: null,
  timer: 0,
  busy: false,
  stage: false,
  stageTimer: 0,
  stageBusy: false,
  stageRect: null,
  stageDrag: null,
  stageMoved: false,
  clickTimer: 0,
  wheelAt: 0,
};

function bridgeLog(message, bad = false) {
  const box = $('#bridgeLog');
  if (!box) return;
  const line = document.createElement('div');
  line.textContent = (bad ? '! ' : '· ') + message;
  if (bad) line.style.color = 'var(--danger)';
  box.appendChild(line);
  while (box.children.length > 8) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function bridgeMeta(text) {
  const el = $('#bridgeMeta');
  if (el) el.textContent = text;
}

function bridgeContext() {
  const context = BRIDGE.context || {};
  const state = BRIDGE.state || {};
  const href = String(context.href || state.href || '');
  const match = href.match(/chat\/(\d+)/);
  const title = String(context.title || state.title || '').split(' - ')[0].trim();
  return { conversationId: match ? match[1] : '', title };
}

function bridgeRender() {
  const box = $('#bridgeNodes');
  if (!box) return;
  const nodes = BRIDGE.nodes || [];
  if (!nodes.length) {
    box.innerHTML = '<div class="bridge-empty">画布不在场，或当前会话没有可读节点。点「召回 / 保证在场」再试。</div>';
    return;
  }
  box.innerHTML = nodes.map((node) => {
    const kind = node.kind || 'unknown';
    const gen = node.generation && node.generation.state ? node.generation.state : '';
    const frame = node.geometry && node.geometry.frameSize ? (node.geometry.frameSize.w + '×' + node.geometry.frameSize.h) : '—';
    const screen = node.geometry && node.geometry.screenRect ? (node.geometry.screenRect.w + '×' + node.geometry.screenRect.h) : '—';
    const id = escapeHtml(node.id || '');
    return '<div class="bridge-node' + (node.selected ? ' selected' : '') + '">'
      + '<div class="bn-head"><span class="bn-kind">' + escapeHtml(kind) + '</span>'
      + '<span class="bn-state">' + escapeHtml(gen || 'idle') + '</span></div>'
      + '<div class="bn-id">' + id + '</div>'
      + '<div class="bn-meta">frame ' + escapeHtml(frame) + ' · screen ' + escapeHtml(screen) + '</div>'
      + '<div class="bn-actions">'
      + '<button data-bridge-act="select" data-id="' + id + '">选中</button>'
      + '<button data-bridge-act="left" data-id="' + id + '">左移</button>'
      + '<button data-bridge-act="right" data-id="' + id + '">右移</button>'
      + '<button data-bridge-act="delete" data-id="' + id + '" class="danger">删除</button>'
      + '</div></div>';
  }).join('');
}

async function bridgeRead() {
  if (BRIDGE.busy) return;
  BRIDGE.busy = true;
  bridgeMeta('读取中…');
  try {
    const contextRes = await api('/api/bridge/context');
    BRIDGE.context = contextRes;
    const stateRes = await api('/api/bridge/state');
    const state = stateRes.state || stateRes;
    BRIDGE.state = state;
    const flow = state.flowRect;
    const ctx = bridgeContext();
    bridgeMeta((state.ok ? '已连接' : '不可读') + ' · ' + (ctx.title || '当前会话') + (flow ? ' · flow ' + flow.w + '×' + flow.h : ''));
    const modelRes = await api('/api/bridge/model');
    BRIDGE.nodes = (modelRes.model && modelRes.model.nodes) || [];
    bridgeRender();
    bridgeLog('读取完成 · ' + BRIDGE.nodes.length + ' 个节点');
  } catch (error) {
    const ctx = bridgeContext();
    bridgeMeta('未连接 · ' + (ctx.title || '当前会话') + ' · ' + error.message);
    bridgeLog(error.message, true);
  } finally {
    BRIDGE.busy = false;
  }
}

async function bridgeEnsure() {
  if (!BRIDGE.state) await bridgeRead();
  const ctx = bridgeContext();
  const canvasId = ($('#bridgeCanvasId')?.value || '').trim();
  if (!ctx.conversationId) { bridgeLog('当前会话没有 conversationId', true); return; }
  if (!canvasId) { bridgeLog('缺少 canvasId', true); return; }
  try {
    bridgeMeta('正在召回…');
    const result = await api('/api/bridge/ensure', {
      method: 'POST',
      body: { canvasId, conversationId: ctx.conversationId, title: ctx.title },
    });
    bridgeLog('召回 ' + (result.opened ? '成功' : '完成') + (result.open && result.open.via ? ' · via ' + result.open.via : ''));
    await bridgeRead();
  } catch (error) {
    bridgeLog('召回失败 · ' + error.message, true);
  }
}

function bridgeStageMeta(text, bad = false) {
  const el = $('#bridgeStageMeta');
  if (!el) return;
  el.textContent = text;
  el.style.color = bad ? 'var(--danger)' : '';
}

function bridgeStageEmpty(text) {
  const img = $('#bridgeStageImg');
  const empty = $('#bridgeStageEmpty');
  if (empty) {
    empty.innerHTML = text;
    empty.classList.toggle('hidden', !text);
  }
  if (img) img.classList.toggle('hidden', Boolean(text));
}

async function bridgeStageRefresh() {
  if (!BRIDGE.stage || BRIDGE.stageBusy || document.hidden) return;
  BRIDGE.stageBusy = true;
  try {
    const shot = await api('/api/bridge/screenshot?quality=72');
    if (!BRIDGE.stage) return;
    const img = $('#bridgeStageImg');
    if (img && shot.dataUrl) {
      img.src = shot.dataUrl;
      img.classList.remove('hidden');
    }
    BRIDGE.stageRect = shot.panelRect || null;
    bridgeStageEmpty('');
    const flow = shot.flowRect;
    bridgeStageMeta((shot.nodeCount || 0) + ' 节点 · flow ' + (flow ? flow.w + '×' + flow.h : '—') + ' · 点击/滚轮已映射');
  } catch (error) {
    bridgeStageEmpty('内置画布暂不可读。<br><code>' + escapeHtml(error.message) + '</code><br>先点右上角「召回」，让右侧画布展开。');
    bridgeStageMeta('读取失败 · ' + error.message, true);
  } finally {
    BRIDGE.stageBusy = false;
  }
}

async function bridgeStageEnsure() {
  bridgeStageMeta('正在召回内置画布…');
  bridgeStageEmpty('正在召回内置画布…');
  await bridgeEnsure();
  await bridgeStageRefresh();
}

function bridgeStagePoint(event) {
  const img = $('#bridgeStageImg');
  if (!img || !img.getBoundingClientRect().width) return null;
  const rect = img.getBoundingClientRect();
  const nx = (event.clientX - rect.left) / rect.width;
  const ny = (event.clientY - rect.top) / rect.height;
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return null;
  return { nx, ny, rect };
}

async function bridgeStageAct(path, body, label) {
  try {
    await api(path, { method: 'POST', body });
    bridgeStageMeta('已发送 · ' + label);
    setTimeout(bridgeStageRefresh, 620);
  } catch (error) {
    bridgeStageMeta(label + ' 失败 · ' + error.message, true);
  }
}

function bridgeStageMarquee(show, from, to) {
  const box = $('#bridgeStageMarquee');
  if (!box) return;
  if (!show || !from || !to) { box.classList.add('hidden'); return; }
  const img = $('#bridgeStageImg');
  const rect = img.getBoundingClientRect();
  const stageRect = $('#bridgeStageBody').getBoundingClientRect();
  const left = rect.left - stageRect.left + Math.min(from.x, to.x);
  const top = rect.top - stageRect.top + Math.min(from.y, to.y);
  box.style.left = left + 'px';
  box.style.top = top + 'px';
  box.style.width = Math.abs(to.x - from.x) + 'px';
  box.style.height = Math.abs(to.y - from.y) + 'px';
  box.classList.remove('hidden');
}

function bindBridgeStage() {
  $('#bridgeStageBtn')?.addEventListener('click', openBridgeStage);
  $('#bridgeStageClose')?.addEventListener('click', closeBridgeStage);
  $('#bridgeStageRefresh')?.addEventListener('click', bridgeStageRefresh);
  $('#bridgeStageEnsure')?.addEventListener('click', bridgeStageEnsure);
  const img = $('#bridgeStageImg');
  if (!img) return;

  img.addEventListener('click', (event) => {
    if (BRIDGE.stageMoved) return;
    const point = bridgeStagePoint(event);
    if (!point) return;
    clearTimeout(BRIDGE.clickTimer);
    BRIDGE.clickTimer = setTimeout(() => {
      bridgeStageAct('/api/bridge/click', { nx: point.nx, ny: point.ny, button: 'left', count: 1 }, '单击');
    }, 240);
  });

  img.addEventListener('dblclick', (event) => {
    clearTimeout(BRIDGE.clickTimer);
    const point = bridgeStagePoint(event);
    if (!point) return;
    bridgeStageAct('/api/bridge/click', { nx: point.nx, ny: point.ny, button: 'left', count: 2 }, '双击');
  });

  img.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    const point = bridgeStagePoint(event);
    if (!point) return;
    bridgeStageAct('/api/bridge/click', { nx: point.nx, ny: point.ny, button: 'right', count: 1 }, '右键');
  });

  img.addEventListener('wheel', (event) => {
    event.preventDefault();
    const now = Date.now();
    if (now - BRIDGE.wheelAt < 220) return;
    BRIDGE.wheelAt = now;
    const point = bridgeStagePoint(event);
    if (!point) return;
    bridgeStageAct('/api/bridge/wheel', { nx: point.nx, ny: point.ny, deltaY: event.deltaY }, '滚轮');
  }, { passive: false });

  img.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    const point = bridgeStagePoint(event);
    if (!point) return;
    BRIDGE.stageMoved = false;
    BRIDGE.stageDrag = { start: { nx: point.nx, ny: point.ny, x: event.clientX, y: event.clientY }, last: { nx: point.nx, ny: point.ny, x: event.clientX, y: event.clientY } };
    try { img.setPointerCapture(event.pointerId); } catch (error) {}
    event.preventDefault();
  });

  img.addEventListener('pointermove', (event) => {
    if (!BRIDGE.stageDrag) return;
    const point = bridgeStagePoint(event);
    if (!point) return;
    BRIDGE.stageDrag.last = { nx: point.nx, ny: point.ny, x: event.clientX, y: event.clientY };
    const drag = BRIDGE.stageDrag;
    if (!BRIDGE.stageMoved && Math.abs(event.clientX - drag.start.x) + Math.abs(event.clientY - drag.start.y) > 8) {
      BRIDGE.stageMoved = true;
    }
    if (BRIDGE.stageMoved) {
      const imgRect = img.getBoundingClientRect();
      bridgeStageMarquee(true, { x: drag.start.x - imgRect.left, y: drag.start.y - imgRect.top }, { x: event.clientX - imgRect.left, y: event.clientY - imgRect.top });
    }
  });

  const finishDrag = () => {
    if (!BRIDGE.stageDrag) return;
    const drag = BRIDGE.stageDrag;
    BRIDGE.stageDrag = null;
    if (BRIDGE.stageMoved) {
      bridgeStageMarquee(false);
      bridgeStageAct('/api/bridge/drag', {
        from: { nx: drag.start.nx, ny: drag.start.ny },
        to: { nx: drag.last.nx, ny: drag.last.ny },
      }, '拖动');
      setTimeout(() => { BRIDGE.stageMoved = false; }, 300);
    } else {
      BRIDGE.stageMoved = false;
    }
  };
  img.addEventListener('pointerup', finishDrag);
  img.addEventListener('pointercancel', () => { BRIDGE.stageDrag = null; BRIDGE.stageMoved = false; bridgeStageMarquee(false); });

  window.addEventListener('keydown', (event) => {
    if (!BRIDGE.stage) return;
    if (event.target && event.target.closest && event.target.closest('#bridgeStage button, #bridgeStage input, #bridgeStage textarea')) return;
    if (['F5', 'F11', 'F12'].includes(event.key)) return;
    if (event.ctrlKey && event.shiftKey && ['I', 'J'].includes(event.key)) return;
    if (event.key === 'Meta' || event.key === 'Control' || event.key === 'Alt' || event.key === 'Shift') return;
    const printable = event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;
    const body = {
      type: 'keyDown',
      key: event.key,
      code: event.code,
      ctrlKey: event.ctrlKey,
      shiftKey: event.shiftKey,
      altKey: event.altKey,
      metaKey: event.metaKey,
      text: printable ? event.key : '',
    };
    api('/api/bridge/key', { method: 'POST', body }).catch(() => {});
    if (printable || ['Backspace', 'Delete', 'Enter', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Escape'].includes(event.key)) {
      event.preventDefault();
    }
  }, true);
}

function openBridgeStage() {
  const stage = $('#bridgeStage');
  if (!stage) return;
  closeBridgePanel();
  stage.classList.remove('hidden');
  BRIDGE.stage = true;
  bridgeStageMeta('正在召回内置画布…');
  bridgeStageEmpty('正在召回内置画布…');
  bridgeStageEnsure();
  clearInterval(BRIDGE.stageTimer);
  BRIDGE.stageTimer = setInterval(bridgeStageRefresh, 1200);
}

function closeBridgeStage() {
  BRIDGE.stage = false;
  clearInterval(BRIDGE.stageTimer);
  BRIDGE.stageTimer = 0;
  BRIDGE.stageDrag = null;
  bridgeStageMarquee(false);
  $('#bridgeStage')?.classList.add('hidden');
}


async function bridgeAct(act, id) {
  if (!id) return;
  try {
    if (act === 'select') await api('/api/bridge/select', { method: 'POST', body: { nodeId: id } });
    if (act === 'left') await api('/api/bridge/move', { method: 'POST', body: { nodeId: id, dx: -80, dy: 0 } });
    if (act === 'right') await api('/api/bridge/move', { method: 'POST', body: { nodeId: id, dx: 80, dy: 0 } });
    if (act === 'delete') {
      if (!window.confirm('删除内置画布上的这个节点？')) return;
      await api('/api/bridge/delete', { method: 'POST', body: { nodeId: id } });
    }
    bridgeLog(act + ' 完成 · ' + id);
    await bridgeRead();
  } catch (error) {
    bridgeLog(act + ' 失败 · ' + error.message, true);
  }
}

function openBridgePanel() {
  const panel = $('#bridgePanel');
  if (!panel) return;
  panel.classList.remove('hidden');
  BRIDGE.open = true;
  bridgeLog('打开内置画布映射');
  bridgeRead();
  clearInterval(BRIDGE.timer);
  BRIDGE.timer = setInterval(() => {
    if (BRIDGE.open && !document.hidden) bridgeRead();
  }, 5000);
}

function closeBridgePanel() {
  BRIDGE.open = false;
  clearInterval(BRIDGE.timer);
  $('#bridgePanel')?.classList.add('hidden');
}

function bindBridge() {
  $('#bridgeBtn')?.addEventListener('click', () => {
    if (BRIDGE.open) closeBridgePanel();
    else openBridgePanel();
  });
  $('#bridgeClose')?.addEventListener('click', closeBridgePanel);
  $('#bridgeRefresh')?.addEventListener('click', bridgeRead);
  $('#bridgeEnsure')?.addEventListener('click', bridgeEnsure);
  $('#bridgeNodes')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-bridge-act]');
    if (!button) return;
    bridgeAct(button.dataset.bridgeAct, button.dataset.id);
  });
  $('#bridgeCanvasId')?.addEventListener('change', () => bridgeLog('canvasId 已改为 ' + $('#bridgeCanvasId').value));
}

/* ── 启动 ───────────────────────────────────────────────── */

bindButtons();
bindBridge();
bindBridgeStage();
boot();
if (BRIDGE_MODE === 'stage') setTimeout(openBridgeStage, 700);
else if (BRIDGE_MODE === '1') setTimeout(openBridgePanel, 600);

window.addEventListener('resize', () => { scheduleCull(); scheduleMinimap(); scheduleComposerFollow(); });
