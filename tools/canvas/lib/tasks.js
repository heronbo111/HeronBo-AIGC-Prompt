const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const fsp = fs.promises;
const { grabLatest, baselineSrcs } = require('./cdp_grab');
const { DEFAULT_PRICING, estimatePoints } = require('./pricing');
const builtin = require('./builtin-canvas');

/**
 * 生成任务队列。
 *
 * 为什么分两条车道：视频一单要等好几分钟，如果和图片共用一条队列，
 * 后面排着的图片全被堵住；反过来图片偶尔并发两张也没问题。
 * 网关自己有 RPM 限制（默认 20），这里的并发刻意保守。
 */
class Lane {
  constructor(limit) {
    this.limit = limit;
    this.running = 0;
    this.queue = [];
  }

  add(job) {
    return new Promise((resolve, reject) => {
      this.queue.push({ job, resolve, reject });
      this.pump();
    });
  }

  pump() {
    while (this.running < this.limit && this.queue.length) {
      const item = this.queue.shift();
      this.running += 1;
      Promise.resolve()
        .then(item.job)
        .then(item.resolve, item.reject)
        .finally(() => {
          this.running -= 1;
          this.pump();
        });
    }
  }
}

class TaskManager {
  constructor({ store, getGateway, ledger = null, log = () => {} }) {
    this.store = store;
    this.getGateway = getGateway;
    this.ledger = ledger;
    this.log = log;
    this.tasks = new Map();
    this.lanes = { image: new Lane(2), video: new Lane(1) };
  }

  create(input) {
    const id = 't' + crypto.randomBytes(8).toString('hex');
    const task = {
      id,
      engine: input.engine === 'builtin' ? 'builtin' : 'gateway',
      kind: input.kind === 'video' ? 'video' : 'image',
      canvasId: String(input.canvasId || ''),
      nodeId: String(input.nodeId || ''),
      prompt: String(input.prompt || ''),
      ratio: String(input.ratio || ''),
      quality: String(input.quality || ''),
      model: String(input.model || ''),
      count: Number(input.count) || 1,
      duration: Number(input.duration) || 5,
      refAssetIds: Array.isArray(input.refAssetIds) ? input.refAssetIds.slice(0, 8).map(String) : [],
      conversationId: String(input.conversationId || ''),
      conversationTitle: String(input.conversationTitle || ''),
      builtinItemId: String(input.builtinItemId || ''),
      status: 'queued',
      message: '排队中',
      assets: [],
      createdAt: Date.now(),
      startedAt: 0,
      finishedAt: 0,
      error: '',
      cancelled: false,
    };
    task.points = pointsFor(task, (this.store.getConfig() || {}).pricing);
    task.pointsEstimated = true;
    this.tasks.set(id, task);
    this.trim();
    const lane = this.lanes[task.kind];
    const ahead = lane.queue.length + lane.running;
    task.message = task.kind === 'video'
      ? `排队中（视频任务单车道，前面还有 ${Math.max(0, ahead - 1)} 个）`
      : `排队中（前面还有 ${Math.max(0, ahead - 1)} 个）`;
    lane.add(() => (task.engine === 'builtin' ? this.runBuiltin(task) : this.run(task))).catch((error) => this.fail(task, error));
    return task;
  }

  get(id) {
    return this.tasks.get(String(id || '')) || null;
  }

  list() {
    return [...this.tasks.values()]
      .filter((task) => task.status === 'queued' || task.status === 'running' || task.status === 'downloading')
      .map((task) => this.publicView(task));
  }

  publicView(task) {
    return {
      id: task.id,
      engine: task.engine || 'gateway',
      kind: task.kind,
      nodeId: task.nodeId,
      canvasId: task.canvasId,
      status: task.status,
      message: task.message,
      error: task.error,
      assets: task.assets,
      builtinItemId: task.builtinItemId || '',
      createdAt: task.createdAt,
      startedAt: task.startedAt,
      finishedAt: task.finishedAt,
      sessionSid: task.sessionSid || '',
      points: task.points || 0,
      pointsEstimated: Boolean(task.pointsEstimated),
    };
  }

  /** 这张画布还有没有没跑完的任务（Agent 与生成互斥用）。 */
  hasRunningForCanvas(canvasId) {
    const target = String(canvasId || '');
    if (!target) return false;
    for (const task of this.tasks.values()) {
      if (task.canvasId !== target) continue;
      if (['queued', 'running', 'downloading'].includes(task.status)) return true;
    }
    return false;
  }

  cancel(id) {
    const task = this.get(id);
    if (!task) return false;
    if (task.status === 'done' || task.status === 'error' || task.status === 'cancelled') return false;
    task.cancelled = true;
    task.status = 'cancelled';
    task.message = '已取消（豆包那边的生成不会因此停止）';
    task.finishedAt = Date.now();
    this.settle(task, 'cancelled');
    return true;
  }

  /**
   * 把一次生成落进账本。任务只会结算一次——重试、补抓、取消都走这里，不会重复扣账。
   */
  settle(task, status) {
    if (!task || task.settled) return;
    task.settled = true;
    if (!this.ledger) return;
    this.ledger.record({
      kind: task.kind,
      action: 'generate',
      canvasId: task.canvasId,
      nodeId: task.nodeId,
      model: task.model || '（默认）',
      quality: task.quality || '',
      duration: task.duration || 0,
      count: task.count || 0,
      points: task.points || 0,
      pointsEstimated: true,
      status,
      error: task.error || '',
      ms: (task.finishedAt || Date.now()) - (task.createdAt || Date.now()),
      charsIn: String(task.prompt || '').length,
      note: status === 'done' ? '按档位估算' : '失败或取消也可能已产生扣费，按档位估算',
    });
  }

  /**
   * 内置画布执行器：本地前台点确认后，真正在内置画布里提交并等结果。
   * 与旧 run() 的区别：不走网关、不走 actMedia，全部动作都落在内置画布的 UI 上。
   */
  async runBuiltin(task) {
    if (task.cancelled) return;
    task.status = 'running';
    task.startedAt = Date.now();
    const cfg = this.store.getConfig() || {};
    const conversationId = String(task.conversationId || cfg.conversationId || '');
    const conversationTitle = String(task.conversationTitle || cfg.conversationTitle || '豆包无限画布项目交接');
    const port = Number(cfg.doubaoworkPort) || 9333;
    if (!conversationId) throw new Error('内置画布缺少 conversationId：请在本地生成面板里填写目标会话');
    let itemId = task.builtinItemId;
    try {
      builtin.setActiveConversation(conversationId);
      task.message = '连接内置画布…';
      await builtin.ensureBuiltinCanvas({
        canvasId: task.canvasId,
        conversationId,
        title: conversationTitle,
      }, port);
      if (task.cancelled) return;

      if (!itemId) {
        task.message = task.kind === 'video' ? '在内置画布创建视频节点…' : '在内置画布创建图片节点…';
        const created = await builtin.createBuiltinGenerationNode(task.kind, port);
        itemId = (created.created && created.created[0] && created.created[0].id) || '';
        if (!itemId) throw new Error('内置画布没有返回新节点 id');
        task.builtinItemId = itemId;
      }
      if (task.cancelled) return;

      task.message = '写入提示词并同步参数…';
      await builtin.fillBuiltinPromptForItem(itemId, task.prompt, port);
      const configured = await builtin.configureBuiltinGeneration(itemId, {
        kind: task.kind,
        model: task.model,
        ratio: task.ratio,
        duration: task.duration,
        quality: task.quality,
      }, port);
      task.builtinLabel = configured && configured.model || '';
      if (task.cancelled) return;

      // 提交前的基线：只认新出现的媒体，避免把内置画布上旧结果当成这次结果。
      const before = await builtin.readBuiltinGeneration(itemId, port);
      const baseline = new Set((before.node.media || []).map((entry) => entry.src).filter(Boolean));

      task.message = task.kind === 'video' ? '提交到内置画布，等待视频…' : '提交到内置画布，等待图片…';
      const clicked = await builtin.startBuiltinGeneration(itemId, port);
      task.submittedAt = Date.now();
      this.log(`[task] ${task.id} 内置提交 item=${itemId} send=${JSON.stringify(clicked)}`);

      const waited = await builtin.waitBuiltinGeneration(itemId, {
        port,
        timeoutMs: task.kind === 'video' ? 600000 : 240000,
        intervalMs: 5000,
      });
      if (task.cancelled) return;
      const node = waited.node || {};
      let media = (node.media || []).filter((entry) => entry && entry.src && !baseline.has(entry.src));
      if (!media.length) media = (node.media || []).filter((entry) => entry && entry.src);
      if (!media.length) {
        const state = String(node.generationState || '').toLowerCase();
        throw new Error(`内置画布没有回传媒体（状态：${state || '未知'}）`);
      }

      task.status = 'downloading';
      task.message = `从内置画布取回结果（${media.length} 个）…`;
      const assets = [];
      for (const entry of media.slice(0, task.kind === 'image' ? 4 : 1)) {
        if (task.cancelled) return;
        try {
          const downloaded = await builtin.downloadBuiltinMedia(entry.src, port);
          const asset = await this.store.saveMedia(downloaded.buffer, {
            mime: downloaded.mime || (task.kind === 'video' ? 'video/mp4' : 'image/png'),
            kind: task.kind,
            url: entry.src,
            w: Number(entry.width) || 0,
            h: Number(entry.height) || 0,
            duration: Number(entry.duration) || 0,
            source: 'builtin',
          });
          if (!assets.some((item) => item.id === asset.id)) {
            assets.push({ id: asset.id, kind: asset.kind, w: asset.w, h: asset.h, duration: asset.duration });
          }
        } catch (error) {
          this.log(`[task] ${task.id} 内置媒体下载失败（${entry.src}）：${error.message}`);
        }
      }
      if (!assets.length) throw new Error('内置画布结果下载失败，没有可落库的媒体');
      task.assets = assets;
      task.status = 'done';
      task.message = task.kind === 'video'
        ? `完成，内置画布生成 ${assets.length} 个视频`
        : `完成，内置画布生成 ${assets.length} 张图片`;
      task.finishedAt = Date.now();
      this.settle(task, 'done');
      await this.persistCanvas(task).catch((error) => this.log(`[task] ${task.id} 本地节点回写失败：${error.message}`));
      this.log(`[task] ${task.id} 内置完成：${task.message}`);
    } catch (error) {
      if (task.cancelled) return;
      this.fail(task, error);
      await this.persistCanvas(task).catch((writeError) => this.log(`[task] ${task.id} 本地节点回写失败：${writeError.message}`));
    }
  }

  async persistCanvas(task) {
    if (!task.canvasId || !task.nodeId) return;
    const canvas = await this.store.readCanvas(task.canvasId);
    if (!canvas) return;
    const node = (canvas.nodes || []).find((item) => item.id === task.nodeId);
    if (!node) return;
    node.status = task.status === 'done' ? 'done' : task.status === 'error' ? 'error' : node.status;
    node.statusText = task.message || task.error || '';
    node.builtinItemId = task.builtinItemId || node.builtinItemId || '';
    node.updatedAt = Date.now();
    if (task.status === 'done') {
      node.assets = (task.assets || []).map((asset) => ({ ...asset }));
    }
    await this.store.saveCanvas(task.canvasId, canvas);
  }

  async run(task) {
    if (task.cancelled) return;
    task.status = 'running';
    task.startedAt = Date.now();
    const gateway = this.getGateway();
    let baseline = null;
    let watchPort = 9333;

    try {
      const refKey = await this.prepareRef(task, gateway);      if (task.cancelled) return;
      task.message = task.kind === 'video'
        ? '豆包正在生成视频（通常 1~5 分钟）…'
        : '豆包正在生成图片…';

      // 桥接取结果不可靠（豆包有时只在某个标签页渲染），这里并行开一个直连 CDP 的看守：
      // 谁先拿到结果用谁的，桥接那边超时也不影响出片。
      let sid = '';
      try { sid = gateway.sessionSidFor ? gateway.sessionSidFor(task.canvasId) || '' : ''; } catch { /* ignore */ }
      const port = Number((this.store.getConfig() || {}).doubaoworkPort) || 9333;
      // 看守不再要求已知会话 id：先拍一张"当前页面已有媒体"的基线，
      // 之后只认基线里没有的新图。否则第一次生成（会话还没绑定）时看守是关着的，
      // 桥接又看不见后台标签页，就成了"豆包出图了画布收不到"。
      let watcher = null;
      if (gateway.kind === 'doubaowork') {
        watchPort = port;
        baseline = await baselineSrcs({ port, sid }).catch(() => new Set());
        watcher = (async () => {
          for (let i = 0; i < 70; i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 5000));
            if (task.cancelled) return null;
            const got = await grabLatest({ port, sid, kind: task.kind, exclude: baseline }).catch(() => []);
            if (got && got.length) return got;
          }
          return null;
        })();
      }
      const bridge = (async () => (task.kind === 'video'
        ? gateway.generateVideo({ prompt: task.prompt, ratio: task.ratio, refKey, canvasId: task.canvasId, duration: task.duration, quality: task.quality, model: task.model })
        : gateway.generateImage({ prompt: task.prompt, ratio: task.ratio, refKey, canvasId: task.canvasId, quality: task.quality, count: task.count, model: task.model })))();
      let payload = null;
      let grabbed = null;
      let bridgeError = null;
      if (watcher) {
        // 桥接失败不立刻判死刑：看守还在跑，可能已经抓到图了。
        const winner = await Promise.race([
          bridge.then((data) => ({ from: 'bridge', data }), (error) => ({ from: 'bridge-error', error })),
          watcher.then((list) => (list && list.length ? { from: 'grab', list } : new Promise(() => {}))),
        ]);
        if (winner.from === 'grab') {
          grabbed = winner.list;
        } else if (winner.from === 'bridge-error') {
          bridgeError = winner.error;
          const late = await watcher;
          if (late && late.length) grabbed = late;
          else throw bridgeError;
        } else {
          payload = winner.data;
        }
      } else {
        payload = await bridge;
      }
      let payloadJson = '';
      try { payloadJson = JSON.stringify(payload); } catch { payloadJson = '<unserializable>'; }
      this.diag(`[task] ${task.id} bridge 返回 typeof=${typeof payload} json=${String(payloadJson).slice(0, 300)}`);
      if (task.cancelled) return;

      if (grabbed) {
        task.status = 'downloading';
        task.message = `已从豆包页面直接取回（${grabbed.length} 个）…`;
        // 桥接可能还在傻等，掐掉它，别把后面的生成任务堵在串行队列里。
        try { if (gateway.abortCurrent) gateway.abortCurrent(); } catch { /* ignore */ }
        const assets = [];
        for (const item of grabbed) {
          const asset = await this.store.saveMedia(item.buffer, {
            mime: item.mime || '',
            kind: task.kind,
            url: item.url || '',
            w: item.w || 0,
            h: item.h || 0,
            duration: item.duration || 0,
            source: 'collected',
          });
          assets.push({ id: asset.id, kind: asset.kind, w: asset.w, h: asset.h, duration: asset.duration });
        }
        task.assets = assets;
        task.status = 'done';
        task.message = `完成，生成 ${assets.length} 个${task.kind === 'video' ? '视频' : '图片'}（直连取回）`;
        task.finishedAt = Date.now();
        // 顺手把这次真正落到的会话 id 记下来，下次看守就能直接盯那一页。
        const chatSid = (grabbed[0] && grabbed[0].chatSid) || sid;
        task.sessionSid = chatSid;
        try { if (gateway.rememberSession) gateway.rememberSession(task.canvasId, chatSid); } catch { /* ignore */ }
        this.settle(task, 'done');
        this.log(`[task] ${task.id} 完成（直连取回）：${task.message}`);
        return;
      }

      const items = Array.isArray(payload.data) ? payload.data : [];
      if (!items.length) {
        const shape = payload === null || payload === undefined
          ? String(payload)
          : 'keys=' + Object.keys(payload).join(',') + ' data=' + typeof payload.data;
        this.diag(`[task] ${task.id} 结果为空：payload(${shape}) 来自=${payload === null ? '?' : 'gateway'}`);
        const hint = payload.message ? String(payload.message) : '网关没有返回任何结果';
        throw new Error(hint);
      }

      task.status = 'downloading';
      task.message = `下载结果（${items.length} 个）…`;
      const assets = [];
      for (const item of items) {
        if (task.cancelled) return;
        const url = item.url || item.video_url || item.videoUrl || '';
        if (!url) continue;
        const downloaded = await gateway.download(url);
        const kind = task.kind === 'video' ? 'video' : 'image';
        const asset = await this.store.saveMedia(downloaded.buffer, {
          mime: downloaded.mime || item.mime || '',
          kind,
          url,
          name: item.name || '',
          w: Number(item.width) || 0,
          h: Number(item.height) || 0,
          duration: Number(item.duration) || 0,
          source: 'generated',
        });
        let record = assets.find((entry) => entry.id === asset.id);
        if (!record) {
          record = { id: asset.id, kind: asset.kind, w: asset.w, h: asset.h, duration: asset.duration };
          assets.push(record);
        }
        // 视频封面也顺手存一份，卡片上先显示封面，点开才加载视频。
        const coverUrl = item.cover_url || item.coverUrl || '';
        if (kind === 'video' && coverUrl) {
          try {
            const cover = await gateway.download(coverUrl);
            const coverAsset = await this.store.saveMedia(cover.buffer, {
              mime: cover.mime,
              kind: 'image',
              url: coverUrl,
              source: 'generated',
            });
            record.coverId = coverAsset.id;
          } catch (error) {
            this.log('[task] 视频封面下载失败（不影响视频）:', error.message);
          }
        }
      }

      if (!assets.length) throw new Error('结果里没有可下载的媒体地址');
      task.assets = assets;
      task.status = 'done';
      task.message = task.kind === 'video'
        ? `完成，生成 ${assets.length} 个视频`
        : `完成，生成 ${assets.length} 张图片`;
      task.finishedAt = Date.now();
      try { if (gateway.sessionSidFor) task.sessionSid = gateway.sessionSidFor(task.canvasId) || ''; } catch { /* ignore */ }
      this.settle(task, 'done');
      this.log(`[task] ${task.id} 完成：${task.message}`);
    } catch (error) {
      if (task.cancelled) return;
      // 桥接超时/报错时，客户端可能其实已经出片了：按基线再直连抓一次（只认新出现的媒体）。
      if (baseline) {
        const late = await grabLatest({ port: watchPort, sid: '', kind: task.kind, exclude: baseline }).catch(() => []);
        if (late && late.length && !task.cancelled) {
          const assets = [];
          for (const item of late) {
            try {
              const asset = await this.store.saveMedia(item.buffer, {
                mime: item.mime || '',
                kind: task.kind,
                url: item.url || '',
                w: item.w || 0,
                h: item.h || 0,
                duration: item.duration || 0,
                source: 'collected',
              });
              assets.push({ id: asset.id, kind: asset.kind, w: asset.w, h: asset.h, duration: asset.duration });
            } catch { /* 单个失败跳过 */ }
          }
          if (assets.length) {
            task.assets = assets;
            task.status = 'done';
            task.error = '';
            task.message = `桥接没等到，但客户端已出片，已直接取回 ${assets.length} 个`;
            task.finishedAt = Date.now();
            task.sessionSid = (late[0] && late[0].chatSid) || task.sessionSid || '';
            this.settle(task, 'done');
            this.diag(`[task] ${task.id} 桥接失败后补抓成功：${assets.length} 个`);
            this.log(`[task] ${task.id} 补抓成功：${task.message}`);
            return;
          }
        }
      }
      this.fail(task, error);
    }
  }

  fail(task, error) {
    task.status = 'error';
    task.error = friendlyError(error);
    task.message = task.error;
    task.finishedAt = Date.now();
    this.settle(task, 'error');
    this.diag(`[task] ${task.id} 失败：${(error && error.stack) || error}`);
    this.log(`[task] ${task.id} 失败：${task.error}`);
  }

  /** 任务级诊断日志：写到 data/logs/tasks.log，出问题时有据可查。 */
  diag(line) {
    try {
      const dir = path.join(this.store.dataDir, 'logs');
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, 'tasks.log');
      try { if (fs.statSync(file).size > 1024 * 1024) fs.renameSync(file, file + '.1'); } catch { /* 首次写 */ }
      fs.appendFileSync(file, '=== ' + new Date().toISOString() + ' ===\n' + line + '\n');
    } catch { /* 日志失败不影响任务 */ }
  }

  async prepareRef(task, gateway) {
    // 参考素材可来自多条上游连线：每条连线取它的一张。
    // 按类型分别限流（图片 6 / 视频 1 / 音频 1），这样主体挂满 6 张形象时音色也不会被挤掉
    // —— 桥那边 --video/--audio 本来也只能各带一条。
    const refs = [];
    const seen = new Set();
    let images = 0;
    let videos = 0;
    let audios = 0;
    for (const id of task.refAssetIds) {
      const asset = this.store.getAsset(id);
      if (!asset || !['image', 'video', 'audio'].includes(asset.kind) || seen.has(asset.id)) continue;
      // 图片任务只吃图片参考：视频/音频既送不进桥，复制到 refs 也是白占地方。
      if (task.kind !== 'video' && asset.kind !== 'image') continue;
      if (asset.kind === 'image') {
        if (images >= 6) continue;
        images += 1;
      } else if (asset.kind === 'video') {
        if (videos >= 1) continue;
        videos += 1;
      } else {
        if (audios >= 1) continue;
        audios += 1;
      }
      seen.add(asset.id);
      refs.push(asset);
    }
    if (!refs.length) return [];
    task.message = `准备参考素材（${refs.length} 个）…`;
    const prepared = [];
    for (const asset of refs) {
      const file = this.store.assetPath(asset);
      const stat = await fsp.stat(file).catch(() => null);
      if (!stat || stat.size < 128) {
        throw new Error(`参考素材「${asset.name || asset.id.slice(0, 8)}」的本地文件无效（${stat ? stat.size + ' 字节' : '不存在'}），请重新上传这张素材后再生成。`);
      }
      if (gateway.kind === 'doubaowork') {
        // 豆包工作客户端：把素材复制成带语义名字的文件，让豆包直接按路径读。
        const preparedPath = await gateway.prepareRefFile(file, asset.name, task.canvasId, asset.kind);
        prepared.push({ path: preparedPath, kind: asset.kind });
      } else {
        // 外部网关模式只支持图片参考：视频 / 音频素材跳过。
        if (asset.kind !== 'image') continue;
        if (asset.gatewayKey) { prepared.push({ path: asset.gatewayKey, kind: 'image' }); continue; }
        const buffer = await fsp.readFile(this.store.assetPath(asset));
        const rawName = asset.name || `${asset.id}.png`;
        const filename = /\.[a-z0-9]{2,5}$/i.test(rawName) ? rawName : `${rawName}.png`;
        const uploaded = await gateway.uploadImage(buffer, filename, asset.mime);
        await this.store.setAssetGatewayKey(asset.id, uploaded.uri);
        prepared.push({ path: uploaded.uri, kind: 'image' });
        break; // 外部网关单参考图限制
      }
    }
    return prepared;
  }

  trim() {
    if (this.tasks.size <= 300) return;
    const finished = [...this.tasks.values()]
      .filter((task) => task.finishedAt)
      .sort((a, b) => a.finishedAt - b.finishedAt);
    for (const task of finished) {
      if (this.tasks.size <= 250) break;
      this.tasks.delete(task.id);
    }
  }
}

function mergedPricing(custom = {}) {
  const c = custom || {};
  return {
    note: c.note || DEFAULT_PRICING.note,
    image: {
      perSheet: { ...DEFAULT_PRICING.image.perSheet, ...(c.image && c.image.perSheet) },
      modelFactor: { ...DEFAULT_PRICING.image.modelFactor, ...(c.image && c.image.modelFactor) },
    },
    video: {
      perSecond: { ...DEFAULT_PRICING.video.perSecond, ...(c.video && c.video.perSecond) },
    },
  };
}

/** 生成前先按档位估算；估不出来就记 0，不让定价问题挡住生成。 */
function pointsFor(task, custom) {
  try {
    const est = estimatePoints({
      kind: task.kind,
      model: task.model,
      quality: task.quality,
      duration: task.duration,
      count: task.count,
    }, mergedPricing(custom));
    return Math.round((Number(est.points) || 0) * 10) / 10;
  } catch {
    return 0;
  }
}

function friendlyError(error) {
  const message = String((error && error.message) || error || '未知错误');
  if (/没等到生成结果|桥超时|timeout/i.test(message)) {
    return '豆包没有回结果（可能卡在确认弹窗、会话跳转或排队）。已记录桥接日志：设置 → 打开数据目录 → logs/bridge.log。原始信息：' + message;
  }
  if (/豆包工作客户端没启动|豆包工作没启动|豆包工作没登录|豆包工作桥失败|找不到输入框|调试端口/i.test(message)) {
    return '豆包工作客户端没就绪：点左侧「启动豆包工作」，在客户端里登录后重试。原始信息：' + message;
  }
  if (/ECONNREFUSED|fetch failed|连不上网关/i.test(message)) {
    return '连不上本机豆包网关：先启动 doubao2api（start.bat），登录豆包工作版后再试。原始信息：' + message;
  }
  if (/Not logged in|未登录|扫码/i.test(message)) {
    return '豆包还没登录：在左侧「豆包」卡片点「扫码登录豆包」，用手机豆包 App 扫码后重试。原始信息：' + message;
  }
  if (/401|403/.test(message)) {
    return '网关没有登录豆包：打开 doubao2api 管理页扫码登录，或检查 API Key。原始信息：' + message;
  }
  if (/额度|次数|会员|套餐|专属能力/.test(message)) {
    return '豆包账号当前不可用：' + message;
  }
  return message;
}

module.exports = { TaskManager, friendlyError };
