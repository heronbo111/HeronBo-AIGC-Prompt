const { spawn } = require('node:child_process');
const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { fileURLToPath } = require('node:url');
const { GatewayError } = require('./gateway');

const MIME_BY_EXT = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
  '.gif': 'image/gif', '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
};

/**
 * 豆包工作客户端（DoubaoWork.exe）CDP 桥。
 *
 * 跟 HeronBo 工作台同一套：不需要 doubao2api、API Key 或启动脚本，只要本机装了
 * 豆包工作客户端并登录；画布通过 `--remote-debugging-port` 接管它，在客户端里
 * 直接生成图片 / 视频。
 *
 * vendor/doubao_cdp.mjs 是工作台验证过的桥，这里加了 login / show / media 三个动作。
 */
class DoubaoWorkGateway {
  constructor({ rootDir, dataDir, config = {} }) {
    this.rootDir = rootDir;
    this.dataDir = dataDir;
    this.exe = String(config.doubaoworkExe || '').trim();
    // 工作台拉起画布时会通过 HERONBO_DOUBAO_CDP 指向它自己的 doubao_cdp.mjs；
    // 这样两边驱动的是同一个桥、同一个 9333 豆包实例，不是画布里那份旧副本。
    this.script = process.env.HERONBO_DOUBAO_CDP ||
      path.join(rootDir, 'vendor', 'doubao_cdp.mjs');
    this.stateFile = path.join(dataDir, 'doubaowork-state.json');
    this.kind = 'doubaowork';
    this.baseUrl = '豆包工作客户端';
    this.apiKey = '';
    this.queue = Promise.resolve();
    this.lastStatus = null;
    this.lastStatusAt = 0;
  }

  serialize(fn) {
    const job = this.queue.catch(() => {}).then(fn);
    this.queue = job.catch(() => {});
    return job;
  }

  run(action, args = [], { timeoutMs = 120000 } = {}) {
    return new Promise((resolve, reject) => {
      if (!fs.existsSync(this.script)) {
        reject(new GatewayError('缺少 vendor/doubao_cdp.mjs（打包不完整）'));
        return;
      }
      const env = { ...process.env };
      // 打包后 process.execPath 是 Electron/应用 exe；统一按 Node 子进程跑 vendor 脚本。
      // 纯 node 下这个环境变量会被忽略，不影响。
      env.ELECTRON_RUN_AS_NODE = '1';
      env.HERONBO_CDP_STATE = this.stateFile;
      if (this.exe) env.HERONBO_DOUBAO_EXE = this.exe;
      const child = spawn(process.execPath, [this.script, action, ...args], {
        cwd: this.rootDir,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let out = '';
      let err = '';
      let settled = false;
      this._child = child;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (this._child === child) this._child = null;
        fn(value);
      };
      const timer = setTimeout(() => {
        try { child.kill(); } catch { /* ignore */ }
        finish(reject, new GatewayError(`豆包工作桥超时（${Math.round(timeoutMs / 1000)} 秒）`, { timeout: true }));
      }, timeoutMs);
      child.stdout.on('data', (chunk) => { out += chunk.toString('utf8'); });
      child.stderr.on('data', (chunk) => { err += chunk.toString('utf8'); });
      child.on('error', (error) => finish(reject, new GatewayError('启动豆包工作桥失败：' + error.message)));
      child.on('close', (code) => {
        this.writeBridgeLog(action, args, out, err, code);
        // Node 24 在 Windows 上偶发“打印完结果后 process.exit 触发 libuv 断言”，
        // 退出码是 0xC0000409，但 stdout 里的 JSON / 答复是完整的 —— 以输出为准。
        const parsed = this.parseJson(out);
        if (code !== 0) {
          if (parsed || (action === 'ask' && out.trim())) {
            finish(resolve, { out, err, code });
            return;
          }
          const detail = (err.trim().split(/\r?\n/).slice(-4).join(' ') || out.slice(-400) || '无输出');
          finish(reject, new GatewayError(`豆包工作桥失败（code=${code}）：${detail}`.slice(0, 800)));
          return;
        }
        finish(resolve, { out, err, code });
      });
    });
  }

  parseJson(text) {
    const raw = String(text || '').trim();
    if (!raw) return null;
    const candidates = [raw, raw.split(/\r?\n/).filter(Boolean).pop()];
    for (const candidate of candidates) {
      if (!candidate || (candidate[0] !== '{' && candidate[0] !== '[')) continue;
      try { return JSON.parse(candidate); } catch { /* 继续 */ }
    }
    return null;
  }

  async status({ fresh = false } = {}) {
    if (!fresh && this.lastStatus && Date.now() - this.lastStatusAt < 3000) return this.lastStatus;
    let state;
    try {
      const { out } = await this.run('login', [], { timeoutMs: 20000 });
      const json = this.parseJson(out) || {};
      state = {
        ok: json.cdp === true,
        cdp: json.cdp === true,
        loggedIn: json.loggedIn === true,
        pageUrl: String(json.pageUrl || ''),
        error: String(json.why || ''),
        mode: 'doubaowork',
      };
    } catch (error) {
      state = { ok: false, cdp: false, loggedIn: false, error: error.message, mode: 'doubaowork' };
    }
    this.lastStatus = state;
    this.lastStatusAt = Date.now();
    return state;
  }

  async health({ canvasId = '' } = {}) {
    const state = await this.status();
    return {
      ok: state.ok,
      mode: 'doubaowork',
      baseUrl: '豆包工作客户端',
      latency: 0,
      models: ['doubao-image', 'doubao-video', 'doubao-pro'],
      login: { known: true, loggedIn: state.loggedIn, pageUrl: state.pageUrl, mode: 'doubaowork' },
      sessionSid: this.sessionSidFor(canvasId),
      error: state.error,
      canLaunch: !state.ok,
    };
  }

  async loginStatus() {
    const state = await this.status();
    return { known: true, loggedIn: state.loggedIn, pageUrl: state.pageUrl, mode: 'doubaowork' };
  }

  /**
   * 掐掉当前正在跑的桥接子进程。
   * 直连看守已经拿到结果时用：桥接还在傻等，会把后面的串行队列一起堵住。
   * 豆包那边已经发出的生成不受影响。
   */
  abortCurrent() {
    const child = this._child;
    if (!child) return false;
    this._child = null;
    try { child.kill(); } catch { /* ignore */ }
    return true;
  }

  async show() {
    const { out } = await this.run('show', [], { timeoutMs: 90000 });
    this.lastStatus = null;
    const json = this.parseJson(out) || {};
    if (json.ok === false) throw new GatewayError(json.error || '没能打开豆包工作客户端');
    return json;
  }

  async startQrLogin() {
    await this.show();
    return { status: 'manual', message: '已打开豆包工作客户端，请在客户端里扫码登录' };
  }

  async pollQrLogin() {
    const state = await this.status({ fresh: true });
    if (state.loggedIn) return { status: 'success' };
    if (!state.cdp) return { status: 'failed', error: state.error || '豆包工作没启动' };
    return { status: 'waiting', message: '等待客户端登录' };
  }

  sessionKey(canvasId, kind = 'media') {
    const clean = String(canvasId || '').replace(/[^A-Za-z0-9._-]/g, '');
    if (!clean) return '';
    // 生成用 c<画布>，Agent 用 a<画布>：两者绝不能共用一段对话，
    // 否则生成中途用 Agent 会把规划提示词插进正在生成的会话，直接打断生成。
    const prefix = kind === 'agent' ? 'a' : 'c';
    return clean.startsWith(prefix) ? clean : prefix + clean;
  }

  /** 清掉这张画布的会话绑定，下次生成开一段干净对话。 */
  async resetSession(canvasId) {
    const state = JSON.parse(fs.readFileSync(this.stateFile, 'utf8').toString() || '{}');
    for (const kind of ['media', 'agent']) {
      const key = this.sessionKey(canvasId, kind);
      if (!key) continue;
      delete state[key + ':sessionSid'];
      delete state[key + ':sessionTarget'];
    }
    await fsp.writeFile(this.stateFile, JSON.stringify(state, null, 1));
    return true;
  }

  sessionSidFor(canvasId) {
    const key = this.sessionKey(canvasId);
    if (!key) return '';
    try {
      const state = JSON.parse(fs.readFileSync(this.stateFile, 'utf8'));
      return String(state[key + ':sessionSid'] || '');
    } catch {
      return '';
    }
  }

  /** 记下某张画布实际落到哪个豆包会话，下次直连取回可以直接盯那一页。 */
  rememberSession(canvasId, sid) {
    const key = this.sessionKey(canvasId);
    const clean = String(sid || '').trim();
    if (!key || !clean) return false;
    try {
      let state = {};
      try { state = JSON.parse(fs.readFileSync(this.stateFile, 'utf8')) || {}; } catch { /* 首次写 */ }
      if (String(state[key + ':sessionSid'] || '') === clean) return true;
      state[key + ':sessionSid'] = clean;
      fs.writeFileSync(this.stateFile, JSON.stringify(state, null, 1));
      return true;
    } catch {
      return false;
    }
  }

  async generateImage({ prompt, ratio, refKey, canvasId, quality = '', count = 1, model = '' }) {
    return this.media('image', { prompt, ratio, quality, count, model, refs: Array.isArray(refKey) ? refKey : (refKey ? [refKey] : []), session: this.sessionKey(canvasId) });
  }

  async generateVideo({ prompt, ratio, refKey, canvasId, duration = 5, quality = '', model = '' }) {
    return this.media('video', { prompt, ratio, duration, quality, model, refs: Array.isArray(refKey) ? refKey : (refKey ? [refKey] : []), session: this.sessionKey(canvasId) });
  }

  /**
   * 把画布素材复制成豆包可读的参考文件：名字带语义（「参考图1_晓艳1.png」），
   * 放在 data\refs\<画布>\ 下；返回绝对路径列表。豆包工作客户端能直接读本机路径。
   */
  /** 把每次桥接调用的结果留档（提示词脱敏），排查"生成了但画布没收到"时用。 */
  writeBridgeLog(action, args, out, err, code) {
    try {
      const dir = path.join(this.dataDir, 'logs');
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, 'bridge.log');
      try { if (fs.statSync(file).size > 2 * 1024 * 1024) fs.renameSync(file, file + '.1'); } catch { /* 首次写 */ }
      const safeArgs = [];
      for (let i = 0; i < args.length; i += 1) {
        if (args[i] === '--prompt') { safeArgs.push('--prompt', '<' + String(args[i + 1] || '').length + ' 字>'); i += 1; continue; }
        safeArgs.push(args[i]);
      }
      const chunk = [
        '=== ' + new Date().toISOString() + ' ' + action + ' code=' + code + ' ===',
        'args: ' + safeArgs.join(' '),
        '--- stdout (tail) ---',
        String(out || '').slice(-4000),
        '--- stderr (tail) ---',
        String(err || '').slice(-2000),
        '',
      ].join(String.fromCharCode(10));
      fs.appendFileSync(file, chunk);
    } catch { /* 日志失败不影响生成 */ }
  }

  async prepareRefFile(sourcePath, originalName, canvasId = '', kind = 'image') {
    await fsp.mkdir(path.join(this.dataDir, 'refs'), { recursive: true });
    const prefix = kind === 'video' ? '参考视频' : kind === 'audio' ? '参考音频' : '参考图';
    let base = String(originalName || '').replace(/\.[a-z0-9]+$/i, '').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40);
    if (!base) base = prefix;
    let ext = path.extname(sourcePath).toLowerCase();
    const allowed = kind === 'video'
      ? /^\.(mp4|mov|webm|mkv)$/
      : kind === 'audio'
        ? /^\.(mp3|wav|m4a|aac|ogg|flac)$/
        : /^\.(png|jpe?g|webp|gif)$/;
    if (!allowed.test(ext)) ext = kind === 'video' ? '.mp4' : kind === 'audio' ? '.mp3' : '.png';
    const dir = path.join(this.dataDir, 'refs', this.sessionKey(canvasId) || 'common');
    await fsp.mkdir(dir, { recursive: true });
    this._refCounter = (this._refCounter || 0) + 1;
    const target = path.join(dir, `${prefix}${this._refCounter}_${base}${ext}`);
    await fsp.copyFile(sourcePath, target);
    return target;
  }

  /** 只取结果不发送：豆包那边已经出图但画布没收到时用。 */
  async collect(canvasId, kind = 'image', match = '', sid = '') {
    return this.serialize(async () => {
      const outDir = path.join(this.dataDir, 'tmp', `collect-${Date.now()}-${crypto.randomBytes(3).toString('hex')}`);
      await fsp.mkdir(outDir, { recursive: true });
      const args = ['--kind', kind === 'video' ? 'video' : 'image', '--out', outDir];
      const session = this.sessionKey(canvasId);
      if (session) args.push('--session', session);
      if (match) args.push('--match-b64', Buffer.from(String(match).slice(0, 40), 'utf8').toString('base64'));
      if (sid) args.push('--sid', String(sid).slice(0, 40));
      const { out } = await this.run('collect', args, { timeoutMs: 150000 });
      const json = this.parseJson(out);
      if (!json) throw new GatewayError('豆包工作没有返回结果');
      if (json.state === 'error') throw new GatewayError(json.error || '取回失败');
      const files = Array.isArray(json.files) ? json.files : [];
      if (!files.length) throw new GatewayError('没有取到媒体文件');
      return {
        data: files.map((file) => ({
          url: 'file:///' + String(file.path).replace(/\\/g, '/'),
          width: Number(file.w) || 0,
          height: Number(file.h) || 0,
          duration: Number(file.duration) || 0,
        })),
      };
    });
  }

  async media(kind, { prompt, ratio, duration = 5, quality = '', count = 1, model = '', refs = [], session = '' }) {
    return this.serialize(async () => {
      const outDir = path.join(this.dataDir, 'tmp', `media-${Date.now()}-${crypto.randomBytes(3).toString('hex')}`);
      await fsp.mkdir(outDir, { recursive: true });
      const args = [
        '--kind', kind,
        '--prompt', prompt,
        '--ratio', ratio || '1:1',
        '--out', outDir,
        '--timeout', kind === 'video' ? '900' : '300',
      ];
      if (session) args.push('--session', session);
      if (kind === 'video') args.push('--duration', String(duration || 5));
      if (model) args.push('--model', model);
      if (quality) args.push('--quality', quality);
      if (kind === 'image' && Number(count) > 1) args.push('--count', String(count));
      // 参考素材按类型走不同参数：桥只认 --image（可重复）/ --video（单条）/ --audio（单条）。
      // 之前所有素材都塞进 --image，音频就成了豆包读不了的"图"，主体的声音自然合并不进去。
      const images = [];
      const videos = [];
      const audios = [];
      for (const ref of refs) {
        const file = typeof ref === 'string' ? ref : String((ref && ref.path) || '');
        if (!file) continue;
        const refKind = typeof ref === 'string' ? 'image' : String((ref && ref.kind) || 'image');
        if (refKind === 'video') videos.push(file);
        else if (refKind === 'audio') audios.push(file);
        else images.push(file);
      }
      for (const file of images) args.push('--image', file);
      // 视频 / 音频参考只对视频任务有意义；图片任务带上去豆包也读不了。
      if (kind === 'video' && videos.length) args.push('--video', videos[0]);
      if (kind === 'video' && audios.length) args.push('--audio', audios[0]);
      const { out } = await this.run('media', args, { timeoutMs: kind === 'video' ? 960000 : 360000 });
      const json = this.parseJson(out);
      if (!json) throw new GatewayError('豆包工作没有返回结果');
      if (json.state === 'error') throw new GatewayError(json.error || '生成失败');
      const files = Array.isArray(json.files) ? json.files : [];
      if (!files.length) throw new GatewayError('没有拿到媒体文件');
      return {
        data: files.map((file) => ({
          url: 'file:///' + String(file.path).replace(/\\/g, '/'),
          width: Number(file.w) || 0,
          height: Number(file.h) || 0,
          duration: Number(file.duration) || 0,
        })),
      };
    });
  }

  async uploadImage(buffer, filename) {
    const hash = crypto.createHash('sha256').update(buffer).digest('hex');
    let ext = path.extname(filename || '').toLowerCase();
    if (!/^\.[a-z0-9]{2,5}$/.test(ext)) ext = '.png';
    const dir = path.join(this.dataDir, 'refs');
    await fsp.mkdir(dir, { recursive: true });
    const file = path.join(dir, hash.slice(0, 32) + ext);
    await fsp.writeFile(file, buffer);
    return file;
  }

  async download(url) {
    let file = '';
    if (String(url).startsWith('file://')) file = fileURLToPath(String(url));
    else if (path.isAbsolute(String(url)) && fs.existsSync(String(url))) file = String(url);
    if (file) {
      const buffer = await fsp.readFile(file);
      const mime = MIME_BY_EXT[path.extname(file).toLowerCase()] || 'application/octet-stream';
      await fsp.rm(file, { force: true }).catch(() => {});
      return { buffer, mime };
    }
    let response;
    try {
      response = await fetch(String(url), {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36' },
        signal: AbortSignal.timeout(180000),
      });
    } catch (error) {
      throw new GatewayError('下载豆包结果失败：' + error.message);
    }
    if (!response.ok) throw new GatewayError(`下载豆包结果失败（HTTP ${response.status}）`);
    return {
      buffer: Buffer.from(await response.arrayBuffer()),
      mime: String(response.headers.get('content-type') || '').split(';')[0].trim(),
    };
  }

  async chat({ messages, canvasId }) {
    const prompt = (messages || [])
      .map((message) => (message.role === 'system' ? '【系统要求】\n' : '') + String(message.content || ''))
      .join('\n\n');
    const args = ['--prompt', prompt, '--timeout', '240'];
    const session = this.sessionKey(canvasId, 'agent');
    if (session) args.push('--session', session);
    const { out } = await this.serialize(() => this.run('ask', args, { timeoutMs: 300000 }));
    const answer = String(out || '').trim();
    if (!answer) throw new GatewayError('豆包工作没有返回内容');
    return answer;
  }
}

module.exports = { DoubaoWorkGateway };
