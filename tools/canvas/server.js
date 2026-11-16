const http = require('node:http');
const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const crypto = require('node:crypto');
const os = require('node:os');
const { spawn } = require('node:child_process');

const { Store, sniffKind, extFor } = require('./lib/store');
const { grabLatest } = require('./lib/cdp_grab');
const { Gateway, detectApiKey, GatewayError } = require('./lib/gateway');
const { DoubaoWorkGateway } = require('./lib/doubaowork');
const { TaskManager } = require('./lib/tasks');
const { SkillLibrary } = require('./lib/skills');
const { DEFAULT_PRICING, estimatePoints } = require('./lib/pricing');
const { Ledger } = require('./lib/ledger');
const { safeName } = require('./lib/json-file');
const { readBuiltinState, readBuiltinContext, readBuiltinModel, openBuiltinCanvas, ensureBuiltinCanvas, selectBuiltinNode, moveBuiltinNode, deleteBuiltinNode, deselectBuiltin, probeCdp, captureBuiltinScreenshot, clickBuiltinNormalized, wheelBuiltinNormalized, dragBuiltinNormalized, keyBuiltinNormalized, createBuiltinGenerationNode, fillBuiltinPrompt, fillBuiltinPromptForItem, configureBuiltinGeneration, prepareCanvasEntry, readCanvasTarget, diagnoseCanvas } = require('./lib/builtin-canvas');
const PKG = require('./package.json');

const ROOT = __dirname;
const DATA_ROOT = process.env.CANVAS_ROOT || __dirname;
const PUBLIC_DIR = path.join(ROOT, 'public');
const PORT = Number(process.env.PORT) || 9190;
const HOST = '127.0.0.1';
const TOKEN = crypto.randomBytes(18).toString('hex');

const store = new Store(DATA_ROOT);
const skills = new SkillLibrary(ROOT);
const ledger = new Ledger(store.dataDir);
const tasks = new TaskManager({ store, getGateway: makeGateway, ledger, log: logLine });

function logLine(...args) {
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  console.log(`[${time}]`, ...args);
}

function makeGateway() {
  const full = store.getConfig();
  if ((full.doubaoMode || 'doubaowork') === 'doubaowork') {
    return new DoubaoWorkGateway({ rootDir: ROOT, dataDir: store.dataDir, config: full });
  }
  const config = full.gateway || {};
  let apiKey = config.apiKey || detectApiKey(ROOT);
  // 分发到别人电脑时，网关目录是用户自己选的：顺手读它旁边的 .api_key。
  if (!apiKey) {
    const script = full.gatewayStartScript;
    if (script) {
      try {
        const file = path.join(path.dirname(script), '.api_key');
        if (fs.existsSync(file)) apiKey = fs.readFileSync(file, 'utf8').trim();
      } catch { /* ignore */ }
    }
  }
  return new Gateway({
    baseUrl: config.baseUrl,
    apiKey,
  });
}

/**
 * 工作模型能不能选：只有外部网关模式才谈得上「选模型」。
 * CDP 桥接是把文字塞进豆包工作客户端，模型由客户端自己决定，传什么都无效——
 * 这里必须如实返回，界面上不能给出一个点了没反应的假下拉。
 */
async function agentModelCapability(config = store.getConfig()) {
  if ((config.doubaoMode || 'doubaowork') === 'doubaowork') {
    return {
      models: [],
      selectable: false,
      reason: '当前是「豆包工作客户端」接入，模型由客户端决定，画布不能选。切到「外部网关」后可选。',
    };
  }
  try {
    const gateway = makeGateway();
    const health = await gateway.health();
    const models = (health.models || []).filter(Boolean);
    return {
      models,
      selectable: models.length > 0,
      reason: models.length ? '' : '网关没有返回模型列表，先点「检测网关」。',
    };
  } catch (error) {
    return { models: [], selectable: false, reason: '网关不可用：' + error.message };
  }
}

/** 当前工作模型；CDP 模式下它只是个回显值，网关模式才是真选型。 */
function agentModel() {
  const config = store.getConfig();
  return String(config.agentModel || 'doubao-pro').trim() || 'doubao-pro';
}

/**
 * 文本调用落账：不计生成点数，只记次数、字符数和（网关返回了才有）token。
 * CDP 模式不回传 usage，这一点直接写在 note 里，别让人误以为是账单级数据。
 */
function recordChat({ action, canvasId = '', model = '', startedAt = Date.now(), raw = '', charsIn = 0, gateway = null, status = 'done', error = '' }) {
  const usage = gateway && gateway.lastUsage;
  ledger.record({
    kind: 'chat',
    action,
    canvasId: String(canvasId || ''),
    model: model || '（默认）',
    status,
    error: error || '',
    ms: Date.now() - startedAt,
    charsIn,
    charsOut: String(raw || '').length,
    tokensIn: Number((usage && (usage.prompt_tokens || usage.input_tokens)) || 0),
    tokensOut: Number((usage && (usage.completion_tokens || usage.output_tokens)) || 0),
    points: 0,
    pointsEstimated: false,
    note: usage ? '网关返回了 usage' : '客户端不回传 token，只记调用次数与字符数',
  });
}

// ── HTTP 小工具 ─────────────────────────────────────────────

function json(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function fail(res, status, message) {
  json(res, status, { error: String(message || '出错了') });
}

function bridgeFail(res, error) {
  const message = String(error && error.message || error || '桥接失败');
  const code = error && error.code || (message.includes('canvas-hidden') ? 'canvas-hidden' : message.includes('no-flow') ? 'no-flow' : 'bridge-unavailable');
  const status = code === 'canvas-hidden' || code === 'no-flow' ? 409 : 502;
  return json(res, status, {
    ok: false,
    code,
    error: message,
    hint: '内置画布必须在前台；客户端需带 --remote-debugging-port=9333 --remote-allow-origins=* 启动，并打开一个画布标签。',
  });
}

/** 把内置画布的模型档位映射回本地计价表的模型 key。 */
function pricingModelForBuiltin(kind, model) {
  const value = String(model || '');
  if (kind === 'video') {
    if (/2\.5|2_5/.test(value)) return 'seedance_2.5';
    if (/Mini/i.test(value)) return 'seedance_2.0_mini';
    if (/Fast/i.test(value)) return 'seedance_2.0_fast';
    return 'seedance_2.0';
  }
  if (/5\.5|5_5/.test(value)) return 'seedream_5.5';
  if (/Pro/i.test(value)) return 'seedream_5.0_pro';
  if (/4\.5/.test(value)) return 'seedream_4.5';
  if (/4\.0/.test(value)) return 'seedream_4.0';
  return 'seedream_5.0_flash';
}

function builtinPlan(input = {}, cfg = {}) {
  const kind = input.kind === 'video' ? 'video' : 'image';
  const model = pricingModelForBuiltin(kind, input.model);
  const quality = String(input.quality || (kind === 'video' ? '720P' : '1K'));
  const count = kind === 'image' ? ([1, 2, 4].includes(Number(input.count)) ? Number(input.count) : 1) : 1;
  const duration = kind === 'video' ? Math.min(15, Math.max(4, Math.round(Number(input.duration) || 5))) : 0;
  let points = 0;
  try {
    points = Number(estimatePoints({ kind, model, quality, duration, count }, DEFAULT_PRICING).points) || 0;
  } catch { points = 0; }
  points = Math.round(points * 10) / 10;
  return {
    kind,
    model,
    modelLabel: String(input.model || ''),
    ratio: String(input.ratio || '自动'),
    duration,
    quality,
    count,
    points,
    pointsEstimated: true,
    note: '点数按本地档位估算，实际以豆包扣点为准',
  };
}

async function readJsonBody(req, limit = 8 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(Object.assign(new Error('请求体太大'), { status: 413 }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (!chunks.length) return resolve({});
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch {
        reject(Object.assign(new Error('请求体不是合法 JSON'), { status: 400 }));
      }
    });
    req.on('error', reject);
  });
}

function requireToken(req, res) {
  if (req.headers['x-canvas-token'] !== TOKEN) {
    fail(res, 403, '缺少画布令牌（刷新页面重试）');
    return false;
  }
  return true;
}

function decodeName(req) {
  const raw = String(req.headers['x-filename'] || '');
  try { return decodeURIComponent(raw); } catch { return raw; }
}

const MIME_BY_EXT = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
  '.gif': 'image/gif', '.avif': 'image/avif', '.mp4': 'video/mp4', '.webm': 'video/webm',
  '.mov': 'video/quicktime', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
};

function contentTypeOf(asset) {
  return asset.mime || MIME_BY_EXT[path.extname(asset.file).toLowerCase()] || 'application/octet-stream';
}

// ── 静态文件 ───────────────────────────────────────────────

async function serveStatic(req, res, pathname) {
  let rel = pathname === '/' ? '/index.html' : pathname;
  if (rel.includes('..')) return fail(res, 400, '非法路径');
  const file = path.join(PUBLIC_DIR, rel.replace(/^\/+/, ''));
  if (!file.startsWith(PUBLIC_DIR)) return fail(res, 400, '非法路径');
  let stat;
  try { stat = await fsp.stat(file); } catch { return fail(res, 404, '页面不存在'); }
  if (!stat.isFile()) return fail(res, 404, '页面不存在');

  if (rel === '/index.html' || rel === '/bridge.html') {
    let html = await fsp.readFile(file, 'utf8');
    html = html.replace('__TOKEN_VALUE__', TOKEN);
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': Buffer.byteLength(html),
      'Cache-Control': 'no-store',
      'Content-Security-Policy': "default-src 'self'; img-src 'self' data: blob:; media-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'",
    });
    return res.end(html);
  }

  const ext = path.extname(file).toLowerCase();
  const type = MIME_BY_EXT[ext] || (ext === '.js' ? 'text/javascript; charset=utf-8' : ext === '.css' ? 'text/css; charset=utf-8' : ext === '.svg' ? 'image/svg+xml' : 'application/octet-stream');
  res.writeHead(200, {
    'Content-Type': type,
    'Content-Length': stat.size,
    'Cache-Control': ext === '.js' || ext === '.css' ? 'no-cache' : 'public, max-age=3600',
  });
  fs.createReadStream(file).pipe(res);
}

// ── 媒体读取（支持 Range，视频才能拖进度）──────────────────

async function serveMedia(req, res, id) {
  const asset = store.getAsset(id);
  if (!asset) return fail(res, 404, '素材不存在');
  const file = store.assetPath(asset);
  let stat;
  try { stat = await fsp.stat(file); } catch { return fail(res, 404, '素材文件不在磁盘上'); }

  const type = contentTypeOf(asset);
  const range = String(req.headers.range || '');
  const headers = {
    'Content-Type': type,
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'public, max-age=31536000, immutable',
  };
  if (req.url.includes('download=1')) {
    headers['Content-Disposition'] = `attachment; filename="${encodeURIComponent(asset.name || asset.file)}"`;
  }

  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (match) {
      let start = match[1] ? Number(match[1]) : 0;
      let end = match[2] ? Number(match[2]) : stat.size - 1;
      if (!match[1] && match[2]) {
        start = Math.max(0, stat.size - Number(match[2]));
        end = stat.size - 1;
      }
      start = Math.max(0, Math.min(start, stat.size - 1));
      end = Math.max(start, Math.min(end, stat.size - 1));
      headers['Content-Range'] = `bytes ${start}-${end}/${stat.size}`;
      headers['Content-Length'] = end - start + 1;
      res.writeHead(206, headers);
      return fs.createReadStream(file, { start, end }).pipe(res);
    }
  }

  headers['Content-Length'] = stat.size;
  res.writeHead(200, headers);
  fs.createReadStream(file).pipe(res);
}

// ── 上传：原始字节流落盘 ────────────────────────────────────

const MAX_UPLOAD = 300 * 1024 * 1024;

async function handleUpload(req, res) {
  const tmp = path.join(store.mediaDir, `.upload-${process.pid}-${Date.now()}.tmp`);
  const hash = crypto.createHash('sha256');
  let size = 0;
  let head = Buffer.alloc(0);
  const out = fs.createWriteStream(tmp);

  try {
    await new Promise((resolve, reject) => {
      req.on('data', (chunk) => {
        size += chunk.length;
        if (size > MAX_UPLOAD) {
          reject(Object.assign(new Error('文件超过 300MB'), { status: 413 }));
          req.destroy();
          return;
        }
        if (head.length < 16) head = Buffer.concat([head, chunk.subarray(0, 16 - head.length)]);
        hash.update(chunk);
        if (!out.write(chunk)) req.pause();
      });
      out.on('drain', () => req.resume());
      req.on('end', () => out.end(resolve));
      out.on('error', reject);
      req.on('error', reject);
    });
  } catch (error) {
    out.destroy();
    await fsp.rm(tmp, { force: true }).catch(() => {});
    throw error;
  }

  const mime = String(req.headers['content-type'] || '').split(';')[0].trim();
  const kind = sniffKind(head, mime);
  if (kind !== 'image' && kind !== 'video' && kind !== 'audio') {
    await fsp.rm(tmp, { force: true }).catch(() => {});
    return fail(res, 415, '只支持图片、视频或音频文件');
  }
  const name = safeName(decodeName(req), kind === 'video' ? 'video.mp4' : kind === 'audio' ? 'audio.mp3' : 'image.png');
  const digest = hash.digest('hex');
  const ext = extFor(mime, kind, '');
  const existing = store.getAsset(digest);
  if (existing) {
    await fsp.rm(tmp, { force: true }).catch(() => {});
    return json(res, 200, { asset: publicAsset(existing) });
  }

  const file = `${digest.slice(0, 32)}.${ext}`;
  await fsp.rename(tmp, path.join(store.mediaDir, file));
  const asset = {
    id: digest,
    file,
    kind,
    mime: mime || MIME_BY_EXT['.' + ext] || '',
    bytes: size,
    w: Number(req.headers['x-width']) || 0,
    h: Number(req.headers['x-height']) || 0,
    duration: Number(req.headers['x-duration']) || 0,
    name,
    source: 'upload',
    createdAt: Date.now(),
  };
  store.media.assets[digest] = asset;
  await store.persistMediaIndex();
  json(res, 200, { asset: publicAsset(asset) });
}

function publicAsset(asset) {
  return {
    id: asset.id,
    kind: asset.kind,
    w: asset.w,
    h: asset.h,
    duration: asset.duration,
    name: asset.name,
    bytes: asset.bytes,
    createdAt: Number(asset.createdAt) || 0,
    url: `/api/media/${asset.id}`,
  };
}

/**
 * 宽容解析模型返回的 JSON：先直解，失败再把字符串里的裸换行/制表符转义后重试。
 * 豆包经常在 prompt 字符串里直接换行（JSON 不允许），直解会整个失败。
 */
function parseLooseJson(text) {
  const raw = String(text || '');
  const start = raw.indexOf('{');
  const end = raw.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  const slice = raw.slice(start, end + 1);
  try { return JSON.parse(slice); } catch { /* 继续尝试修复 */ }
  let out = '';
  let inString = false;
  let escaped = false;
  for (const ch of slice) {
    if (inString) {
      if (escaped) { out += ch; escaped = false; continue; }
      if (ch === '\\') { out += ch; escaped = true; continue; }
      if (ch === '"') { inString = false; out += ch; continue; }
      if (ch === '\n') { out += '\\n'; continue; }
      if (ch === '\r') continue;
      if (ch === '\t') { out += '\\t'; continue; }
      out += ch;
      continue;
    }
    if (ch === '"') inString = true;
    out += ch;
  }
  try { return JSON.parse(out); } catch { return null; }
}

function normalizeAgentPlan(raw, allowedNodeIds = []) {
  const obj = parseLooseJson(raw);
  if (!obj) throw new Error('Agent 没有返回有效的 JSON 计划');
  const rawNodes = Array.isArray(obj.nodes) ? obj.nodes : [];
  const rawUpdates = Array.isArray(obj.updates) ? obj.updates : [];
  if (!rawNodes.length && !rawUpdates.length) throw new Error('Agent 没有给出可用的节点');
  const allowedRatios = new Set(['1:1', '4:3', '3:4', '16:9', '9:16']);
  const allowedIds = new Set((Array.isArray(allowedNodeIds) ? allowedNodeIds : []).map(String));
  const keys = new Set();
  const nodes = [];
  for (const rawNode of rawNodes.slice(0, 6)) {
    if (!rawNode || typeof rawNode !== 'object') continue;
    const kind = ['image', 'video', 'text'].includes(rawNode.kind) ? rawNode.kind : 'image';
    const prompt = typeof rawNode.prompt === 'string' ? rawNode.prompt.trim().slice(0, 2000) : '';
    const textValue = typeof rawNode.text === 'string' ? rawNode.text.trim().slice(0, 4000) : '';
    if (kind !== 'text' && !prompt) continue;
    if (kind === 'text' && !textValue && !prompt) continue;
    let key = String(rawNode.key || `n${nodes.length + 1}`).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 20) || `n${nodes.length + 1}`;
    while (keys.has(key)) key += '_';
    const refRaw = rawNode.ref ? String(rawNode.ref) : '';
    const ref = refRaw && keys.has(refRaw) ? refRaw : null;
    keys.add(key);
    nodes.push({
      key,
      kind,
      title: String(rawNode.title || ({ image: '图片', video: '视频', text: '文本' }[kind])).trim().slice(0, 60),
      prompt,
      text: textValue || (kind === 'text' ? prompt : ''),
      ratio: allowedRatios.has(rawNode.ratio) ? rawNode.ratio : '16:9',
      ref,
    });
  }
  const updates = [];
  for (const rawUpdate of rawUpdates.slice(0, 6)) {
    if (!rawUpdate || typeof rawUpdate !== 'object') continue;
    const nodeId = String(rawUpdate.nodeId || rawUpdate.id || '').slice(0, 40);
    if (!nodeId) continue;
    // 只允许改这次真正传给模型的选中节点，避免模型编 id 改错节点。
    // 没有选中节点时不允许任何 updates：模型手上有全画布 id，否则会擅自改别的节点。
    if (!allowedIds.size || !allowedIds.has(nodeId)) continue;
    const patch = { nodeId };
    if (typeof rawUpdate.title === 'string' && rawUpdate.title.trim()) patch.title = rawUpdate.title.trim().slice(0, 60);
    if (typeof rawUpdate.prompt === 'string' && rawUpdate.prompt.trim()) patch.prompt = rawUpdate.prompt.trim().slice(0, 4000);
    if (typeof rawUpdate.text === 'string' && rawUpdate.text.trim()) patch.text = rawUpdate.text.trim().slice(0, 4000);
    if (!patch.title && !patch.prompt && !patch.text) continue;
    if (updates.some((entry) => entry.nodeId === nodeId)) continue;
    updates.push(patch);
  }
  if (!nodes.length && !updates.length) throw new Error('Agent 没有给出可用的节点');
  const autoRun = Array.isArray(obj.autoRun)
    ? obj.autoRun.map(String).filter((key) => keys.has(key))
    : [];
  const summary = nodes.length && updates.length
    ? `新建 ${nodes.length} 个节点、改写 ${updates.length} 个`
    : nodes.length
      ? `已拆解为 ${nodes.length} 个节点`
      : `已改写 ${updates.length} 个节点的提示词`;
  return {
    summary: String(obj.summary || summary).slice(0, 300),
    nodes,
    updates,
    autoRun,
  };
}

/** 「只出提示词」模式的结果：一张提示词清单，不涉及画布结构。 */
function normalizeAgentPrompts(raw) {
  const obj = parseLooseJson(raw);
  if (!obj) throw new Error('Agent 没有返回有效的提示词 JSON');
  const list = Array.isArray(obj.prompts) ? obj.prompts : (Array.isArray(obj.nodes) ? obj.nodes : []);
  const prompts = [];
  for (const item of list.slice(0, 4)) {
    if (!item || typeof item !== 'object') continue;
    const prompt = String(item.prompt || '').trim().slice(0, 4000);
    if (!prompt) continue;
    const kind = ['image', 'video'].includes(item.kind) ? item.kind : 'image';
    prompts.push({
      title: String(item.title || (kind === 'video' ? '视频提示词' : '图片提示词')).trim().slice(0, 60),
      kind,
      prompt,
    });
  }
  if (!prompts.length) throw new Error('Agent 没有给出可用的提示词');
  return {
    summary: String(obj.summary || `给了 ${prompts.length} 条提示词`).slice(0, 300),
    prompts,
  };
}

/** 把模型返回的"提示词"洗成可以直接塞进输入框的一段正文。 */
function cleanPromptText(raw) {  let text = String(raw || '').trim();
  if (!text) return '';
  text = text.replace(/^```[a-z]*\s*/i, '').replace(/```\s*$/, '').trim();
  text = text.replace(/^(改写后(的)?提示词|提示词|正文|prompt)\s*[:：]\s*/i, '').trim();
  text = text.replace(/^["'“”「『]+/, '').replace(/["'“”」』]+$/, '').trim();
  // 模型偶尔会先写一段解释再给正文：空行分段后如果某一段明显更长，只留最长那段。
  if (/\n{2,}/.test(text)) {
    const parts = text.split(/\n{2,}/).map((part) => part.trim()).filter(Boolean);
    if (parts.length > 1) {
      const best = parts.reduce((a, b) => (b.length > a.length ? b : a), '');
      const restLength = parts.filter((part) => part !== best).join('').length;
      if (best.length > restLength) text = best;
    }
  }
  return text.replace(/\s*\n+\s*/g, ' ').replace(/[ \t]{2,}/g, ' ').trim().slice(0, 3000);
}

/**
 * 把提示词里的 @名字 归一化：模型很爱在名字里塞空格（@晓艳 1），
 * 这里按已知素材名做"最长匹配 + 去掉内部空格"重建，保证 @ 能和本地参考文件对上。
 */
function normalizeMentions(text, names) {
  const list = (Array.isArray(names) ? names : [])
    .map((name) => String(name || '').replace(/\s+/g, ''))
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  const src = String(text || '');
  if (!list.length) return src;
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
    let matched = '';
    for (const name of list) {
      if (normalized.startsWith(name) && name.length > matched.length) matched = name;
    }
    if (!matched) { out += '@'; cursor = at + 1; continue; }
    let consumed = 0;
    let letters = 0;
    while (consumed < run.length && letters < matched.length) {
      if (!/\s/.test(run[consumed])) letters += 1;
      consumed += 1;
    }
    out += '@' + matched;
    cursor = at + 1 + consumed;
  }
  return out;
}

// ── 路由 ───────────────────────────────────────────────────
async function handleApi(req, res, url) {
  const pathname = url.pathname;

  // 媒体读取不需要令牌（<img>/<video> 带不了自定义头），只读且只认索引里的 id。
  if (pathname === '/api/subjects' && req.method === 'GET') {
    return json(res, 200, { subjects: store.listSubjects() });
  }
  if (pathname === '/api/subjects' && req.method === 'POST') {
    const body = await readJsonBody(req);
    try {
      const subject = await store.createSubject({ name: body.name, assetIds: body.assetIds, note: body.note, audioId: body.audioId, draft: body.draft === true });
      return json(res, 200, { subject });
    } catch (error) {
      return fail(res, error.status || 400, error.message);
    }
  }
  const subjectId = pathname.startsWith('/api/subjects/') ? pathname.slice('/api/subjects/'.length) : '';
  if (subjectId && /^[A-Za-z0-9_-]{4,60}$/.test(subjectId) && req.method === 'PATCH') {
    const body = await readJsonBody(req);
    const subject = await store.updateSubject(subjectId, body || {});
    if (!subject) return fail(res, 404, '主体不存在');
    return json(res, 200, { subject });
  }
  if (subjectId && /^[A-Za-z0-9_-]{4,60}$/.test(subjectId) && req.method === 'DELETE') {
    const ok = await store.deleteSubject(subjectId);
    if (!ok) return fail(res, 404, '主体不存在');
    return json(res, 200, { ok: true });
  }

  if (pathname === '/api/media' && req.method === 'GET') {
    return json(res, 200, { assets: store.listAssets().map(publicAsset) });
  }
  if (pathname.startsWith('/api/media/') && req.method === 'GET') {
    return serveMedia(req, res, pathname.slice('/api/media/'.length));
  }
  if (pathname === '/api/health' && req.method === 'GET') {
    return json(res, 200, {
      ok: true,
      version: PKG.version,
      desktop: process.env.HERONBO_DESKTOP === '1',
      pid: process.pid,
    });
  }
  if (!requireToken(req, res)) return;

  // ── 内置画布 CDP 桥：读模型 / 选中 / 取消选中 ──────────────
  if (pathname === '/api/bridge/probe' && req.method === 'GET') {
    try {
      return json(res, 200, { ok: true, info: await probeCdp() });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/context' && req.method === 'GET') {
    try {
      return json(res, 200, { ok: true, ...(await readBuiltinContext()) });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/screenshot' && req.method === 'GET') {
    try {
      return json(res, 200, await captureBuiltinScreenshot({
        quality: Number(url.searchParams.get('quality')) || 72,
      }));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/click' && req.method === 'POST') {
    const body = await readJsonBody(req);
    try {
      return json(res, 200, await clickBuiltinNormalized(body.nx, body.ny, {
        button: body.button,
        count: body.count,
      }));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/wheel' && req.method === 'POST') {
    const body = await readJsonBody(req);
    try {
      return json(res, 200, await wheelBuiltinNormalized(body.nx, body.ny, Number(body.deltaY) || 0));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/drag' && req.method === 'POST') {
    const body = await readJsonBody(req);
    try {
      return json(res, 200, await dragBuiltinNormalized(body.from || {}, body.to || {}));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/key' && req.method === 'POST') {
    const body = await readJsonBody(req);
    if (!body.key) return fail(res, 400, '缺少 key');
    try {
      return json(res, 200, await keyBuiltinNormalized(body));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/state' && req.method === 'GET') {
    try {
      return json(res, 200, { ok: true, state: await readBuiltinState() });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/model' && req.method === 'GET') {
    try {
      return json(res, 200, { ok: true, ...(await readBuiltinModel()) });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/select' && req.method === 'POST') {
    const body = await readJsonBody(req);
    if (!body.nodeId) return fail(res, 400, '缺少 nodeId');
    try {
      return json(res, 200, await selectBuiltinNode(String(body.nodeId)));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/open' && req.method === 'POST') {
    const body = await readJsonBody(req);
    if (!body.canvasId) return fail(res, 400, '缺少 canvasId');
    if (!body.conversationId) return fail(res, 400, '缺少 conversationId');
    try {
      return json(res, 200, { ok: true, ...(await openBuiltinCanvas({
        canvasId: String(body.canvasId),
        conversationId: String(body.conversationId),
        title: body.title ? String(body.title) : '',
      })) });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/ensure' && req.method === 'POST') {
    const body = await readJsonBody(req);
    if (!body.canvasId) return fail(res, 400, '缺少 canvasId');
    if (!body.conversationId) return fail(res, 400, '缺少 conversationId');
    try {
      const result = await ensureBuiltinCanvas({
        canvasId: String(body.canvasId),
        conversationId: String(body.conversationId),
        title: body.title ? String(body.title) : '',
        context: body.context && typeof body.context === 'object' ? body.context : {},
        // 绝不替用户「拉起」画布：脚本拉起来的是本地画布网页，不是豆包内置画布。
        // 这里只读已经开着的画布，没开就让用户手动点一次。
        autoOpen: false,
      });
      return json(res, 200, {
        ok: true,
        opened: result.opened,
        open: result.open,
        state: result.state,
        model: result.model,
      });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/move' && req.method === 'POST') {
    const body = await readJsonBody(req);
    if (!body.nodeId) return fail(res, 400, '缺少 nodeId');
    try {
      return json(res, 200, await moveBuiltinNode(String(body.nodeId), Number(body.dx) || 0, Number(body.dy) || 0));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }

  // 本地画布前台：只在后台内置画布创建待填节点并写入提示词，不点击“开始创建”。
  // 真正耗点的提交仍由用户确认后再走下一步。
  if (pathname === '/api/builtin/prepare' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const kind = body.kind === 'image' ? 'image' : 'video';
    const prompt = String(body.prompt || '').trim();
    if (!prompt) return fail(res, 400, '提示词不能为空');
    if (!body.conversationId) return fail(res, 400, '缺少画布会话信息');
    try {
      await ensureBuiltinCanvas({
        canvasId: String(body.canvasId),
        conversationId: String(body.conversationId),
        title: body.title ? String(body.title) : '',
        // 绝不替用户「拉起」画布：脚本拉起来的是本地画布网页，不是豆包内置画布。
        // 这里只读已经开着的画布，没开就让用户手动点一次。
        autoOpen: false,
      });
      let itemId = String(body.itemId || '');
      let created = null;
      if (!itemId) {
        created = await createBuiltinGenerationNode(kind);
        itemId = (created.created && created.created[0] && created.created[0].id) || '';
      }
      if (!itemId) throw new Error('内置画布没有返回新节点 id');
      const filled = await fillBuiltinPromptForItem(itemId, prompt);
      const configured = (body.model || body.ratio || body.duration || body.quality)
        ? await configureBuiltinGeneration(itemId, { kind, model: body.model, ratio: body.ratio, duration: body.duration, quality: body.quality })
        : null;
      const plan = builtinPlan({
        kind,
        model: (configured && configured.model) || body.model || '',
        ratio: (configured && configured.ratio) || body.ratio || '',
        duration: (configured && configured.duration) || body.duration || (kind === 'video' ? 5 : 0),
        quality: body.quality || '',
        count: kind === 'image' ? ([1, 2, 4].includes(Number(body.count)) ? Number(body.count) : 1) : 1,
      }, store.getConfig() || {});
      return json(res, 200, {
        ok: true,
        kind,
        itemId,
        created: (created && created.created) || [],
        filled: filled.filled || null,
        configured,
        plan,
        state: filled.state || null,
      });
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/delete' && req.method === 'POST') {
    const body = await readJsonBody(req);
    if (!body.nodeId) return fail(res, 400, '缺少 nodeId');
    try {
      return json(res, 200, await deleteBuiltinNode(String(body.nodeId)));
    } catch (error) {
      return bridgeFail(res, error);
    }
  }
  if (pathname === '/api/bridge/deselect' && req.method === 'POST') {
    try {
      return json(res, 200, await deselectBuiltin());
    } catch (error) {
      return bridgeFail(res, error);
    }
  }

  if (pathname === '/api/config' && req.method === 'GET') {
    const config = store.getConfig();
    const agentProbe = await agentModelCapability(config);
    return json(res, 200, {
      config: {
        gateway: { baseUrl: config.gateway.baseUrl, apiKeySet: Boolean(config.gateway.apiKey || detectApiKey(ROOT)), apiKeyHint: maskKey(config.gateway.apiKey || detectApiKey(ROOT)) },
        gatewayStartScript: config.gatewayStartScript || '',
        doubaoMode: config.doubaoMode || 'doubaowork',
        canvasEntry: config.canvasEntry === 'manual' ? 'manual' : 'cdp',
        doubaoworkExe: config.doubaoworkExe || '',
        downloadDir: config.downloadDir,
        theme: config.theme,
        performanceMode: config.performanceMode,
        agentModel: config.agentModel || '',
        agentModels: agentProbe.models,
        agentModelSelectable: agentProbe.selectable,
        agentModelReason: agentProbe.reason,
      },
      paths: { data: store.dataDir, media: store.mediaDir },
      platform: process.platform,
    });
  }

  if (pathname === '/api/config' && req.method === 'PATCH') {
    const body = await readJsonBody(req);
    const patch = {};
    if (body.gateway && typeof body.gateway === 'object') {
      patch.gateway = {};
      if (typeof body.gateway.baseUrl === 'string') patch.gateway.baseUrl = body.gateway.baseUrl.trim().slice(0, 300) || 'http://127.0.0.1:9090';
      if (typeof body.gateway.apiKey === 'string' && body.gateway.apiKey !== '__KEEP__') patch.gateway.apiKey = body.gateway.apiKey.trim().slice(0, 300);
    }
    if (typeof body.gatewayStartScript === 'string') patch.gatewayStartScript = body.gatewayStartScript.trim().slice(0, 500);
    if (['doubaowork', 'external'].includes(body.doubaoMode)) patch.doubaoMode = body.doubaoMode;
    if (['cdp', 'manual'].includes(body.canvasEntry)) patch.canvasEntry = body.canvasEntry;
    if (typeof body.doubaoworkExe === 'string') patch.doubaoworkExe = body.doubaoworkExe.trim().slice(0, 500);
    if (typeof body.downloadDir === 'string' && body.downloadDir.trim()) patch.downloadDir = body.downloadDir.trim().slice(0, 500);
    if (body.theme === 'dark' || body.theme === 'light') patch.theme = body.theme;
    if (['auto', 'quality', 'speed'].includes(body.performanceMode)) patch.performanceMode = body.performanceMode;
    if (typeof body.agentModel === 'string') patch.agentModel = body.agentModel.trim().slice(0, 80);
    const config = await store.updateConfig(patch);
    return json(res, 200, { ok: true, config: { gateway: { baseUrl: config.gateway.baseUrl, apiKeySet: Boolean(config.gateway.apiKey) }, doubaoMode: config.doubaoMode, canvasEntry: config.canvasEntry === 'manual' ? 'manual' : 'cdp', theme: config.theme, agentModel: config.agentModel || '' } });
  }

  if (pathname === '/api/gateway/status' && req.method === 'GET') {
    const gateway = makeGateway();
    const health = await gateway.health({ canvasId: String(url.searchParams.get('canvasId') || '') });
    if (health.ok) health.login = await gateway.loginStatus();
    return json(res, 200, { ...health, baseUrl: gateway.baseUrl });
  }

  if (pathname === '/api/gateway/open-admin' && req.method === 'POST') {
    const gateway = makeGateway();
    if (gateway.kind === 'doubaowork') {
      try {
        await gateway.show();
        return json(res, 200, { ok: true });
      } catch (error) {
        return fail(res, 502, error.message);
      }
    }
    if (!gateway.apiKey) return fail(res, 400, '还没有配置网关 API Key');
    const url = `${gateway.baseUrl}/admin?key=${encodeURIComponent(gateway.apiKey)}`;
    openWith('cmd.exe', ['/c', 'start', '', url]);
    return json(res, 200, { ok: true });
  }

  if (pathname === '/api/gateway/qr-login' && req.method === 'POST') {
    try {
      const result = await makeGateway().startQrLogin();
      return json(res, 200, result);
    } catch (error) {
      return fail(res, 502, error.message);
    }
  }

  if (pathname === '/api/gateway/qr-login' && req.method === 'GET') {
    try {
      const result = await makeGateway().pollQrLogin();
      return json(res, 200, result);
    } catch (error) {
      return fail(res, 502, error.message);
    }
  }

  // 「帮我打开豆包，我自己点画布」这条通道的准备动作：
  // 只把窗口提到前台 + 右侧工作台展开，不创建任何画布 tab。
  if (pathname === '/api/canvas/prepare' && req.method === 'POST') {
    const body = await readJsonBody(req).catch(() => ({}));
    const config = store.getConfig();
    const port = Number(config.doubaoworkPort) || 9333;
    if ((config.canvasEntry || 'cdp') === 'cdp') {
      return json(res, 200, {
        ok: true,
        skipped: true,
        hasCanvas: false,
        hint: '当前是「纯 CDP · 不开画布」模式：不会自动开画布。需要内置画布时手动打开一次即可。',
      });
    }
    try {
      const gateway = makeGateway();
      if (gateway.kind === 'doubaowork' && !body.dryRun) await gateway.show().catch(() => null);
      const result = await prepareCanvasEntry({ dryRun: Boolean(body.dryRun) }, port);
      return json(res, 200, { ok: true, ...result });
    } catch (error) {
      return json(res, 200, { ok: false, hasCanvas: false, error: error.message, hint: '豆包工作客户端没连上：先点「启动豆包工作」。' });
    }
  }

  // 读当前豆包页绑定的会话 id 和画布 id。前端不再写死这两个，换机器/换会话都能自己认。
  if (pathname === '/api/canvas/target' && req.method === 'GET') {
    const config = store.getConfig();
    const port = Number(config.doubaoworkPort) || 9333;
    try {
      const target = await readCanvasTarget(port);
      return json(res, 200, { ok: true, ...target });
    } catch (error) {
      return json(res, 200, { ok: false, conversationId: '', canvasId: '', title: '', error: error.message });
    }
  }

  // 跨机器排错：豆包开了哪几个页面、每个页面右面板宽度与 React Flow 情况。
  if (pathname === '/api/canvas/diag' && req.method === 'GET') {
    const config = store.getConfig();
    const port = Number(config.doubaoworkPort) || 9333;
    try {
      const report = await diagnoseCanvas(port);
      return json(res, 200, report);
    } catch (error) {
      return json(res, 200, { ok: false, error: error.message });
    }
  }

  if (pathname === '/api/gateway/launch' && req.method === 'POST') {
    const gateway = makeGateway();
    if (gateway.kind === 'doubaowork') {
      try {
        await gateway.show();
        return json(res, 200, { ok: true, message: '已打开豆包工作客户端，请在客户端里登录' });
      } catch (error) {
        return json(res, 200, { ok: false, message: error.message });
      }
    }
    const result = await launchGateway();
    return json(res, result.ok ? 200 : 400, result);
  }

  if (pathname === '/api/canvases' && req.method === 'GET') {
    return json(res, 200, { canvases: await store.listCanvases() });
  }

  if (pathname === '/api/canvases' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const canvas = await store.createCanvas(body.title || '未命名画布');
    return json(res, 200, { canvas });
  }

  const canvasMatch = /^\/api\/canvases\/([a-z0-9-]{6,40})$/i.exec(pathname);
  if (canvasMatch) {
    const id = canvasMatch[1];
    if (req.method === 'GET') {
      const canvas = await store.readCanvas(id);
      if (!canvas) return fail(res, 404, '画布不存在');
      return json(res, 200, { canvas });
    }
    if (req.method === 'PUT') {
      const body = await readJsonBody(req, 24 * 1024 * 1024);
      const canvas = await store.saveCanvas(id, body || {});
      if (!canvas) return fail(res, 404, '画布不存在');
      return json(res, 200, { ok: true, updatedAt: canvas.updatedAt });
    }
    if (req.method === 'DELETE') {
      await store.deleteCanvas(id);
      return json(res, 200, { ok: true });
    }
  }

  if (pathname === '/api/upload' && req.method === 'POST') {
    return handleUpload(req, res);
  }

  if (pathname === '/api/gateway/reset-session' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const canvasId = String(body.canvasId || '');
    if (!canvasId) return fail(res, 400, '缺少画布');
    try {
      await makeGateway().resetSession(canvasId);
      return json(res, 200, { ok: true });
    } catch (error) {
      return fail(res, 500, error.message);
    }
  }

  if (pathname === '/api/agent/plan' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const goal = String(body.goal || '').trim().slice(0, 2000);
    if (!goal) return fail(res, 400, '先写下你想做什么');
    if (tasks.hasRunningForCanvas(String(body.canvasId || ''))) {
      return fail(res, 409, '这个画布正在生成，先等它结束再用「与 AI 对话」：Agent 会占用豆包会话，中途插话会打断生成。');
    }
    const gateway = makeGateway();
    const health = await gateway.health();
    if (!health.ok) {
      return fail(res, 502, health.mode === 'doubaowork'
        ? '豆包工作客户端没启动：点左侧「启动豆包工作」'
        : '连不上本机豆包网关：' + (health.error || '未知原因'));
    }
    if (health.login && health.login.known && health.login.loggedIn === false) {
      return fail(res, 503, health.mode === 'doubaowork'
        ? '豆包工作还没登录：点「打开豆包工作」后在客户端里扫码'
        : '豆包还没登录：点「扫码登录豆包」');
    }
    const context = Array.isArray(body.context && body.context.nodes)
      ? body.context.nodes.slice(0, 30).map((node) => ({
        id: String(node.id || '').slice(0, 40),
        title: String(node.title || '').slice(0, 60),
        kind: String(node.kind || '').slice(0, 20),
        prompt: String(node.prompt || '').slice(0, 240),
      }))
      : [];
    // 素材识别的真相源是磁盘上的画布文档：客户端只报 id/标题，素材明细在这里补齐。
    let canvasDoc = null;
    try { canvasDoc = await store.readCanvas(String(body.canvasId || '')); } catch { canvasDoc = null; }
    const nodeMap = new Map((((canvasDoc || {}).nodes) || []).map((node) => [String(node.id), node]));
    const assetBrief = (node) => (node && Array.isArray(node.assets) ? node.assets : []).slice(0, 6).map((asset) => ({
      kind: String(asset.kind || ''),
      name: String(asset.name || '').slice(0, 50),
      w: Number(asset.w) || 0,
      h: Number(asset.h) || 0,
      duration: Number(asset.duration) || 0,
    }));
    const subjectBrief = (node) => {
      if (!node || node.kind !== 'subject') return null;
      const subject = store.getSubject(String(node.subjectId || ''));
      if (!subject) return null;
      return { name: subject.name, images: (subject.assetIds || []).length, hasVoice: Boolean(subject.audioId) };
    };
    const describeNode = (raw) => {
      const docNode = nodeMap.get(String(raw.id || ''));
      const assets = assetBrief(docNode);
      const subject = subjectBrief(docNode);
      return {
        id: String(raw.id || '').slice(0, 40),
        title: String((docNode && docNode.title) || raw.title || '').slice(0, 60),
        kind: String((docNode && docNode.kind) || raw.kind || '').slice(0, 20),
        assets,
        subject,
        has: {
          image: assets.some((asset) => asset.kind === 'image'),
          video: assets.some((asset) => asset.kind === 'video'),
          audio: assets.some((asset) => asset.kind === 'audio'),
        },
      };
    };
    const mentionedRaw = (body.context && body.context.mentioned) || body.mentioned;
    const mentionedIds = Array.isArray(mentionedRaw)
      ? mentionedRaw.map((id) => String(id || '')).filter(Boolean).slice(0, 8)
      : [];
    const mentioned = mentionedIds
      .map((id) => nodeMap.get(id))
      .filter(Boolean)
      .map((node) => describeNode({ id: node.id, title: node.title, kind: node.kind }));
    // 画布上所有带素材的节点：让模型知道"手头有哪些素材可用"。
    const inventory = [...nodeMap.values()]
      .filter((node) => (node.assets || []).length || node.kind === 'subject')
      .slice(0, 20)
      .map((node) => {
        const brief = describeNode({ id: node.id, title: node.title, kind: node.kind });
        return {
          id: brief.id,
          title: brief.title,
          kind: brief.kind,
          assets: brief.assets,
          subject: brief.subject,
        };
      });
    const subjectList = store.listSubjects().slice(0, 12).map((subject) => ({
      id: subject.id,
      name: subject.name,
      images: (subject.assetIds || []).length,
      hasVoice: Boolean(subject.audioId),
    }));
    const selected = Array.isArray(body.context && body.context.selected)
      ? body.context.selected.slice(0, 8).map((node) => ({
        ...describeNode(node),
        prompt: String(node.prompt || '').slice(0, 1200),
        text: String(node.text || '').slice(0, 1200),
        ratio: String(node.ratio || '').slice(0, 12),
        params: node.params && typeof node.params === 'object' ? node.params : {},
        upstream: Array.isArray(node.upstream) ? node.upstream.slice(0, 8).map((name) => String(name).slice(0, 40)) : [],
      }))
      : [];
    const edgeList = Array.isArray(body.context && body.context.edges)
      ? body.context.edges.slice(0, 60).map((edge) => ({
        from: String(edge.from || '').slice(0, 40),
        to: String(edge.to || '').slice(0, 40),
      }))
      : [];
    const mode = body.mode === 'prompt' ? 'prompt' : 'plan';
    const planSystem = [
      '你是「HeronBo 豆包画布」的创作助理。你可以（a）把目标拆成新节点，或（b）直接改写用户选中的节点。',
      '只输出一个 JSON 对象，不要 Markdown、不要解释。',
      'JSON 结构：{"summary":"一句话说明","nodes":[{"key":"a","kind":"image","title":"首帧","prompt":"...","text":"","ratio":"16:9","ref":null}],"updates":[{"nodeId":"真实节点id","title":"可选新标题","prompt":"可选新提示词","text":"可选新文案"}],"autoRun":["a"]}',
      '规则：',
      '- kind 只能是 image / video / text。text 节点用 text 字段写文案；image/video 用 prompt 字段。',
      '- ref 指向上游节点的 key，只能引用数组里更早的节点；没有上游就 null。',
      '- 分镜类目标：先 image 首帧，再 video 镜头；视频 prompt 写清镜头运动、节奏、光线。',
      '- 节点数 1~6 个；提示词要具体、可直接给豆包用。',
      '- updates 只能引用「当前选中的节点」里给出的 nodeId，不要编造 id。',
      '- 提示词里引用参考素材名字时要**原样照抄**（例如 @晓艳1、@1四级真题详解+模拟预测），不要在 @ 与名字之间、名字中间加空格。',
      '- @名字 指的是画布上的某个节点（它带着具体素材，明细见「画布现有素材」的 assets）。用户用 @ 指定素材时，提示词里必须原样保留这个 @名字，不要改写成文件名或泛泛的描述。',
      '- 想用某个已有素材节点做参考：直接在 prompt 里写 @它的标题即可（画布会自动把它接成上游参考）；nodes 里的 ref 只能指向本次新建节点的 key，不能填已有节点 id。',
      '- 只能用「画布现有素材」「主体库」里真实存在的名字，不要编造素材名、不要写本地文件路径。',
      '- 用户说“改写/优化/润色/扩写这个提示词”“换个风格”“帮我写提示词”时：只输出 updates，nodes 留空数组。',
      '- 用户要新增内容时：输出 nodes；除非他明确要求，不要顺手改已有节点。',
      '- autoRun 填建议立即生成的节点 key（用户明确说“直接生成/出图/出片”时）；不确定就空数组。',
      '- 不要输出画布以外的内容。',
    ].join('\n');
    // 只出提示词：不建节点、不改画布，直接给 1~4 条可复制的提示词。
    const promptSystem = [
      '你是「HeronBo 豆包画布」的提示词写手。用户只要提示词，不要画布结构、不要建节点。',
      '只输出一个 JSON 对象，不要 Markdown、不要解释。',
      'JSON 结构：{"summary":"一句话说明","prompts":[{"title":"这条提示词的用途，≤8 字","kind":"image|video","prompt":"完整提示词正文"}]}',
      '规则：',
      '- prompts 给 1~4 条，每条都是可以直接粘进生成框、完整可用的中文一段式正文。',
      '- 正文里不要出现画幅 / 清晰度 / 时长 / 张数（这些由画布单独传参）。',
      '- 需要参考素材时用 @素材名 引用，名字必须来自「画布现有素材」「主体库」，不要编造、不要写文件路径。',
      '- 需要保持人物/商品一致时，写明「以参考图为准」，不要自己手写外观去覆盖参考图。',
      '- 每条 80~320 字，具体可拍，不要空话；不要输出参数表格或分点清单。',
      '- @名字 原样照抄，不要在 @ 与名字之间、名字中间加空格。',
    ].join('\n');
    const system = mode === 'prompt' ? promptSystem : planSystem;
    // 提示词 skill：选了具体 skill 就把它的口径整段喂进去；auto 给索引让模型自己套。
    const wantedSkill = skills.get(body.skill);
    const skillBlock = wantedSkill
      ? `\n\n【本次必须遵循的提示词 skill：${wantedSkill.name}】\n${wantedSkill.body}`
      : `\n\n【可选提示词 skill（按任务类型自行套用对应口径，不要在输出里提 skill 名字）】\n${skills.indexText()}`;
    const user = [
      `目标：${goal}`,
      `用户用 @ 明确指定的节点：${JSON.stringify(mentioned)}`,
      `当前选中节点（可被 updates 改写）：${JSON.stringify(selected)}`,
      `画布全部节点：${JSON.stringify(context)}`,
      `画布现有素材（标题 + 素材明细）：${JSON.stringify(inventory)}`,
      `主体库：${JSON.stringify(subjectList)}`,
      `连线（from→to）：${JSON.stringify(edgeList)}`,
    ].join('\n');
    let raw;
    const chatStarted = Date.now();
    const chatModel = agentModel();
    const charsIn = system.length + skillBlock.length + user.length;
    try {
      raw = await gateway.chat({
        messages: [{ role: 'system', content: system + skillBlock }, { role: 'user', content: user }],
        model: chatModel,
        temperature: 0.7,
        canvasId: String(body.canvasId || ''),
      });
    } catch (error) {
      recordChat({ action: mode, canvasId: body.canvasId, model: chatModel, startedAt: chatStarted, charsIn, gateway, status: 'error', error: error.message });
      return fail(res, 502, error.message);
    }
    recordChat({ action: mode, canvasId: body.canvasId, model: chatModel, startedAt: chatStarted, raw, charsIn, gateway });
    const knownNames = [...new Set([
      ...selected.flatMap((node) => node.upstream || []),
      ...context.map((node) => node.title),
      ...mentioned.map((node) => node.title),
      ...inventory.map((node) => node.title),
    ])].filter(Boolean);
    if (mode === 'prompt') {
      try {
        const result = normalizeAgentPrompts(raw);
        for (const item of result.prompts) {
          if (knownNames.length) item.prompt = normalizeMentions(item.prompt, knownNames);
        }
        return json(res, 200, { mode, summary: result.summary, prompts: result.prompts, skill: wantedSkill ? wantedSkill.id : 'auto', raw: raw.slice(0, 4000) });
      } catch (error) {
        return json(res, 200, { mode, prompts: null, skill: wantedSkill ? wantedSkill.id : 'auto', error: error.message, raw: raw.slice(0, 4000) });
      }
    }
    try {
      const plan = normalizeAgentPlan(raw, selected.map((node) => node.id));
      // 计划里的提示词同样洗一遍 @名字（素材名来自选中节点的上游 + 画布节点标题）。
      if (knownNames.length) {
        for (const update of plan.updates || []) {
          if (update.prompt) update.prompt = normalizeMentions(update.prompt, knownNames);
          if (update.text) update.text = normalizeMentions(update.text, knownNames);
        }
        for (const node of plan.nodes || []) {
          if (node.prompt) node.prompt = normalizeMentions(node.prompt, knownNames);
          if (node.text) node.text = normalizeMentions(node.text, knownNames);
        }
      }
      return json(res, 200, { mode, plan, skill: wantedSkill ? wantedSkill.id : 'auto', raw: raw.slice(0, 4000) });
    } catch (error) {
      return json(res, 200, { mode, plan: null, skill: wantedSkill ? wantedSkill.id : 'auto', error: error.message, raw: raw.slice(0, 4000) });
    }
  }

  // 单条提示词改写：生成面板上「用 skill 优化」按钮走这里，不建节点、不动画布结构。
  if (pathname === '/api/agent/rewrite' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const prompt = String(body.prompt || '').trim().slice(0, 4000);
    if (!prompt) return fail(res, 400, '提示词不能为空');
    const kind = body.kind === 'video' ? 'video' : 'image';
    if (tasks.hasRunningForCanvas(String(body.canvasId || ''))) {
      return fail(res, 409, '这个画布正在生成，等它结束再优化提示词（优化会占用豆包会话）。');
    }
    const gateway = makeGateway();
    const health = await gateway.health();
    if (!health.ok) {
      return fail(res, 502, health.mode === 'doubaowork'
        ? '豆包工作客户端没启动：点左侧「启动豆包工作」'
        : '连不上本机豆包网关：' + (health.error || '未知原因'));
    }
    if (health.login && health.login.known && health.login.loggedIn === false) {
      return fail(res, 503, health.mode === 'doubaowork'
        ? '豆包工作还没登录：点「打开豆包工作」后在客户端里扫码'
        : '豆包还没登录：点「扫码登录豆包」');
    }
    const wantedSkill = skills.get(body.skill);
    const system = [
      '你是提示词编辑，负责把用户给的提示词改写成可直接投喂生成模型的一条中文提示词。',
      '只输出改写后的正文：一整段（首尾不要引号）、不分点、不加小标题、不解释、不写“以下是”。',
      `目标类型：${kind === 'video' ? '视频（首帧图或参考素材已经给模型，动作/运镜/节奏要写清）' : '图片（Seedream 出图）'}。`,
      '画幅、清晰度、时长、张数不要写进正文——程序会单独传这些参数，写进去只会打架。',
      '保留原文里已经正确的 @素材名 和具体设定，不要自己新增素材、不要编造文件名。',
      wantedSkill
        ? `【必须遵循的提示词 skill：${wantedSkill.name}】\n${wantedSkill.body}`
        : `【按任务类型套用下面 skill 的口径】\n${skills.indexText()}`,
    ].join('\n');
    const contextLines = [];
    if (body.title) contextLines.push('节点标题：' + String(body.title).slice(0, 60));
    if (Array.isArray(body.upstream) && body.upstream.length) {
      contextLines.push('可用参考素材：' + body.upstream.slice(0, 8).map((name) => String(name).slice(0, 40)).join('、'));
    }
    if (body.note) contextLines.push('用户补充要求：' + String(body.note).slice(0, 400));
    const user = (contextLines.length ? contextLines.join('\n') + '\n\n' : '') + '原提示词：\n' + prompt;
    let raw;
    const chatStarted = Date.now();
    const chatModel = agentModel();
    const charsIn = system.length + user.length;
    try {
      raw = await gateway.chat({
        messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
        model: chatModel,
        temperature: 0.6,
        canvasId: String(body.canvasId || ''),
      });
    } catch (error) {
      recordChat({ action: 'rewrite', canvasId: body.canvasId, model: chatModel, startedAt: chatStarted, charsIn, gateway, status: 'error', error: error.message });
      return fail(res, 502, error.message);
    }
    recordChat({ action: 'rewrite', canvasId: body.canvasId, model: chatModel, startedAt: chatStarted, raw, charsIn, gateway });
    const cleaned = normalizeMentions(cleanPromptText(raw), body.upstream);
    if (!cleaned) return fail(res, 502, '模型没有返回可用的提示词');
    return json(res, 200, { prompt: cleaned, skill: wantedSkill ? wantedSkill.id : 'auto' });
  }

  // 消耗账本：最近的调用明细 + 汇总，给「设置 → 消耗记录」用。
  if (pathname === '/api/ledger' && req.method === 'GET') {
    const limit = Math.min(1000, Math.max(1, Number(url.searchParams.get('limit')) || 200));
    const rows = ledger.recent(limit);
    return json(res, 200, { rows, summary: ledger.summary(rows), path: ledger.file });
  }

  if (pathname === '/api/skills' && req.method === 'GET') {
    return json(res, 200, { skills: skills.list() });
  }

  // 点数预估表：默认值 + 用户在 config.json 里的覆盖
  if (pathname === '/api/pricing' && req.method === 'GET') {
    const custom = (store.getConfig() || {}).pricing || {};
    const merged = {
      note: custom.note || DEFAULT_PRICING.note,
      image: {
        perSheet: { ...DEFAULT_PRICING.image.perSheet, ...(custom.image && custom.image.perSheet) },
        modelFactor: { ...DEFAULT_PRICING.image.modelFactor, ...(custom.image && custom.image.modelFactor) },
      },
      video: {
        perSecond: {
          ...DEFAULT_PRICING.video.perSecond,
          ...(custom.video && custom.video.perSecond),
        },
      },
    };
    return json(res, 200, { pricing: merged });
  }

  if (pathname === '/api/generate' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const kind = body.kind === 'video' ? 'video' : 'image';
    const prompt = String(body.prompt || '').trim();
    if (!prompt) return fail(res, 400, '提示词不能为空');
    // canvasId 允许为空：内置画布模式走的是「当前打开的这张画布」，
    // 别人的机器上不一定能读出画布 id，不能因为读不到就拒绝提交。
    if (!body.nodeId) return fail(res, 400, '缺少节点信息');
    const rawParams = body.params && typeof body.params === 'object' ? body.params : {};
    if (body.engine === 'builtin') {
      const task = tasks.create({
        engine: 'builtin',
        kind,
        prompt,
        ratio: body.ratio,
        quality: ['1K', '2K', '4K', '480P', '720P', '1080P'].includes(rawParams.quality) ? rawParams.quality : '',
        model: typeof rawParams.model === 'string' ? rawParams.model.trim().slice(0, 40) : '',
        count: [1, 2, 4].includes(Number(rawParams.count)) ? Number(rawParams.count) : 1,
        duration: Math.min(15, Math.max(4, Math.round(Number(rawParams.duration) || 5))),
        nodeId: body.nodeId,
        canvasId: body.canvasId,
        conversationId: String(body.conversationId || ''),
        conversationTitle: String(body.conversationTitle || ''),
        builtinItemId: String(body.builtinItemId || ''),
      });
      return json(res, 200, { task: tasks.publicView(task), engine: 'builtin' });
    }
    const health = await makeGateway().health();
    if (!health.ok) {
      return fail(res, 502, health.mode === 'doubaowork'
        ? '豆包工作客户端没启动：点左侧「启动豆包工作」'
        : '连不上本机豆包网关：' + (health.error || '未知原因'));
    }
    if (health.login && health.login.known && health.login.loggedIn === false) {
      return fail(res, 503, health.mode === 'doubaowork'
        ? '豆包工作还没登录：点「打开豆包工作」后在客户端里扫码'
        : '豆包还没登录：点「扫码登录豆包」');
    }
    const params = body.params && typeof body.params === 'object' ? body.params : {};
    // 主体：把主体库里的参考图合并进参考素材，并把名字写进提示词。
    const subjectIds = Array.isArray(body.subjectIds) ? body.subjectIds.slice(0, 6).map(String) : [];
    const subjectRefs = [];
    const subjectNames = [];
    for (const sid of subjectIds) {
      const subject = store.getSubject(sid);
      if (!subject) continue;
      subjectNames.push(subject.name);
      for (const assetId of subject.assetIds) if (!subjectRefs.includes(assetId)) subjectRefs.push(assetId);
      // 声音只有做视频才有意义；图片任务带音频会占名额、还会被当成读不了的参考。
      if (kind === 'video' && subject.audioId && !subjectRefs.includes(subject.audioId)) subjectRefs.push(subject.audioId);
    }
    const refAssetIds = [];
    for (const raw of [...(Array.isArray(body.refAssetIds) ? body.refAssetIds : []), ...subjectRefs]) {
      const id = String(raw || '');
      if (id && !refAssetIds.includes(id)) refAssetIds.push(id);
    }
    const finalPrompt = subjectNames.length
      ? prompt + '\n\n主体：' + subjectNames.join('、') + '（严格保持该主体的外观 / 长相 / 服饰一致，见参考图）'
      : prompt;
    const task = tasks.create({
      kind,
      prompt: finalPrompt,
      ratio: body.ratio,
      quality: ['1K', '2K', '4K', '480P', '720P', '1080P'].includes(params.quality) ? params.quality : '',
      model: typeof params.model === 'string' ? params.model.trim().slice(0, 40) : '',
      count: [1, 2, 4].includes(Number(params.count)) ? Number(params.count) : 1,
      duration: Math.min(30, Math.max(4, Math.round(Number(params.duration) || 5))),
      nodeId: body.nodeId,
      canvasId: body.canvasId,
      // 按类型上限（图 6 / 视频 1 / 音频 1）在 tasks.prepareRef 里把关；这里留足余量，
      // 免得音频排在最后时被一刀切掉。
      refAssetIds: refAssetIds.slice(0, 12),
    });
    return json(res, 200, { task: tasks.publicView(task) });
  }

  if (pathname === '/api/collect' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const kind = body.kind === 'video' ? 'video' : 'image';
    const gateway = makeGateway();
    const health = await gateway.health();
    if (!health.ok) return fail(res, 502, '豆包工作客户端没启动：先启动客户端再取回');
    if (!gateway.collect) return fail(res, 400, '当前网关不支持取回结果');
    try {
      const sid = String(body.sid || '').slice(0, 40);
      // 先走直连 CDP：不依赖桥接的会话接管，最稳。
      let grabbed = [];
      const port = Number(store.getConfig().doubaoworkPort) || 9333;
      grabbed = await grabLatest({ port, sid, kind }).catch(() => []);
      if (grabbed.length) {
        const assets = [];
        for (const item of grabbed) {
          const asset = await store.saveMedia(item.buffer, {
            mime: item.mime, kind, url: item.url || '',
            w: item.w || 0, h: item.h || 0, duration: item.duration || 0, source: 'collected',
          });
          assets.push(publicAsset(asset));
        }
        return json(res, 200, { assets });
      }
      const payload = await gateway.collect(String(body.canvasId || ''), kind, String(body.match || '').slice(0, 40), sid);
      const assets = [];
      for (const item of payload.data || []) {
        const downloaded = await gateway.download(item.url);
        const asset = await store.saveMedia(downloaded.buffer, {
          mime: downloaded.mime || item.mime || '',
          kind,
          url: item.url,
          name: item.name || '',
          w: Number(item.width) || 0,
          h: Number(item.height) || 0,
          duration: Number(item.duration) || 0,
          source: 'collected',
        });
        assets.push(publicAsset(asset));
      }
      return json(res, 200, { assets });
    } catch (error) {
      return fail(res, 502, error.message);
    }
  }
  const taskMatch = /^\/api\/tasks\/([a-z0-9]+)$/i.exec(pathname);
  if (taskMatch && req.method === 'GET') {
    const task = tasks.get(taskMatch[1]);
    if (!task) return fail(res, 404, '任务不存在（可能已过期）');
    return json(res, 200, { task: tasks.publicView(task) });
  }
  const cancelMatch = /^\/api\/tasks\/([a-z0-9]+)\/cancel$/i.exec(pathname);
  if (cancelMatch && req.method === 'POST') {
    const ok = tasks.cancel(cancelMatch[1]);
    return json(res, 200, { ok });
  }

  if (pathname === '/api/download' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const asset = store.getAsset(body.assetId);
    if (!asset) return fail(res, 404, '素材不存在');
    const dir = store.getConfig().downloadDir || path.join(store.dataDir, 'downloads');
    await fsp.mkdir(dir, { recursive: true });
    const base = safeName(asset.name || `doubao-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}`, 'doubao');
    const ext = path.extname(asset.file) || '.bin';
    const name = path.extname(base) ? base : base + ext;
    let target = path.join(dir, name);
    let index = 1;
    while (fs.existsSync(target)) {
      target = path.join(dir, `${path.basename(name, path.extname(name))}-${index}${path.extname(name)}`);
      index += 1;
    }
    await fsp.copyFile(store.assetPath(asset), target);
    return json(res, 200, { ok: true, path: target, dir });
  }

  if (pathname === '/api/open-download-dir' && req.method === 'POST') {
    const dir = store.getConfig().downloadDir || path.join(store.dataDir, 'downloads');
    await fsp.mkdir(dir, { recursive: true });
    openWith('explorer.exe', [dir]);
    return json(res, 200, { ok: true, path: dir });
  }

  if (pathname === '/api/stats' && req.method === 'GET') {
    return json(res, 200, { media: await store.mediaStats(), tasks: tasks.list().length });
  }

  if (pathname === '/api/media/clean' && req.method === 'POST') {
    const body = await readJsonBody(req);
    const days = Number(body.olderThanDays) || 0;
    const removed = await store.cleanMedia({ olderThanMs: days > 0 ? days * 86400000 : 0 });
    return json(res, 200, { ok: true, removed, media: await store.mediaStats() });
  }

  return fail(res, 404, '接口不存在');
}

function maskKey(key) {
  const text = String(key || '');
  if (!text) return '';
  if (text.length <= 8) return '****';
  return text.slice(0, 4) + '…' + text.slice(-4);
}

function openWith(exe, args) {
  try {
    const child = spawn(exe, args, { detached: true, stdio: 'ignore', windowsHide: false });
    child.unref();
  } catch (error) {
    logLine('打开失败：', error.message);
  }
}

async function launchGateway() {
  const config = store.getConfig();
  const candidates = [
    config.gatewayStartScript,
    path.join(ROOT, '..', 'doubao2api', 'start.bat'),
    path.join(ROOT, '..', 'doubao2api', 'start_watchdog.bat'),
  ].filter(Boolean);
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const child = spawn('cmd.exe', ['/c', file], {
        cwd: path.dirname(file),
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      });
      child.unref();
      logLine('已启动豆包网关：', file);
      return { ok: true, script: file, message: '已启动 doubao2api，等它登录完成后点「检测」' };
    } catch (error) {
      logLine('启动网关失败：', error.message);
    }
  }
  return { ok: false, message: '没找到 doubao2api 的 start.bat；在设置里填一下「网关启动脚本」路径' };
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || HOST}`);
  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE', 'Access-Control-Allow-Headers': 'content-type,x-canvas-token,x-filename' });
    return res.end();
  }
  if (url.pathname.startsWith('/api/')) {
    handleApi(req, res, url).catch((error) => {
      const status = error && error.status ? error.status : 500;
      if (status >= 500) logLine('接口出错：', error && error.stack || error);
      fail(res, status, error && error.message || '服务器内部错误');
    });
    return;
  }
  serveStatic(req, res, url.pathname).catch((error) => fail(res, 500, error.message));
});

async function main() {
  await store.init();
  if (!store.getConfig().gateway.apiKey) {
    const detected = detectApiKey(ROOT);
    if (detected) {
      await store.updateConfig({ gateway: { ...store.getConfig().gateway, apiKey: detected } });
      logLine('已自动读取豆包网关 API Key');
    }
  }

  server.listen(PORT, HOST, () => {
    const url = `http://${HOST}:${PORT}/`;
    logLine('豆包无限画布已启动：' + url);
    const startupConfig = store.getConfig();
    logLine('数据目录：' + store.dataDir);
    logLine('豆包接入：' + ((startupConfig.doubaoMode || 'doubaowork') === 'doubaowork'
      ? '豆包工作客户端（CDP 自动接管）'
      : '外部网关 ' + startupConfig.gateway.baseUrl));
    console.log('\n  ⌁ 这是本地绿色版：所有画布和素材都在 data\\ 目录，不上传任何云端\n');
    if (process.env.OPEN === '1') openWith('cmd.exe', ['/c', 'start', '', url]);
  });

  server.on('error', (error) => {
    if (error.code === 'EADDRINUSE') {
      console.error(`端口 ${PORT} 被占用了。关掉占用它的程序，或者用 PORT=9191 npm start 换一个端口。`);
      process.exit(1);
    }
    throw error;
  });
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    logLine('正在退出…');
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  });
}

main().catch((error) => {
  console.error('启动失败：', error);
  process.exit(1);
});
