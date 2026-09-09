# -*- coding: utf-8 -*-
"""评价回收：找出评分网页新产出、还没被吸收进规则的评价。

用法：
    python 评价回收.py                          # 列出待吸收评价（六维/结论/备注摘要）
    python 评价回收.py --all                    # 列出全部评价（含已吸收）
    python 评价回收.py --mark "<样本>/<文件名>"  # 吸收完一条后记账
    python 评价回收.py --mark-all               # 把当前待吸收的全部记账

账本：references/eval-absorbed.local.json（本机文件，已 gitignore）。
样本库根目录取自 references/paths.local.md 的 SAMPLES_ROOT（兼容旧版 paths.md 表格）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.normpath(os.path.join(HERE, ".."))
LOCAL_MD = os.path.join(SKILL_ROOT, "references", "paths.local.md")
PATHS_MD = os.path.join(SKILL_ROOT, "references", "paths.md")
LEDGER = os.path.join(SKILL_ROOT, "references", "eval-absorbed.local.json")
DIMS = ["口型同步", "动作跟随", "形象一致性", "语速", "节奏卡点"]
BANNED = ["字幕", "运镜", "AI畸变"]


def read_local():
    vals = {}
    try:
        with open(LOCAL_MD, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return vals
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip("`").strip(chr(34))
    return vals


def legacy_value(key):
    try:
        with open(PATHS_MD, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        if key in line and line.strip().startswith("|"):
            cells = [c.strip().strip("`").strip() for c in line.split("|") if c.strip()]
            if cells:
                return cells[-1]
    return None


def samples_root():
    root = read_local().get("SAMPLES_ROOT") or legacy_value("${SAMPLES_ROOT}")
    if root and "<" not in root and os.path.isdir(root):
        return root
    return None


def load_ledger():
    try:
        with open(LEDGER, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("absorbed"), list):
        data["absorbed"] = []
    return data


def save_ledger(data):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def scan(root):
    items = []
    for name in sorted(os.listdir(root)):
        ev = os.path.join(root, name, "评价")
        if not os.path.isdir(ev):
            continue
        for fn in sorted(os.listdir(ev)):
            if not fn.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(ev, fn), encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            items.append({"key": "%s/%s" % (name, fn), "sample": name, "file": fn, "data": data})
    return items


def fmt(item):
    d = item["data"]
    dims = d.get("六维") or {}
    vals = [dims.get(k) for k in DIMS if isinstance(dims.get(k), (int, float))]
    avg = round(sum(vals) / len(vals), 1) if vals else "-"
    banned = d.get("违禁项") or {}
    out = ["样本：%s" % item["sample"], "  文件：%s" % item["file"]]
    meta = []
    if d.get("评价时间"):
        meta.append("评价时间 %s" % d["评价时间"])
    if d.get("整体评分"):
        meta.append("整体 %s/5" % d["整体评分"])
    if d.get("结论"):
        meta.append("结论 %s" % d["结论"])
    if meta:
        out.append("  " + " | ".join(meta))
    out.append("  六维均分：%s  (%s)" % (avg, " ".join("%s%s" % (k, dims.get(k, "-")) for k in DIMS)))
    out.append("  违禁项：%s" % (" ".join("%s=%s" % (k, banned.get(k, "-")) for k in BANNED)))
    if d.get("备注"):
        out.append("  备注：%s" % d["备注"])
    return "\n".join(out)


def main(argv):
    root = samples_root()
    if root is None:
        print("[评价回收] 还没配置样本库根目录。先跑：python tools/首次配置.py --project \"<项目目录>\"")
        return 1
    items = scan(root)
    if not items:
        print("[评价回收] %s 下没有找到 评价/*.json —— 先让用户用评分网页打分。" % root)
        return 0
    ledger = load_ledger()
    absorbed = set(ledger["absorbed"])

    if "--mark" in argv:
        i = argv.index("--mark")
        if i + 1 >= len(argv):
            print("[评价回收] --mark 缺少 \"<样本>/<文件名>\"。")
            return 2
        key = argv[i + 1].replace("\\", "/")
        absorbed.add(key)
        ledger["absorbed"] = sorted(absorbed)
        save_ledger(ledger)
        print("[评价回收] 已记账：%s（累计 %d 条）" % (key, len(absorbed)))
        return 0

    pending = [it for it in items if it["key"] not in absorbed]

    if "--mark-all" in argv:
        for it in pending:
            absorbed.add(it["key"])
        ledger["absorbed"] = sorted(absorbed)
        save_ledger(ledger)
        print("[评价回收] 已记账 %d 条（累计 %d 条）" % (len(pending), len(absorbed)))
        return 0

    show = items if "--all" in argv else pending
    if not show:
        print("[评价回收] 没有待吸收评价（全部 %d 条已记账）。" % len(items))
        return 0
    print("[评价回收] 样本库：%s" % root)
    print("[评价回收] %s %d 条：" % ("全部评价" if "--all" in argv else "待吸收评价", len(show)))
    print("")
    for it in show:
        print(fmt(it))
        print("")
    print("[评价回收] 吸收完（更新 rules/模板/samples-db 后）执行：python tools/评价回收.py --mark-all")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
