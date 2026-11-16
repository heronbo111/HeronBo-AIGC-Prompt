const fs = require('node:fs');
const path = require('node:path');

/**
 * 消耗账本：每次文本调用、每次生成任务都落一条，追加写 data/ledger.jsonl。
 *
 * 口径必须写在记录里，避免以后有人把估算当账单：
 * - 生成：豆包客户端不回传实际扣费，按 data/config.json 的 pricing 档位估算，pointsEstimated=true。
 * - 文本：CDP 模式不回传 token，只记调用次数与输入/输出字符数；网关模式若返回 usage 则记 tokens。
 */
class Ledger {
  constructor(dataDir) {
    this.dataDir = dataDir;
    this.file = path.join(dataDir, 'ledger.jsonl');
  }

  record(entry = {}) {
    const row = {
      id: 'l' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
      at: new Date().toISOString(),
      kind: 'chat',
      action: '',
      canvasId: '',
      nodeId: '',
      model: '',
      quality: '',
      duration: 0,
      count: 0,
      points: 0,
      pointsEstimated: false,
      status: 'done',
      error: '',
      ms: 0,
      charsIn: 0,
      charsOut: 0,
      tokensIn: 0,
      tokensOut: 0,
      note: '',
      ...entry,
    };
    try {
      fs.mkdirSync(this.dataDir, { recursive: true });
      fs.appendFileSync(this.file, JSON.stringify(row) + '\n');
      this.trimIfNeeded();
    } catch { /* 账本失败不影响主流程 */ }
    return row;
  }

  /** 倒序读最近 N 条；坏行跳过，不让一条脏数据毁掉整页。 */
  recent(limit = 200) {
    let text = '';
    try { text = fs.readFileSync(this.file, 'utf8'); } catch { return []; }
    const lines = text.split('\n').filter(Boolean);
    const rows = [];
    for (let i = lines.length - 1; i >= 0 && rows.length < limit; i -= 1) {
      try { rows.push(JSON.parse(lines[i])); } catch { /* 跳过坏行 */ }
    }
    return rows;
  }

  /** 汇总：总量、今日、近 7 天、按类型与模型分组。 */
  summary(rows = this.recent(1000)) {
    const now = Date.now();
    const dayStart = new Date();
    dayStart.setHours(0, 0, 0, 0);
    const todayFrom = dayStart.getTime();
    const weekFrom = now - 7 * 24 * 60 * 60 * 1000;
    const total = { points: 0, calls: 0 };
    const today = { points: 0, calls: 0 };
    const week = { points: 0, calls: 0 };
    const byKind = {};
    const byModel = {};
    for (const row of rows) {
      const points = Number(row.points) || 0;
      const at = Date.parse(row.at) || 0;
      total.points += points;
      total.calls += 1;
      if (at >= todayFrom) { today.points += points; today.calls += 1; }
      if (at >= weekFrom) { week.points += points; week.calls += 1; }
      const kind = row.kind || 'chat';
      if (!byKind[kind]) byKind[kind] = { points: 0, calls: 0 };
      byKind[kind].points += points;
      byKind[kind].calls += 1;
      const model = row.model || '（默认）';
      if (!byModel[model]) byModel[model] = { points: 0, calls: 0 };
      byModel[model].points += points;
      byModel[model].calls += 1;
    }
    return {
      total: round(total),
      today: round(today),
      week: round(week),
      byKind: mapRound(byKind),
      byModel: mapRound(byModel),
      file: this.file,
      note: '生成点数按档位估算（客户端不回传实际扣费，以豆包账单为准）；文本调用不计生成点数。',
    };
  }

  /** 文件超过 4MB 轮转一次，保留 ledger.jsonl.1。 */
  trimIfNeeded() {
    try {
      const stat = fs.statSync(this.file);
      if (stat.size < 4 * 1024 * 1024) return;
      const backup = this.file + '.1';
      try { fs.rmSync(backup, { force: true }); } catch { /* ignore */ }
      fs.renameSync(this.file, backup);
    } catch { /* 首次写 */ }
  }
}

function round(row) {
  return { points: Math.round(row.points * 10) / 10, calls: row.calls };
}

function mapRound(map) {
  const out = {};
  for (const key of Object.keys(map)) out[key] = round(map[key]);
  return out;
}

module.exports = { Ledger };
