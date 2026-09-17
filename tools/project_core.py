# -*- coding: utf-8 -*-
"""项目框架 / 素材投放核心（评分窗口版与命令行版共用，也被 agent 直接调用）。

约定来源：`references/rules.md` 第 264-272 条
    框架：<SAMPLES_ROOT>/<实验名>/{文案,素材,成片,废片,评价,备注}/
    实验名一律不带日期戳；同一主题重开依次加序号 1、2、3（「0」就是第一个，不写 0）。
    根目录由用户指定，登记进 `references/paths.local.md`（已 gitignore）。

设计取舍（对照 `_StoryVia拆解/02_素材管理与落盘.md`）：
- 框架文件跟素材同目录、只写**相对项目根的 POSIX 路径** → 整包拷给别人/换盘符都不炸。
- 清单是缓存、磁盘是真相：提供 `rescan()` 全量重扫 + 按相对路径合并 + 保留原 id 的自愈逻辑。
- 素材字段比 StoryVia 厚：除 path/fileName/type/addedAt 外，补 size / hash / width / height /
  duration / category / tags / note（它只有 5 个字段，agent 拿不到这些）。
- 同名不覆盖：`name_1.ext`、`name_2.ext`；并用 hash 判定内容是否其实是同一个文件。
- 支持音频与文本（StoryVia 完全没有 audio 逻辑）。
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def find_skill_root():
    """技能仓库根目录（**exe 打包后也要找对**，否则会把配置写到临时目录）。

    顺序：显式指定 → 从本文件往上找含 SKILL.md 的目录 → 从 exe 所在位置往上找
    → 老行为（本文件的上一级）。

    为什么非这样不可（2026-09-15 实测）：PyInstaller 打包后 `__file__` 落在
    `%TEMP%\\_MEIxxxx`，于是 `HERE/..` 就是 %TEMP% —— 当时的表现是 exe 把
    `paths.local.md` 写进了 `%TEMP%\\references\\`，Windows 一清临时目录，
    "样本库根"就丢了。改成往上找 SKILL.md，exe 放在仓库的 tools\\dist\\ 里
    也能正确定位到仓库根。
    """
    env = os.environ.get("HERONBO_SKILL_ROOT")
    if env and os.path.isfile(os.path.join(env, "SKILL.md")):
        return os.path.normpath(env)
    starts = [HERE]
    if getattr(sys, "frozen", False):
        try:
            starts.append(os.path.dirname(os.path.abspath(sys.executable)))
        except (OSError, ValueError):
            pass
    for st in starts:
        d = st
        for _ in range(6):
            if os.path.isfile(os.path.join(d, "SKILL.md")):
                return d
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    return os.path.normpath(os.path.join(HERE, ".."))


SKILL_ROOT = find_skill_root()
LOCAL_MD = os.path.join(SKILL_ROOT, "references", "paths.local.md")
PATHS_MD = os.path.join(SKILL_ROOT, "references", "paths.md")

PROJECT_DIRS = ["文案", "素材", "成片", "废片", "评价", "备注"]
MATERIAL_DIR = "素材"
SKELETON_JSON = "框架.json"
SKELETON_MD = "框架.md"
SCHEMA = "heronbo.project/1"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
TEXT_EXT = {".txt", ".md", ".json", ".csv", ".srt", ".ass"}

BIG_FILE_MB = 500                 # 超过这个体积给警告（不硬拒绝，用户可能就是要投大视频）
HASH_LIMIT_MB = 256               # 小于此体积算全量 sha1；更大的只算「首尾 1MB + 体积」的快指纹

ASK_PLACE = "请问您要把项目建在哪里？您提供好素材后，我会自动将其进行归类"


# ── paths.local.md（本机取值，已 gitignore）────────────────────────────────
HEADER = "# 本机取值（不提交仓库；由首次使用时的用户回答写入）\n"
ORDER = ["SAMPLES_ROOT", "AI_CREATE_ROOT", "MATERIALS_ROOT", "PLATFORM", "CLI"]


def read_local(local_md=None):
    vals = {}
    try:
        with open(local_md or LOCAL_MD, encoding="utf-8-sig") as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return vals
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip().strip("`").strip('"')
    return vals


def write_local(local_md=None, **kw):
    path = local_md or LOCAL_MD
    vals = read_local(path)
    for k, v in kw.items():
        if v:
            vals[k] = v
    lines = [HEADER]
    for k in ORDER:
        if vals.get(k):
            lines.append("%s=%s\n" % (k, vals[k]))
    for k in sorted(set(vals) - set(ORDER)):
        lines.append("%s=%s\n" % (k, vals[k]))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
    except OSError:
        return vals
    return vals


def looks_unset(value):
    return (not value) or any(h in value for h in ("<", "待用户指定", "你的样本库根目录"))


def detect_root(cli_root=None):
    """样本库根目录：命令行 > paths.local.md > paths.md 兼容取值。

    注意：命令行给的路径**即使还不存在也要认**（用户就是要新建到这个位置），
    所以这里不能加 os.path.isdir 判断 —— 否则会悄悄退回旧配置、把项目建到别处去。
    """
    if cli_root and cli_root.strip():
        return os.path.normpath(cli_root.strip())
    root = read_local().get("SAMPLES_ROOT")
    if not looks_unset(root) and os.path.isdir(root):
        return os.path.normpath(root)
    try:
        with open(PATHS_MD, encoding="utf-8-sig") as f:
            for line in f:
                if "${SAMPLES_ROOT}" in line and line.strip().startswith("|"):
                    cells = [c.strip().strip("`") for c in line.split("|") if c.strip()]
                    cand = cells[-1] if cells else ""
                    if not looks_unset(cand) and os.path.isdir(cand):
                        return os.path.normpath(cand)
    except (FileNotFoundError, OSError):
        pass
    return None


# ── 命名：同主题重开依次加序号（「0」= 第一个，不写 0）─────────────────────
def next_project_name(root, base):
    """在 root 下为 base 找一个可用的实验名：base、base1、base2 …"""
    base = (base or "").strip().strip("/\\")
    if not base:
        return ""
    cand, n = base, 0
    while os.path.exists(os.path.join(root, cand)):
        n += 1
        cand = "%s%d" % (base, n)
    return cand


def name_conflict(root, name):
    return os.path.exists(os.path.join(root, name))


# ── 素材类型与元数据 ───────────────────────────────────────────────────────
def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TEXT_EXT:
        return "text"
    return "other"


def file_hash(path):
    """小文件全量 sha1；大文件用「体积 + 首尾 1MB」快指纹（省时间，够用来判重）。"""
    size = os.path.getsize(path)
    h = hashlib.sha1()
    h.update(str(size).encode())
    chunk = 1024 * 1024
    with open(path, "rb") as f:
        if size <= HASH_LIMIT_MB * 1024 * 1024:
            for blk in iter(lambda: f.read(chunk), b""):
                h.update(blk)
        else:
            h.update(f.read(chunk))
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    return "sha1:" + h.hexdigest()[:20]


_FFPROBE = None


def ffprobe_exe():
    """找 ffprobe：先 PATH，再常见安装位置（沙箱/精简 PATH 下也能用）。"""
    global _FFPROBE
    if _FFPROBE is not None:
        return _FFPROBE or None
    import glob
    cand = shutil.which("ffprobe")
    if not cand:                       # 仓库自带的那份（装机时 tools\deploy.py install 会解开）
        _v = os.path.join(HERE, "_vendor", "ffmpeg", "bin", "ffprobe.exe")
        if os.path.isfile(_v):
            cand = _v
    if not cand:
        pats = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                         "Packages", "Gyan.FFmpeg*", "**", "bin", "ffprobe.exe"),
            r"C:\ffmpeg\bin\ffprobe.exe",
            r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
            os.path.join(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey"),
                         "bin", "ffprobe.exe"),
        ]
        for p in pats:
            hit = glob.glob(p, recursive=True)
            if hit:
                cand = hit[0]
                break
    _FFPROBE = cand or ""
    return cand or None


def wav_duration(path):
    """不依赖 ffprobe 读 WAV 时长（RIFF 头解析）。"""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"RIFF":
                return None
            f.read(4)
            if f.read(4) != b"WAVE":
                return None
            rate = channels = bits = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                cid, size = hdr[:4], int.from_bytes(hdr[4:8], "little")
                if cid == b"fmt ":
                    body = f.read(size)
                    channels = int.from_bytes(body[2:4], "little")
                    rate = int.from_bytes(body[4:8], "little")
                    bits = int.from_bytes(body[14:16], "little")
                elif cid == b"data":
                    if rate and channels and bits:
                        return round(size / float(rate * channels * bits // 8), 3)
                    return None
                else:
                    f.seek(size + (size & 1), os.SEEK_CUR)
    except (OSError, ZeroDivisionError):
        return None


def _no_window_kwargs():
    """ffprobe 也是控制台程序：从 GUI（exe/pythonw）里起它会**弹一个终端窗口**。

    与 `agent_bridge.no_window_kwargs()` 同款修法（那份是给 node 用的；这里独立一份，
    免得 project_core 反向依赖 agent_bridge）。Win11 默认终端是 Windows Terminal，
    归类素材时每个视频/音频都会闪一下，用户看到的就是"莫名其妙弹黑窗"。
    """
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                      # SW_HIDE
            kw["startupinfo"] = si
        except (AttributeError, ValueError):
            pass
    return kw


def _ffprobe(path):
    """用 ffprobe 读宽高与时长；没装 ffprobe 就安静返回空。"""
    exe = ffprobe_exe()
    if not exe:
        return {}
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, timeout=20, **_no_window_kwargs())
        if p.returncode != 0:
            return {}
        data = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
        st = (data.get("streams") or [{}])[0]
        dur = (data.get("format") or {}).get("duration")
        out = {}
        if st.get("width"):
            out["width"] = int(st["width"])
            out["height"] = int(st["height"])
        if dur:
            out["duration"] = round(float(dur), 3)
        return out
    except Exception:                                        # noqa: BLE001
        return {}


def meta_of(path):
    st = os.stat(path)
    m = {"size": st.st_size}
    try:
        m["hash"] = file_hash(path)
    except OSError:
        m["hash"] = ""
    k = kind_of(path)
    if k == "video":
        m.update(_ffprobe(path))
    elif k == "audio":
        m.update(_ffprobe(path))
        if not m.get("duration"):                     # 没有 ffprobe → 至少把 WAV 读出来
            wd = wav_duration(path)
            if wd:
                m["duration"] = wd
    elif k == "image":
        try:                                                 # 只读文件头，不解码整图
            from struct import unpack
            with open(path, "rb") as f:
                head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = unpack(">II", head[16:24])
                m.update(width=w, height=h)
            elif head[:2] == b"\xff\xd8":
                with open(path, "rb") as f:
                    f.seek(2)
                    while True:
                        b = f.read(1)
                        while b and b != b"\xff":
                            b = f.read(1)
                        while b == b"\xff":
                            b = f.read(1)
                        if not b:
                            break
                        if b[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                                    0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                            f.read(3)
                            hh, ww = unpack(">HH", f.read(4))
                            m.update(width=ww, height=hh)
                            break
                        ln = unpack(">H", f.read(2))[0]
                        f.seek(ln - 2, os.SEEK_CUR)
        except Exception:                                    # noqa: BLE001
            pass
    return m


# ── 框架 ───────────────────────────────────────────────────────────────────

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

def ensure_root(root):
    root = os.path.normpath(root)
    os.makedirs(root, exist_ok=True)
    note = os.path.join(root, "README.txt")
    if not os.path.exists(note):
        try:
            with open(note, "w", encoding="utf-8") as f:
                f.write("这是本 skill 的样本库根目录（评分工具与素材投放都连本目录）。\n")
                f.write("每个项目/实验一个子文件夹，统一结构：\n")
                f.write("  <实验名>/\n")
                f.write("    ├── 框架.json            # 机器读（agent 优先读这份）\n")
                f.write("    ├── 框架.md              # 人读摘要\n")
                f.write("    ├── 文案/                # 口播稿 / 提示词\n")
                f.write("    ├── 素材/                # 用户提供的参考图 / 音视频（本工具自动归类）\n")
                f.write("    ├── 成片/                # 验收成片\n")
                f.write("    ├── 废片/                # 作废抽卡（文件名=日期-废因）\n")
                f.write("    ├── 评价/*.json          # 六维评价（评分工具产出）\n")
                f.write("    └── 备注/备注.txt        # 主观备注 + 生成参数\n")
        except OSError:
            pass
    return root


def ensure_project(project_dir, dirs=None):
    project_dir = os.path.normpath(project_dir)
    for d in (dirs or PROJECT_DIRS):
        os.makedirs(os.path.join(project_dir, d), exist_ok=True)
    for extra in (SESSION_DIR, UPLOAD_DIR):
        os.makedirs(os.path.join(project_dir, extra), exist_ok=True)
    return project_dir


# ══ 会话协议：项目文件夹就是 exe 与 agent 的接口 ═══════════════════════════
# 为什么不做成"程序调 API、agent 调 API"：两边都可能不在、都可能被关掉。
# 把状态放磁盘上，谁先起来谁读盘，就永远不会丢件，也不需要两边同时在线。
#
#   <项目>/_会话/
#   ├── 状态.json      程序写：exe 是否在跑、当前阶段、agent 会话 id、本轮编号
#   ├── 待办.jsonl     程序写：用户的反馈/请求（agent 读一条处理一条，处理完标 done）
#   ├── 回执.jsonl     agent 写：我做了什么、产出了哪些文件（程序读来显示）
#   └── 轮次/001-反馈.txt  每轮用户反馈原文留档（agent 复盘用）
SESSION_DIR = "_会话"
UPLOAD_DIR = "平台上传"          # 2026-09-17 用户要求：skill 已支持多平台，不再叫"即梦上传"
UPLOAD_DIR_OLD = "即梦上传"      # 老项目里仍是这个名字 → **读的时候两个都认**，不必强迁


def _dir_file_count(d):
    """目录里的文件数（含子目录）——用来判断"哪份上传夹是装着东西的那份"。"""
    n = 0
    for _root, _dirs, files in os.walk(d):
        n += len(files)
    return n


def upload_dir(pdir, create=False):
    """项目的"上传夹"路径（放要上传到平台的素材副本 + 上传说明.txt）。

    规则（2026-09-17 由「即梦上传」改名时定的）：
      · 只有 `平台上传/` → 用它；只有 `即梦上传/` → 用它（老项目不必强迁）；
      · **两个都在 → 谁有文件用谁**（空的新夹不能遮住装着东西的旧夹——实测踩到过：
        重命名失败的项目里，旧夹 122 个文件、新夹空着，界面就显示成"没有上传件"）；
      · 都没有 → `create=True` 时按新名建。
    """
    new = os.path.join(pdir, UPLOAD_DIR)
    old = os.path.join(pdir, UPLOAD_DIR_OLD)
    has_new, has_old = os.path.isdir(new), os.path.isdir(old)
    if has_new and has_old:
        return old if _dir_file_count(old) > _dir_file_count(new) else new
    if has_new:
        return new
    if has_old:
        return old
    if create:
        os.makedirs(new, exist_ok=True)
    return new
STATE_JSON = "状态.json"
TODO_JSONL = "待办.jsonl"
RECEIPT_JSONL = "回执.jsonl"
ROUND_DIR = "轮次"

STAGES = ["新建", "等素材", "等提示词", "等成片", "等反馈", "已完成"]


def session_dir(project_dir):
    return os.path.join(os.path.normpath(project_dir), SESSION_DIR)


def _jsonl_append(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _jsonl_read(path):
    out = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except (FileNotFoundError, OSError):
        pass
    return out


def read_state(project_dir):
    p = os.path.join(session_dir(project_dir), STATE_JSON)
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def write_state(project_dir, **kw):
    d = session_dir(project_dir)
    os.makedirs(d, exist_ok=True)
    st = read_state(project_dir)
    st.update(kw)
    st["updatedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    p = os.path.join(d, STATE_JSON)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        pass
    return st


def push_todo(project_dir, kind, text, files=None, by="user"):
    """程序 → agent。kind: 反馈 / 出提示词 / 归位 / 其它"""
    d = session_dir(project_dir)
    todos = _jsonl_read(os.path.join(d, TODO_JSONL))
    rec = {"i": len(todos) + 1, "at": datetime.datetime.now().isoformat(timespec="seconds"),
           "kind": kind, "text": text or "", "files": list(files or []),
           "by": by, "status": "open"}
    _jsonl_append(os.path.join(d, TODO_JSONL), rec)
    write_state(project_dir, stage="等提示词" if kind in ("反馈", "出提示词") else None,
                pending=len([t for t in todos if t.get("status") != "done"]) + 1)
    return rec


def read_todos(project_dir, only_open=True):
    todos = _jsonl_read(os.path.join(session_dir(project_dir), TODO_JSONL))
    if only_open:
        todos = [t for t in todos if t.get("status") != "done"]
    return todos


def mark_todos_done(project_dir, upto=None):
    d = session_dir(project_dir)
    path = os.path.join(d, TODO_JSONL)
    todos = _jsonl_read(path)
    for i, t in enumerate(todos):
        if t.get("status") != "done" and (upto is None or t.get("i", 0) <= upto):
            t["status"] = "done"
            t["doneAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for t in todos:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    except OSError:
        pass
    write_state(project_dir, pending=len([t for t in todos if t.get("status") != "done"]))
    return todos


def push_receipt(project_dir, text, files=None, kind="完成"):
    """agent → 程序：我做了什么。"""
    d = session_dir(project_dir)
    receipts = _jsonl_read(os.path.join(d, RECEIPT_JSONL))
    rec = {"i": len(receipts) + 1,
           "at": datetime.datetime.now().isoformat(timespec="seconds"),
           "kind": kind, "text": text or "", "files": list(files or [])}
    _jsonl_append(os.path.join(d, RECEIPT_JSONL), rec)
    return rec


def read_receipts(project_dir, since=0):
    return [r for r in _jsonl_read(os.path.join(session_dir(project_dir), RECEIPT_JSONL))
            if r.get("i", 0) > since]


def new_round(project_dir, feedback_text, push=True, kind="反馈"):
    """把用户这轮反馈**留档并推给 agent**，返回轮次号（001 起）。

    留档与待办必须成对：只留档不推待办时，agent 读 `待办.jsonl` 拿不到这轮反馈
    （2026-09-15 联调自检跑出来的断点，原实现要调用方另外再调一次 `push_todo`）。
    `push=False` 只用于"仅留档、不派活"的场合（如纯备忘）。
    """
    d = os.path.join(session_dir(project_dir), ROUND_DIR)
    os.makedirs(d, exist_ok=True)
    n = 1
    if os.path.isdir(d):
        nums = [int(f[:3]) for f in os.listdir(d) if f[:3].isdigit()]
        n = (max(nums) + 1) if nums else 1
    fn = "%03d-反馈.txt" % n
    try:
        with open(os.path.join(d, fn), "w", encoding="utf-8", newline="\n") as f:
            f.write("# 第 %d 轮用户反馈\n" % n)
            f.write("# 记录时间：%s\n\n"
                    % datetime.datetime.now().isoformat(timespec="seconds"))
            f.write(feedback_text or "")
    except OSError:
        pass
    write_state(project_dir, round=n, stage="等提示词")
    if push and (feedback_text or "").strip():
        push_todo(project_dir, kind, feedback_text, by="user")
    return n


def list_rounds(project_dir):
    d = os.path.join(session_dir(project_dir), ROUND_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".txt"))


def accept_deliverables(project_dir, paths, verdict="good", note="", on_log=None):
    """接收成片/废片：good → 成片/，bad → 废片/（文件名=日期-废因，符合命名约定）。

    返回 (放入的文件相对路径列表, log)。这是"成片和废片也要有窗口接收"的落地。
    """
    log = []

    def emit(m):
        log.append(m)
        if on_log:
            on_log(m)

    project_dir = os.path.normpath(project_dir)
    ensure_project(project_dir)
    sub = "成片" if verdict == "good" else "废片"
    folder = os.path.join(project_dir, sub)
    os.makedirs(folder, exist_ok=True)
    today = datetime.date.today().isoformat()
    placed = []
    for src in collect_files(paths):
        base = os.path.basename(src)
        if verdict == "bad":                      # 废片：文件名=日期-废因
            why = _clean_name(note) or "未注明"
            stem, ext = os.path.splitext(base)
            base = "%s-%s%s" % (today, why, ext or ".mp4")
        dst_name = _unique_target(folder, base)
        try:
            shutil.copy2(src, os.path.join(folder, dst_name))
        except (OSError, shutil.Error) as e:
            emit("跳过 %s（复制失败：%s）" % (base, e))
            continue
        rel = "%s/%s" % (sub, dst_name)
        placed.append(rel)
        emit("已接收 → %s" % rel)
    if placed:
        sk = load_skeleton(project_dir) or {"schema": SCHEMA, "project": {}}
        g = sk["project"].setdefault("generated", [])
        for rel in placed:
            g.append({"file": rel, "verdict": "选用" if verdict == "good" else "作废",
                      "note": note,
                      "at": datetime.datetime.now().isoformat(timespec="seconds")})
        save_skeleton(project_dir, sk)
        write_state(project_dir, stage="等反馈",
                    generated=len(g),
                    lastVerdict="good" if verdict == "good" else "bad")
    return placed, log



def skeleton_path(project_dir):
    return os.path.join(project_dir, SKELETON_JSON)


def load_skeleton(project_dir):
    p = skeleton_path(project_dir)
    if not os.path.isfile(p):                      # 旧项目还在用 框架.json
        legacy = os.path.join(os.path.normpath(project_dir), "框架.json")
        if os.path.isfile(legacy):
            p = legacy
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def new_id(seq):
    return "m%04d" % seq


def build_skeleton(root, name, platform="", project_dir=None, register=True):
    """建框架目录 + 写框架.json/框架.md；返回 (project_dir, skeleton dict, log list)。

    register=False 时不动 `references/paths.local.md`（自检/测试用）。
    """
    log = []
    root = ensure_root(root)
    project_dir = os.path.normpath(project_dir or os.path.join(root, name))
    existed = os.path.exists(project_dir)
    ensure_project(project_dir)

    sk = load_skeleton(project_dir) or {}
    if not sk:
        sk = {
            "schema": SCHEMA,
            "project": {
                "name": os.path.basename(project_dir),
                "createdAt": datetime.datetime.now().isoformat(timespec="seconds"),
                "updatedAt": "",
                "platform": platform or read_local().get("PLATFORM", ""),
                "materials": [],
                "generated": [],
                "prompts": [],
                "review": None,
            },
        }
        log.append("新建框架文件 %s" % SKELETON_JSON)
    else:
        log.append("已有框架文件，补全目录并保留原清单")
    if platform:
        sk["project"]["platform"] = platform
    sk["project"]["name"] = os.path.basename(project_dir)

    log.append("目录：%s" % "、".join(PROJECT_DIRS))
    if existed:
        log.append("（项目目录原本已存在，未覆盖任何已有文件）")

    save_skeleton(project_dir, sk)
    if not read_state(project_dir):          # 新框架补一份初始状态：agent 一读就有阶段可看
        write_state(project_dir, stage="新建", round=0, pending=0, exeAlive=False)
        log.append("写入初始 %s（阶段=新建）" % STATE_JSON)
    if register:
        write_local(SAMPLES_ROOT=root)
        log.append("已登记 SAMPLES_ROOT=%s" % root)
    return project_dir, sk, log


def save_skeleton(project_dir, sk):
    sk["project"]["updatedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    sk["counts"] = count_kinds(sk["project"].get("materials", []))   # 每次重算，别用 setdefault
    p = skeleton_path(project_dir)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sk, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        return None
    legacy = os.path.join(project_dir, "骨架.json")   # 旧命名的残留，写成功后清掉
    if os.path.isfile(legacy):
        try:
            os.remove(legacy)
        except OSError:
            pass
    try:                                                     # 同目录再落一份人读摘要
        with open(os.path.join(project_dir, SKELETON_MD), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(render_md(sk))
    except OSError:
        pass
    return p


def count_kinds(materials):
    c = {"image": 0, "video": 0, "audio": 0, "text": 0, "other": 0}
    for m in materials:
        c[m.get("type", "other")] = c.get(m.get("type", "other"), 0) + 1
    return c


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def render_md(sk):
    p = sk.get("project", {})
    mats = p.get("materials", [])
    lines = ["# 框架 · %s" % p.get("name", ""), "",
             "- 建立时间：%s" % p.get("createdAt", ""),
             "- 最近更新：%s" % p.get("updatedAt", ""),
             "- 平台：%s" % (p.get("platform") or "（未登记）"),
             "- 素材：%d 个（图 %d / 视频 %d / 音频 %d / 文本 %d / 其它 %d）"
             % (len(mats), *[count_kinds(mats)[k] for k in
                             ("image", "video", "audio", "text", "other")]),
             "", "## 素材清单", "",
             "| # | 文件 | 类型 | 尺寸 | 时长 | 体积 | 备注 |", "|---|---|---|---|---|---|---|"]
    for i, m in enumerate(mats, 1):
        wh = ("%d×%d" % (m["width"], m["height"])) if m.get("width") else ""
        dur = ("%.1fs" % m["duration"]) if m.get("duration") else ""
        lines.append("| %d | `%s` | %s | %s | %s | %s | %s |"
                     % (i, m.get("file", ""), m.get("type", ""), wh, dur,
                        _human(m.get("size", 0)), m.get("note", "")))
    if p.get("prompts"):
        lines += ["", "## 提示词", ""]
        for pr in p["prompts"]:
            lines += ["### %s" % pr.get("name", ""), "", pr.get("text", ""), ""]
    lines += ["", "---", "",
              "> 本文件由素材工具自动生成；`框架.json` 是机器读的版本，字段更全。"]
    return "\n".join(lines) + "\n"


# ── 角色识别（rules.md 第11/16/30 条 + 第210/211 行）────────────────────────
# 素材角色是"一素材一职责"落地的关键：同一份素材只能是一个角色，模型才不会互相打架。
ROLES = ["形象参考", "台词配音", "音色参考", "参考视频", "产品图", "文案", "其它"]

# 台词配音 vs 音色参考：规则给的界线是"整段口播（时长≈成片）"对"几秒短采样"。
SPEECH_MIN_SEC = 8.0

PRODUCT_HINT = ("产品", "商品", "包装", "礼盒", "书", "封面", "包装盒", "盒子",
                "货", "sku", "product", "box")
FACE_HINT = ("形象", "人物", "角色", "老师", "主播", "三视图", "参考图", "脸",
             "portrait", "character", "avatar")
SCRIPT_HINT = ("口播", "台词", "文案", "话术", "script", "copy")
PROMPT_HINT = ("提示词", "prompt", "指令")


def detect_role(path, meta=None):
    """按「类型 + 时长 + 文件名线索」判素材角色。返回 ROLES 之一。"""
    meta = meta or {}
    name = os.path.basename(path).lower()
    kind = kind_of(path)
    if kind == "text":
        if any(h in name for h in PROMPT_HINT):
            return "文案"
        if any(h in name for h in SCRIPT_HINT):
            return "文案"
        return "文案"
    if kind == "audio":
        dur = meta.get("duration")
        if dur is not None:
            # 规则给的界线：整段口播（时长≈成片）对几秒短采样
            return "台词配音" if dur >= SPEECH_MIN_SEC else "音色参考"
        # 读不到时长 → 退回文件名线索，仍判不出就当音色参考（宁可少挂，不要乱配）
        if any(h in name for h in ("音色", "采样", "短", "timbre", "voice")):
            return "音色参考"
        if any(h in name for h in ("口播", "台词", "完整", "全文", "配音", "整段")):
            return "台词配音"
        return "音色参考"
    if kind == "video":
        return "参考视频"
    if kind == "image":
        if any(h in name for h in PRODUCT_HINT):
            return "产品图"
        if any(h in name for h in FACE_HINT):
            return "形象参考"
        return "形象参考"                     # 图片默认当形象/场景参考
    return "其它"


def target_dir_for(role):
    """角色 → 归到哪个目录（rules.md 第267 条归位规则）。"""
    return "文案" if role == "文案" else MATERIAL_DIR


# ── 自动规划：根目录 / 项目名 / 角色，全都由软件推 ────────────────────────────
FOLDER_NOISE = ("素材", "素材包", "原始素材", "原始", "新建文件夹", "待整理", "给agent的素材",
                "给agent", "参考", "项目", "批次", "打包")


def _clean_name(s):
    """把候选名洗成"能当项目名"的样子：去扩展名/去标点后半句/去噪声后缀。"""
    s = re.sub(r"[\r\n\t]+", " ", str(s or "")).strip()
    s = re.sub(r"\.(txt|md|png|jpe?g|webp|mp4|mov|mkv|mp3|wav|m4a)$", "", s, flags=re.I)
    # 只取第一个标点之前 —— 避免把整句文案吞成项目名
    for ch in "，。、；：！？,;:!?（(【[":
        if ch in s:
            s = s.split(ch)[0]
    s = re.sub(r"[_\-—]+", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip(" ._-—")
    for noise in FOLDER_NOISE:                 # 去掉"素材/素材包"这类无信息后缀
        while s.endswith(noise) and len(s) > len(noise):
            s = s[: -len(noise)].rstrip("_-— ")
    return s[:24]


def guess_project_name(paths):
    """从输入里推项目名，可靠性从高到低：

    ① 用户丢进来的**文件夹名**（信息量最大，通常是"四六级三本书带货"这种主题名）
    ② 文案类文件的**文件名**（口播稿.txt 这种通用名会被滤掉）
    ③ 第一个视频/图片的文件名主干
    推不出返回空串，由调用方兜底，绝不硬编日期戳（命名约定）。
    """
    # ① 文件夹名
    for p in paths or []:
        if os.path.isdir(p):
            cand = _clean_name(os.path.basename(os.path.normpath(p)))
            if len(cand) >= 3:
                return cand
    files = collect_files(paths)
    generic = ("口播稿", "台词", "文案", "提示词", "脚本", "说明", "备注", "素材", "上传说明")
    # ② 文案文件名
    for f in [x for x in files if kind_of(x) == "text"]:
        stem = _clean_name(os.path.basename(f))
        if len(stem) >= 3 and stem not in generic:
            return stem
    # ③ 视频 / 图片文件名主干
    for f in [x for x in files if kind_of(x) in ("video", "image")] + files:
        stem = _clean_name(os.path.basename(f))
        if len(stem) >= 3 and stem not in generic:
            return stem
    return ""


def auto_plan(paths, root=None, name=None):
    """给一批素材做一次"自动规划"：定根目录、定项目名、判每个素材的角色。

    返回 dict，可直接拿去执行，也可以先在界面上给用户看/改。
    """
    files = collect_files(paths)
    root = os.path.normpath(root) if root else (detect_root() or "")
    nm = (name or "").strip() or guess_project_name(paths)
    items = []
    for f in files:
        try:
            meta = meta_of(f)
        except OSError:
            meta = {}
        items.append({"src": f, "name": os.path.basename(f),
                      "type": kind_of(f), "role": detect_role(f, meta), "meta": meta})
    by_role = {}
    for it in items:
        by_role.setdefault(it["role"], []).append(it)
    final = next_project_name(root, nm) if (root and nm) else nm
    return {"root": root, "guess_name": nm, "final_name": final,
            "items": items, "by_role": by_role, "count": len(items)}


def auto_build(paths, root=None, name=None, platform="", register=True, on_log=None):
    """一键：自动定根/定名 → 建框架（含 平台上传/）→ 按角色归类 → 写清单。

    这就是"软件自行生成框架"：用户只管把素材丢进来，其余全自动；
    任何一步的判断结果都落在日志与框架里，可回溯、可手改。
    """
    log = []

    def emit(m):
        log.append(m)
        if on_log:
            on_log(m)

    plan = auto_plan(paths, root=root, name=name)
    if not plan["root"]:
        return None, plan, log + ["还没有样本库根目录，无法自动建框架"]
    if not plan["final_name"]:
        plan["final_name"] = "新项目"
        emit("推不出项目名 → 先用「新项目」，建好后随手改名即可")
    pdir, sk, l2 = build_skeleton(plan["root"], plan["final_name"],
                                  platform=platform or None, register=register)
    for m in l2:
        emit(m)
    # 上传夹（rules.md 第270 条：交付提示词时必须同步建；名字 2026-09-17 由「即梦上传」改为「平台上传」）
    os.makedirs(upload_dir(pdir, create=True), exist_ok=True)
    emit("已建 %s/（规则第270条）" % UPLOAD_DIR)

    # 按角色分批落户
    buckets = {}
    for it in plan["items"]:
        buckets.setdefault(target_dir_for(it["role"]), []).append(it["src"])
    total_added = 0
    for sub, srcs in buckets.items():
        dest_root = os.path.join(pdir, sub)
        added, skipped, _ = add_materials(pdir, srcs, copy=True,
                                          note="", on_log=emit, into=sub)
        total_added += len(added)
    # 把角色回填到框架里
    sk = load_skeleton(pdir) or sk
    roles = {os.path.basename(it["src"]): it["role"] for it in plan["items"]}
    for m in sk["project"].get("materials", []):
        r = roles.get(m.get("name"))
        if r:
            m["role"] = r
    save_skeleton(pdir, sk)
    emit("共归类 %d 个素材，按角色写入框架" % total_added)
    return pdir, plan, log


# ── 素材投放 ───────────────────────────────────────────────────────────────
def _unique_target(folder, filename):
    """同名不覆盖：xx.jpg → xx_1.jpg → xx_2.jpg"""
    stem, ext = os.path.splitext(filename)
    cand, n = filename, 0
    while os.path.exists(os.path.join(folder, cand)):
        n += 1
        cand = "%s_%d%s" % (stem, n, ext)
    return cand


def collect_files(paths):
    """把「文件 + 文件夹」展开成文件清单。"""
    out = []
    for p in paths:
        p = os.path.normpath(p)
        if os.path.isfile(p):
            out.append(p)
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in sorted(filenames):
                    if not fn.startswith("."):
                        out.append(os.path.join(dirpath, fn))
    seen, uniq = set(), []
    for f in out:
        k = os.path.normcase(os.path.abspath(f))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def add_materials(project_dir, paths, copy=True, note="", on_log=None, into=None):
    """把素材投放到 <项目>/<into>/（默认 素材/）并登记进框架。

    返回 (added list, skipped list, log list)。幂等：同一文件（按 hash）已在清单里就不再登记。
    """
    log = []
    into = into or MATERIAL_DIR

    def emit(msg):
        log.append(msg)
        if on_log:
            on_log(msg)

    project_dir = os.path.normpath(project_dir)
    ensure_project(project_dir)
    folder = os.path.join(project_dir, into)
    os.makedirs(folder, exist_ok=True)
    sk = load_skeleton(project_dir)
    if not sk:
        sk = {"schema": SCHEMA, "project": {"name": os.path.basename(project_dir),
                                            "createdAt": datetime.datetime.now()
                                            .isoformat(timespec="seconds"),
                                            "materials": [], "generated": [],
                                            "prompts": [], "review": None}}
    mats = sk["project"].setdefault("materials", [])
    known = {(m.get("file", ""), m.get("hash", "")) for m in mats}
    known_hash = {m.get("hash") for m in mats if m.get("hash")}
    added, skipped = [], []
    seq = len(mats) + 1

    for src in collect_files(paths):
        try:
            meta = meta_of(src)
        except OSError as e:
            skipped.append({"src": src, "why": "读不到文件：%s" % e})
            emit("跳过 %s（读不到）" % os.path.basename(src))
            continue
        base = os.path.basename(src)
        if meta.get("hash") and meta["hash"] in known_hash:
            skipped.append({"src": src, "why": "内容与已投放的素材相同（hash 一致）"})
            emit("跳过 %s（内容重复）" % base)
            continue
        rel_name = base
        if copy:
            dst_name = _unique_target(folder, base)
            dst = os.path.join(folder, dst_name)
            try:
                shutil.copy2(src, dst)
            except (OSError, shutil.Error) as e:
                skipped.append({"src": src, "why": "复制失败：%s" % e})
                emit("跳过 %s（复制失败）" % base)
                continue
            rel_name = dst_name
            if dst_name != base:
                emit("同名已存在 → 存为 %s" % dst_name)
        rec = {
            "id": new_id(seq),
            "file": "%s/%s" % (into, rel_name),
            "name": rel_name,
            "srcPath": "" if copy else os.path.abspath(src),
            "type": kind_of(rel_name),
            "addedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        rec.update(meta)
        if note:
            rec["note"] = note
        rec.setdefault("tags", [])
        key = (rec["file"], rec.get("hash", ""))
        if key in known:
            skipped.append({"src": src, "why": "清单里已有同名同 hash 记录"})
            continue
        known.add(key)
        if rec.get("hash"):
            known_hash.add(rec["hash"])
        mats.append(rec)
        added.append(rec)
        seq += 1
        size_mb = rec.get("size", 0) / 1048576.0
        warn = "  ⚠ 体积较大" if size_mb > BIG_FILE_MB else ""
        emit("已投放 %s（%s，%s）%s"
             % (rec["name"], rec["type"], _human(rec.get("size", 0)), warn))

    save_skeleton(project_dir, sk)
    return added, skipped, log


def rescan(project_dir):
    """磁盘→清单的自愈重扫：扫 素材/ 与 文案/，按相对路径合并，保留原 id 与人工字段。"""
    project_dir = os.path.normpath(project_dir)
    sk = load_skeleton(project_dir) or {"schema": SCHEMA, "project": {
        "name": os.path.basename(project_dir), "materials": []}}
    mats = sk["project"].setdefault("materials", [])
    by_file = {m.get("file"): m for m in mats}
    on_disk = []
    for sub in (MATERIAL_DIR, "文案"):
        folder = os.path.join(project_dir, sub)
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if fn.startswith("."):
                continue
            if os.path.isfile(os.path.join(folder, fn)):
                on_disk.append("%s/%s" % (sub, fn))
    added = removed = 0
    seq = len(mats) + 1
    for rel in on_disk:
        if rel in by_file:
            continue
        fp = os.path.join(project_dir, rel.replace("/", os.sep))
        try:
            meta = meta_of(fp)
        except OSError:
            continue
        rec = {"id": new_id(seq), "file": rel, "name": os.path.basename(rel),
               "type": kind_of(rel), "srcPath": "",
               "role": detect_role(fp, meta),
               "addedAt": datetime.datetime.now().isoformat(timespec="seconds"),
               "tags": [], "note": ""}
        rec.update(meta)
        mats.append(rec)
        by_file[rel] = rec
        seq += 1
        added += 1
    keep = []
    for m in mats:
        fp = os.path.join(project_dir, str(m.get("file", "")).replace("/", os.sep))
        if m.get("file") and not os.path.isfile(fp):
            removed += 1
            continue
        keep.append(m)
    sk["project"]["materials"] = keep
    save_skeleton(project_dir, sk)
    return added, removed, sk


# ── 命令行入口（agent 直接调用；输出可用 --json 解析）──────────────────────
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="project_core",
        description="项目框架 / 素材投放（agent 与工具共用同一套实现）")
    ap.add_argument("--root", help="样本库根目录（登记为 SAMPLES_ROOT）")
    ap.add_argument("--name", help="实验名；不带日期戳，重开自动加序号")
    ap.add_argument("--project", help="直接指定项目目录（建框架或投素材都可用）")
    ap.add_argument("--platform", default="", help="平台名（即梦 / 小云雀 / updream）")
    ap.add_argument("--add", nargs="+", metavar="PATH",
                    help="投放素材（文件或文件夹，可多个）")
    ap.add_argument("--reference", action="store_true",
                    help="只登记原路径不复制（默认复制进 <项目>/素材/）")
    ap.add_argument("--note", default="", help="给这批素材记一句备注")
    ap.add_argument("--rescan", action="store_true", help="按磁盘重扫、自愈清单")
    # ── 会话协议（exe ↔ agent）──
    ap.add_argument("--todos", action="store_true", help="读未处理的待办（agent 用）")
    ap.add_argument("--todo-done", action="store_true", help="把所有待办标记为已完成")
    ap.add_argument("--receipt", metavar="文字", help="写一条回执（agent → 程序）")
    ap.add_argument("--receipt-files", nargs="+", metavar="FILE", default=None,
                    help="回执里附带的产出文件")
    ap.add_argument("--state", action="store_true", help="看/写会话状态")
    ap.add_argument("--stage", help="配合 --state：设置阶段")
    ap.add_argument("--new-round", metavar="反馈文字", help="把本轮反馈留档并返回轮次号")
    ap.add_argument("--deliver", nargs="+", metavar="FILE",
                    help="接收成片；配 --bad 则当废片接收（废因用 --why）")
    ap.add_argument("--bad", action="store_true", help="--deliver 的判定设为废片")
    ap.add_argument("--why", default="", help="废因（作废文件名 = 日期-废因）")
    ap.add_argument("--no-register", action="store_true",
                    help="不把 --root 写进 references/paths.local.md（自检用）")
    ap.add_argument("--show", action="store_true", help="打印当前框架摘要")
    ap.add_argument("--auto", action="store_true",
                    help="全自动：自动定根目录与项目名、自动判素材角色、自动建框架并归类")
    ap.add_argument("--plan", action="store_true",
                    help="只做规划不落地：打印推出来的根目录/项目名/每个素材的角色")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    a = ap.parse_args(argv)

    # 0) 全自动模式：软件自己定根/定名/判角色/建框架/归类
    if a.auto or a.plan:
        if not a.add:
            out = {"ok": False, "error": "--auto / --plan 需要配合 --add <素材路径>"}
            print(json.dumps(out, ensure_ascii=False, indent=2) if a.json
                  else "[自动] 请用 --add 把素材路径给我")
            return 2
        if a.plan:
            plan = auto_plan(a.add, root=a.root, name=a.name)
            data = {"ok": True, "root": plan["root"], "guess_name": plan["guess_name"],
                    "final_name": plan["final_name"],
                    "items": [{"name": it["name"], "type": it["type"],
                               "role": it["role"]} for it in plan["items"]]}
            if a.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print("[规划] 根目录 =", plan["root"])
                print("[规划] 项目名 = %s（避让后 = %s）"
                      % (plan["guess_name"] or "(未推出)", plan["final_name"]))
                for it in plan["items"]:
                    print("[规划]   %-28s %-6s → %s" % (it["name"], it["type"], it["role"]))
                print("[规划] 素材 %d 个" % plan["count"])
            return 0
        pdir, plan, log = auto_build(a.add, root=a.root, name=a.name,
                                     platform=a.platform,
                                     register=bool(a.root) and not a.no_register)
        out = {"ok": bool(pdir), "project": pdir, "plan": {
            "root": plan["root"], "guess_name": plan["guess_name"],
            "final_name": plan["final_name"]}, "log": log}
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for m in log:
                print("[自动]", m)
            print("[自动] 项目 = %s" % pdir)
        return 0 if pdir else 1

    out = {"ok": True, "actions": []}
    root = detect_root(a.root)
    # 纯会话操作（读待办/写回执/看状态…）不要顺手重建框架，免得刷一堆噪声日志
    session_only = bool(a.todos or a.todo_done or a.receipt or a.state
                        or a.new_round or a.deliver)

    # 1) 建框架
    if (a.project or a.name) and not session_only:
        if not root and not a.project:
            out.update(ok=False, error="还没指定样本库根目录",
                       ask=ASK_PLACE,
                       hint='先问用户，再跑：--root "<样本库根>" --name "<实验名>"')
            print(json.dumps(out, ensure_ascii=False, indent=2) if a.json
                  else "[框架] 还没指定根目录。先问用户：%s" % ASK_PLACE)
            return 1
        name = a.name or os.path.basename(os.path.normpath(a.project))
        if root and not a.project and name_conflict(root, name):   # 同主题重开 → 加序号
            new = next_project_name(root, name)
            out["renamed_from"] = name
            name = new
        pdir = os.path.normpath(a.project) if a.project else os.path.join(root, name)
        # 只有显式给了 --root 才改写 references/paths.local.md；单给 --project 时只建框架，
        # 不动用户已配置好的本机取值。
        pdir, sk, log = build_skeleton(root or os.path.dirname(pdir), name,
                                       platform=a.platform, project_dir=pdir,
                                       register=bool(a.root) and not a.no_register)
        out["actions"].append({"build_skeleton": pdir, "log": log})
        if not a.json:
            for l in log:
                print("[框架]", l)

    # 2) 投素材
    if a.add:
        if not a.project:
            out.update(ok=False, error="--add 需要同时给 --project <项目目录>")
            print(json.dumps(out, ensure_ascii=False, indent=2) if a.json
                  else "[素材] --add 必须配 --project")
            return 2
        pdir = os.path.normpath(a.project)
        if not os.path.isdir(pdir):
            os.makedirs(pdir, exist_ok=True)
            build_skeleton(os.path.dirname(pdir), os.path.basename(pdir),
                           project_dir=pdir, register=bool(a.root))
        added, skipped, log = add_materials(pdir, a.add, copy=not a.reference,
                                            note=a.note)
        out["actions"].append({"add_materials": pdir,
                               "added": len(added), "skipped": len(skipped),
                               "files": [m["file"] for m in added],
                               "why_skipped": skipped})
        if not a.json:
            for l in log:
                print("[素材]", l)
            for s in skipped:
                print("[素材] 跳过 %s：%s" % (os.path.basename(s["src"]), s["why"]))

    # 3) 重扫
    if a.rescan:
        if not a.project:
            out.update(ok=False, error="--rescan 需要 --project")
            return 2
        ad, rm, sk = rescan(os.path.normpath(a.project))
        out["actions"].append({"rescan": a.project, "added": ad, "removed": rm})
        if not a.json:
            print("[重扫] 补录 %d，剔除 %d" % (ad, rm))

    # 3b) 会话协议：待办 / 回执 / 状态 / 轮次 / 交付接收
    if a.todos or a.todo_done or a.receipt or a.state or a.new_round or a.deliver:
        if not a.project:
            out.update(ok=False, error="会话相关动作都需要 --project <项目目录>")
            print(json.dumps(out, ensure_ascii=False, indent=2) if a.json
                  else "[会话] 需要 --project")
            return 2
        proj = os.path.normpath(a.project)
        if a.new_round:
            n = new_round(proj, a.new_round)          # 留档 + 落待办（new_round 内部已经做了）
            out["round"] = n
            if not a.json:
                print("[会话] 本轮反馈已留档：第 %d 轮（并已落一条待办给 agent）" % n)
        if a.deliver:
            placed, log = accept_deliverables(proj, a.deliver,
                                             verdict="bad" if a.bad else "good",
                                             note=a.why,
                                             on_log=None if a.json else print)
            out["deliver"] = placed
            if a.json:
                out.setdefault("log", []).extend(log)
        if a.receipt:
            rec = push_receipt(proj, a.receipt, files=a.receipt_files)
            out["receipt"] = rec
            if not a.json:
                print("[会话] 回执已写：%s" % rec["text"][:60])
        if a.todo_done:
            mark_todos_done(proj)
            out["todos_done"] = True
            if not a.json:
                print("[会话] 待办已全部标记完成")
        if a.stage:
            write_state(proj, stage=a.stage)
        if a.state or a.stage:
            st = write_state(proj) if a.stage else read_state(proj)
            out["state"] = st
            if not a.json:
                print("[会话] 状态 =", json.dumps(st, ensure_ascii=False))
        if a.todos:
            todos = read_todos(proj)
            out["todos"] = todos
            if not a.json:
                if not todos:
                    print("[会话] 没有未处理的待办")
                for t in todos:
                    print("[会话] 待办 #%d [%s] %s" % (t["i"], t["kind"], t["text"]))
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    # 4) 摘要
    target = a.project or (os.path.join(root, a.name) if (root and a.name) else None)
    if a.show or (not a.json and target and os.path.isdir(target)):
        sk = load_skeleton(target) if target else None
        if sk:
            mats = sk["project"].get("materials", [])
            out["skeleton"] = {"dir": target, "name": sk["project"].get("name"),
                               "counts": count_kinds(mats), "total": len(mats)}
            if not a.json:
                c = count_kinds(mats)
                print("[框架] %s：素材 %d（图 %d / 视频 %d / 音频 %d / 文本 %d / 其它 %d）"
                      % (target, len(mats), c["image"], c["video"], c["audio"],
                         c["text"], c["other"]))
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
