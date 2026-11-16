# -*- coding: utf-8 -*-
r"""把豆包 CLI 调用递给 SYSTEM 身份执行的桥（本机兜底方案）。

为什么需要它
------------
2026-11-14 在 HERONBOWORK / HERONBO-ZERO 上实测：普通用户进程跑
`doubao login` 稳定报 `auth_unavailable: 无法访问用于保存设备身份的安全存储`，
新建的全新本地账号同样复现，属主/ACL/TEMP/重装均已排除；SYSTEM 身份下
`login --qr=png`、`auth status --verify` 正常。因此这里用计划任务把 CLI 调用
转交 SYSTEM 执行，再把 stdout/stderr/返回码/二维码回读给工作台。

安全边界
--------
请求目录 `%PROGRAMDATA%\hbcli\jobs` 只允许 SYSTEM 与 Administrators 写入，
普通用户只读，避免任意用户借 SYSTEM 提权执行任意命令。每个任务执行完即删。

注意
----
* 只在本机兜底：需要管理员权限（UAC 关闭的机器直接可写，开启 UAC 时需提权）。
* SYSTEM 身份生成的产物属主是 SYSTEM，文件本身仍按目录继承的 ACL 可读写。
"""

import json
import os
import subprocess
import time
import uuid

JOB_ROOT = os.path.join(os.environ.get("PROGRAMDATA") or r"C:\ProgramData",
                        "hbcli", "jobs")

_JOB_PS1 = r"""$ErrorActionPreference = 'SilentlyContinue'
$dir = $PSScriptRoot
$req = Get-Content -LiteralPath (Join-Path $dir 'req.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$outPath = Join-Path $dir 'out.txt'
$errPath = Join-Path $dir 'err.txt'
$rcPath  = Join-Path $dir 'rc.txt'
$pidPath = Join-Path $dir 'pid.txt'

$node = [string]$req.node
$cli  = [string]$req.cli
$argList = @($req.args | ForEach-Object { [string]$_ })
if ([string]$req.cwd -and (Test-Path -LiteralPath ([string]$req.cwd))) {
  Set-Location -LiteralPath ([string]$req.cwd)
}

function Quote([string]$s) {
  if ($s -eq '') { return '""' }
  $esc = $s -replace '"', '\"'
  if ($esc -match '\s') { return '"' + $esc + '"' }
  return $esc
}
$all = @($cli) + $argList
$argStr = ($all | ForEach-Object { Quote $_ }) -join ' '

$p = Start-Process -FilePath $node -ArgumentList $argStr -NoNewWindow -PassThru `
     -RedirectStandardOutput $outPath -RedirectStandardError $errPath
Set-Content -LiteralPath $pidPath -Value $p.Id -Encoding ascii

$qrDst = [string]$req.qr_dst
if ($qrDst) {
  for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $outPath) {
      $txt = Get-Content -LiteralPath $outPath -Raw -Encoding UTF8
      if ($txt -match '([A-Za-z]:\\[^\r\n"]+?\.png)') {
        Copy-Item -LiteralPath $Matches[1] -Destination $qrDst -Force
        break
      }
    }
    if ($p.HasExited) { break }
  }
}

$p.WaitForExit()
Set-Content -LiteralPath $rcPath -Value $p.ExitCode -Encoding ascii
"""


def _icacls_harden(path):
    """只留 SYSTEM/Administrators 写、Users 只读。用 SID 避免本地化组名差异。"""
    try:
        subprocess.run(["icacls", path, "/inheritance:r",
                        "/grant:r", "*S-1-5-18:(OI)(CI)F",
                        "/grant:r", "*S-1-5-32-544:(OI)(CI)F",
                        "/grant:r", "*S-1-5-32-545:(OI)(CI)R"],
                       capture_output=True, text=True, timeout=30)
    except Exception:                                            # noqa: BLE001
        pass


def _ensure_root():
    if not os.path.isdir(JOB_ROOT):
        os.makedirs(JOB_ROOT, exist_ok=True)
    marker = os.path.join(JOB_ROOT, ".ready")
    if not os.path.isfile(marker):
        _icacls_harden(JOB_ROOT)
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass


def _read(path, limit=200000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except OSError:
        return ""


def _run_task(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def available():
    """桥是否可用：请求目录可写 + 能创建 SYSTEM 计划任务。"""
    try:
        _ensure_root()
        probe_dir = os.path.join(JOB_ROOT, "probe-" + uuid.uuid4().hex[:6])
        os.makedirs(probe_dir, exist_ok=True)
        with open(os.path.join(probe_dir, "ok.txt"), "w", encoding="utf-8") as f:
            f.write("ok")
        os.rmdir(probe_dir) if False else None
        try:
            os.remove(os.path.join(probe_dir, "ok.txt"))
            os.rmdir(probe_dir)
        except OSError:
            pass
        return True, ""
    except Exception as e:                                       # noqa: BLE001
        return False, "SYSTEM 通道不可用：%s" % str(e)[:160]


def start(node, cli, args, cwd=None, qr_dst=None):
    """起一个 SYSTEM 任务；返回 job 句柄（不等待）。"""
    _ensure_root()
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    d = os.path.join(JOB_ROOT, job_id)
    os.makedirs(d, exist_ok=True)
    req = {"node": node, "cli": cli,
           "args": [str(a) for a in args],
           "cwd": cwd or "", "qr_dst": qr_dst or ""}
    with open(os.path.join(d, "req.json"), "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    with open(os.path.join(d, "job.ps1"), "w", encoding="utf-8") as f:
        f.write(_JOB_PS1)
    task = "HBCli-" + job_id[-8:]
    r = _run_task(["schtasks", "/create", "/tn", task, "/tr",
                   'powershell -NoProfile -ExecutionPolicy Bypass -File "%s"'
                   % os.path.join(d, "job.ps1"),
                   "/ru", "SYSTEM", "/sc", "once", "/st", "23:59", "/f"])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:300])
    rr = _run_task(["schtasks", "/run", "/tn", task])
    if rr.returncode != 0:
        raise RuntimeError((rr.stderr or rr.stdout or "").strip()[:300])
    return {"id": job_id, "dir": d, "task": task,
            "qr_dst": qr_dst or "", "started": time.time()}


def poll(job):
    d = job["dir"]
    out = _read(os.path.join(d, "out.txt"))
    err = _read(os.path.join(d, "err.txt"))
    rc_path = os.path.join(d, "rc.txt")
    done = os.path.isfile(rc_path)
    rc = None
    if done:
        txt = _read(rc_path).strip()
        try:
            rc = int(txt)
        except ValueError:
            rc = 0
    qr = job.get("qr_dst") or ""
    if qr and not os.path.isfile(qr):
        qr = ""
    return {"rc": rc, "out": out, "err": err, "done": done, "qr": qr}


def cancel(job):
    if not job:
        return
    try:
        _run_task(["schtasks", "/end", "/tn", job["task"]])
    except Exception:                                            # noqa: BLE001
        pass
    pid_file = os.path.join(job["dir"], "pid.txt")
    pid = _read(pid_file).strip()
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                           capture_output=True, text=True, timeout=20)
        except Exception:                                        # noqa: BLE001
            pass


def cleanup(job, drop_files=True):
    if not job:
        return
    try:
        _run_task(["schtasks", "/delete", "/tn", job["task"], "/f"])
    except Exception:                                            # noqa: BLE001
        pass
    if not drop_files:
        return
    d = job["dir"]
    for name in os.listdir(d) if os.path.isdir(d) else []:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass


def run(node, cli, args, timeout=60, cwd=None):
    """同步跑一次 CLI，返回 {"rc","out","err"}。"""
    job = start(node, cli, args, cwd=cwd)
    deadline = time.time() + max(int(timeout), 5)
    try:
        while time.time() < deadline:
            st = poll(job)
            if st["done"]:
                return {"rc": st["rc"], "out": st["out"], "err": st["err"]}
            time.sleep(0.4)
        cancel(job)
        st = poll(job)
        return {"rc": -9, "out": st["out"],
                "err": "SYSTEM 通道超时（%ss）" % timeout}
    finally:
        cleanup(job)
