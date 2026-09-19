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
    #   **段落式**（2026-09-18 用户裁定）：正文是一整段、不带【】小节名——这是用户指定的默认形态
    #   （参照他们一直在用的那条：括号包起来的一段，动作与台词逐句绑定）。此时跳过小节/拍点类检查，
    #   只保留"内容覆盖"检查（台词逐字、负面四词、密度、@一致等）。
    secs = [(m.group(1), i) for i, ln in enumerate(lines) for m in [SEC_RE.search(ln)] if m]
    para_mode = not secs
    if para_mode:
        say("PASS", "段落式正文（用户 2026-09-18 指定形态）",
            "不带【】小节名；内容覆盖改用下面的检查项把关（口诀：人物形象 / 场景 / 镜头 / "
            "口播与音色 / 动作与台词 / 禁令 六件事都要在段里点到）")
    seen = []
    for name, _i in secs:
        canon = name if name in dict((s[0], s) for s in SECTIONS) else ALIASES.get(name, "")
        if canon and canon not in seen:
            seen.append(canon)
    required = [s[0] for s in SECTIONS if s[1] == "必"]
    miss = [s for s in required if s not in seen]
    if para_mode:
        pass                      # 段落式：不按小节名判，改由"六件事覆盖"检查（见下）
    elif miss:
        say("FAIL", "必备小节齐", "缺：" + "、".join("【%s】" % m for m in miss))
    else:
        say("PASS", "必备小节齐")
    order_map = {s[0]: n for n, s in enumerate(SECTIONS)}
    pos = [order_map[n] for n in seen if n in order_map]
    if para_mode:
        pass
    elif pos != sorted(pos):
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
    if not beats and para_mode:
        say("PASS", "动作与时间轴（段落式）",
            "段落式把动作绑在台词句里、不设 `a–b秒` 拍点；**动作必须逐句写清楚**"
            "（手部动作 + 眼神/眉/下巴 + 语气 + 收势），并由 @视频1 兜住节奏")
    elif not beats:
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

    # ④.5 「提示词正文.txt」＝**只放能复制的那段正文**（2026-09-16 用户要求：
    # ③栏只显示最核心要复制的东西）。它必须与 提示词.txt 里的主版正文逐字一致，否则会两处打架。
    body_name = "提示词正文.txt"
    tdir = os.path.dirname(path) if os.path.basename(path) != body_name else os.path.dirname(
        os.path.dirname(path))
    cands = [os.path.join(tdir, body_name), os.path.join(os.path.dirname(tdir), body_name)]
    body_path = next((x for x in cands if os.path.isfile(x)), "")
    if os.path.basename(path) == body_name:
        say("PASS", "提示词正文.txt", "（本次体检的就是它）")
    elif not body_path:
        say("WARN", "有 提示词正文.txt", "③栏与「复制提示词」默认显示它——建议把主版正文（【总纲】到【负面】）"
            "单独存一份到项目根 %s" % body_name)
    else:
        try:
            bt = io.open(body_path, encoding="utf-8-sig").read().strip()
        except OSError:
            bt = ""
        if not bt:
            say("FAIL", "提示词正文.txt 非空", body_path)
        elif re.search(r"^\s*(提示词\s*[·:：]|形态[:：]|台词[:：]|上传[:：])", bt, re.M):
            say("FAIL", "提示词正文.txt 里只有正文",
                "它不该带元信息行（提示词 ·/形态：/台词：/上传：）——那是 提示词.txt 的活")
        elif bt not in text:
            say("FAIL", "提示词正文.txt 与 提示词.txt 一致",
                "正文.txt 的内容在 提示词.txt 里找不到（两份打架了）——改完提示词记得同步它")
        else:
            say("PASS", "提示词正文.txt 与 提示词.txt 一致", "逐字对上")

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
        want = re.sub(r"（[^）]*）|\([^)]*\)", "", m.group(1)).strip().strip("\"'“”「」『』")
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

    # ⑧.4 替换类写法升级（2026-09-19 加；来源：B站 UP「逢坂的大河」的 H3 替换提示词实测，见 rules 第 66 条）
    #   两个最容易漏、漏了直接影响成片的点（都判**注意**级，不判不合格，避免误伤历史稿件）：
    #     · **形象参考边界**：素材图多是人物的生活照或商品棚拍图 → 不写这句，模型会把原图的
    #       **背景、站姿、棚拍角度**一起搬进成片（我们反复踩过：教室照的黑板横幅、礼盒图的白底）
    #     · **毫秒逐拍 + 每拍「严格保持/复刻」句**：UP 口述"每段时间角色在干什么写清楚，模型自动对接，
    #       不会画面串角色串" → 时间轴写 `0000–1600ms`，每拍＝镜头状态 + 动作链 + 保持维度
    if re.search(r"形态[:：][^\n]*替换", head):
        if re.search(r"不要复制|仅为形象参考|不要出现在视频中", text):
            say("PASS", "替换类·形象参考边界")
        else:
            say("WARN", "替换类·形象参考边界",
                "缺「图片只提供外观（发型/五官/身体比例/服装），不要复制图片里的姿势、动作、站姿和构图；"
                "图片仅为形象参考，不要出现在视频中」——不写这句，模型容易把素材图的背景与站姿带进成片（rules 第66条）")
        ms = re.findall(r"\d+\s*[–—\-~]\s*\d+\s*ms", text)
        keep = re.search(r"严格(保持|复刻)", text)
        if not ms and not keep:
            say("WARN", "替换类·毫秒逐拍与保持句",
                "既没有 `0000–1600ms` 毫秒拍点、也没有「严格保持/复刻…」句：按 rules 第66条，"
                "替换类宜逐拍写「镜头状态 + 动作链 + 严格保持（身体姿态/手部/大小/镜头角度/动作幅度/速度/重心）」")
        else:
            say("PASS", "替换类·毫秒逐拍与保持句",
                "%s；%s" % ("毫秒拍点 %d 处" % len(ms) if ms else "无毫秒拍点",
                           "有「严格保持/复刻」句" if keep else "无保持句"))

    # ⑧.5 细节密度（2026-09-17 加，用户："怎么感觉少了点细节刻画，例如表情什么的"）
    #   背景：骨架只规定了"外形"（小节名/顺序），**从来没规定密度**，于是把一条提示词写薄了没人拦。
    #   阈值按**库内实测**标定（2026-09-17 扫全库 23 个项目），只抓"明显写薄"，不误伤既有好稿：
    #     · 无台词类（素材/替换/空镜）：库内 33.8–41.6 → **<32 不合格、<40 注意**
    #     · 有台词类（口播带货）：库内 50.8–90.7（用户认可的那条 56.7）→ **<35 不合格、<45 注意**
    #     · 神态/微表情词：**有台词却一处都没有 = 不合格**（违反 rules 第27条的情绪七项）；
    #       无台词片不强制（素材转绘里人偶没五官，写表情反而是错的）
    has_talk = bool(re.search(r"台词[:：]", head)) and ("无台词" not in head)
    # 只量**描述性内容**：把【负面】与【画面纪律】这两节（禁止清单，列举越短越清楚）剔掉，
    # 否则"禁项写得紧凑"会被误判成"细节薄"（2026-09-17 口径）。
    desc = re.sub(r"【(?:负面|画面纪律)】[\s\S]*?(?=" + "\n" + r"【|\Z)", "", text)
    sents_all = [s.strip() for s in re.split(r"[。；;]", desc) if len(s.strip()) >= 6]
    if len(sents_all) >= 6:
        avg_len = sum(len(s) for s in sents_all) / len(sents_all)
        bad, warn = (35, 45) if has_talk else (32, 40)
        kind = "有台词类" if has_talk else "无台词类"
        beats = [m.group(1) for m in re.finditer(r"^\s*\d+(?:\.\d+)?\s*[–—\-~至]\s*\d+(?:\.\d+)?\s*秒\s*(.*)$",
                                                 text, re.M)]
        extra = ("；每拍均 %d 字" % (sum(len(b) for b in beats) / len(beats))) if len(beats) >= 4 else ""
        if avg_len < bad:
            say("FAIL", "细节密度·每句均字", "%s 只有 %.1f 字/句（库内下界 %s）——写得太薄，"
                "动作/神态/语气都要落到句子里%s" % (kind, avg_len, bad, extra))
        elif avg_len < warn:
            say("WARN", "细节密度·每句均字", "%s %.1f 字/句，偏薄（库内好稿 51–91）；"
                "对照 rules 第27条：动机/眼神/呼吸/脸部/身体/声音/收尾%s" % (kind, avg_len, extra))
        else:
            say("PASS", "细节密度·每句均字", "%.1f 字/句%s" % (avg_len, extra))
    expr_words = [w for w in ("眼神", "眼睑", "眉", "嘴角", "笑意", "神情", "表情", "下巴", "歪头",
                              "点头", "摇头", "耸肩", "语气", "呼吸", "神色") if w in text]
    if not expr_words:
        if has_talk:
            say("FAIL", "细节密度·神态与微表情", "有台词却一处神态/微表情都没有——按 rules 第27条，"
                "每拍至少给「眼神/眼睑/眉/嘴角/笑意/下巴/语气」里的任一项")
        else:
            say("PASS", "细节密度·神态与微表情", "无台词片不强制（素材转绘里人偶没有五官，写表情反而是错的）")
    else:
        n_hit = sum(text.count(w) for w in expr_words)
        per = len(text) / max(1, n_hit)
        say("PASS" if per <= 300 else "WARN", "细节密度·神态与微表情",
            "%d 类命中 %d 处（约每 %d 字 1 处）" % (len(expr_words), n_hit, int(per)))

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
        say("PASS", "多版本分段", "（单版本；多版本时每段要自带「上传：」行）")
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
