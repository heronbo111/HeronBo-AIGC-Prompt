const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const crypto = require('node:crypto');
const { writeJsonAtomic, readJson, sha256, safeName } = require('./json-file');

const MIME_EXT = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
  'image/gif': 'gif',
  'image/avif': 'avif',
  'video/mp4': 'mp4',
  'video/webm': 'webm',
  'video/quicktime': 'mov',
  'audio/mpeg': 'mp3',
  'audio/mp3': 'mp3',
  'audio/mp4': 'm4a',
  'audio/x-m4a': 'm4a',
  'audio/wav': 'wav',
  'audio/x-wav': 'wav',
  'audio/aac': 'aac',
  'audio/ogg': 'ogg',
  'audio/flac': 'flac',
  'audio/x-flac': 'flac',
};

function sniffKind(buffer, mime) {
  const m = String(mime || '');
  if (m.startsWith('video/')) return 'video';
  if (m.startsWith('image/')) return 'image';
  if (m.startsWith('audio/')) return 'audio';
  if (buffer.length > 12) {
    if (buffer[0] === 0x89 && buffer[1] === 0x50 && buffer[2] === 0x4e && buffer[3] === 0x47) return 'image';
    if (buffer[0] === 0xff && buffer[1] === 0xd8) return 'image';
    if (buffer.subarray(0, 4).toString('ascii') === 'RIFF' && buffer.subarray(8, 12).toString('ascii') === 'WEBP') return 'image';
    if (buffer.subarray(0, 3).toString('ascii') === 'GIF') return 'image';
    if (buffer.subarray(4, 8).toString('ascii') === 'ftyp') return 'video';
    if (buffer[0] === 0x1a && buffer[1] === 0x45 && buffer[2] === 0xdf && buffer[3] === 0xa3) return 'video';
    // 音频：ID3 标签 / WAVE / OggS / fLaC / ADTS-AAC / ftypM4A
    if (buffer.subarray(0, 3).toString('ascii') === 'ID3') return 'audio';
    if (buffer.subarray(0, 4).toString('ascii') === 'RIFF' && buffer.subarray(8, 12).toString('ascii') === 'WAVE') return 'audio';
    if (buffer.subarray(0, 4).toString('ascii') === 'OggS') return 'audio';
    if (buffer.subarray(0, 4).toString('ascii') === 'fLaC') return 'audio';
    if (buffer[0] === 0xff && (buffer[1] & 0xf6) === 0xf0) return 'audio';
    if (buffer.subarray(4, 8).toString('ascii') === 'ftyp' && buffer.subarray(8, 11).toString('ascii') === 'M4A') return 'audio';
  }
  return 'file';
}

function extFor(mime, kind, url) {
  const fromMime = MIME_EXT[String(mime || '').toLowerCase()];
  if (fromMime) return fromMime;
  try {
    const ext = path.extname(new URL(url).pathname).replace('.', '').toLowerCase();
    if (/^[a-z0-9]{2,5}$/.test(ext)) return ext;
  } catch { /* ignore */ }
  return kind === 'video' ? 'mp4' : kind === 'image' ? 'png' : kind === 'audio' ? 'mp3' : 'bin';
}

class Store {
  constructor(rootDir) {
    this.root = rootDir;
    this.dataDir = path.join(rootDir, 'data');
    this.canvasDir = path.join(this.dataDir, 'canvases');
    this.mediaDir = path.join(this.dataDir, 'media');
    this.configFile = path.join(this.dataDir, 'config.json');
    this.mediaIndexFile = path.join(this.mediaDir, 'index.json');
    this.subjectFile = path.join(this.dataDir, 'subjects.json');
    this.config = null;
    this.media = null;
    this.subjects = [];
    this.canvasWriteQueue = new Map();
  }

  async init() {
    await fsp.mkdir(this.canvasDir, { recursive: true });
    await fsp.mkdir(this.mediaDir, { recursive: true });
    let loaded = {};
    try {
      const text = await fsp.readFile(this.configFile, 'utf8');
      loaded = JSON.parse(text);
    } catch (error) {
      if (error && error.code !== 'ENOENT') {
        // 配置坏了不要静默覆盖：留一份原件，再用默认值启动，用户还能找回手改的内容。
        const backup = `${this.configFile}.bad-${Date.now()}`;
        await fsp.rename(this.configFile, backup).catch(() => {});
        console.warn(`[config] config.json 解析失败（${error.message}），已备份到 ${backup}，本次使用默认配置`);
      }
    }
    this.config = { ...this.defaultConfig(), ...(loaded && typeof loaded === 'object' ? loaded : {}) };
    this.media = await readJson(this.mediaIndexFile, { assets: {} });
    if (!this.media || typeof this.media.assets !== 'object') this.media = { assets: {} };
    const subjects = await readJson(this.subjectFile, { subjects: [] });
    this.subjects = Array.isArray(subjects && subjects.subjects) ? subjects.subjects.filter((item) => item && item.id) : [];
  }

  defaultConfig() {
    return {
      gateway: { baseUrl: 'http://127.0.0.1:9090', apiKey: '' },
      gatewayStartScript: '',
      doubaoMode: 'doubaowork',
      doubaoworkExe: '',
      downloadDir: path.join(this.dataDir, 'downloads'),
      theme: 'light',
      performanceMode: 'auto',
    };
  }

  getConfig() {
    return this.config;
  }

  async updateConfig(patch) {
    const next = { ...this.config, ...patch };
    next.gateway = { ...this.config.gateway, ...(patch.gateway || {}) };
    this.config = next;
    await writeJsonAtomic(this.configFile, this.config, { pretty: true });
    return this.config;
  }

  // ── 画布 ────────────────────────────────────────────────

  canvasFile(id) {
    return path.join(this.canvasDir, `${id}.json`);
  }

  async listCanvases() {
    const items = [];
    const names = await fsp.readdir(this.canvasDir).catch(() => []);
    for (const name of names) {
      if (!name.endsWith('.json') || name.startsWith('.')) continue;
      const data = await readJson(path.join(this.canvasDir, name), null);
      if (!data || !data.id) continue;
      items.push({
        id: data.id,
        title: data.title || '未命名画布',
        nodeCount: Array.isArray(data.nodes) ? data.nodes.length : 0,
        edgeCount: Array.isArray(data.edges) ? data.edges.length : 0,
        createdAt: data.createdAt || 0,
        updatedAt: data.updatedAt || 0,
        cover: this.coverOf(data),
      });
    }
    items.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    return items;
  }

  coverOf(data) {
    for (const node of data.nodes || []) {
      const asset = (node.assets || []).find((item) => item && item.id);
      if (asset && asset.kind === 'image') return `/api/media/${asset.id}`;
    }
    return '';
  }

  async readCanvas(id) {
    if (!/^[a-z0-9-]{6,40}$/i.test(String(id || ''))) return null;
    return readJson(this.canvasFile(id), null);
  }

  async createCanvas(title = '未命名画布') {
    const id = 'c' + crypto.randomBytes(9).toString('hex');
    const now = Date.now();
    const canvas = {
      id,
      title: String(title || '未命名画布').slice(0, 60),
      createdAt: now,
      updatedAt: now,
      viewport: { x: 120, y: 120, scale: 1 },
      nodes: [],
      edges: [],
    };
    await writeJsonAtomic(this.canvasFile(id), canvas);
    return canvas;
  }

  async saveCanvas(id, incoming) {
    const current = await this.readCanvas(id);
    if (!current) return null;
    const canvas = {
      id,
      title: String(incoming.title || current.title || '未命名画布').slice(0, 60),
      createdAt: current.createdAt,
      updatedAt: Date.now(),
      viewport: this.sanitizeViewport(incoming.viewport || current.viewport),
      nodes: incoming.nodes === undefined ? current.nodes : this.sanitizeNodes(incoming.nodes),
      edges: incoming.edges === undefined ? current.edges : this.sanitizeEdges(incoming.edges),
    };
    // 同一张画布串行写，避免两次保存交错把文件写成半新半旧。
    const prev = this.canvasWriteQueue.get(id) || Promise.resolve();
    const job = prev.catch(() => {}).then(() => writeJsonAtomic(this.canvasFile(id), canvas));
    this.canvasWriteQueue.set(id, job);
    await job;
    this.canvasWriteQueue.delete(id);
    return canvas;
  }

  // ── 主体库（人物 / 商品 / 风格，用素材当参考图） ──────────
  listSubjects() {
    return this.subjects.slice().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  }

  getSubject(id) {
    return this.subjects.find((item) => item.id === id) || null;
  }

  sanitizeSubjectAssets(assetIds) {
    const out = [];
    for (const raw of Array.isArray(assetIds) ? assetIds.slice(0, 6) : []) {
      const id = String(raw || '');
      if (this.media.assets[id] && !out.includes(id)) out.push(id);
    }
    return out;
  }

  async saveSubjects() {
    await writeJsonAtomic(this.subjectFile, { subjects: this.subjects }, { pretty: true });
  }

  sanitizeSubjectAudio(audioId) {
    const asset = this.media.assets[String(audioId || '')];
    return asset && asset.kind === 'audio' ? asset.id : '';
  }

  async createSubject({ name, assetIds, note, audioId, draft = false }) {
    const assets = this.sanitizeSubjectAssets(assetIds);
    if (!assets.length && !draft) throw Object.assign(new Error('主体至少要有一张形象参考图'), { status: 400 });
    const subject = {
      id: 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      name: String(name || '').trim().slice(0, 40) || '未命名主体',
      note: String(note || '').trim().slice(0, 200),
      assetIds: assets,
      audioId: this.sanitizeSubjectAudio(audioId),
      coverId: assets[0] || '',
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    this.subjects.push(subject);
    await this.saveSubjects();
    return subject;
  }

  async updateSubject(id, patch) {
    const subject = this.getSubject(id);
    if (!subject) return null;
    if (typeof patch.name === 'string' && patch.name.trim()) subject.name = patch.name.trim().slice(0, 40);
    if (typeof patch.note === 'string') subject.note = patch.note.trim().slice(0, 200);
    if (Array.isArray(patch.assetIds)) {
      const assets = this.sanitizeSubjectAssets(patch.assetIds);
      if (assets.length) { subject.assetIds = assets; subject.coverId = assets[0]; }
    }
    if ('audioId' in patch) subject.audioId = this.sanitizeSubjectAudio(patch.audioId);
    subject.updatedAt = Date.now();
    await this.saveSubjects();
    return subject;
  }

  async deleteSubject(id) {
    const index = this.subjects.findIndex((item) => item.id === id);
    if (index < 0) return false;
    this.subjects.splice(index, 1);
    await this.saveSubjects();
    return true;
  }

  listAssets() {
    const items = Object.values(this.media.assets || {});
    return items.slice().sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  }

  sanitizeViewport(viewport) {
    const source = viewport && typeof viewport === 'object' ? viewport : {};
    const scale = Number(source.scale);
    return {
      x: Number.isFinite(Number(source.x)) ? Number(source.x) : 0,
      y: Number.isFinite(Number(source.y)) ? Number(source.y) : 0,
      scale: Number.isFinite(scale) ? Math.min(4, Math.max(0.05, scale)) : 1,
    };
  }

  /** 生成参数：白名单 + 范围校验，和前端 nodeParams 的默认值保持一致。 */
  sanitizeParams(raw) {
    const source = raw && typeof raw === 'object' ? raw : {};
    const model = typeof source.model === 'string' && /^[a-z0-9._-]{2,40}$/.test(source.model) ? source.model : '';
    const quality = ['1K', '2K', '4K', '480P', '720P', '1080P'].includes(source.quality) ? source.quality : '';
    return {
      model,
      quality,
      count: [1, 2, 4].includes(Number(source.count)) ? Number(source.count) : 1,
      duration: Math.min(30, Math.max(4, Math.round(Number(source.duration) || 5))),
    };
  }

  sanitizeNodes(nodes) {    if (!Array.isArray(nodes)) return [];
    const out = [];
    const seen = new Set();
    for (const raw of nodes.slice(0, 2000)) {
      if (!raw || typeof raw !== 'object') continue;
      const id = String(raw.id || '').slice(0, 60);
      if (!/^[A-Za-z0-9_-]{4,60}$/.test(id) || seen.has(id)) continue;
      seen.add(id);
      const kind = ['image', 'video', 'upload', 'text', 'subject'].includes(raw.kind) ? raw.kind : 'image';
      const assets = Array.isArray(raw.assets)
        ? raw.assets.slice(0, 24).map((asset) => ({
          id: String(asset && asset.id || '').slice(0, 80),
          kind: asset && ['video', 'audio'].includes(asset.kind) ? asset.kind : 'image',
          w: Number(asset && asset.w) || 0,
          h: Number(asset && asset.h) || 0,
          duration: Number(asset && asset.duration) || 0,
          coverId: String(asset && asset.coverId || ''),
        })).filter((asset) => asset.id && this.media.assets[asset.id])
        : [];
      const node = {
        id,
        kind,
        title: String(raw.title || defaultTitle(kind)).slice(0, 60),
        x: Math.round(Number(raw.x) || 0),
        y: Math.round(Number(raw.y) || 0),
        w: Math.min(560, Math.max(220, Math.round(Number(raw.w) || 300))),
        prompt: String(raw.prompt || '').slice(0, 8000),
        text: String(raw.text || '').slice(0, 8000),
        ratio: String(raw.ratio || '1:1').slice(0, 12),
        params: this.sanitizeParams(raw.params),
        builtinItemId: String(raw.builtinItemId || '').slice(0, 80),
        builtinCanvasId: String(raw.builtinCanvasId || '').slice(0, 80),
        bridgePrompt: String(raw.bridgePrompt || '').slice(0, 8000),
        bridgeRefCount: Math.max(0, Number(raw.bridgeRefCount) || 0),
        subjectIds: Array.isArray(raw.subjectIds) ? raw.subjectIds.map((id) => String(id).slice(0, 40)).slice(0, 6) : [],
        subjectId: String(raw.subjectId || '').slice(0, 40),
        status: ['idle', 'queued', 'running', 'error', 'done'].includes(raw.status) ? raw.status : 'idle',
        statusText: String(raw.statusText || '').slice(0, 400),
        assets,
        createdAt: Number(raw.createdAt) || Date.now(),
        updatedAt: Number(raw.updatedAt) || Date.now(),
      };
      // 服务重启后不可能还有"进行中"的任务，标记成中断，避免界面永远转圈。
      if (node.status === 'queued' || node.status === 'running') {
        node.status = 'error';
        node.statusText = '任务因软件重启而中断';
      }
      out.push(node);
    }
    return out;
  }

  sanitizeEdges(edges) {
    if (!Array.isArray(edges)) return [];
    const nodeIds = new Set();
    const out = [];
    const seen = new Set();
    for (const raw of edges.slice(0, 4000)) {
      if (!raw || typeof raw !== 'object') continue;
      const from = String(raw.from || '');
      const to = String(raw.to || '');
      const id = String(raw.id || '').slice(0, 80) || `e-${from}-${to}`;
      if (!from || !to || from === to || seen.has(id)) continue;
      seen.add(id);
      nodeIds.add(from);
      nodeIds.add(to);
      out.push({ id, from, to });
    }
    return out;
  }

  async deleteCanvas(id) {
    if (!/^[a-z0-9-]{6,40}$/i.test(String(id || ''))) return false;
    await fsp.rm(this.canvasFile(id), { force: true });
    return true;
  }

  // ── 素材 ────────────────────────────────────────────────

  async saveMedia(buffer, { mime = '', kind = '', url = '', name = '', w = 0, h = 0, duration = 0, source = 'generated' } = {}) {
    const digest = sha256(buffer);
    const existing = this.media.assets[digest];
    if (existing) {
      await this.persistMediaIndex();
      return existing;
    }
    const guessedKind = kind || sniffKind(buffer, mime);
    const ext = extFor(mime, guessedKind, url);
    const file = `${digest.slice(0, 32)}.${ext}`;
    await fsp.writeFile(path.join(this.mediaDir, file), buffer);
    const asset = {
      id: digest,
      file,
      kind: guessedKind === 'video' ? 'video' : guessedKind === 'image' ? 'image' : 'file',
      mime: mime || '',
      bytes: buffer.length,
      w: Number(w) || 0,
      h: Number(h) || 0,
      duration: Number(duration) || 0,
      name: safeName(name, file),
      source,
      createdAt: Date.now(),
    };
    this.media.assets[digest] = asset;
    await this.persistMediaIndex();
    return asset;
  }

  async importMediaFile(sourcePath, meta = {}) {
    const buffer = await fsp.readFile(sourcePath);
    return this.saveMedia(buffer, { ...meta, name: meta.name || path.basename(sourcePath) });
  }

  getAsset(id) {
    return this.media.assets[String(id || '')] || null;
  }

  assetPath(asset) {
    return path.join(this.mediaDir, asset.file);
  }

  async setAssetGatewayKey(id, key) {
    const asset = this.getAsset(id);
    if (!asset) return null;
    asset.gatewayKey = String(key || '');
    await this.persistMediaIndex();
    return asset;
  }

  async persistMediaIndex() {
    await writeJsonAtomic(this.mediaIndexFile, this.media, { pretty: true });
  }

  async mediaStats() {
    let bytes = 0;
    for (const asset of Object.values(this.media.assets)) bytes += Number(asset.bytes) || 0;
    return { count: Object.keys(this.media.assets).length, bytes };
  }

  /** 删除没有被任何画布引用的素材（以及过旧的可选范围）。返回删除数量。 */
  async cleanMedia({ olderThanMs = 0 } = {}) {
    const referenced = new Set();
    const names = await fsp.readdir(this.canvasDir).catch(() => []);
    for (const name of names) {
      if (!name.endsWith('.json') || name.startsWith('.')) continue;
      const data = await readJson(path.join(this.canvasDir, name), null);
      if (!data) continue;
      for (const node of data.nodes || []) {
        for (const asset of node.assets || []) {
          if (asset && asset.id) referenced.add(asset.id);
          if (asset && asset.coverId) referenced.add(asset.coverId);
        }
      }
    }
    const now = Date.now();
    let removed = 0;
    for (const [id, asset] of Object.entries(this.media.assets)) {
      if (referenced.has(id)) continue;
      if (olderThanMs && now - (asset.createdAt || 0) < olderThanMs) continue;
      await fsp.rm(this.assetPath(asset), { force: true }).catch(() => {});
      delete this.media.assets[id];
      removed += 1;
    }
    if (removed) await this.persistMediaIndex();
    return removed;
  }
}

function defaultTitle(kind) {
  return { image: '图片生成', video: '视频生成', upload: '本地素材', text: '文本' }[kind] || '节点';
}

module.exports = { Store, sniffKind, extFor, MIME_EXT };
