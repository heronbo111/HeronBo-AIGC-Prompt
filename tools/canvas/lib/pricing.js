/**
 * 生成点数预估。
 *
 * 豆包客户端不回传实际扣费，这里只能"按档位估算"。数值放在
 * data/config.json 的 pricing 字段里，可随时按真实账单校准。
 */
const DEFAULT_PRICING = {
  note: '预估：客户端不回传实际扣费，按档位/秒数折算；以豆包账单为准。',
  image: {
    // 2026-11 校准：原表 1K=1 / 2K=2 / 4K=4 明显偏低，实际扣点约为其两倍。
    perSheet: { '1K': 2, '2K': 4, '4K': 8 },
    modelFactor: { 'seedream_5.5': 2.5, 'seedream_5.0_flash': 1, 'seedream_5.0_pro': 2, 'seedream_4.5': 1.5, 'seedream_4.0': 1.2 },
  },
  video: {
    // 2026-11 按实测记录校准（720×1280）：seedance_2.5 约 37.5 点/秒，
    // 2.0_fast 约 40 点/秒；其余档位无实测，按画质/档位比例推算。
    perSecond: {
      'seedance_2.5': { '480P': 22, '720P': 37.5, '1080P': 56 },
      'seedance_2.0': { '720P': 30, '1080P': 45 },
      'seedance_2.0_fast': { '720P': 40 },
      'seedance_2.0_mini': { '720P': 20 },
    },
  },
};

function pickRate(table, model, quality) {
  const row = table && table[model];
  if (!row) return 0;
  if (typeof row === 'number') return row;
  return Number(row[quality]) || Number(row[Object.keys(row)[0]]) || 0;
}

/** 返回 { points, unit, rate, detail }；points 为 0 表示没配到价格。 */
function estimatePoints({ kind, model, quality, duration, count } = {}, pricing = DEFAULT_PRICING) {
  if (kind === 'video') {
    const rate = pickRate(pricing.video && pricing.video.perSecond, model, quality);
    const seconds = Math.max(1, Number(duration) || 5);
    return {
      points: Math.round(rate * seconds * 10) / 10,
      unit: '点',
      rate,
      detail: rate ? `${rate} 点/秒 × ${seconds} 秒` : '这个档位还没配价格',
    };
  }
  const base = (pricing.image && pricing.image.perSheet && pricing.image.perSheet[quality]) || 0;
  const factor = (pricing.image && pricing.image.modelFactor && pricing.image.modelFactor[model]) || 1;
  const sheets = Math.max(1, Number(count) || 1);
  return {
    points: Math.round(base * factor * sheets * 10) / 10,
    unit: '点',
    rate: base * factor,
    detail: base ? `${base} 点/张 × ${factor}（模型系数）× ${sheets} 张` : '这个清晰度还没配价格',
  };
}

module.exports = { DEFAULT_PRICING, estimatePoints };
