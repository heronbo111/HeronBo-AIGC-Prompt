# -*- coding: utf-8 -*-
r"""工作台启动器（**源码方式**跑，不需要打包好的 exe）。

为什么要有它：exe 只是分发的一个方便形式，"clone 下来 + 装完依赖就能用"更重要——
`python tools\工作台.py` 直接起同一套界面（走 `workbench_server.launch_web` 的三层：
pywebview 独立窗口 → Edge 窗口 → 默认浏览器；WebView2 坏了会自动落到 Edge，不会白屏）。

用法（在仓库根目录或 tools 下都行）：
    python tools\工作台.py                      # 起工作台（默认还是上次那个项目/样本库）
    python tools\工作台.py --project "F:\...\某项目"
    python tools\工作台.py --root "F:\...\样本库根"
    python tools\工作台.py --port 8799          # 固定端口（默认随机）
    python tools\工作台.py --edge               # 强制用 Edge 窗口（排查用）
    python tools\工作台.py --no-wait            # 只起服务、不开窗口（给别的程序调）
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description="起 HeronBo AI 视频工作台（源码方式）")
    ap.add_argument("--project", default="", help="直接打开这个项目目录")
    ap.add_argument("--root", default="", help="样本库根目录")
    ap.add_argument("--hint", default="", help="按名字关键字定位项目（如「三本书」）")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--edge", action="store_true", help="强制用 Edge 窗口（不用独立窗口）")
    ap.add_argument("--no-wait", action="store_true", help="只起服务，不开窗口、不阻塞")
    a = ap.parse_args()

    try:
        import workbench_server as ws
    except ImportError as e:
        print("[工作台] 起不来：%s" % e)
        print("        workbench_server.py 要在 tools\\ 目录里；别把它挪走。")
        return 2

    # 依赖体检：缺 pywebview 也能跑（会退化到 Edge 窗口），但先把话说清楚
    try:
        import webview  # noqa: F401
        has_pw = True
    except ImportError:
        has_pw = False
    no_win = bool(os.environ.get("HERONBO_NO_WINDOW"))
    if no_win:
        print("[工作台] HERONBO_NO_WINDOW=1：只起服务、不开窗口")
    elif not has_pw:
        print("[工作台] 没装 pywebview → 用 Edge 窗口打开（能正常用；想要独立窗口就跑 "
              "python tools\\部署.py install --yes）")
    elif not a.edge:
        okk, ver, why = ws.webview2_state()
        ws._wv2_report(okk, ver, why)          # 打印体检结论；不可用会自动退 Edge

    srv, url, how = ws.launch_web(root=a.root, project=a.project, hint=a.hint,
                                  port=a.port, wait=not (a.no_wait or no_win),
                                  backend="edge" if a.edge or not has_pw else None)
    if srv is None:
        print("[工作台] 已有一个在跑：%s" % (url or ""))
        return 0
    print("[工作台] 窗口方式：%s" % how)
    if a.no_wait or no_win:
        print("[工作台] %s（服务在跑，Ctrl+C 结束）" % url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
