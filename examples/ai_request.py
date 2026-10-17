"""通用 Chat-Completions 风格 API 示例；不会改变实际 AI API 地址。

需要 AI_API_URL（完整请求地址）、AI_API_KEY、AI_MODEL。
POOL_PROXY 默认 socks5h://127.0.0.1:1080；启用认证时自行填入代理凭据。
实际字段和鉴权方式请按你的 AI 服务商调整。
"""
import os
import httpx

proxy = os.environ.get('POOL_PROXY', 'socks5h://127.0.0.1:1080')

with httpx.Client(
    proxy=proxy,
    # 核心：每个 HTTP 请求使用新连接；禁止 HTTP/2 多路复用。
    limits=httpx.Limits(max_keepalive_connections=0, max_connections=20),
    http2=False,
    follow_redirects=False,
    timeout=httpx.Timeout(300, connect=30, pool=30),
) as client:
    for index in range(3):
        # 流式响应直到本次结束都使用同一条连接，不在中途强行换 IP。
        with client.stream(
            'POST', os.environ['AI_API_URL'],
            headers={'Authorization': 'Bearer ' + os.environ['AI_API_KEY']},
            json={'model': os.environ['AI_MODEL'],
                  'messages': [{'role': 'user', 'content': '只回复 OK'}], 'stream': True},
        ) as response:
            response.raise_for_status()
            print(f'--- 第 {index + 1} 次 API 请求 ---')
            for line in response.iter_lines():
                print(line, flush=True)
