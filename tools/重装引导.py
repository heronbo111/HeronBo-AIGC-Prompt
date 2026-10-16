# -*- coding: utf-8 -*-
r"""「需要重新安装一次」的迁移引导程序（一次性产物，跟着某个版本的 Release 走）。

为什么要有它（2026-10-16，用户在别人电脑上实测反馈）：
  0.6.2 及更早的工作台是**单文件形态**；0.6.3 起改成「目录式」（exe + 旁边的 _internal\）。
  老版本点「检查更新」时，远端没有它能直接替换的单文件内核（换上残缺件会把程序装坏），
  所以老客户端会更新失败——**它们需要的是一次「完整安装包重装」（一次性迁移）**。
  但老客户端自己的失败文案不指向这条路（它只会说"稍后再试"），用户会以为网络问题。

  做法：把本程序**打成 score-tool.exe** 挂到新版 Release 上（version.json 的 exe 字段指向它）。
  老客户端「检查更新」→ 下载并换上它 → 用户再点工作台图标时看到的就是这个窗口：
  说清楚"这一版需要重装一次"、给出下载地址与一键打开按钮。装完新版后，旧的这个 exe
  就可以删掉（新装的会有自己的快捷方式）。

  新客户端（目录式，0.6.3+）不受影响：它们读 version.json 的 `pkg`（zip 更新包）走在线更新，
  根本不看 exe 字段。

打包（产物就是要在 Release 上叫 score-tool.exe）：
    python tools\打迁移引导.py            # 用 PyInstaller 打成 build\迁移引导\score-tool.exe
    （也可以直接 python tools\重装引导.py 看窗口长什么样）

它是**一次性历史产物**：0.6.2 及更早的用户都迁移完之后，这个脚本和 Release 附件都可以退场。
"""
import os
import sys
import webbrowser

TITLE = "HeronBo 视频工作台 · 需要重新安装一次"
BODY = """你现在的这份工作台是旧版（单个文件的那种），
新版改成了「目录式」，没法自己原地升级——
需要重新装一次完整安装包（只此一次，之后就能在线更新了）。

步骤：
  1. 点下面的「打开下载页」；
  2. 下载 HeronBo-Workbench-Setup-…exe（完整安装包）；
  3. 双击安装（装完会有新的工作台快捷方式，用新的就好）。

你原来的项目与素材都放在项目库里，安装不会动它们。"""
URL = "https://github.com/heronbo111/HeronBo-AIGC-Prompt/releases/latest"
URL_ALT = "https://gitee.com/HeronBo/HeronBo-AIGC-Prompt/releases/latest"


def main():
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except ImportError:
        # 没 tkinter（罕见）：退回控制台说明
        print(TITLE)
        print(BODY)
        print("下载页：", URL)
        input("按回车退出…")
        return 0
    root = tk.Tk()
    root.title(TITLE)
    root.configure(bg="#ffffff")
    root.resizable(False, False)
    f1 = tkfont.Font(family="Microsoft YaHei UI", size=12, weight="bold")
    f2 = tkfont.Font(family="Microsoft YaHei UI", size=10)
    tk.Label(root, text="HeronBo 视频工作台", font=f1, bg="#ffffff", fg="#111111").pack(padx=28, pady=(22, 2))
    tk.Label(root, text="这一版需要重新安装一次（仅此一次）", font=f1, bg="#ffffff",
             fg="#c0392b").pack(padx=28, pady=(0, 10))
    tk.Label(root, text=BODY, font=f2, bg="#ffffff", fg="#333333", justify="left").pack(padx=28, pady=(0, 14))
    row = tk.Frame(root, bg="#ffffff")
    row.pack(padx=28, pady=(0, 20))

    def open_gh():
        webbrowser.open(URL)

    def open_gitee():
        webbrowser.open(URL_ALT)

    for txt, fn, main_btn in (("打开下载页（GitHub）", open_gh, True),
                              ("国内打不开？用 Gitee 下载页", open_gitee, False),
                              ("退出", root.destroy, False)):
        b = tk.Button(row, text=txt, font=f2, command=fn,
                      bg=("#1a73e8" if main_btn else "#f1f3f4"),
                      fg=("#ffffff" if main_btn else "#333333"),
                      activebackground=("#1558b0" if main_btn else "#e4e6e8"),
                      relief="flat", padx=14, pady=7, cursor="hand2")
        b.pack(side="left", padx=(0, 10))
    # 居中
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry("+%d+%d" % (x, y))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
