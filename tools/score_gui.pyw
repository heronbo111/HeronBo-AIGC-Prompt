# -*- coding: utf-8 -*-
"""成片六维评分 · 图形界面版（tkinter，无浏览器、无命令行对话）

用法：双击 tools\\score_gui.cmd（或 pythonw tools\\评分工具_GUI.pyw）
与命令行版/网页版**同一套口径、同一份 json 结构**，`评价回收.py` 照常回收。
"""
import importlib.util
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import argparse   # noqa: F401  仅供动态加载的 score_core 用，PyInstaller 需要能静态看到
import datetime   # noqa: F401
import json       # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = getattr(sys, "_MEIPASS", HERE)          # PyInstaller 打包后资源在 _MEIPASS


def _load_core():
    for d in (BASE, HERE):
        p = os.path.join(d, "score_core.py")
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("score_core", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("找不到 score_core.py")


core = _load_core()
try:
    _log = os.path.join(os.environ.get('TEMP', '.'), 'score_gui.log')
    open(_log, 'w', encoding='utf-8').write('core loaded ok: ' + str(getattr(core, '__file__', '?')) + chr(10))
except Exception:
    pass

DIMS = [(k, opts) for k, opts, _s in core.DIMS]
FORBID = [k for k, _s in core.FORBID]
STARS = ["1", "2", "3", "4", "5"]
CONCL = ["可用", "可改", "作废"]


class App:
    def __init__(self, root):
        self.root = root
        root.title("Seedance 成片六维评分 · 图形版")
        root.geometry("760x640")
        try:
            root.option_add("*Font", ("Microsoft YaHei UI", 10))
        except Exception:
            pass

        self.samples_root = self._find_root()
        self.samples = core.scan(self.samples_root)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="样本库：%s" % self.samples_root).pack(side="left")
        ttk.Button(top, text="刷新", command=self.reload).pack(side="right")

        mid = ttk.Frame(root, padding=(8, 0))
        mid.pack(fill="both", expand=True)

        left = ttk.LabelFrame(mid, text="样本", padding=6)
        left.pack(side="left", fill="y")
        self.lb = tk.Listbox(left, width=26, height=18, exportselection=False)
        self.lb.pack(side="left", fill="y")
        self.lb.bind("<<ListboxSelect>>", lambda e: self.on_sample())
        sb = ttk.Scrollbar(left, orient="vertical", command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.config(yscrollcommand=sb.set)

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        row = ttk.Frame(right); row.pack(fill="x")
        ttk.Label(row, text="成片：").pack(side="left")
        self.video = ttk.Combobox(row, width=40, state="readonly")
        self.video.pack(side="left")

        self.vars = {}
        for key, opts in DIMS:
            f = ttk.LabelFrame(right, text=key, padding=4)
            f.pack(fill="x", pady=2)
            v = tk.StringVar(value=opts[0])
            self.vars[key] = v
            for o in opts:
                ttk.Radiobutton(f, text=o, value=o, variable=v).pack(side="left", padx=4)

        f = ttk.LabelFrame(right, text="违禁项", padding=4)
        f.pack(fill="x", pady=2)
        for key in FORBID:
            v = tk.StringVar(value="无")
            self.vars[key] = v
            ttk.Label(f, text=key).pack(side="left", padx=(6, 2))
            ttk.Radiobutton(f, text="无", value="无", variable=v).pack(side="left")
            ttk.Radiobutton(f, text="有", value="有", variable=v).pack(side="left", padx=(0, 10))

        f = ttk.Frame(right); f.pack(fill="x", pady=4)
        ttk.Label(f, text="整体评分").pack(side="left")
        self.star = ttk.Combobox(f, width=3, state="readonly", values=STARS)
        self.star.set("4"); self.star.pack(side="left", padx=4)
        ttk.Label(f, text="结论").pack(side="left", padx=(12, 0))
        self.concl = ttk.Combobox(f, width=6, state="readonly", values=CONCL)
        self.concl.set("可改"); self.concl.pack(side="left", padx=4)

        f = ttk.Frame(right); f.pack(fill="x", pady=2)
        ttk.Label(f, text="备注").pack(side="left")
        self.note = ttk.Entry(f)
        self.note.pack(side="left", fill="x", expand=True, padx=4)

        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="选一个样本 + 成片，逐项打分后点保存")
        self.status.pack(side="left")
        ttk.Button(bar, text="保存评分", command=self.save).pack(side="right")

        self.reload()

    def _find_root(self):
        """找样本库根：核心逻辑 → 环境变量 → exe/脚本同级的 references/paths.local.md"""
        try:
            return core.detect_root(None)
        except SystemExit:
            pass
        import re
        cands = [os.path.join(HERE, "references", "paths.local.md"),
                 os.path.join(os.path.dirname(HERE), "references", "paths.local.md"),
                 os.path.join(BASE, "references", "paths.local.md")]
        for c in cands:
            if os.path.isfile(c):
                txt = open(c, encoding="utf-8").read()
                m = re.search(r"[A-Za-z]:[\/][^\s`\"']+", txt)
                if m and os.path.isdir(m.group(0)):
                    return m.group(0)
        messagebox.showerror("找不到样本库", "请在技能包 references/paths.local.md 里配置样本库根目录，"
                                              "或把 exe 放到技能包 tools\ 目录下再运行。")
        raise SystemExit(1)

    def reload(self):
        self.samples = core.scan(self.samples_root)
        self.lb.delete(0, tk.END)
        for s in self.samples:
            mark = "★" if s["videos"] and not s["reviews"] else ("·" if s["reviews"] else " ")
            self.lb.insert(tk.END, "%s %s（成片%d）" % (mark, s["name"], len(s["videos"])))

    def cur(self):
        sel = self.lb.curselection()
        return self.samples[sel[0]] if sel else None

    def on_sample(self):
        s = self.cur()
        if not s:
            return
        self.video["values"] = s["videos"] or ["（无成片）"]
        self.video.current(0 if s["videos"] else 0)
        if s["reviews"]:
            j = s["reviews"][-1][1]
            for key, v in (j.get("六维") or {}).items():
                if key in self.vars:
                    self.vars[key].set(v)
            for key, v in (j.get("违禁项") or {}).items():
                if key in self.vars:
                    self.vars[key].set(v)
            if j.get("整体评分"):
                self.star.set(str(j["整体评分"]))
            if j.get("结论"):
                self.concl.set(j["结论"])
            self.note.delete(0, tk.END); self.note.insert(0, j.get("备注") or "")
            self.status.config(text="已载入上一次评价（%s），可修改后另存" % s["reviews"][-1][0])
        else:
            self.status.config(text="%s：尚无评价" % s["name"])

    def save(self):
        s = self.cur()
        if not s:
            messagebox.showwarning("提示", "先在左侧选一个样本")
            return
        video = self.video.get()
        if not s["videos"]:
            video = ""
        dims = {k: self.vars[k].get() for k, _o in DIMS}
        forb = {k: self.vars[k].get() for k in FORBID}
        try:
            fn = core.save(self.samples_root, s["name"], video, dims, forb,
                           int(self.star.get()), self.concl.get(), self.note.get())
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.status.config(text="已保存：%s" % fn)
        self.reload()
        for i, x in enumerate(self.samples):
            if x["name"] == s["name"]:
                self.lb.selection_clear(0, tk.END); self.lb.selection_set(i)
                break


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
