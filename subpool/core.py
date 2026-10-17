from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .net import reserve_ports


@dataclass
class Candidate:
    alias: str
    port: int
    exit_ip: str = ''
    latency_ms: float = 0
    failures: int = 0
    dynamic_exit: bool = False


def exact_filter(name: str) -> str:
    # Go regexp 与 Python re.escape 的空格/横杠处理不同；只转义 RE2 元字符。
    return '^' + ''.join('\\' + c if c in r'\.+*?()|[]{}^$' else c for c in name) + '$'


class Generation:
    def __init__(self, config: Config):
        self.config = config
        self.path = config.data_dir / ('generation-' + secrets.token_hex(8))
        self.path.mkdir(parents=True, mode=0o700)
        self.path.chmod(0o700)
        self.api_port = reserve_ports(1)[0]
        self.secret = secrets.token_urlsafe(32)
        self.process = None
        self.candidates: list[Candidate] = []
        self.active_connections = 0
        self.retired = False
        self.closed = False
        self.loaded_at = 0.0

    def make_config(self, names: list[str]) -> dict:
        groups = []
        listeners = []
        for index, (name, candidate) in enumerate(zip(names, self.candidates)):
            group = f'__pool_{index}'
            groups.append({'name': group, 'type': 'select', 'use': ['subscription'],
                           'filter': exact_filter(name), 'empty-fallback': 'REJECT'})
            listeners.append({'name': f'in-{index}', 'type': 'socks', 'listen': '127.0.0.1',
                              'port': candidate.port, 'udp': False, 'proxy': group})
        if not groups:
            groups = [{'name': '__discovery', 'type': 'select', 'use': ['subscription'],
                       'empty-fallback': 'REJECT'}]
        return {
            'mode': 'rule', 'log-level': 'silent', 'ipv6': True,
            'external-controller': f'127.0.0.1:{self.api_port}', 'secret': self.secret,
            'allow-lan': False, 'profile': {'store-selected': False},
            'dns': {'enable': False},
            'proxy-providers': {'subscription': {'type': 'file',
                'path': './subscription.txt', 'health-check': {'enable': False}}},
            'proxy-groups': groups, 'listeners': listeners, 'rules': ['MATCH,REJECT'],
        }

    def write_config(self, data: dict) -> None:
        path = self.path / 'config.json'
        path.write_text(json.dumps(data, ensure_ascii=False))
        path.chmod(0o600)

    async def api(self, method: str, path: str, body=None):
        def call():
            data = None if body is None else json.dumps(body).encode()
            request = urllib.request.Request(
                f'http://127.0.0.1:{self.api_port}{path}', data=data, method=method,
                headers={'Authorization': 'Bearer ' + self.secret,
                         'Content-Type': 'application/json'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                raw = response.read(4 * 1024 * 1024)
                return json.loads(raw) if raw else None
        return await asyncio.to_thread(call)

    async def start(self, raw: bytes):
        subscription = self.path / 'subscription.txt'
        subscription.write_bytes(raw)
        subscription.chmod(0o600)
        self.write_config(self.make_config([]))
        self.process = await asyncio.create_subprocess_exec(
            str(self.config.core_path), '-d', str(self.path), '-f', str(self.path / 'config.json'),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, 'HOME': str(self.path), 'XDG_CACHE_HOME': str(self.path / '.cache'),
                 'TMPDIR': str(self.config.data_dir / 'tmp')})
        for _ in range(100):
            if self.process.returncode is not None:
                raise RuntimeError('内核启动失败（订阅格式、端口或内核配置错误）')
            try:
                provider = await self.api('GET', '/providers/proxies/subscription')
                if provider.get('proxies'):
                    break
                await asyncio.sleep(0.1)
            except (OSError, ValueError):
                await asyncio.sleep(0.1)
        else:
            raise TimeoutError('内核 API 未就绪')
        names = []
        seen = set()
        for node in provider.get('proxies', []):
            name = node.get('name', '')
            kind = node.get('type', '').lower()
            if (not name or len(name) > 512 or '`' in name or name.startswith('__pool_')
                    or name == '__discovery' or any(ord(c) < 32 for c in name)):
                continue
            if kind in ('direct', 'reject', 'rejectdrop', 'compatible', 'pass', 'dns'):
                continue
            if name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) >= self.config.max_nodes:
                break
        if not names:
            raise ValueError('订阅没有可用的受支持代理节点')
        ports = reserve_ports(len(names))
        self.candidates = [Candidate(f'node-{i+1:04d}', port) for i, port in enumerate(ports)]
        self.write_config(self.make_config(names))
        await self.api('PUT', '/configs?force=true', {'path': str(self.path / 'config.json')})
        # 配置加载成功不代表 listener 一定绑定成功；每个端口必须完成 SOCKS 握手。
        async def check(port):
            async with asyncio.timeout(5):
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                try:
                    writer.write(b'\x05\x01\x00')
                    await writer.drain()
                    if await reader.readexactly(2) != b'\x05\x00':
                        raise RuntimeError('节点监听器握手失败')
                finally:
                    writer.close()
        await asyncio.gather(*(check(p) for p in ports))

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        shutil.rmtree(self.path, ignore_errors=True)
