const fs = require('node:fs');
const path = require('node:path');

/**
 * 内置提示词 skill 库。
 *
 * 一个 skill = prompts/skills/<id>.md，开头是 frontmatter（id/name/icon/hint/keywords），
 * 正文是喂给 Agent 的写作口径。用户想改/加，直接改这个目录里的 md，不用动代码。
 * 目录 mtime 变了才重新读，避免每次请求都扫盘。
 */
class SkillLibrary {
  constructor(rootDir) {
    this.dir = path.join(rootDir, 'prompts', 'skills');
    this.cache = [];
    this.stamp = -1;
  }

  parse(raw, fallbackId) {
    const text = String(raw || '');
    const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
    const meta = {};
    let body = text;
    if (match) {
      body = text.slice(match[0].length);
      for (const line of match[1].split(/\r?\n/)) {
        const at = line.indexOf(':');
        if (at <= 0) continue;
        const key = line.slice(0, at).trim();
        const value = line.slice(at + 1).trim();
        if (key) meta[key] = value;
      }
    }
    const id = String(meta.id || fallbackId || '').replace(/[^a-z0-9_-]/gi, '');
    if (!id) return null;
    return {
      id,
      name: String(meta.name || id).slice(0, 30),
      icon: String(meta.icon || '✳').slice(0, 4),
      hint: String(meta.hint || '').slice(0, 120),
      keywords: String(meta.keywords || '').split(/[,，\s]+/).filter(Boolean).slice(0, 20),
      body: body.trim(),
    };
  }

  load(force = false) {
    let stamp = 0;
    try {
      stamp = fs.statSync(this.dir).mtimeMs;
    } catch {
      this.cache = [];
      this.stamp = -1;
      return this.cache;
    }
    if (!force && stamp === this.stamp) return this.cache;
    let names = [];
    try {
      names = fs.readdirSync(this.dir).filter((name) => /\.md$/i.test(name)).sort();
    } catch {
      names = [];
    }
    const list = [];
    for (const name of names) {
      try {
        const skill = this.parse(fs.readFileSync(path.join(this.dir, name), 'utf8'), name.replace(/\.md$/i, ''));
        if (skill && skill.body) list.push(skill);
      } catch { /* 单个文件坏掉不影响其它 skill */ }
    }
    this.cache = list;
    this.stamp = stamp;
    return list;
  }

  list() {
    return this.load().map(({ id, name, icon, hint, keywords }) => ({ id, name, icon, hint, keywords }));
  }

  get(id) {
    const key = String(id || '').trim().toLowerCase();
    if (!key) return null;
    return this.load().find((skill) => skill.id.toLowerCase() === key) || null;
  }

  /** 给模型的 skill 索引（auto 模式用）。 */
  indexText() {
    return this.load()
      .map((skill) => `- ${skill.id}（${skill.name}）：${skill.hint}`)
      .join('\n');
  }
}

module.exports = { SkillLibrary };
