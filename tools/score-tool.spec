# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('score_core.py', '.'), ('project_core.py', '.'), ('agent_bridge.py', '.'),
         ('workbench_server.py', '.'), ('workbench', 'workbench')]
# workbench/ 是 HTML/CSS/JS 前端（界面默认入口）；workbench_server.py 是它的本地服务
binaries = []
# workbench_server.py 是**运行时动态加载**的（_load_core），PyInstaller 静态分析
# 看不见它 import 了什么 → 必须在这里把它们全列出来，否则打包后报
# ModuleNotFoundError: No module named 'queue'（2026-09-15 实踩）。
hiddenimports = ['json', 'datetime', 'argparse',
                 'queue', 'threading', 'socket', 'selectors', 'socketserver',
                 'http', 'http.server', 'http.client', 'mimetypes',
                 'urllib', 'urllib.parse', 'urllib.request', 'webbrowser',
                 'shutil', 'glob', 'base64', 'struct', 'zlib',
                 'email', 'email.utils', 'email.parser',
                 'tkinter', 'tkinter.filedialog', 'tkinter.font', 'tkinter.messagebox']
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# 独立窗口（pywebview 内嵌 WebView2）：界面默认入口，必须整包带上，
# 否则 exe 里 import webview 失败 → 只能退回 Edge app 窗口（用户不要那个）。
for pkg in ('webview', 'pythonnet', 'clr_loader'):
    try:
        _r = collect_all(pkg)
        datas += _r[0]; binaries += _r[1]; hiddenimports += _r[2]
    except Exception as _e:                                      # noqa: BLE001
        print('collect_all(%s) 失败: %r' % (pkg, _e))
hiddenimports += ['webview.platforms.edgechromium', 'webview.platforms.winforms',
                  'clr_loader', 'pythonnet', 'System', 'System.Windows.Forms',
                  'System.Drawing', 'System.Threading']


a = Analysis(
    ['score_gui.pyw'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='score-tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
