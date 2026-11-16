'use strict';

/**
 * 豆包画布 · 节点模型统一层
 *
 * 目标不是把本地节点和内置节点改成同一个对象，而是：
 *   1. 两边各自保留原生结构（本地 doc.nodes / 内置 React Flow canvas-node）；
 *   2. 中间定义一层 CanonicalNode，只承载“可映射的语义”；
 *   3. 所有桥接动作先经过能力表与适配器，不允许直接把一边的字段拷到另一边。
 *
 * 关键差异（已实测）：
 *   - 本地节点：5 种 kind（image/video/upload/text/subject），卡片宽 220–560，x/y 是本地世界坐标。
 *   - 内置节点：统一 React Flow 类型 canvas-node，节点本身是 2048×2048 画板帧；
 *     实际资源类型藏在 data-canvas-open-resource-id 的前缀里（如 video:xxx）。
 *   - 因此几何不能 1:1 同步：卡片是 card，内置是 frame；只同步“相对位移/是否选中”，
 *     不同步绝对 x/y/w/h。这个决定写在 geometry.compatible 里，调用方必须检查。
 */

const SCHEMA_VERSION = 1;

const NODE_KINDS = ['image', 'video', 'audio', 'text', 'group', 'unknown'];
const NODE_ROLES = ['source', 'generator', 'result', 'reference', 'note', 'unknown'];
const GEOMETRY_MODES = ['card', 'frame'];
const GEN_STATES = ['idle', 'queued', 'running', 'done', 'error'];

const LOCAL_STATUS_TO_STATE = {
  idle: 'idle',
  queued: 'queued',
  running: 'running',
  done: 'done',
  error: 'error',
};

const BUILTIN_STATE_TO_STATE = {
  pending: 'queued',
  queued: 'queued',
  generating: 'running',
  running: 'running',
  ready: 'done',
  done: 'done',
  success: 'done',
  completed: 'done',
  failed: 'error',
  error: 'error',
  canceled: 'idle',
  cancelled: 'idle',
};

const BUILTIN_RESOURCE_KINDS = {
  image: 'image',
  video: 'video',
  audio: 'audio',
  text: 'text',
  file: 'unknown',
  subject: 'group',
  group: 'group',
};

const LOCAL_KIND_TO_BUILTIN = {
  image: 'image',
  video: 'video',
  audio: 'audio',
  text: 'text',
  group: 'group',
  unknown: 'unknown',
};

const CAPABILITY_NAMES = [
  'read',
  'select',
  'deselect',
  'rename',
  'move',
  'delete',
  'copy',
  'cut',
  'paste',
  'layer',
  'upload',
  'generate',
  'collect',
  'editImage',
  'marker',
];

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, Number(n) || 0));
}

function text(value, max = 8000) {
  return String(value == null ? '' : value).slice(0, max);
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function uniq(list) {
  return [...new Set(list.filter(Boolean))];
}

function nowStamp(now) {
  return typeof now === 'number' ? now : Date.now();
}

function makeKey(source, id) {
  return `${source}:${id}`;
}

function isFrameGeometry(node) {
  return Boolean(node && node.geometry && node.geometry.mode === 'frame');
}

function isCardGeometry(node) {
  return Boolean(node && node.geometry && node.geometry.mode === 'card');
}

function localKindOf(node) {
  const kind = String(node && node.kind || '');
  return ['image', 'video', 'upload', 'text', 'subject'].includes(kind) ? kind : 'unknown';
}

function localAssetKind(asset) {
  const kind = String(asset && asset.kind || '');
  return ['image', 'video', 'audio'].includes(kind) ? kind : 'image';
}

/**
 * 本地上传节点可以装多张图/多条视频。内置一个 canvas-node 只承载一个资源，
 * 所以上传节点不是 1:1，必须显式标出 cardinality，后续要么 fanout，要么只取第一张。
 */
function localCardinality(node) {
  const assets = Array.isArray(node && node.assets) ? node.assets : [];
  if (!assets.length) return 'none';
  return assets.length === 1 ? 'one' : 'many';
}

function localKindFromUpload(node) {
  const assets = Array.isArray(node && node.assets) ? node.assets : [];
  const kinds = uniq(assets.map(localAssetKind));
  if (kinds.length === 1) return kinds[0];
  if (kinds.length > 1) return 'group';
  return 'unknown';
}

function roleOfLocal(node) {
  const kind = localKindOf(node);
  if (kind === 'subject') return 'reference';
  if (kind === 'text') return text(node.text).trim() && !text(node.prompt).trim() ? 'note' : 'source';
  if (kind === 'upload') return 'source';
  if (kind === 'image' || kind === 'video') {
    if (text(node.prompt).trim()) return 'generator';
    if (Array.isArray(node.assets) && node.assets.length) {
      return node.status === 'done' ? 'result' : 'source';
    }
    return 'generator';
  }
  return 'unknown';
}

function capabilitiesOf(node) {
  const caps = new Set(['read', 'select', 'deselect']);
  if (!node || node.kind === 'unknown') return [...caps];
  caps.add('rename');
  caps.add('move');
  caps.add('delete');
  caps.add('copy');
  caps.add('cut');
  caps.add('paste');
  caps.add('layer');
  if (node.kind === 'image' || node.kind === 'video') {
    caps.add('generate');
    caps.add('collect');
    caps.add('upload');
  }
  if (node.kind === 'image') caps.add('editImage');
  if (node.kind === 'text') caps.add('rename');
  if (node.role === 'reference') caps.add('reference');
  if (node.source === 'builtin') caps.add('read');
  return [...caps].filter((name) => CAPABILITY_NAMES.includes(name));
}

function normalizeAsset(asset, ctx = {}) {
  const id = text(asset && asset.id, 120);
  const kind = localAssetKind(asset);
  const mediaUrl = typeof ctx.mediaUrl === 'function' ? ctx.mediaUrl(id) : `/api/media/${id}`;
  return {
    id,
    kind,
    url: text(asset && asset.url, 2000) || (id ? mediaUrl : ''),
    coverId: text(asset && asset.coverId, 120),
    coverUrl: asset && asset.coverId && typeof ctx.mediaUrl === 'function' ? ctx.mediaUrl(asset.coverId) : '',
    width: num(asset && asset.w),
    height: num(asset && asset.h),
    duration: num(asset && asset.duration),
    source: 'local',
  };
}

/**
 * 本地节点 → CanonicalNode。
 * ctx 可选：mediaUrl(id), rect, link, place, now。
 */
function normalizeLocalNode(node, ctx = {}) {
  const id = text(node && node.id, 80);
  const errors = [];
  if (!id) errors.push('local node 缺少 id');
  const rawKind = localKindOf(node);
  // upload 是本地容器，不是一种内置资源类型；按实际素材类型归一，多类型才落到 group。
  // subject 是本地参考组，内置没有同构节点；归一成 group，后续只作为 reference 处理。
  const kind = rawKind === 'upload' ? localKindFromUpload(node) : rawKind === 'subject' ? 'group' : rawKind;
  const assets = Array.isArray(node && node.assets) ? node.assets.map((asset) => normalizeAsset(asset, ctx)) : [];
  const role = roleOfLocal(node);
  const geometry = {
    mode: isFrameGeometry(node) ? 'frame' : 'card',
    compatible: false,
    x: Math.round(num(node && node.x)),
    y: Math.round(num(node && node.y)),
    w: clamp(num(node && node.w) || 300, 1, 4096),
    h: ctx.rect && Number.isFinite(Number(ctx.rect.h)) ? Math.round(Number(ctx.rect.h)) : null,
    screenRect: ctx.rect || null,
    frameSize: null,
    note: '本地卡片坐标，不能直接写进内置 2048 画板帧；只用于本地渲染与相对位移。',
  };
  const params = node && node.params && typeof node.params === 'object' ? { ...node.params } : {};
  const canonical = {
    schemaVersion: SCHEMA_VERSION,
    key: makeKey('local', id || '(missing)'),
    id,
    source: 'local',
    remoteId: text(ctx.link && ctx.link.builtinId, 120) || null,
    kind,
    role,
    title: text(node && node.title, 120) || '',
    geometry,
    content: {
      prompt: text(node && node.prompt),
      text: text(node && node.text),
      ratio: text(node && node.ratio, 12) || '1:1',
      params,
      assets,
      cardinality: localCardinality(node),
      container: rawKind,
    },
    generation: {
      state: LOCAL_STATUS_TO_STATE[node && node.status] || 'idle',
      version: null,
      error: node && node.status === 'error' ? text(node.statusText, 400) : '',
    },
    selection: { selected: false },
    capabilities: [],
    links: {
      localId: id || null,
      builtinId: text(ctx.link && ctx.link.builtinId, 120) || null,
      remoteIds: [],
    },
    warnings: [],
    raw: null,
  };
  if (kind === 'upload' && assets.length > 1) {
    canonical.warnings.push('本地 upload 节点含多个资源，内置 canvas-node 只能一节点一资源；需要 fanout 或只取首项。');
  }
  if (kind === 'subject') {
    canonical.warnings.push('本地 subject 是参考素材组，内置画布没有同构节点；只能作为生成参考，不应创建节点。');
  }
  canonical.capabilities = capabilitiesOf(canonical);
  return { ok: errors.length === 0, errors, node: canonical };
}

function builtinResourceKind(resourceId, explicitKind) {
  if (explicitKind && BUILTIN_RESOURCE_KINDS[explicitKind]) return BUILTIN_RESOURCE_KINDS[explicitKind];
  const prefix = String(resourceId || '').split(':')[0];
  return BUILTIN_RESOURCE_KINDS[prefix] || 'unknown';
}

function builtinMediaAssets(snapshot, ctx = {}) {
  const media = Array.isArray(snapshot && snapshot.media) ? snapshot.media : [];
  return media.map((item) => {
    const tag = String(item && item.tag || '').toLowerCase();
    const kind = tag === 'video' ? 'video' : tag === 'audio' ? 'audio' : 'image';
    return {
      id: text(item && (item.id || item.src), 2000),
      kind,
      url: text(item && item.src, 2000),
      coverUrl: text(item && item.poster, 2000),
      width: num(item && item.width),
      height: num(item && item.height),
      duration: num(item && item.duration),
      source: 'builtin',
      builtinResourceId: text(snapshot && snapshot.resourceId, 200),
    };
  });
}

function roleOfBuiltin(kind, state, assets) {
  if (kind === 'text') return 'note';
  if (kind === 'group') return 'reference';
  if (['queued', 'running'].includes(state)) return 'generator';
  if (assets.length && state === 'done') return 'result';
  if (assets.length) return 'source';
  return 'unknown';
}

/**
 * 内置节点快照 → CanonicalNode。
 * snapshot 期望字段（由 bridge 的 DOM 探针提供）：
 *   id, nodeType, resourceId, resourceKind, generationState, generationVersion,
 *   rect, frameSize, flowPosition, transform, zIndex, selected, media, text, title
 * ctx 可选：link, now。
 */
function normalizeBuiltinNode(snapshot, ctx = {}) {
  const id = text(snapshot && snapshot.id, 120);
  const errors = [];
  if (!id) errors.push('builtin node 缺少 data-id');
  const resourceKind = builtinResourceKind(snapshot && snapshot.resourceId, snapshot && snapshot.resourceKind);
  const kind = resourceKind;
  const state = BUILTIN_STATE_TO_STATE[snapshot && snapshot.generationState] || 'idle';
  const assets = builtinMediaAssets(snapshot, ctx);
  const role = roleOfBuiltin(kind, state, assets);
  const frameSize = snapshot && snapshot.frameSize && (num(snapshot.frameSize.w) || num(snapshot.frameSize.h))
    ? { w: Math.round(num(snapshot.frameSize.w)), h: Math.round(num(snapshot.frameSize.h)) }
    : null;
  const position = snapshot && snapshot.flowPosition && typeof snapshot.flowPosition === 'object'
    ? { x: Math.round(num(snapshot.flowPosition.x)), y: Math.round(num(snapshot.flowPosition.y)) }
    : { x: 0, y: 0 };
  const rect = snapshot && snapshot.rect && typeof snapshot.rect === 'object'
    ? {
      x: Math.round(num(snapshot.rect.x)),
      y: Math.round(num(snapshot.rect.y)),
      w: Math.round(num(snapshot.rect.w)),
      h: Math.round(num(snapshot.rect.h)),
    }
    : null;
  const geometry = {
    mode: 'frame',
    compatible: false,
    x: position.x,
    y: position.y,
    w: frameSize ? frameSize.w : (rect ? rect.w : null),
    h: frameSize ? frameSize.h : (rect ? rect.h : null),
    screenRect: rect,
    frameSize,
    note: '内置画板帧按 flow 坐标；与本地卡片不同坐标系、不同尺寸，禁止绝对几何同步。',
  };
  const canonical = {
    schemaVersion: SCHEMA_VERSION,
    key: makeKey('builtin', id || '(missing)'),
    id,
    source: 'builtin',
    remoteId: null,
    kind,
    role,
    title: text(snapshot && snapshot.title, 120) || '',
    geometry,
    content: {
      prompt: text(snapshot && snapshot.prompt),
      text: text(snapshot && snapshot.text),
      ratio: text(snapshot && snapshot.ratio, 12) || '',
      params: snapshot && snapshot.params && typeof snapshot.params === 'object' ? { ...snapshot.params } : {},
      assets,
      cardinality: assets.length ? (assets.length === 1 ? 'one' : 'many') : 'none',
      container: 'builtin',
    },
    generation: {
      state,
      version: num(snapshot && snapshot.generationVersion) || null,
      error: state === 'error' ? text(snapshot && snapshot.error, 400) : '',
    },
    selection: { selected: Boolean(snapshot && snapshot.selected) },
    capabilities: [],
    links: {
      localId: text(ctx.link && ctx.link.localId, 80) || null,
      builtinId: id || null,
      remoteIds: [],
    },
    warnings: [],
    raw: {
      nodeType: text(snapshot && snapshot.nodeType, 120),
      resourceId: text(snapshot && snapshot.resourceId, 200),
      generationState: text(snapshot && snapshot.generationState, 60),
      generationVersion: num(snapshot && snapshot.generationVersion) || null,
      zIndex: num(snapshot && snapshot.zIndex) || null,
      transform: text(snapshot && snapshot.transform, 200),
    },
  };
  if (!frameSize) canonical.warnings.push('内置节点缺少 frameSize，无法确认 2048 画板尺寸；按 screenRect 兜底。');
  if (!assets.length && ['done', 'idle'].includes(state)) canonical.warnings.push('内置节点未读到媒体；可能是空节点、文本节点，或媒体尚未挂载。');
  if (kind === 'unknown') canonical.warnings.push('未知内置资源前缀；先按只读占位，不创建本地节点。');
  canonical.capabilities = capabilitiesOf(canonical);
  return { ok: errors.length === 0, errors, node: canonical };
}

/**
 * CanonicalNode（来自内置）→ 本地 node patch。
 * 只返回本地 store 能接受的字段；不返回 id，除非 ctx.allowId。
 * 资源 URL 需要 ctx.assetResolver(url) 换成已落盘的本地 asset id，否则不写入 assets。
 */
function toLocalPatch(node, ctx = {}) {
  if (!node || node.schemaVersion !== SCHEMA_VERSION) {
    return { ok: false, errors: ['不是当前 schema 的 CanonicalNode'], patch: {}, unsupported: [], warnings: [] };
  }
  const patch = {};
  const unsupported = [];
  const warnings = [];
  const kind = node.kind;
  const isSourceAsset = node.role === 'source' && ['image', 'video', 'audio'].includes(kind);
  if (kind === 'unknown') unsupported.push('unknown-kind');
  if (isSourceAsset) {
    // 内置的素材节点回到本地时，落到 upload 容器，保留“素材源”语义；不要伪装成生成结果。
    patch.kind = 'upload';
  } else if (kind === 'group') {
    if (!ctx.allowSubject) unsupported.push('subject-needs-reference-group');
    else patch.kind = 'subject';
  } else if (kind === 'audio') {
    patch.kind = 'upload';
  } else if (['image', 'video', 'text'].includes(kind)) {
    patch.kind = kind;
  } else {
    unsupported.push('unsupported-kind:' + kind);
  }
  if (node.title) patch.title = text(node.title, 60);
  if (node.content && node.content.prompt) patch.prompt = text(node.content.prompt);
  if (node.content && node.content.text) patch.text = text(node.content.text);
  if (node.content && node.content.ratio) patch.ratio = text(node.content.ratio, 12);
  if (node.content && node.content.params && Object.keys(node.content.params).length) patch.params = { ...node.content.params };

  const assets = Array.isArray(node.content && node.content.assets) ? node.content.assets : [];
  const resolved = [];
  for (const asset of assets) {
    const localId = typeof ctx.assetResolver === 'function' ? ctx.assetResolver(asset.url, asset) : null;
    if (localId) {
      resolved.push({
        id: String(localId),
        kind: ['image', 'video', 'audio'].includes(asset.kind) ? asset.kind : 'image',
        w: num(asset.width),
        h: num(asset.height),
        duration: num(asset.duration),
        coverId: text(asset.coverId, 120),
      });
    } else {
      warnings.push('asset-not-collected:' + (asset.url || asset.id || 'unknown'));
    }
  }
  if (resolved.length) patch.assets = resolved;
  else if (assets.length) warnings.push('内置资源需要先 collect 落盘，不能直接把 blob/http URL 写进本地 assets。');

  if (isFrameGeometry(node)) {
    // 内置画板帧 → 本地卡片只能是“预览卡”，不是等比例缩放；绝对坐标不写。
    patch.w = clamp(ctx.previewWidth || 300, 220, 560);
    warnings.push('frame→card：仅生成预览卡，不同步绝对 x/y/w/h；位置由本地布局决定。');
    unsupported.push('frame-geometry-sync');
  } else if (isCardGeometry(node)) {
    patch.x = Math.round(num(node.geometry.x));
    patch.y = Math.round(num(node.geometry.y));
    patch.w = clamp(node.geometry.w || 300, 220, 560);
  }

  const canShowResult = resolved.length > 0 || node.kind === 'text';
  patch.status = node.generation && node.generation.state === 'done' && canShowResult
    ? 'done'
    : node.generation && node.generation.state === 'error'
      ? 'error'
      : 'idle';
  if (node.generation && node.generation.error) patch.statusText = text(node.generation.error, 400);
  if (ctx.allowId && node.links && node.links.localId) patch.id = text(node.links.localId, 60);

  return {
    ok: unsupported.length === 0 || Boolean(ctx.allowPartial),
    patch,
    unsupported,
    warnings,
    meta: {
      builtinId: node.links && node.links.builtinId || node.id,
      frame: isFrameGeometry(node),
      collectedAssets: resolved.length,
    },
  };
}

/**
 * CanonicalNode（来自本地）→ 内置动作计划。
 * 这里只生成“要做什么”的声明，不直接执行；执行器负责把 op 翻译成 CDP 鼠标/键盘。
 * 会耗点数的动作必须 requiresConfirm。
 */
function toBuiltinPlan(node, ctx = {}) {
  if (!node || node.schemaVersion !== SCHEMA_VERSION) {
    return { ok: false, errors: ['不是当前 schema 的 CanonicalNode'], action: 'none', steps: [], unsupported: [], warnings: [] };
  }
  const unsupported = [];
  const warnings = [];
  const steps = [];
  const hasRemote = Boolean(node.links && node.links.builtinId) || Boolean(node.remoteId);
  if (node.kind === 'unknown') {
    return { ok: false, action: 'none', steps: [], unsupported: ['unknown-kind'], warnings: ['未知类型不落内置'] };
  }
  if (node.content && node.content.container === 'upload') {
    // 本地 upload 是容器，不是参考组；多资源走 fanout，单资源走上传，不进入生成。
    const targets = Array.isArray(node.content.assets)
      ? node.content.assets.map((asset) => ({ kind: asset.kind, url: asset.url, id: asset.id }))
      : [];
    steps.push({ op: 'click-toolbar', target: 'upload' });
    steps.push({ op: 'upload-files', assets: targets, requiresFilePath: true });
    return {
      ok: true,
      action: targets.length > 1 ? 'upload-fanout' : 'upload',
      steps,
      unsupported: targets.length > 1 ? ['one-local-node-to-many-builtin-frames'] : [],
      warnings: targets.length > 1
        ? ['本地 upload 含多个资源：内置按一节点一资源上传，需 fanout。']
        : [],
    };
  }
  if (node.kind === 'group' || node.role === 'reference') {
    return {
      ok: false,
      action: 'none',
      steps: [],
      unsupported: ['reference-only'],
      warnings: ['主体/参考组只作为生成参考，不创建内置节点；生成时通过引用输入传递。'],
    };
  }
  if (node.kind === 'text') {
    steps.push({ op: hasRemote ? 'select-node' : 'click-toolbar', target: hasRemote ? node.links.builtinId : 'text' });
    if (!hasRemote) steps.push({ op: 'create-text-node', text: text(node.content && node.content.text) });
    else steps.push({ op: 'set-node-text', nodeId: node.links.builtinId, text: text(node.content && node.content.text) });
    return { ok: true, action: hasRemote ? 'update-text' : 'create-text', steps, unsupported, warnings };
  }
  if (node.role === 'source' && ['image', 'video', 'audio'].includes(node.kind)) {
    // 素材源不是“生成任务”，应走内置的上传/资产入口；本地只有 URL，执行器需要先拿到本地文件路径。
    const targets = Array.isArray(node.content && node.content.assets)
      ? node.content.assets.map((asset) => ({ kind: asset.kind, url: asset.url, id: asset.id }))
      : [];
    steps.push({ op: 'click-toolbar', target: 'upload' });
    steps.push({ op: 'upload-files', assets: targets, requiresFilePath: true });
    return {
      ok: true,
      action: targets.length > 1 ? 'upload-fanout' : 'upload',
      steps,
      unsupported: targets.length > 1 ? ['one-local-node-to-many-builtin-frames'] : [],
      warnings: targets.length > 1
        ? ['本地素材节点含多个资源：内置按一节点一资源上传，需 fanout。']
        : [],
    };
  }
  if (node.kind === 'image' || node.kind === 'video') {
    if (hasRemote) {
      steps.push({ op: 'select-node', nodeId: node.links.builtinId });
      if (node.title) steps.push({ op: 'rename-node', nodeId: node.links.builtinId, title: text(node.title, 60) });
      if (node.content && node.content.prompt) steps.push({ op: 'set-prompt', nodeId: node.links.builtinId, prompt: text(node.content.prompt) });
      return { ok: true, action: 'update-node', steps, unsupported, warnings };
    }
    steps.push({ op: 'click-toolbar', target: node.kind === 'video' ? 'video' : 'image' });
    steps.push({ op: 'fill-generation', kind: node.kind, prompt: text(node.content && node.content.prompt), params: { ...(node.content && node.content.params || {}) } });
    steps.push({ op: 'submit-generation', requiresConfirm: true, estimatedCost: ctx.estimatedCost || null, cost: 'points' });
    return {
      ok: true,
      action: 'create-generation',
      steps,
      unsupported,
      warnings,
      requiresConfirm: true,
    };
  }
  // 本地 upload / 多资源节点：内置一节点一资源，必须 fanout。
  if (node.content && node.content.cardinality === 'many') {
    unsupported.push('one-local-node-to-many-builtin-frames');
    warnings.push('本地 upload 含多个资源：按每个资源拆成一个内置节点（fanout），不能塞进一个画板帧。');
    return { ok: false, action: 'fanout', steps: [], unsupported, warnings };
  }
  return { ok: false, action: 'none', steps: [], unsupported: ['unsupported-kind:' + node.kind], warnings };
}

function stableValue(value) {
  if (value == null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(stableValue);
  const out = {};
  for (const key of Object.keys(value).sort()) out[key] = stableValue(value[key]);
  return out;
}

function equalValue(a, b) {
  return JSON.stringify(stableValue(a)) === JSON.stringify(stableValue(b));
}

/**
 * 比较本地与内置的同一语义节点。
 * 几何永远不参与 safe 列表：card/frame 不可直接同步。
 */
function diffNode(localNode, builtinNode) {
  const fields = [];
  const add = (field, local, builtin, safe = true, note = '') => {
    fields.push({ field, local, builtin, equal: equalValue(local, builtin), safe, note });
  };
  add('title', localNode && localNode.title, builtinNode && builtinNode.title);
  add('prompt', localNode && localNode.content && localNode.content.prompt, builtinNode && builtinNode.content && builtinNode.content.prompt);
  add('text', localNode && localNode.content && localNode.content.text, builtinNode && builtinNode.content && builtinNode.content.text);
  add('ratio', localNode && localNode.content && localNode.content.ratio, builtinNode && builtinNode.content && builtinNode.content.ratio);
  add('params', localNode && localNode.content && localNode.content.params, builtinNode && builtinNode.content && builtinNode.content.params);
  add('assetCount', localNode && localNode.content && localNode.content.assets ? localNode.content.assets.length : 0,
    builtinNode && builtinNode.content && builtinNode.content.assets ? builtinNode.content.assets.length : 0);
  add('generationState', localNode && localNode.generation && localNode.generation.state, builtinNode && builtinNode.generation && builtinNode.generation.state);
  add('geometry', localNode && localNode.geometry, builtinNode && builtinNode.geometry, false, 'card/frame 不同坐标系，不能同步');
  add('link', localNode && localNode.links, builtinNode && builtinNode.links);
  const summary = { same: 0, differs: 0, unsafe: 0, safeToApply: [] };
  for (const f of fields) {
    if (f.equal) summary.same += 1;
    else if (f.safe) { summary.differs += 1; summary.safeToApply.push(f.field); }
    else summary.unsafe += 1;
  }
  return {
    fields,
    summary,
    canAutoApply: summary.unsafe === 0,
    note: '只把 safeToApply 里的字段交给写入层；geometry 永不自动写。',
  };
}

/**
 * 双向 ID 链接表。独立于本地 store，避免把内置 ID 写进本地节点被 sanitize 掉。
 */
class NodeLinkTable {
  constructor(initial = null) {
    this.byLocal = new Map();
    this.byBuiltin = new Map();
    if (initial && typeof initial === 'object') {
      for (const [localId, builtinId] of Object.entries(initial)) this.link(localId, builtinId);
    }
  }

  link(localId, builtinId, meta = {}) {
    const l = text(localId, 80);
    const b = text(builtinId, 120);
    if (!l || !b) return { ok: false, error: 'localId/builtinId 不能为空' };
    if (this.byLocal.has(l) && this.byLocal.get(l).builtinId !== b) {
      return { ok: false, error: '本地节点已链接到另一个内置节点' };
    }
    if (this.byBuiltin.has(b) && this.byBuiltin.get(b).localId !== l) {
      return { ok: false, error: '内置节点已链接到另一个本地节点' };
    }
    const entry = { localId: l, builtinId: b, meta: { ...meta } };
    this.byLocal.set(l, entry);
    this.byBuiltin.set(b, entry);
    return { ok: true, entry };
  }

  getByLocal(localId) {
    return this.byLocal.get(String(localId || '')) || null;
  }

  getByBuiltin(builtinId) {
    return this.byBuiltin.get(String(builtinId || '')) || null;
  }

  unlinkLocal(localId) {
    const entry = this.byLocal.get(String(localId || ''));
    if (!entry) return false;
    this.byLocal.delete(entry.localId);
    this.byBuiltin.delete(entry.builtinId);
    return true;
  }

  toJSON() {
    const out = {};
    for (const [localId, entry] of this.byLocal.entries()) out[localId] = entry.builtinId;
    return out;
  }
}

module.exports = {
  SCHEMA_VERSION,
  NODE_KINDS,
  NODE_ROLES,
  GEOMETRY_MODES,
  GEN_STATES,
  CAPABILITY_NAMES,
  BUILTIN_RESOURCE_KINDS,
  LOCAL_KIND_TO_BUILTIN,
  normalizeLocalNode,
  normalizeBuiltinNode,
  toLocalPatch,
  toBuiltinPlan,
  diffNode,
  NodeLinkTable,
  capabilitiesOf,
  _private: { text, num, clamp, uniq, equalValue, stableValue, localKindFromUpload },
};
