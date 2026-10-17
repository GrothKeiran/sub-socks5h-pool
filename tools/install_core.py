#!/usr/bin/env python3
"""从 Mihomo 官方发布下载固定版本；校验固定 SHA-256，不执行远程脚本。"""
import gzip
import hashlib
import os
import platform
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = 'v1.19.32'
ASSETS = {
    'x86_64': ('amd64-compatible', 'ba3ce607747a07f948fc35780e108a4a7c7f552a38b9bd4d115f313ebcb89c20'),
    'aarch64': ('arm64', '9dd862e28b46ff7d775f169cceebc28deccaa0a9e804237d421cd2571e0caba0'),
}


def main():
    if platform.system() != 'Linux' or platform.machine() not in ASSETS:
        print('自动安装仅支持 Linux amd64/arm64；其他平台请手动安装并设置 CORE_PATH。')
        return 1
    arch, expected = ASSETS[platform.machine()]
    name = f'mihomo-linux-{arch}-{VERSION}.gz'
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    package = cache / name
    url = f'https://github.com/MetaCubeX/mihomo/releases/download/{VERSION}/{name}'
    if not package.exists():
        temp = package.with_suffix('.partial')
        try:
            with urllib.request.urlopen(url, timeout=120) as response, temp.open('wb') as out:
                size = 0
                while data := response.read(1024 * 1024):
                    size += len(data)
                    if size > 100 * 1024 * 1024:
                        raise ValueError('发布文件超过大小限制')
                    out.write(data)
            temp.replace(package)
        finally:
            temp.unlink(missing_ok=True)
    if hashlib.sha256(package.read_bytes()).hexdigest() != expected:
        package.unlink()
        raise ValueError('内核 SHA-256 校验失败；文件已删除')
    binary = ROOT / 'bin/mihomo'
    binary.parent.mkdir(exist_ok=True)
    temporary = binary.with_suffix('.tmp')
    temporary.write_bytes(gzip.decompress(package.read_bytes()))
    temporary.chmod(0o755)
    os.replace(temporary, binary)
    print(f'Mihomo {VERSION} 已安装，SHA-256 校验通过，架构 {arch}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('内核安装失败：' + type(exc).__name__)
        sys.exit(1)
