#!/usr/bin/env python3
"""为 Docker 生成本地认证配置；不会打印订阅 token 或密码。"""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / '.env'
values = {}
if path.exists():
    for raw in path.read_text().splitlines():
        if raw.strip() and not raw.lstrip().startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            values[key.strip()] = value.strip()
if not values.get('SUBSCRIPTION_URL'):
    values['SUBSCRIPTION_URL'] = 'https://your-subscription.example/sub?token=replace-me'
for key, default in [('SOCKS_USERNAME', 'pool'), ('SOCKS_PASSWORD', secrets.token_urlsafe(24)),
                     ('ADMIN_TOKEN', secrets.token_urlsafe(32)),
                     ('PUID', str(os.getuid())), ('PGID', str(os.getgid()))]:
    if not values.get(key):
        values[key] = default
path.write_text('\n'.join(f'{key}={value}' for key,value in values.items()) + '\n')
path.chmod(0o600)
(root / 'data/tmp').mkdir(parents=True, exist_ok=True, mode=0o700)
(root / 'data').chmod(0o700)
print('已生成/补全 .env 的认证配置，未输出任何凭据。请确认 SUBSCRIPTION_URL 后启动。')
