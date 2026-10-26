#!/usr/bin/env node
/* HeronBo skill 安装器：把包内的 skill/ 复制到各 agent 的 skills 目录。
 *
 * 用法：
 *   npm i -g heronbo-aigc-prompt        → 全局装，postinstall 自动拷 skill
 *   npx heronbo-aigc-prompt             → 手动跑一次（装完就拷）
 *   HERONBO_SKILL_DIRS="D:/x/skills"    → 指定额外/替代目标目录（分号分隔）
 *
 * 只往「已经存在的 agent 目录」里装：没装那个 agent 就不给它建目录，
 * 免得在用户家目录里堆一堆空壳。一个都没探测到就打印手动指引，不报错退出。
 */
const os = require("os");
const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..", "skill");
const SKILL_DIRNAME = "heronbo-aigc-prompt";

function targets() {
  const home = os.homedir();
  const env = process.env.HERONBO_SKILL_DIRS || "";
  if (env.trim()) {
    return env.split(/[;\n]/).map((s) => s.trim()).filter(Boolean);
  }
  const cands = [
    path.join(home, ".zcode", "skills"),
    path.join(home, ".agents", "skills"),
    path.join(home, ".codebuddy", "skills"),
    path.join(home, ".claude", "skills"),
    path.join(home, ".codex", "skills"),
  ];
  return cands.filter((p) => {
    try {
      return fs.statSync(p).isDirectory();
    } catch (e) {
      return false;
    }
  });
}

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const name of fs.readdirSync(src)) {
    const s = path.join(src, name);
    const d = path.join(dst, name);
    const st = fs.statSync(s);
    if (st.isDirectory()) {
      // 缓存/临时目录不拷
      if (name === "__pycache__" || name === ".git") continue;
      copyDir(s, d);
    } else {
      if (name.endsWith(".pyc")) continue;
      fs.copyFileSync(s, d);
    }
  }
}

function main() {
  if (!fs.existsSync(SRC)) {
    console.error("[heronbo] 包内缺少 skill/ 目录，安装中止（包可能没打全）。");
    process.exit(0); // postinstall 失败会让整个 npm install 红，这里不阻断
  }
  const dirs = targets();
  if (!dirs.length) {
    console.log(
      "[heronbo] 没找到任何 agent 的 skills 目录。\n" +
      "  手动装法：把本包的 skill/ 目录整个复制到 <agent>/skills/heronbo-aigc-prompt/\n" +
      "  也可以设环境变量指定：HERONBO_SKILL_DIRS=\"C:/xx/skills\" npm i -g heronbo-aigc-prompt"
    );
    return;
  }
  for (const base of dirs) {
    const dst = path.join(base, SKILL_DIRNAME);
    try {
      copyDir(SRC, dst);
      console.log("[heronbo] 已装到 " + dst);
    } catch (e) {
      console.error("[heronbo] 装到 " + dst + " 失败：" + e.message);
    }
  }
  console.log(
    "[heronbo] 完成。提示词工具里的 Python 脚本需要 Python 3.9+（首次运行可跑 " +
    "python tools/环境检查.py 看缺什么）。"
  );
}

main();
