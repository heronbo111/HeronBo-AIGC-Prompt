# -*- coding: utf-8 -*-
r"""给 exe 加数字签名：有凭据就签，没有就把"缺什么、怎么补"说清楚（不阻塞打包）。

为什么需要它：未签名的程序会被 Windows SmartScreen 报"未知发布者"、被**智能应用控制（SAC）
直接拦下**（那台机器的报错就是这个）、被火绒/360 这类杀软加重误报。签名是唯一能让
"看签名的那些机制"放行的办法——打包方式（单文件/目录式）都影响不了它们。

⚠️ **自签名证书没用**（2026-10-13 说明，免得白折腾）：
   `New-SelfSignedCertificate` 签出来的东西，只有"本机把这张证书装进受信任根"的机器才认；
   发给别人照样被 SmartScreen / SAC / 杀软拦。**必须是受信任 CA 签发的代码签名证书**。

怎么用（配好环境变量后，打包流程会自动调它）：
    HERONBO_SIGN_PFX     .pfx 证书路径
    HERONBO_SIGN_PASS    .pfx 密码
    HERONBO_SIGN_TS      时间戳服务器（默认 http://timestamp.digicert.com）
    # Azure Trusted Signing 也可以（走微软信任链，约 $9.99/月）：
    HERONBO_SIGN_DLIB    dlib 路径（如 Azure.CodeSigning.Dlib.dll）
    HERONBO_SIGN_METADATA  元数据 json（含 endpoint / 证书账户 / 证书配置名）

    python tools\签名.py --check                 # 只看现状：凭据、signtool、已签文件
    python tools\签名.py <文件或目录> [...]        # 逐个签（目录＝递归找 .exe）
    python tools\签名.py --file <安装包.exe>       # 同一个东西的另一种写法

拿证书的路径（按性价比排）：
  · **Azure Trusted Signing**：约 $9.99/月，签出来进微软信任链（SAC 认）；个人需通过身份验证
  · **OV 代码签名证书**：国内约 ¥1000–3000/年（沃通 / CFCA / 腾讯云 / 阿里云），个人实名可办
  · **EV 证书**：约 ¥3000+/年、带硬件 token，SmartScreen 立即信任、SAC 通过率最高（最贵但最省事）
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TS_DEFAULT = "http://timestamp.digicert.com"


def say(m=""):
    print(m, flush=True)


# ── 找 signtool ───────────────────────────────────────────────────────────
def find_signtool():
    """按可靠性顺序找 signtool.exe：显式指定 → PATH → 项目自带 → Windows Kits → NuGet 缓存。"""
    env = (os.environ.get("HERONBO_SIGNTOOL") or "").strip()
    if env and os.path.isfile(env):
        return env
    hit = shutil.which("signtool")
    if hit:
        return hit
    cands = [os.path.join(HERE, "_vendor", "signtool", "signtool.exe")]
    for root in (os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
                 os.environ.get("ProgramFiles") or r"C:\Program Files"):
        cands += glob.glob(os.path.join(root, "Windows Kits", "10", "bin", "*", "x64", "signtool.exe"))
    cands += glob.glob(os.path.join(os.path.expanduser("~"), ".nuget", "packages",
                                    "microsoft.windows.sdk.buildtools", "*",
                                    "bin", "*", "x64", "signtool.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


SIGNTOOL_HINT = """  没找到 signtool.exe —— 三条路（任选其一）：
    ① 最省事：装 Windows SDK 的"Windows SDK Signing Tools"（装完自带 signtool）
    ② 只下工具（约 50MB）：从 NuGet 取 Microsoft.Windows.SDK.BuildTools 包（就是 zip），
       解压出 bin\\<版本>\\x64\\signtool.exe 放到 tools\\_vendor\\signtool\\signtool.exe
    ③ 用 Azure Trusted Signing 的客户端（它自带签名能力）
  放好之后本脚本会自动找到它（也可用环境变量 HERONBO_SIGNTOOL 指定路径）。"""


def cred_state():
    """返回 (模式, 说明)：pfx / azure / none。"""
    pfx = (os.environ.get("HERONBO_SIGN_PFX") or "").strip()
    dlib = (os.environ.get("HERONBO_SIGN_DLIB") or "").strip()
    md = (os.environ.get("HERONBO_SIGN_METADATA") or "").strip()
    if pfx and os.path.isfile(pfx):
        return "pfx", "证书 %s" % pfx
    if dlib and md and os.path.isfile(dlib) and os.path.isfile(md):
        return "azure", "Azure Trusted Signing（dlib=%s）" % dlib
    return "none", ""


def is_signed(path):
    """用 signtool verify 判断是否已签（找不到 signtool 时返回 None＝未知）。"""
    st = find_signtool()
    if not st:
        return None
    try:
        r = subprocess.run([st, "verify", "/pa", path], capture_output=True, text=True,
                           timeout=60, errors="replace")
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def sign_one(path):
    """签一个文件，返回 (ok, 说明)。"""
    if not os.path.isfile(path):
        return False, "文件不在：%s" % path
    st = find_signtool()
    if not st:
        return False, "没找到 signtool"
    mode, note = cred_state()
    ts = (os.environ.get("HERONBO_SIGN_TS") or "").strip() or TS_DEFAULT
    if mode == "pfx":
        cmd = [st, "sign", "/fd", "SHA256", "/td", "SHA256", "/tr", ts,
               "/f", os.environ["HERONBO_SIGN_PFX"]]
        pw = os.environ.get("HERONBO_SIGN_PASS") or ""
        if pw:
            cmd += ["/p", pw]
        cmd += [path]
    elif mode == "azure":
        cmd = [st, "sign", "/v", "/fd", "SHA256", "/td", "SHA256", "/tr", ts,
               "/dlib", os.environ["HERONBO_SIGN_DLIB"],
               "/dmdf", os.environ["HERONBO_SIGN_METADATA"], path]
    else:
        return False, "没配签名凭据"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return False, "调用签名工具失败：%r" % (e,)
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
        return False, "签名失败：%s" % " / ".join(tail)
    return True, os.path.basename(path)


def iter_targets(paths):
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for dp, _dn, fns in os.walk(p):
                out += [os.path.join(dp, f) for f in fns if f.lower().endswith(".exe")]
        elif os.path.isfile(p):
            out.append(p)
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser(description="给 exe 加数字签名（没凭据就只报告，不报错）")
    ap.add_argument("paths", nargs="*", help="要签的文件或目录（目录递归找 .exe）")
    ap.add_argument("--file", action="append", default=[], help="要签的单个文件（可重复）")
    ap.add_argument("--check", action="store_true", help="只看现状：凭据 / signtool / 目标签名状态")
    ap.add_argument("--quiet-skip", action="store_true",
                    help="没凭据时一句话带过（打包脚本内部调用用，别刷屏）")
    a = ap.parse_args()

    targets = iter_targets(a.paths + a.file)
    mode, note = cred_state()
    st = find_signtool()

    if a.check or not targets:
        say("[签名] 凭据：%s" % (note if mode != "none" else "**没配**（HERONBO_SIGN_PFX 或 "
                                "HERONBO_SIGN_DLIB+HERONBO_SIGN_METADATA）"))
        say("[签名] signtool：%s" % (st or "**没找到**"))
        if not st:
            say(SIGNTOOL_HINT)
        if mode == "none":
            say("  没凭据＝打包不会签名；发给别人的包在 SmartScreen / 智能应用控制那里仍会被拦。")
            say("  拿证书的路子（详见本文件顶部）：Azure Trusted Signing（约 $9.99/月，SAC 认）"
                " / OV 证书（国内约 ¥1000–3000/年） / EV（SmartScreen 立即信任）。")
        if targets:
            for t in targets:
                s = is_signed(t)
                say("  %-60s %s" % (os.path.basename(t),
                                    {True: "已签名", False: "未签名", None: "签名状态未知"}[s]))
        return 0

    if mode == "none":
        tail = "跳过签名（没配证书）" if a.quiet_skip else \
            ("跳过签名：没配证书。要签就给环境变量 HERONBO_SIGN_PFX / HERONBO_SIGN_PASS "
             "（或用 Azure Trusted Signing 的 DLIB+METADATA），详见 tools\\签名.py 顶部说明。")
        say("[签名] %s" % tail)
        return 0
    if not st:
        say("[签名] 有证书但没找到 signtool —— 跳过。")
        say(SIGNTOOL_HINT)
        return 0

    ok_n = 0
    say("[签名] 用 %s，签 %d 个文件…" % (st, len(targets)))
    for t in targets:
        ok, why = sign_one(t)
        say("  [%s] %s" % ("OK" if ok else "!!", why))
        ok_n += 1 if ok else 0
    say("[签名] 完成：%d/%d" % (ok_n, len(targets)))
    return 0 if ok_n == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
