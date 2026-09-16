# -*- coding: utf-8 -*-
r"""提示词体检：按《统一骨架》检查一份交付提示词，把"风格不一致"变成机器能判的事。

为什么要有它（2026-09-16 用户反馈）：同一天两个项目的提示词**小节集合、顺序、交付格式各写各的**——
一个项目是纯正文 + 【素材分工】【替换】【景别与构图】…，另一个是 markdown 报告、正文被埋在第二节里、
小节叫【角色】【场景与站位】【动作与台词】。用户的原话是"风格不一致的错乱情况"。
人写提示词时靠自觉记不住，所以把骨架写成**可检查的规则**：agent 出完提示词跑一遍，不合格就改，
而不是等用户看出来。

骨架的唯一真相源：`references/prompt-templates.md` 开头的「统一骨架」一节（本工具的检查项与它一一对应）。

用法：
    python tools\提示词体检.py --project "F:\...\某项目"      # 读 项目根\提示词.txt 或 文案\提示词.txt
    python tools\提示词体检.py -i "<路径>\提示词.txt"
    python tools\提示词体检.py --project "<项目>" --json      # 机读（给 agent / 工作台用）

退出码：0＝通过（可能有 WARN）· 1＝有 FAIL。
"""
import argparse
import io
import json
import os
import re
import sys

# 小节的固定名字与固定顺序（改这里＝改骨架，必须同时改 prompt-templates.md 的同一节）
SECTIONS = [
    ("总纲", "必", "一句话说清这条以谁为准、做什么"),
    ("素材分工", "必", "每个 @ 只提供什么、不提供什么"),
    ("主体", "必", "人物/角色锚定：严格参考@图片N + 逐项外观"),
    ("场景", "按需", "环境与光线；无场景素材就写「背景全部由@图片N决定」"),
    ("道具", "按需", "产品/道具锚定；没有整节删掉"),
    ("动作与时间轴", "必", "每拍一行：`0–2.5秒 动作（镜头运动）`"),
    ("口播·音色·口型", "按需", "有台词必写；无台词改【声音】"),
    ("声音", "按需", "无台词片用它写「音乐沿用原声/全片静音」"),
    ("镜头与景别", "必", "机位/运动/构图；静止就写死「全程绝对无运镜」"),
    ("画面纪律", "必", "几个人、不得出现的元素、画幅与时长"),
    ("负面", "必", "集中列禁止项，别和正向句混排"),
]
# 允许出现的别名（历史交付用过的叫法）——**不算违规，但会提示统一**
ALIASES = {"口播": "口播·音色·口型", "质量与负面": "负面", "景别与构图": "镜头与景别",
           "动作与台词": "动作与时间轴", "角色": "主体", "场景与站位": "场景"}

TIME_RE = re.compile(r"^\s*(?:>\s*)?(\d+(?:\.\d+)?)\s*[–—\-~至]\s*(\d+(?:\.\d+)?)\s*秒")
BEAT_RE = re.compile(r"(?:>\s*)?(\d+(?:\.\d+)?)\s*[–—\-~至]\s*(\d+(?:\.\d+)?)\s*秒")
SEC_RE = re.compile(r"【([^】]{1,12})】")
QUOTE_RE = re.compile(r"[\"“]([^\"”]{2,})[\"”]")
REF_RE = re.compile(r"@(图片|视频|音频)(\d+)")

OK, WARN, FAIL = [], [], []


def say(level, name, detail=""):
    (OK if level == "PASS" else WARN if level == "WARN" else FAIL).append((name, detail))


def read_prompt(args):
    if args.inp:
        p = os.path.abspath(args.inp)
        return (p, io.open(p, encoding="utf-8-sig").read()) if os.path.isfile(p) else ("", "")
    root = os.path.abspath(args.project)
    for rel in ("提示词.txt", os.path.join("文案", "提示词.txt")):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            return p, io.open(p, encoding="utf-8-sig").read()
    # 退一步：文案/ 下带"提示词"的任意 txt
    d = os.path.join(root, "文案")
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if "提示词" in fn and fn.lower().endswith(".txt"):
                p = os.path.join(d, fn)
                return p, io.open(p, encoding="utf-8-sig").read()
    return "", ""


def check(path, text, pdir=""):
    if not text:
        say("FAIL", "找得到提示词文件", "项目根\提示词.txt 与 文案\提示词.txt 都没有")
        return
    lines = text.splitlines()

    # ① 元信息头：前 6 行里要有 项目/形态/台词 三样
    head = "\n".join(lines[:6])
    if not re.search(r"提示词\s*[·:：]", head):
        say("FAIL", "元信息第 1 行", "应以「提示词 · <项目名>」开头")
    else:
        say("PASS", "元信息第 1 行")
    if not re.search(r"形态[:：]", head):
        say("WARN", "元信息·形态", "补一行「形态：<四大类> ｜ 平台 ｜ 模型 ｜ 时长 ｜ 画幅」（判类靠它）")
    else:
        say("PASS", "元信息·形态")
    if not re.search(r"台词[:：]", head):
        say("WARN", "元信息·台词", "补一行「台词：<全文>（N字 ≈ Ns）」；没台词写「台词：无台词（音乐沿用原声）」")
    else:
        say("PASS", "元信息·台词")

    # ② 正文里不能混排 markdown 报告（表格/##/水平线/引用块）——那些挪 备注/备注.txt
    bad_doc = []
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if s.startswith("## ") or s.startswith("### "):
            bad_doc.append("第%d行 %s" % (i, s[:24]))
        elif re.match(r"^\|\s*[^|]*\|", s):
            bad_doc.append("第%d行 表格" % i)
        elif s in ("---", "***", "___"):
            bad_doc.append("第%d行 分隔线" % i)
        elif s.startswith("> "):
            bad_doc.append("第%d行 引用块" % i)
    if bad_doc:
        say("FAIL", "正文只用【】小节（不带 markdown 报告壳）",
            "把表格/标题/引用块挪到 备注/备注.txt；正文只留可直接复制的内容。位置：" + "、".join(bad_doc[:4]))
    else:
        say("PASS", "正文只用【】小节（不带 markdown 报告壳）")

    # ③ 小节：名字与顺序
    secs = [(m.group(1), i) for i, ln in enumerate(lines) for m in [SEC_RE.search(ln)] if m]
    seen = []
    for name, _i in secs:
        canon = name if name in dict((s[0], s) for s in SECTIONS) else ALIASES.get(name, "")
        if canon and canon not in seen:
            seen.append(canon)
    required = [s[0] for s in SECTIONS if s[1] == "必"]
    miss = [s for s in required if s not in seen]
    if miss:
        say("FAIL", "必备小节齐", "缺：" + "、".join("【%s】" % m for m in miss))
    else:
        say("PASS", "必备小节齐")
    order_map = {s[0]: n for n, s in enumerate(SECTIONS)}
    pos = [order_map[n] for n in seen if n in order_map]
    if pos != sorted(pos):
        say("FAIL", "小节顺序", "应为：" + " → ".join(s[0] for s in SECTIONS)
            + "；实际：" + " → ".join(seen))
    else:
        say("PASS", "小节顺序", " → ".join(seen) if seen else "（无）")
    odd = [n for n, _i in secs if n not in dict((s[0], s) for s in SECTIONS)]
    if odd:
        unknown = [n for n in dict.fromkeys(odd) if n not in ALIASES]
        if unknown:
            say("WARN", "小节名字统一", "这些不是骨架里的名字：" + "、".join("【%s】" % u for u in unknown))
        else:
            say("WARN", "小节名字统一", "用了历史别名：" + "、".join("【%s】→【%s】" % (o, ALIASES[o])
                                                              for o in dict.fromkeys(odd)))

    # ④ 时间轴（行首允许带 `> `：历史交付里整段正文包在引用块里）
    beats = [TIME_RE.match(ln) for ln in lines if TIME_RE.match(ln)]
    if not beats:
        say("FAIL", "动作与时间轴有拍点", "至少一行 `0–2.5秒 …`（模型最吃这个结构）")
    else:
        say("PASS", "动作与时间轴有拍点", "%d 拍" % len(beats))
        starts = [float(m.group(1)) for m in beats]
        ends = [float(m.group(2)) for m in beats]
        if abs(starts[0]) > 0.05:
            say("WARN", "时间轴从 0 开始", "第一拍起点是 %.1fs" % starts[0])
        else:
            say("PASS", "时间轴从 0 开始")
        gap = [(ends[i], starts[i + 1]) for i in range(len(starts) - 1) if starts[i + 1] - ends[i] > 0.6]
        if gap:
            say("WARN", "时间轴连续（不跳段）",
                "有空档或重叠：" + "、".join("%.1fs→%.1fs" % g for g in gap[:3]))
        else:
            say("PASS", "时间轴连续（不跳段）")
        m = re.search(r"时长[:：]\s*(\d+(?:\.\d+)?)\s*秒|时长[:：]\s*(\d+(?:\.\d+)?)\s*s", head)
        want = float(m.group(1) or m.group(2)) if m else 0
        if want and abs(ends[-1] - want) > 0.6:
            say("WARN", "末拍对齐总时长", "元信息写 %.1fs，最后一拍结束在 %.1fs" % (want, ends[-1]))
        else:
            say("PASS", "末拍对齐总时长", ("%.1fs" % want) if want else "（元信息没写时长，跳过）")

    # ⑤ 引用一致：正文出现的 @ 编号必须在「上传：」行里点名
    uploads = "\n".join(ln for ln in lines if ln.strip().startswith("上传"))
    refs = sorted(set(REF_RE.findall(text)))
    if refs and not uploads:
        say("FAIL", "有「上传：」行点名素材",
            "正文引用了 @%s，但没有一行「上传：@图片1=文件名 ｜ …」" % refs[0][0] + str(refs[0][1]))
    elif refs:
        missing = [k + n for k, n in refs if ("@" + k + n) not in uploads]
        if missing:
            say("FAIL", "上传行点名了所有被引用的素材", "缺：" + "、".join(missing))
        else:
            say("PASS", "上传行点名了所有被引用的素材", "%d 个" % len(refs))
    else:
        say("WARN", "引用检查", "正文里没有任何 @图片/@视频/@音频 引用（纯文生视频才正常）")

    # ⑥ 生成类不许出现 @视频（rules 第10条：整条会作废）
    if re.search(r"形态[:：][^\n]*图生视频", head) and any(k == "视频" for k, _n in refs):
        say("FAIL", "图生视频不写 @视频", "规则第10条：生成类出现「参考视频」字样会让整条指令失效")
    else:
        say("PASS", "生成类不写 @视频")

    # ⑦ 台词一致：元信息台词 → 正文必须原样出现（按分句比，容忍时间轴把台词拆到各拍里）
    m = re.search(r"台词[:：]\s*(.+)$", head, re.M)
    if m and "无台词" not in m.group(1):
        want = re.sub(r"（[^）]*）|\([^)]*\)", "", m.group(1)).strip().strip("\"'“”")
        body = text[text.find(m.group(0)) + len(m.group(0)):]
        clauses = [c.strip() for c in re.split(r"[，,。！!？?；;、\s]+", want) if len(c.strip()) >= 3]
        missc = [c for c in clauses if c not in body]
        if clauses and len(missc) == len(clauses):
            say("FAIL", "台词与正文一致", "元信息里的台词在正文里没出现：%s…" % want[:14])
        elif missc:
            say("WARN", "台词与正文一致", "这些分句没在正文里逐字出现（多数是被时间轴拆开了，核一下）："
                + "、".join(missc[:4]))
        else:
            say("PASS", "台词与正文一致", "%d 个分句逐字对上" % len(clauses))
        # 引号配平（写半截引号是常见手误）
        if text.count("“") != text.count("”"):
            say("WARN", "引号配平", "中文引号不成对（“ %d 个 / ” %d 个）" % (text.count("“"), text.count("”")))
    else:
        say("PASS", "台词与正文一致", "（本条无台词）")

    # ⑧ 负面覆盖
    need = [("字幕", "必"), ("文字", "必"), ("水印", "必"), ("畸变", "必"),
            ("音效", "按需"), ("旁白", "按需")]
    miss_n = [w for w, lv in need if lv == "必" and w not in text]
    if miss_n:
        say("FAIL", "负面清单覆盖", "缺：" + "、".join("不要出现%s" % w for w in miss_n))
    else:
        say("PASS", "负面清单覆盖")
    miss_o = [w for w, lv in need if lv == "按需" and w not in text]
    if miss_o:
        say("WARN", "负面清单·声音类", "建议点名：" + "、".join(miss_o) + "（有台词的条也该禁 BGM/音效/旁白）")

    # ⑨ 重复句（同一条禁令写三遍 → 噪声，模型会忽略）。**按版本分段比**：
    #    多版本之间"主体锚定""时间轴"本来就该逐字复用，跨版本比会把对的判成错的。
    chunks, cur = [], []
    for ln in lines:
        if re.match(r"^\s*═══\s*版本", ln) and cur:
            chunks.append(cur)
            cur = []
        cur.append(ln)
    chunks.append(cur)
    if len(chunks) < 2:
        chunks = [lines]
    repeated = []
    for ch in chunks:
        dup = {}
        for ln in ch:
            for sent in re.split(r"[。；;]", ln):
                s = sent.strip()
                if len(s) >= 8:
                    dup[s] = dup.get(s, 0) + 1
        repeated += [(s, n) for s, n in dup.items() if n >= 2]
    if repeated:
        say("WARN", "不重复同一句话", "%d 句重复了，合并一下（重复=噪声）：%s"
            % (len(repeated), "；".join("「%s」×%d" % (s[:18], n) for s, n in repeated[:3])))
    else:
        say("PASS", "不重复同一句话")

    # ⑩ 版本段
    vers = re.findall(r"═══\s*版本(\d+)[^═]*═══", text)
    if len(vers) >= 2:
        say("PASS", "多版本分段", "版本：" + "、".join(vers))
    elif len(vers) == 1:
        say("WARN", "多版本分段", "只写了一段「版本1」；单版本时可不写版本行，多版本必须每段带「上传：」")
    else:
        say("PASS", "多版本分段", "（单版本）")
    return OK, WARN, FAIL


def check_path(path):
    """给程序用的入口：返回 `(ok, 通过, 注意, 不合格)`，每项是 `(名字, 说明)`。"""
    global OK, WARN, FAIL
    OK, WARN, FAIL = [], [], []
    try:
        text = io.open(path, encoding="utf-8-sig").read()
    except OSError:
        return False, [], [], [("读得到提示词文件", path)]
    check(path, text)
    return (not FAIL), list(OK), list(WARN), list(FAIL)


def main():
    ap = argparse.ArgumentParser(description="提示词体检（按统一骨架检查一份交付提示词）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="项目目录（自动找 项目根\提示词.txt 或 文案\提示词.txt）")
    g.add_argument("-i", "--inp", help="直接给提示词文件的路径")
    ap.add_argument("--json", action="store_true", help="机读输出")
    a = ap.parse_args()
    path, text = read_prompt(a)
    if not text:
        print("找不到提示词文件：%s" % (a.project or a.inp))
        return 1
    OK[:], WARN[:], FAIL[:] = [[], [], []]
    check(path, text)
    if a.json:
        print(json.dumps({"file": path, "ok": not FAIL,
                          "pass": [{"name": n, "detail": d} for n, d in OK],
                          "warn": [{"name": n, "detail": d} for n, d in WARN],
                          "fail": [{"name": n, "detail": d} for n, d in FAIL]},
                         ensure_ascii=False, indent=2))
        return 1 if FAIL else 0
    print("提示词体检 · %s" % path)
    print("骨架真相源：references/prompt-templates.md 的「统一骨架」一节\n")
    for n, d in OK:
        print("  [通过] %s%s" % (n, ("  " + d) if d else ""))
    for n, d in WARN:
        print("  [注意] %s%s" % (n, ("\n         ↳ " + d) if d else ""))
    for n, d in FAIL:
        print("  [不合格] %s%s" % (n, ("\n         ↳ " + d) if d else ""))
    print("\n小结：通过 %d · 注意 %d · 不合格 %d" % (len(OK), len(WARN), len(FAIL)))
    if FAIL:
        print("改法：只改 format（小节名/顺序/交付格式），**不要为了过检改内容**。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
