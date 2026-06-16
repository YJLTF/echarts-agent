#!/usr/bin/env python3
"""把外部依赖（ECharts 等）下载到 static/vendor/ 下，让本项目能完全脱机运行。

本脚本不需要任何第三方包，标准库即可。

用法：
    python scripts/download_vendor.py                # 下载全部
    python scripts/download_vendor.py --list         # 只列出要下载什么
    python scripts/download_vendor.py --version 5.5.0  # 指定 ECharts 版本
"""
from __future__ import annotations

import argparse
import os
import ssl
import sys
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(BASE_DIR, "static", "vendor", "echarts")

# 维护一个「要下载的 vendor 资源」清单。新增第三方 JS 时在这里登记。
VENDORS = {
    "echarts.min.js": "https://cdn.jsdelivr.net/npm/echarts@{version}/dist/echarts.min.js",
    "dark.js":        "https://cdn.jsdelivr.net/npm/echarts@{version}/theme/dark.js",
}


def _download(url: str, dest: str) -> int:
    """下载 url → dest，返回写入的字节数。"""
    ctx = ssl.create_default_context()
    print(f"  ↓ {url}")
    try:
        with urllib.request.urlopen(url, timeout=60, context=ctx) as r:
            data = r.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"下载失败 {url}: {e}") from e
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default="5.5.0", help="ECharts 版本，默认 5.5.0")
    ap.add_argument("--list", action="store_true", help="只列出要下载的资源，不下载")
    ap.add_argument("--force", action="store_true", help="强制覆盖已有文件")
    args = ap.parse_args()

    print(f"目标目录：{VENDOR_DIR}")
    print(f"ECharts 版本：{args.version}")
    print()
    if args.list:
        for name, url_tpl in VENDORS.items():
            url = url_tpl.format(version=args.version)
            target = os.path.join(VENDOR_DIR, name)
            exists = os.path.exists(target)
            print(f"  {'✓' if exists else '○'} {name}  ({len(open(target,'rb').read()) if exists else 0} bytes)")
            print(f"      {url}")
        return 0

    for name, url_tpl in VENDORS.items():
        url = url_tpl.format(version=args.version)
        target = os.path.join(VENDOR_DIR, name)
        if os.path.exists(target) and not args.force:
            print(f"  ✓ {name} 已存在（{os.path.getsize(target)} bytes），跳过。加 --force 覆盖。")
            continue
        size = _download(url, target)
        print(f"      写入 {target} ({size} bytes)")
    print()
    print("完成。本项目现在可以完全脱机运行；导出的 HTML 也自带 ECharts，无需网络。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
