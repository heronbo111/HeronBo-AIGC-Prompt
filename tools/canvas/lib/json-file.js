const fs = require('node:fs');
const fsp = fs.promises;
const path = require('node:path');
const crypto = require('node:crypto');

/**
 * 原子写 JSON：先写同目录临时文件再 rename。
 * Windows 上 rename 到已存在的文件会失败（EPERM/EEXIST），所以先删旧文件再换名；
 * 这个窗口里若进程崩了，旧文件已经不在，但临时文件是完整的 —— 启动时 recoverTmp 会捞回来。
 */
async function writeJsonAtomic(file, value, { pretty = false } = {}) {
  const dir = path.dirname(file);
  await fsp.mkdir(dir, { recursive: true });
  const tmp = path.join(dir, '.' + path.basename(file) + '.' + process.pid + '.tmp');
  const text = JSON.stringify(value, null, pretty ? 2 : 0);
  await fsp.writeFile(tmp, text, 'utf8');
  try {
    await fsp.rename(tmp, file);
  } catch (error) {
    if (error && (error.code === 'EPERM' || error.code === 'EEXIST')) {
      await fsp.rm(file, { force: true });
      await fsp.rename(tmp, file);
    } else {
      await fsp.rm(tmp, { force: true }).catch(() => {});
      throw error;
    }
  }
}

async function readJson(file, fallback) {
  try {
    const text = await fsp.readFile(file, 'utf8');
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function safeName(name, fallback = 'file') {
  const raw = String(name || '').replace(/[\\/:*?"<>|\r\n\t]/g, '_').trim();
  return raw || fallback;
}

module.exports = { writeJsonAtomic, readJson, sha256, safeName };
