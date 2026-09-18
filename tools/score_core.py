# -*- coding: utf-8 -*-
"""成片六维评分（命令行版）——不依赖浏览器，任何装了 Python 的机器直接跑。

与窗口版 `score_gui.pyw`、旧网页版 `评价工具.html` **同一套口径与同一份数据结构**（写出的 json 完全等价），
所以 `tools\\评价回收.py` 能照常回收。

用法：
    python 评分.py                      # 问答式打分（列出待评样本 → 选一个 → 逐项打分）
    python 评分.py --list               # 只看清单：每个样本有哪些成片、上次评分多少
    python 评分.py --show "三本书"       # 看某个样本最近一次评价
    python 评分.py --root "<样本库根>"    # 指定样本库根（默认读 references/paths.local.md）
    python 评分.py --sample "课堂走神" --video 成片.mp4 --口型 同步 --动作 准确 --形象 稳定 --语速 合适 --节奏 准 \
                   --字幕 无 --运镜 无 --畸变 无 --评分 4 --结论 可改 --备注 "老师位置对了"   # 非交互（agent/脚本调用）
    python 评分.py --list --dry-run      # 只看，不写任何文件

评分维度（与网页版一致）：
    口型同步  同步 / 偶尔错位 / 明显不对
    台词      无问题 / 错乱 / 音色不对        ← 2026-09-18 加
    动作跟随  准确 / 部分偏离 / 明显违和
    形象一致性 稳定 / 轻微漂移 / 漂移明显
    语速      合适 / 偏快或偏慢 / 不符
    节奏卡点  准 / 一般 / 没卡上
    违禁项    字幕 / 运镜 / AI畸变：无 / 有
    整体评分  1–5     结论  可用 / 可改 / 作废
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DIMS = [
    ("口型同步", ["同步", "偶尔错位", "明显不对"], "口型"),
    # 台词（2026-09-18 用户要求）：口型是「画面对不对得上嘴」，台词是「这句念得对不对」——
    # 生成里最常见的两类事故正好是错乱（念错/串词/顺序乱）与音色不对（音色不像/串音）。
    ("台词", ["无问题", "错乱", "音色不对"], "台词"),
    ("动作跟随", ["准确", "部分偏离", "明显违和"], "动作"),
    ("形象一致性", ["稳定", "轻微漂移", "漂移明显"], "形象"),
    ("语速", ["合适", "偏快或偏慢", "不符"], "语速"),
    ("节奏卡点", ["准", "一般", "没卡上"], "节奏"),
]
FORBID = [("字幕", "字幕"), ("运镜", "运镜"), ("AI畸变", "畸变")]
OK_VALS = {"同步", "准确", "稳定", "合适", "准", "无", "无问题"}


def detect_root(cli_root=None):
    """样本库根：命令行 > references/paths.local.md > 环境变量"""
    if cli_root:
        return os.path.abspath(cli_root)
    for name in ("paths.local.md", "paths.md"):
        p = os.path.join(PKG, "references", name)
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8"):
                s = line.strip()
                for key in ("SAMPLES_ROOT", "样本库根"):
                    if key in s:
                        val = s.split("=")[-1].split("`")[-2] if "`" in s else s.split("=")[-1]
                        val = val.strip().strip('`').strip('"').strip()
                        if val and os.path.isdir(val):
                            return os.path.abspath(val)
    env = os.environ.get("SAMPLES_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    sys.exit("[错误] 没找到样本库根目录：请用 --root 指定，或先把路径写进 references/paths.local.md")


def scan(root):
    """扫描样本：每个子目录里的 成片/ 与 评价/"""
    out = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name.startswith("_"):
            continue
        vids = []
        for sub in ("成片", "废片"):
            vd = os.path.join(d, sub)
            if os.path.isdir(vd):
                for f in sorted(os.listdir(vd)):
                    if f.lower().endswith((".mp4", ".mov", ".webm")):
                        vids.append("%s/%s" % (sub, f))
        revs = []
        rd = os.path.join(d, "评价")
        if os.path.isdir(rd):
            for f in sorted(os.listdir(rd)):
                if f.endswith(".json"):
                    try:
                        revs.append((f, json.load(open(os.path.join(rd, f), encoding="utf-8"))))
                    except Exception:
                        pass
        if vids or revs:
            out.append({"name": name, "videos": vids, "reviews": revs})
    return out


def brief_review(j):
    d = j.get("六维") or {}
    f = j.get("违禁项") or {}
    bad = [k for k, v in list(d.items()) + list(f.items()) if v not in OK_VALS and v]
    return "评分 %s/5 ｜ 结论 %s ｜ %s" % (j.get("整体评分"), j.get("结论"),
                                          ("问题项：" + "、".join(bad)) if bad else "无问题项")


def cmd_list(root, dry):
    rows = scan(root)
    if not rows:
        print("（%s 下没扫到样本；确认目录结构：<样本>/成片、<样本>/评价）" % root)
        return
    print("样本库：%s\n" % root)
    for s in rows:
        last = s["reviews"][-1][1] if s["reviews"] else None
        print("● %s" % s["name"])
        print("    成片：%s" % ("、".join(s["videos"]) if s["videos"] else "（无）"))
        print("    评价：%s 条%s" % (len(s["reviews"]), ("  ｜ 最近：" + brief_review(last)) if last else "  ｜ 待评分"))


def ask(prompt, options, allow_skip=True):
    print("\n%s" % prompt)
    for i, o in enumerate(options, 1):
        print("   %d) %s" % (i, o))
    while True:
        v = input("   请选择编号%s： " % ("（回车=跳过）" if allow_skip else "")).strip()
        if not v and allow_skip:
            return None
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1]
        if v in options:
            return v
        print("   输入无效，请重试")


def score_interactive(root, sample_filter=None):
    rows = scan(root)
    if not rows:
        sys.exit("没扫到样本")
    if sample_filter:
        rows = [r for r in rows if sample_filter in r["name"]] or sys.exit("没找到样本：%s" % sample_filter)
    print("样本库：%s" % root)
    for i, s in enumerate(rows, 1):
        print("  %2d) %-28s 成片 %d 个，已有评价 %d 条" % (i, s["name"], len(s["videos"]), len(s["reviews"])))
    while True:
        v = input("\n给哪个样本打分？编号： ").strip()
        if v.isdigit() and 1 <= int(v) <= len(rows):
            s = rows[int(v) - 1]
            break
        print("输入无效")
    video = None
    if s["videos"]:
        print("\n要评的成片：")
        for i, f in enumerate(s["videos"], 1):
            print("  %d) %s" % (i, f))
        print("  0) 未指定")
        v = input("编号（回车=未指定）： ").strip()
        if v.isdigit() and 1 <= int(v) <= len(s["videos"]):
            video = os.path.basename(s["videos"][int(v) - 1])
    dims = {}
    for key, opts, _short in DIMS:
        r = ask(key, opts)
        if r:
            dims[key] = r
    forb = {}
    for key, _ in FORBID:
        r = ask(key, ["无", "有"])
        if r:
            forb[key] = r
    star = input("\n整体评分（1–5）： ").strip()
    star = int(star) if star.isdigit() and 1 <= int(star) <= 5 else None
    concl = ask("结论", ["可用", "可改", "作废"], allow_skip=False)
    note = input("\n备注（可空）： ").strip()
    save(root, s["name"], video, dims, forb, star, concl, note)


def save(root, sample, video, dims, forb, star, concl, note):
    now = datetime.datetime.now()
    label = os.path.splitext(video)[0] if video else "未指定"
    d = os.path.join(root, sample, "评价")
    os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, "%s-%s.json" % (now.strftime("%Y-%m-%d"), label))
    payload = {
        "样本": sample,
        "成片": video or "",
        "评价时间": now.strftime("%Y-%m-%d %H:%M"),
        "六维": dims,
        "违禁项": forb,
        "整体评分": star,
        "结论": concl,
        "备注": note,
    }
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n[OK] 已写入：%s" % fn)
    print("   评分 %s/5 ｜ 结论 %s" % (star, concl))
    return fn



# ── 控制台安全（2026-09-17）：exe 从 cmd 启动时 stdout 是 GBK 控制台，
# print("[OK] …") 会抛 UnicodeEncodeError（'gbk' codec can't encode '[OK]'），
# 而这条异常会被上层当成"操作失败"弹给用户。这里在导入时就把输出流切成 UTF-8。
def _fix_console():
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                        # noqa: BLE001
            pass


_fix_console()

def main():
    ap = argparse.ArgumentParser(description="成片六维评分（命令行版，与网页版同一口径）")
    ap.add_argument("--root", help="样本库根目录")
    ap.add_argument("--list", action="store_true", help="列出样本/成片/最近评价")
    ap.add_argument("--show", help="看某样本最近一次评价")
    ap.add_argument("--sample", help="非交互模式：样本名")
    ap.add_argument("--video", help="非交互模式：成片文件名")
    ap.add_argument("--口型", dest="d1"); ap.add_argument("--动作", dest="d2")
    ap.add_argument("--形象", dest="d3"); ap.add_argument("--语速", dest="d4")
    ap.add_argument("--节奏", dest="d5")
    ap.add_argument("--字幕", dest="f1"); ap.add_argument("--运镜", dest="f2"); ap.add_argument("--畸变", dest="f3")
    ap.add_argument("--评分", dest="star", type=int); ap.add_argument("--结论", dest="concl")
    ap.add_argument("--备注", dest="note", default="")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    args = ap.parse_args()

    root = detect_root(args.root)

    if args.show:
        rows = [r for r in scan(root) if args.show in r["name"]]
        if not rows:
            sys.exit("没找到样本：%s" % args.show)
        r = rows[0]
        if not r["reviews"]:
            print("%s：还没有评价" % r["name"]); return
        f, j = r["reviews"][-1]
        print("%s ｜ 文件 %s" % (r["name"], f))
        print(json.dumps(j, ensure_ascii=False, indent=2))
        return

    if args.list:
        cmd_list(root, args.dry_run)
        return

    if args.sample:  # 非交互
        dims = {}
        for (key, _o, short), val in zip(DIMS, [args.d1, args.d2, args.d3, args.d4, args.d5]):
            if val:
                dims[key] = val
        forb = {}
        for (key, short), val in zip(FORBID, [args.f1, args.f2, args.f3]):
            if val:
                forb[key] = val
        if not dims and not args.star:
            sys.exit("非交互模式至少要给一项分数（如 --口型 同步 --评分 4 --结论 可改）")
        if args.dry_run:
            print("[dry-run] 将写入：%s/%s/评价/%s-%s.json" % (
                root, args.sample, datetime.date.today(), os.path.splitext(args.video or "未指定")[0]))
            print(json.dumps({"六维": dims, "违禁项": forb, "整体评分": args.star,
                              "结论": args.concl, "备注": args.note}, ensure_ascii=False, indent=2))
            return
        save(root, args.sample, args.video, dims, forb, args.star, args.concl, args.note)
        return

    score_interactive(root)


if __name__ == "__main__":
    main()
