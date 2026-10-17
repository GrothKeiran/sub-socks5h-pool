from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
import struct
import urllib.request
from urllib.parse import urlsplit

MAX_SUBSCRIPTION_BYTES = 5 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_subscription(url: str) -> bytes:
    # 不继承系统 HTTP_PROXY，不向在线转换站泄漏订阅；拒绝重定向。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={
        'User-Agent': 'clash.meta/sub-socks-pool',
        'Accept-Encoding': 'identity',
    })
    with opener.open(request, timeout=30) as response:
        data = response.read(MAX_SUBSCRIPTION_BYTES + 1)
    if not data or len(data) > MAX_SUBSCRIPTION_BYTES:
        raise ValueError('订阅为空或超过 5 MiB')
    return data


def address_bytes(host: str, port: int) -> bytes:
    if not 0 < port <= 65535:
        raise ValueError('目标端口无效')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        domain = host.encode('idna')
        if not domain or len(domain) > 255:
            raise ValueError('目标域名长度无效')
        return b'\x03' + bytes([len(domain)]) + domain + struct.pack('!H', port)
    return (b'\x01' if address.version == 4 else b'\x04') + address.packed + struct.pack('!H', port)


async def read_address(reader: asyncio.StreamReader, atyp: int, allow_zero: bool = False) -> tuple[str, int]:
    if atyp == 1:
        host = str(ipaddress.ip_address(await reader.readexactly(4)))
    elif atyp == 4:
        host = str(ipaddress.ip_address(await reader.readexactly(16)))
    elif atyp == 3:
        n = (await reader.readexactly(1))[0]
        if not n:
            raise ValueError('空域名')
        host = (await reader.readexactly(n)).decode('ascii')
    else:
        raise ValueError('不支持的地址类型')
    port = struct.unpack('!H', await reader.readexactly(2))[0]
    if not port and not allow_zero:
        raise ValueError('零端口')
    return host, port


async def socks_connect(proxy_port: int, host: str, port: int):
    reader, writer = await asyncio.open_connection('127.0.0.1', proxy_port)
    try:
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        if await reader.readexactly(2) != b'\x05\x00':
            raise ConnectionError('内核 SOCKS 握手失败')
        writer.write(b'\x05\x01\x00' + address_bytes(host, port))
        await writer.drain()
        head = await reader.readexactly(4)
        if head[:3] != b'\x05\x00\x00':
            raise ConnectionError('内核拒绝目标连接')
        await read_address(reader, head[3], allow_zero=True)
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def read_http_response(reader: asyncio.StreamReader, max_bytes: int = 65536) -> bytes:
    raw = await reader.readuntil(b'\r\n\r\n')
    if len(raw) > 16384:
        raise ValueError('HTTP 响应头过大')
    lines = raw.decode('iso-8859-1').split('\r\n')
    if lines[0].split(' ')[1] != '200':
        raise ValueError('HTTP 状态不是 200')
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.lower()] = v.strip()
    if headers.get('transfer-encoding', '').lower() == 'chunked':
        result = bytearray()
        while True:
            size = int((await reader.readline()).split(b';', 1)[0], 16)
            if size < 0 or len(result) + size > max_bytes:
                raise ValueError('HTTP 响应体过大')
            if size == 0:
                return bytes(result)
            result.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b'\r\n':
                raise ValueError('无效 chunk')
    if 'content-length' in headers:
        size = int(headers['content-length'])
        if not 0 <= size <= max_bytes:
            raise ValueError('HTTP 响应体过大')
        return await reader.readexactly(size)
    result = bytearray()
    while chunk := await reader.read(min(8192, max_bytes + 1 - len(result))):
        result.extend(chunk)
        if len(result) > max_bytes:
            raise ValueError('HTTP 响应体过大')
    return bytes(result)


async def probe_exit_ip(proxy_port: int, url: str, timeout: float) -> str:
    parsed = urlsplit(url)
    writer = None
    try:
        async with asyncio.timeout(timeout):
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            reader, writer = await socks_connect(proxy_port, host, port)
            if parsed.scheme == 'https':
                await writer.start_tls(ssl.create_default_context(), server_hostname=host)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            host_header = f'[{host}]' if ':' in host else host
            if parsed.port:
                host_header += f':{port}'
            request = (f'GET {path} HTTP/1.1\r\nHost: {host_header}\r\n'
                       'User-Agent: sub-socks-pool/1\r\nAccept-Encoding: identity\r\n'
                       'Connection: close\r\n\r\n')
            writer.write(request.encode('ascii'))
            await writer.drain()
            body = (await read_http_response(reader)).decode().strip()
            if body.startswith('{'):
                body = str(json.loads(body).get('ip', ''))
            ip = ipaddress.ip_address(body)
            if not ip.is_global:
                raise ValueError('探测结果不是公网 IP')
            return str(ip)
    finally:
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, ssl.SSLError, TimeoutError):
                pass


def reserve_ports(count: int) -> list[int]:
    # 持有所有 reservation，防止本批次端口重复；释放到内核绑定间存在极短竞态。
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
