#!/usr/bin/env python3
"""读取受保护的管理状态；令牌不出现在命令行参数或输出中。"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subpool.config import Config

config=Config.from_env()
token=config.admin_token
if not token:
    path=config.data_dir/'admin.token'
    token=path.read_text().strip() if path.exists() else ''
url=f'http://127.0.0.1:{config.admin_port}/status'
request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(request,timeout=5) as response:
        result=json.load(response)
    print(json.dumps(result,ensure_ascii=False,indent=2))
except Exception as exc:
    print('状态读取失败：'+type(exc).__name__)
    sys.exit(1)
