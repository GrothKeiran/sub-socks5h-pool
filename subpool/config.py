from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not key.strip().replace('_', '').isalnum():
            raise ValueError('无效的环境配置行')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def validate_url(url: str) -> None:
    p = urlsplit(url)
    if not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('URL 必须包含主机，且不能包含用户信息或片段')
    local = p.hostname in ('localhost', '127.0.0.1', '::1')
    if p.scheme != 'https' and not (p.scheme == 'http' and local):
        raise ValueError('仅允许 HTTPS；本地测试地址可使用 HTTP')


def is_loopback(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Config:
    subscription_url: str
    data_dir: Path = ROOT / 'data'
    core_path: Path = ROOT / 'bin/mihomo'
    socks_host: str = '127.0.0.1'
    socks_port: int = 1080
    admin_host: str = '127.0.0.1'
    admin_port: int = 8090
    socks_username: str = ''
    socks_password: str = ''
    admin_token: str = ''
    refresh_seconds: float = 7200
    health_seconds: float = 300
    max_stale_seconds: float = 10800
    probe_url: str = 'https://api4.ipify.org'
    probe_timeout: float = 12
    probe_concurrency: int = 8
    max_nodes: int = 256
    require_stable_exit: bool = False
    min_distinct_ips: int = 1
    failure_threshold: int = 2
    connect_timeout: float = 20
    relay_idle_timeout: float = 300

    def validate(self) -> None:
        validate_url(self.subscription_url)
        validate_url(self.probe_url)
        if bool(self.socks_username) != bool(self.socks_password):
            raise ValueError('SOCKS_USERNAME 与 SOCKS_PASSWORD 必须同时配置')
        if any(len(v.encode()) > 255 for v in (self.socks_username, self.socks_password)):
            raise ValueError('SOCKS5 用户名或密码超过 255 字节')
        if not is_loopback(self.socks_host) and not self.socks_username:
            raise ValueError('非回环 SOCKS 监听必须设置用户名和密码')
        if not is_loopback(self.admin_host) and not self.admin_token:
            raise ValueError('非回环管理监听必须显式设置 ADMIN_TOKEN')
        for n in ('refresh_seconds', 'health_seconds', 'max_stale_seconds', 'probe_timeout',
                  'probe_concurrency', 'max_nodes', 'failure_threshold', 'connect_timeout',
                  'relay_idle_timeout', 'min_distinct_ips'):
            if getattr(self, n) <= 0:
                raise ValueError(f'{n} 必须大于零')
        if self.min_distinct_ips > self.max_nodes:
            raise ValueError('MIN_DISTINCT_IPS 不能超过 MAX_NODES')
        if self.max_nodes > 1024 or self.probe_concurrency > 64:
            raise ValueError('节点上限为 1024，探测并发上限为 64')
        for port in (self.socks_port, self.admin_port):
            if not 0 < port < 65536:
                raise ValueError('监听端口无效')

    @classmethod
    def from_env(cls) -> 'Config':
        load_env(ROOT / '.env')
        env = os.environ
        kwargs = {}
        names = {
            'subscription_url': 'SUBSCRIPTION_URL', 'socks_host': 'SOCKS_HOST',
            'admin_host': 'ADMIN_HOST', 'socks_username': 'SOCKS_USERNAME',
            'socks_password': 'SOCKS_PASSWORD', 'admin_token': 'ADMIN_TOKEN',
            'probe_url': 'PROBE_URL',
        }
        for key, name in names.items():
            if name in env:
                kwargs[key] = env[name]
        if 'REQUIRE_STABLE_EXIT' in env:
            value = env['REQUIRE_STABLE_EXIT'].strip().lower()
            if value not in ('true', 'false', '1', '0', 'yes', 'no'):
                raise ValueError('REQUIRE_STABLE_EXIT 必须为 true 或 false')
            kwargs['require_stable_exit'] = value in ('true', '1', 'yes')
        for key in ('socks_port', 'admin_port', 'probe_concurrency', 'max_nodes', 'failure_threshold', 'min_distinct_ips'):
            if key.upper() in env:
                kwargs[key] = int(env[key.upper()])
        for key in ('refresh_seconds', 'health_seconds', 'max_stale_seconds', 'probe_timeout',
                    'connect_timeout', 'relay_idle_timeout', 'min_distinct_ips'):
            if key.upper() in env:
                kwargs[key] = float(env[key.upper()])
        for key in ('data_dir', 'core_path'):
            if key.upper() in env:
                kwargs[key] = Path(env[key.upper()]).resolve()
        if not kwargs.get('subscription_url'):
            raise ValueError('请在 .env 中设置 SUBSCRIPTION_URL')
        result = cls(**kwargs)
        result.validate()
        return result
