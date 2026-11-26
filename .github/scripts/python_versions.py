#!/usr/bin/env python3
"""
列出 CI 要测试的 python 版本：从 MIN_VERSION 到最新正式版的每个小版本。
数据来自 actions/setup-python 使用的 versions-manifest.json，只取有 linux x64 指定 ubuntu 版本构建的正式版，
保证列出的版本 setup-python 都能安装；新的 python 正式版发布并构建后自动加入测试。

用法: python_versions.py <versions-manifest.json> [ubuntu 版本，默认 24.04]
输出: JSON 数组，如 ["3.8", "3.9", ..., "3.15"]
"""

import json
import sys

MIN_VERSION = (3, 8)


def python_versions(manifest, platform_version):
    found = set()
    for release in manifest:
        if not release.get('stable'):
            continue
        if not any(f.get('platform') == 'linux' and f.get('arch') == 'x64'
                   and f.get('platform_version') == platform_version for f in release.get('files', [])):
            continue
        major, minor = (int(x) for x in release['version'].split('.')[:2])
        if (major, minor) >= MIN_VERSION:
            found.add((major, minor))
    return ['{}.{}'.format(*v) for v in sorted(found)]


if __name__ == '__main__':
    with open(sys.argv[1]) as f:
        versions = python_versions(json.load(f), sys.argv[2] if len(sys.argv) > 2 else '24.04')
    if '{}.{}'.format(*MIN_VERSION) not in versions or len(versions) < 2:
        sys.exit('unexpected version list: {}'.format(versions))
    print(json.dumps(versions))
