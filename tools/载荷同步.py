# -*- coding: utf-8 -*-
"""同步「完整安装包载荷」：仓库 -> Inno Setup 载荷目录（_pkg）。

用法：
    python tools\\载荷同步.py <载荷目录>          # 如 ...\\_pkg
    python tools\\载荷同步.py <载荷目录> --dry    # 只预演

做四件事（2026-09-30 固化；起因是 v0.4.4 载荷丢了 node.exe，装机后工作台起不来）：
  1. 预清理载荷里不该进安装包的大件：tools\\_release、tools\\_vendor\\wheels*
     （依赖已全装进便携 Python，wheels 只是离线修理包，修理走网络下载）。
  2. robocopy /MIR 镜像仓库 -> 载荷\\skill（排除 .git/__pycache__/build/_stage/
     _release/wheels*/本地配置/pyc —— 排除后这些目录在载荷侧保持原样、不会被删也不会被带回）。
  3. node.exe 守卫：载荷 runtime\\node\\node.exe 不在就从 tools\\_vendor\\node\\node.exe
     （主本）补——**这是 v0.4.4 翻车的根因，删此检查必后悔**。
  4. 打印体积摘要 + 关键文件核对清单（node.exe / score-tool.exe / doubao_cdp.mjs / _version.json）。
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # ...\\skill\\tools（仓库侧）
REPO = os.path.dirname(HERE)                               # 仓库根

EXCLUDE_DIRS = {".git", "__pycache__", "build", "_stage", "_release",
                "wheels", "wheels-heavy", "wheels-stt", "wheels-depth"}
EXCLUDE_FILES = {"*.local.json", "*.pyc"}

# 载荷里必须存在的关键文件（相对 skill\\）
MUST_HAVE = [
    r"tools\dist\score-tool.exe",
    r"tools\doubao_cdp.mjs",
    r"tools\_version.json",
    r"tools\agent_bridge.py",
    r"tools\workbench\app.js",
]
# 载荷里必须存在的运行时（相对载荷根）
MUST_RUNTIME = [r"runtime\node\node.exe", r"runtime\python\python.exe"]


def mib(path):
    total = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total / 1048576.0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if not args:
        print(__doc__)
        return 2
    pkg = os.path.abspath(args[0])
    dst = os.path.join(pkg, "skill")
    if not os.path.isdir(pkg):
        print("[X] 载荷目录不存在：%s" % pkg)
        return 2

    # 1) 预清理不该进包的大件（/MIR 的 /XD 不会删它们，所以先显式删）
    doomed = [os.path.join(dst, "tools", "_release")]
    doomed += [os.path.join(dst, "tools", "_vendor", w)
               for w in ("wheels", "wheels-heavy", "wheels-stt", "wheels-depth")]
    for d in doomed:
        if os.path.isdir(d):
            print("[清] %-5s %d MB  %s" % ("(dry)" if dry else "已删",
                                           mib(d), os.path.relpath(d, pkg)))
            if not dry:
                shutil.rmtree(d)

    # 2) 镜像 skill
    xd = [" ".join("/XD " + os.path.join(dst, "tools", d) for d in
                   (".git", "__pycache__", "build", "_stage", "_release"))]
    cmd = ["robocopy", REPO, dst, "/MIR", "/NFL", "/NDL", "/NJH", "/NP",
           "/XF", *EXCLUDE_FILES,
           "/XD", ".git", "__pycache__", "build", "_stage", "_release",
           os.path.join(dst, "tools", ".git"),
           # wheels* 按目录名排除（源侧与目的侧都跳过）
           os.path.join(REPO, "tools", "_vendor", "wheels"),
           os.path.join(REPO, "tools", "_vendor", "wheels-heavy"),
           os.path.join(REPO, "tools", "_vendor", "wheels-stt"),
           os.path.join(REPO, "tools", "_vendor", "wheels-depth"),
           os.path.join(dst, "tools", "_vendor", "wheels"),
           os.path.join(dst, "tools", "_vendor", "wheels-heavy"),
           os.path.join(dst, "tools", "_vendor", "wheels-stt"),
           os.path.join(dst, "tools", "_vendor", "wheels-depth"),
           os.path.join(dst, "tools", "_release"),
           ]
    if not dry:
        rc = subprocess.run(cmd).returncode
        if rc > 7:
            print("[X] robocopy 失败 rc=%s" % rc)
            return rc

    # 3) node.exe 守卫
    node_dst = os.path.join(pkg, "runtime", "node", "node.exe")
    if not os.path.isfile(node_dst):
        for cand in (os.path.join(HERE, "_vendor", "node", "node.exe"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                  "Programs", "HeronBo", "runtime", "node", "node.exe")):
            if cand and os.path.isfile(cand):
                print("[补] runtime\\node\\node.exe 不在 ← %s" % cand)
                if not dry:
                    os.makedirs(os.path.dirname(node_dst), exist_ok=True)
                    shutil.copy2(cand, node_dst)
                break
        else:
            print("[X] 找不到可补的 node.exe！载荷将缺 Node（v0.4.4 事故重演）")
            return 3
    else:
        print("[OK] runtime\\node\\node.exe 在（%d MB）" % (os.path.getsize(node_dst) / 1048576))

    # 4) 核对 + 摘要
    bad = [rel for rel in MUST_HAVE if not os.path.isfile(os.path.join(dst, rel))]
    bad += [rel for rel in MUST_RUNTIME if not os.path.isfile(os.path.join(pkg, rel))]
    if bad:
        print("[X] 关键文件缺失：%s" % "; ".join(bad))
        return 3
    print("[OK] 关键文件核对通过（exe / 桥 / 版本 / 前端 / 双运行时）")
    print("---- 体积摘要 ----")
    print("载荷总计      %7.0f MB" % mib(pkg))
    print("  skill       %7.0f MB" % mib(dst))
    print("  runtime     %7.0f MB" % mib(os.path.join(pkg, "runtime")))
    if os.path.isdir(os.path.join(pkg, "dl")):
        print("  dl(不进包)  %7.0f MB" % mib(os.path.join(pkg, "dl")))
    print("建议下一步：双击载荷里的 编启动器.cmd → ISCC 编译 setup.iss")
    return 0


if __name__ == "__main__":
    sys.exit(main())
