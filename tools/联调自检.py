# -*- coding: utf-8 -*-
"""exe ↔ agent 联动自检：不弹窗、不碰真实样本库，在临时目录把协议跑一遍并逐条断言。

覆盖《联调说明》里的全部通道：
  ① 建框架      项目目录 + 业务目录 + 框架.json / 框架.md + _会话/状态.json
  ② 投素材      复制进 素材/，清单带 type/size/hash；幂等（hash 重复跳过）；同名不覆盖
  ③ 反馈信箱    new_round → 待办.jsonl(open) + _会话/轮次/001-反馈.txt
  ④ agent 侧    读待办 → 写回执 → 把待办标 done
  ⑤ 收成片      good → 成片/；bad → 废片/日期-废因；状态同步
  ⑥ 重扫自愈    手丢文件进 素材/ → rescan 补齐，且老条目 id 不变

用法：python tools\\联调自检.py [-v]
退出码：0 = 全部通过；1 = 有失败项。
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import project_core as pc                                     # noqa: E402

_ok, _fail = [], []


def check(name, cond, detail=""):
    (_ok if cond else _fail).append(name)
    mark = "PASS" if cond else "FAIL"
    line = "  [%s] %s" % (mark, name)
    if detail and not cond:
        line += "\n         ↳ " + detail
    print(line)
    return bool(cond)


def _png(path, color=b"\x00\x00\x00"):
    """写一张 1×1 PNG（避免依赖 Pillow）。"""
    import struct
    import zlib
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + color
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def _mp4(path):
    """用 ffmpeg 造一个 0.3 秒的小视频（成片/废片要用真文件）。"""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
           "-i", "color=c=black:s=64x64:d=0.3", "-pix_fmt", "yuv420p", path]
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode == 0 and os.path.isfile(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每步的落盘细节")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排错用）")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="heronbo_joint_")
    stage = os.path.join(tmp, "_投放台")
    os.makedirs(stage, exist_ok=True)
    root = os.path.join(tmp, "样本库")
    os.makedirs(root, exist_ok=True)
    print("临时工作区：%s\n" % tmp)

    try:
        # ① 建框架
        print("① 建框架")
        pd, sk, log = pc.build_skeleton(root=root, name="联调冒烟", register=False)
        if args.verbose:
            for m in log:
                print("     ·", m)
        check("项目目录建出来", os.path.isdir(pd), pd)
        for d in pc.PROJECT_DIRS:
            check("业务目录 %s/" % d, os.path.isdir(os.path.join(pd, d)))
        check("框架.json 落盘", os.path.isfile(pc.skeleton_path(pd)))
        check("框架.md 落盘", os.path.isfile(os.path.join(pd, "框架.md")))
        check("_会话/状态.json 落盘",
              os.path.isfile(os.path.join(pc.session_dir(pd), "状态.json")))
        check("没有登记到 paths.local.md（register=False）",
              "联调冒烟" not in open(pc.LOCAL_MD, encoding="utf-8-sig").read()
              if os.path.isfile(pc.LOCAL_MD) else True)

        # ② 投素材
        print("\n② 投素材")
        p1 = _png(os.path.join(stage, "形象参考.png"))
        t1 = os.path.join(stage, "口播稿.txt")
        open(t1, "w", encoding="utf-8").write("自检用文案")
        added, skipped, mlog = pc.add_materials(pd, [p1, t1], note="自检")
        if args.verbose:
            for m in mlog:
                print("     ·", m)
        check("两张素材都收下", len(added) == 2, "added=%d skipped=%d" % (len(added), len(skipped)))
        sk2 = pc.load_skeleton(pd)
        mats = (sk2 or {}).get("project", {}).get("materials", [])
        check("清单里有 2 条记录", len(mats) == 2, str(len(mats)))
        check("记录了 type", all(m.get("type") for m in mats))
        check("记录了 hash 与 size", all(m.get("hash") and m.get("size") for m in mats))
        check("路径是相对项目根的 POSIX 路径",
              all(m["file"].startswith("素材/") and "\\" not in m["file"] for m in mats))
        check("磁盘上确实有这两个文件",
              all(os.path.isfile(os.path.join(pd, m["file"])) for m in mats))

        # 幂等 + 同名不覆盖
        added2, skipped2, _ = pc.add_materials(pd, [p1], note="再来一次")
        check("幂等：同一张图再投不重复登记", len(added2) == 0 and len(skipped2) == 1,
              "added=%d skipped=%d" % (len(added2), len(skipped2)))
        p1b = _png(os.path.join(stage, "形象参考.png"), color=b"\xff\x00\x00")   # 同名不同内容
        added3, _, _ = pc.add_materials(pd, [p1b])
        names = [m["name"] for m in pc.load_skeleton(pd)["project"]["materials"]]
        check("同名不覆盖 → 存为 _1", added3 and "形象参考_1.png" in names, str(names))

        # ③ 反馈信箱
        print("\n③ 反馈信箱（程序 → agent）")
        rnd = pc.new_round(pd, "结尾三秒画面糊，请缩短并补一句卖点")
        todos = pc.read_todos(pd)
        check("待办写进来了", len(todos) >= 1, str(todos))
        check("待办状态为 open", any(t.get("status") == "open" for t in todos))
        check("待办内容一致", any("结尾三秒" in (t.get("text") or "") for t in todos))
        rdir = os.path.join(pc.session_dir(pd), "轮次")
        rounds = sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []
        check("反馈原文留档 001-反馈.txt", any(r.endswith("-反馈.txt") for r in rounds),
              str(rounds))
        st = pc.read_state(pd)
        check("状态里 pending > 0", (st or {}).get("pending", 0) >= 1, str(st))

        # ④ agent 侧
        print("\n④ agent 侧（读 → 干 → 回执 → 标 done）")
        pc.push_receipt(pd, "已按反馈重切段，台词减 4 字", ["文案/提示词.txt"], kind="出提示词")
        recs = pc.read_receipts(pd)
        check("回执写进来了", len(recs) >= 1, str(recs))
        check("回执里带了产出文件",
              any("文案/提示词.txt" in (r.get("files") or []) for r in recs))
        pc.mark_todos_done(pd)
        check("标 done 后无 open 待办", len(pc.read_todos(pd)) == 0,
              str(pc.read_todos(pd)))
        check("状态 pending 归零", (pc.read_state(pd) or {}).get("pending", 1) == 0)

        # ⑤ 收成片 / 废片
        print("\n⑤ 成片 / 废片（用户 → 程序）")
        good = os.path.join(stage, "可用成片.mp4")
        bad = os.path.join(stage, "翻车抽卡.mp4")
        if not (_mp4(good) and _mp4(bad)):
            check("ffmpeg 可用（造测试视频）", False, "ffmpeg 造片失败")
        else:
            placed, _ = pc.accept_deliverables(pd, [good], verdict="good", note="")
            check("成片进了 成片/", placed and placed[0].startswith("成片/"), str(placed))
            placed_bad, _ = pc.accept_deliverables(pd, [bad], verdict="bad", note="脸崩")
            today = datetime.date.today().isoformat()
            want = "废片/%s-脸崩.mp4" % today
            check("废片命名 = 日期-废因", placed_bad and placed_bad[0] == want,
                  "得到 %s，期望 %s" % (placed_bad, want))
            st2 = pc.read_state(pd)
            check("状态记录已接收成片数", (st2 or {}).get("generated", 0) >= 2, str(st2))
            check("状态 recentVerdict 更新", (st2 or {}).get("lastVerdict") == "bad", str(st2))

        # ⑥ 重扫自愈
        print("\n⑥ 重扫自愈（清单是缓存、磁盘是真相）")
        before = {m["file"]: m["id"] for m in pc.load_skeleton(pd)["project"]["materials"]}
        sneaky = os.path.join(pd, "素材", "手动丢进来的.png")
        shutil.copy2(_png(os.path.join(stage, "手.png"), color=b"\x00\xff\x00"), sneaky)
        pc.rescan(pd)
        after = {m["file"]: m["id"] for m in pc.load_skeleton(pd)["project"]["materials"]}
        check("重扫补上了手动丢的文件", "素材/手动丢进来的.png" in after, str(list(after)))
        check("老条目 id 保留（不是整表重建）",
              all(after.get(k) == v for k, v in before.items()),
              "before=%s after=%s" % (before, after))

    finally:
        print("\n" + "=" * 60)
        print("通过 %d 项，失败 %d 项" % (len(_ok), len(_fail)))
        if _fail:
            print("失败项：")
            for f in _fail:
                print("  -", f)
        if args.keep:
            print("临时目录保留：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
