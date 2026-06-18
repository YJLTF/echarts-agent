# -*- mode: python ; coding: utf-8 -*-
"""
echarts-agent 的 PyInstaller 打包配置。

用法:
    pyinstaller echarts-agent.spec --noconfirm --clean

产物:
    dist/echarts-agent.exe
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve()

block_cipher = None

# 显式收集动态导入的 langchain 子模块，避免遗漏
LANGCHAIN_HIDDEN = [
    "langchain_openai",
    "langchain_openai.chat_models",
    "langchain_openai.chat_models.base",
    "langchain_core",
    "langchain_core.language_models",
    "langchain_core.language_models.chat_models",
    "langchain_core.messages",
    "langchain_core.output_parsers",
    "langchain_core.output_parsers.json",
    "langchain_core.prompts",
    "langchain_core.prompts.chat",
    "langchain_core.runnables",
    "langchain_community",
    "langchain_community.utilities",
    "langchain_community.chat_models",
]

PANDAS_HIDDEN = [
    "pandas",
    "pandas._libs",
    "pandas._libs.tslibs",
    "pandas._libs.tslibs.base",
    "openpyxl",
    "openpyxl.cell",
    "openpyxl.cell._writer",
    "openpyxl.styles",
    "openpyxl.utils",
    "openpyxl.worksheet",
    "openpyxl.reader.excel",
    "openpyxl.writer.excel",
]

EXCEL_HIDDEN = [
    "xlsxwriter",
]

datas = [
    # templates & static 必须随包分发；frozen 模式下 app.py 从 sys._MEIPASS 读取
    (str(PROJECT_ROOT / "templates"), "templates"),
    (str(PROJECT_ROOT / "static"), "static"),
]

hiddenimports = [
    "flask",
    "flask_cors",
    "werkzeug",
    "jinja2",
    "json",
    "sqlite3",
    "waitress",
] + LANGCHAIN_HIDDEN + PANDAS_HIDDEN + EXCEL_HIDDEN

a = Analysis(
    [str(PROJECT_ROOT / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy.tests",
        "PIL",
        "PyQt5",
        "PySide2",
        "scipy",
        "test",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="echarts-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
