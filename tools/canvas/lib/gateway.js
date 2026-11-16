const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');

/**
 * doubao2api（本机豆包网关）客户端。
 *
 * 这个画布只跟本机网关打交道：网关用「豆包工作版」账号登录，负责出图 / 出视频；
 * 画布端不碰豆包的登录态、不注入页面脚本，也没有任何云端依赖。
 */
class GatewayError extends Error {
  constructor(message, { status = 0, detail = '', timeout = false } = {}) {
    super(message);
    this.name = 'GatewayError';
    this.status = status;
    this.detail = detail;
    this.timeout = timeout;
  }
}

class Gateway {
  constructor({ baseUrl, apiKey }) {
    this.baseUrl = String(baseUrl || 'http://127.0.0.1:9090').replace(/\/+$/, '');
    this.apiKey = String(apiKey || '');
  }

  headers(extra = {}) {
    const headers = { ...extra };
    if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`;
    return headers;
  }

  async fetchJson(pathname, { method = 'GET', body, timeoutMs = 30000 } = {}) {
    const url = this.baseUrl + pathname;
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: this.headers(body ? { 'Content-Type': 'application/json' } : {}),
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      const isTimeout = error && (error.name === 'TimeoutError' || error.name === 'AbortError');
      const cause = error && error.cause && (error.cause.code || error.cause.message);
      throw new GatewayError(
        isTimeout ? `连接网关超时（${Math.round(timeoutMs / 1000)} 秒）` : `连不上网关：${cause || error.message}`,
        { timeout: isTimeout },
      );
    }
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON */ }
    if (!response.ok) {
      const detail = data && (
        data.detail
        || data.message
        || (data.error && (data.error.message || data.error.type))
        || data.error
      );
      throw new GatewayError(`网关返回 ${response.status}：${detail ? String(detail) : text.slice(0, 400) || '无详细信息'}`, {
        status: response.status,
        detail: String(detail || ''),
      });
    }
    if (!data) {
      throw new GatewayError('网关返回了无法解析的内容（不是 JSON）', { status: response.status, detail: text.slice(0, 400) });
    }
    return data;
  }

  async health() {
    const startedAt = Date.now();
    try {
      const data = await this.fetchJson('/v1/models', { timeoutMs: 4000 });
      const models = Array.isArray(data.data) ? data.data.map((item) => item.id).filter(Boolean) : [];
      return { ok: true, latency: Date.now() - startedAt, models: models.slice(0, 40) };
    } catch (error) {
      const authError = /401|403|API key|Unauthorized/i.test(error.message);
      return { ok: false, latency: Date.now() - startedAt, error: error.message, authError };
    }
  }

  /** 网关自己维护的「豆包有没有登录」状态。失败不影响主流程。 */
  async loginStatus() {
    try {
      const data = await this.fetchJson('/admin/api/status', { timeoutMs: 6000 });
      return {
        known: true,
        loggedIn: data.logged_in === true,
        ready: data.is_ready_flag === true,
        loginButton: data.login_button_visible === true,
        pageUrl: String(data.page_url || ''),
        browser: data.browser || '',
      };
    } catch (error) {
      return { known: false, loggedIn: false, error: error.message };
    }
  }

  /** 启动网关的扫码登录流程，返回 base64 二维码 PNG。 */
  async startQrLogin() {
    return this.fetchJson('/v1/session/qr-login', { method: 'POST', body: {}, timeoutMs: 30000 });
  }

  async pollQrLogin() {
    return this.fetchJson('/v1/session/qr-login', { timeoutMs: 10000 });
  }

  async chat({ messages, model = 'doubao-pro', temperature = 0.7, timeoutMs = 180000 }) {
    const data = await this.fetchJson('/v1/chat/completions', {
      method: 'POST',
      body: { model, messages, stream: false, temperature },
      timeoutMs,
    });
    // 网关要是回了 usage，就留给账本用；没回也不影响正文。
    this.lastUsage = (data && data.usage) || null;
    const content = data
      && data.choices
      && data.choices[0]
      && data.choices[0].message
      && data.choices[0].message.content;
    if (!content) throw new GatewayError('豆包没有返回文本内容');
    return String(content);
  }

  /** tasks.prepareRef 现在统一返回 [{path, kind}]；外部网关只认图片 key，这里摊平成字符串。 */
  refKeys(refKey) {
    if (!refKey) return undefined;
    if (!Array.isArray(refKey)) return refKey;
    return refKey.map((ref) => (typeof ref === 'string' ? ref : (ref && ref.path) || '')).filter(Boolean);
  }

  async generateImage({ prompt, ratio, refKey }) {
    const body = { model: 'doubao-image', prompt, n: 1, response_format: 'url' };
    if (ratio) body.ratio = ratio;
    const keys = this.refKeys(refKey);
    if (keys) body.ref_image_key = keys;
    return this.fetchJson('/v1/images/generations', { method: 'POST', body, timeoutMs: 240000 });
  }

  async generateVideo({ prompt, ratio, refKey, timeoutMs = 420000 }) {
    const body = { prompt };
    if (ratio) body.ratio = ratio;
    const keys = this.refKeys(refKey);
    if (keys) body.ref_image_key = keys;
    return this.fetchJson('/v1/video/generations', { method: 'POST', body, timeoutMs });
  }

  async uploadImage(buffer, filename, mime) {
    const form = new FormData();
    const blob = new Blob([buffer], { type: mime || 'application/octet-stream' });
    form.append('image', blob, filename);
    const url = this.baseUrl + '/v1/images/upload';
    let response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: this.headers(),
        body: form,
        signal: AbortSignal.timeout(120000),
      });
    } catch (error) {
      const isTimeout = error && (error.name === 'TimeoutError' || error.name === 'AbortError');
      throw new GatewayError(isTimeout ? '上传参考图超时' : `上传参考图失败：${error.message}`, { timeout: isTimeout });
    }
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* ignore */ }
    if (!response.ok || !data) {
      throw new GatewayError(`上传参考图失败（${response.status}）：${(data && (data.detail || data.message)) || text.slice(0, 300)}`, {
        status: response.status,
      });
    }
    if (!data.uri) throw new GatewayError('上传参考图失败：网关没有返回 uri');
    return data;
  }

  async download(url, { timeoutMs = 180000, referer = 'https://www.doubao.com/' } = {}) {
    let response;
    try {
      response = await fetch(url, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
          Referer: referer,
        },
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      const isTimeout = error && (error.name === 'TimeoutError' || error.name === 'AbortError');
      throw new GatewayError(isTimeout ? '下载生成结果超时' : `下载生成结果失败：${error.message}`, { timeout: isTimeout });
    }
    if (!response.ok) {
      throw new GatewayError(`下载生成结果失败（HTTP ${response.status}）`, { status: response.status });
    }
    const buffer = Buffer.from(await response.arrayBuffer());
    if (!buffer.length) throw new GatewayError('下载到的文件是空的');
    return { buffer, mime: String(response.headers.get('content-type') || '').split(';')[0].trim() };
  }
}

/** 网关通常就在 doubao2api 里：优先读项目自带的 key，其次读兄弟目录的 .api_key。 */
function detectApiKey(rootDir) {
  const candidates = [
    process.env.DOUBAO_API_KEY,
    path.join(rootDir, 'data', 'gateway.key'),
    path.join(rootDir, '..', 'doubao2api', '.api_key'),
    path.join(rootDir, '..', 'zcode-doubao-local', '.api_key'),
  ].filter(Boolean);
  // 便携版：用户常把 doubao2api 放在 exe 旁边（甚至同一个文件夹）。
  const portable = process.env.PORTABLE_EXECUTABLE_DIR;
  if (portable) {
    candidates.push(
      path.join(portable, 'doubao2api', '.api_key'),
      path.join(portable, '.api_key'),
      path.join(portable, 'gateway.key'),
    );
  }
  if (process.execPath) {
    candidates.push(path.join(path.dirname(process.execPath), 'doubao2api', '.api_key'));
  }
  for (const file of candidates) {
    try {
      if (!fs.existsSync(file)) continue;
      const text = fs.readFileSync(file, 'utf8').trim();
      if (text) return text;
    } catch { /* 忽略 */ }
  }
  return '';
}

module.exports = { Gateway, GatewayError, detectApiKey };
