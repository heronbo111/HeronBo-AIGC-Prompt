# -*- coding: utf-8 -*-
"""打一个「市场版」Skill 包（照 open.workbuddy.cn/docs/skill 的规范）。

规范要点（照官方文档）：
- 包内结构：{skill-name}/ ├ SKILL.md（必须）├ references/ ├ scripts/ └ templates/
- 目录**最多两级**；references/scripts/templates 之外的目录别放
- SKILL.md 用 YAML frontmatter：description / description_zh / description_en / version / author 必填
- 平台收 ZIP，**≤3MB**
- **绝不带**本机取值、密钥、Cookie、大二进制

本脚本产出：tools/_stage/HeronBo-AIGC-Prompt-市场版-v<版本>.zip
不打包的东西（都是刻意排除，理由写在随包的提交说明里）：
  界面本体（workbench/ + workbench_server.py）：3 层结构超规范，市场版只出「提示词大脑」
  _vendor / dist（二进制与 exe）、samples（大视频）、*.local.*（本机取值与密钥）
"""
import io
import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(ROOT, "_stage", "mp")

INCLUDE_REFS = ["rules.md", "prompt-templates.md", "playbooks.md", "platforms.md",
                "samples-db.md", "paths.md", "eval-cases.md"]
INCLUDE_SCRIPTS = ["project_core.py", "score_core.py", "agent_bridge.py",
                   "评价回收.py", "首次配置.py", "成片拆解.py", "成片对比.py",
                   "提示词体检.py", "下载B站成片.py", "竖版画布.py", "环境检查.py"]

MARKET_NOTE = """
> **本包是「市场版」**（`HeronBo-AIGC-Prompt` v{ver}）。相对仓库完整版，这里只放
> **提示词大脑**：规则 / 模板 / 实战手册 / 平台表 + 纯文本工具脚本。
> 未包含（体积与目录规范所限，也不是提示词生成所必需）：
> 图形工作台（`tools/workbench/` 与 `workbench_server.py`）、评分窗口、离线依赖与 exe、样本视频。
>
> **包内路径映射**：下文凡写 `tools\\xxx.py`，在本包里对应 `scripts/xxx.py`；
> 凡写 `tools\\dist\\...`、`tools\\workbench\\...` 的，本包未包含，遇到时跳过该步即可。
> 本机路径/平台取值按 `references/paths.md` 的说明在本机新建（**不要**把本机路径写进交付物）。
"""


def frontmatter_of(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return m


def main():
    skill_ver = "2.6"
    src_skill = io.open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()
    m = re.search(r"^version:\s*(\S+)", src_skill, re.M)
    if m:
        skill_ver = m.group(1)

    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    name = "HeronBo-AIGC-Prompt"
    dst = os.path.join(STAGE, name)
    os.makedirs(os.path.join(dst, "references"))
    os.makedirs(os.path.join(dst, "scripts"))
    os.makedirs(os.path.join(dst, "templates"))

    # ── SKILL.md：补市场要的字段 + 顶部加「市场版」说明 ──────────────────
    fm = frontmatter_of(src_skill)
    body = src_skill[fm.end():]
    fmtext = fm.group(1)
    desc = re.search(r"^description:\s*(.+)$", fmtext, re.M)
    desc_line = desc.group(1).strip() if desc else "生成各类 AI 视频的提示词"
    new_fm = "\n".join([
        "---",
        "name: %s" % name,
        "display_name: AI 视频提示词生成",
        "display_name_en: AI Video Prompt Generator",
        "description: %s" % desc_line,
        "description_zh: 把文案/台词与素材（图片/视频/音频）整理成可直接粘贴到生成平台的提示词，"
        "覆盖图生视频、文生视频、参考视频生视频、参考视频替换生视频；并按每次成片反馈持续优化规则。",
        "description_en: Turn scripts and assets into copy-paste-ready prompts for AI video "
        "generation platforms (image-to-video, text-to-video, reference-video and replacement "
        "workflows), and keep improving the rulebook from every finished-video feedback.",
        "category: writing",
        "version: %s" % skill_ver,
        "author: HeronBo",
        "agent_created: true",
        "---",
    ])
    io.open(os.path.join(dst, "SKILL.md"), "w", encoding="utf-8", newline="\n").write(
        new_fm + "\n" + MARKET_NOTE.format(ver=skill_ver) + body)
    print("SKILL.md 已改写（frontmatter 补 description_zh/en/author/category）")

    # ── references ──────────────────────────────────────────────────────
    n_ref = 0
    for f in INCLUDE_REFS:
        p = os.path.join(ROOT, "references", f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, "references", f))
            n_ref += 1
    print("references：%d 个" % n_ref)

    # ── scripts ─────────────────────────────────────────────────────────
    n_sc = 0
    for f in INCLUDE_SCRIPTS:
        p = os.path.join(ROOT, "tools", f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(dst, "scripts", f))
            n_sc += 1
    print("scripts：%d 个" % n_sc)

    # ── templates：放一份交付模板占位（平台允许空，但留个可复用骨架更好）──
    io.open(os.path.join(dst, "templates", "交付清单模板.md"), "w",
            encoding="utf-8", newline="\n").write(
        "# 交付清单（每次出提示词后照着填）\n\n"
        "- 项目：\n- 类型：图生视频 / 文生视频 / 参考视频生视频 / 参考视频替换生视频\n"
        "- 素材与引用编号：@图片1=… @视频1=…\n- 提示词条数与镜头单元：\n"
        "- 画幅（成片画幅要在平台手选）：\n- 未经证实的地方（标「待验证」）：\n")
    print("templates：1 个")

    # ── 自检后打包 ──────────────────────────────────────────────────────
    bad = []
    for dp, dn, fn in os.walk(dst):
        for f in fn:
            fp = os.path.join(dp, f)
            rel = os.path.relpath(fp, STAGE).replace("\\", "/")
            if rel.count("/") > 2:
                bad.append("层级过深：%s" % rel)
            if ".local." in f:
                bad.append("夹带本机文件：%s" % rel)
            if f.lower().endswith((".exe", ".dll", ".zip", ".pyd", ".onnx", ".mp4", ".png")):
                bad.append("夹带二进制：%s" % rel)
            try:
                t = io.open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            # 只认「真实本机值」：`C:\Users\<用户名>` 是模板占位符，合法
            if re.search(r"[A-Za-z]:[\\/]AI创作", t) or \
               re.search(r"[A-Za-z]:[\\/]Users[\\/](?!<)", t):
                bad.append("含本机路径：%s" % rel)
            if re.search(r"(?i)(api[_-]?key|access[_-]?key|password|cookie)\s*[:=]\s*\S{8,}", t):
                bad.append("疑似密钥：%s" % rel)
    print("\n自检：%s" % ("通过 ✅" if not bad else "有问题 ❌"))
    for b in bad:
        print("   " + b)
    if bad:
        return 2

    out = os.path.join(ROOT, "_stage", "%s-市场版-v%s.zip" % (name, skill_ver))
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dn, fn in os.walk(dst):
            for f in fn:
                fp = os.path.join(dp, f)
                z.write(fp, os.path.relpath(fp, STAGE).replace("\\", "/"))
                n += 1
    kb = os.path.getsize(out) / 1024.0
    print("\n产物：%s" % out)
    print("  %d 个文件，%.1f KB（上限 3072 KB）%s" % (n, kb, "✅" if kb <= 3072 else "❌ 超了"))
    return 0 if kb <= 3072 else 3


if __name__ == "__main__":
    raise SystemExit(main())
