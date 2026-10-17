from __future__ import annotations

import asyncio
import hmac
import json
import logging
from urllib.parse import urlsplit

from .net import read_address, socks_connect
from .pool import Pool, PoolUnavailable

log = logging.getLogger('subpool')
REPLY = b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00'


async def relay(left_reader, left_writer, right_reader, right_writer, idle_timeout):
    async def pump(reader, writer):
        while True:
            data = await asyncio.wait_for(reader.read(65536), idle_timeout)
            if not data:
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
                return
            writer.write(data)
            await asyncio.wait_for(writer.drain(), idle_timeout)
    tasks = [asyncio.create_task(pump(left_reader, right_writer)),
             asyncio.create_task(pump(right_reader, left_writer))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Servers:
    def __init__(self, pool: Pool):
        self.pool = pool
        self.config = pool.config
        self.tasks = set()
        self.listeners = []
        self.manual_refresh = None

    def track(self, handler):
        async def wrapped(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                await handler(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug('客户端连接结束（%s）', type(exc).__name__)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                self.tasks.discard(task)
        return wrapped

    async def start(self):
        self.listeners.append(await asyncio.start_server(self.track(self.socks),
                              self.config.socks_host, self.config.socks_port, limit=32768))
        try:
            self.listeners.append(await asyncio.start_server(self.track(self.admin),
                                  self.config.admin_host, self.config.admin_port, limit=16384))
        except BaseException:
            await self.close()
            raise

    async def socks(self, reader, writer):
        async with asyncio.timeout(10):
            version, count = await reader.readexactly(2)
            if version != 5 or not count:
                return
            methods = await reader.readexactly(count)
            auth = bool(self.config.socks_username)
            method = 2 if auth else 0
            if method not in methods:
                writer.write(b'\x05\xff')
                await writer.drain()
                return
            writer.write(bytes([5, method]))
            await writer.drain()
            if auth:
                version, n = await reader.readexactly(2)
                username = await reader.readexactly(n)
                n = (await reader.readexactly(1))[0]
                password = await reader.readexactly(n)
                valid = (version == 1 and n > 0 and
                    hmac.compare_digest(username, self.config.socks_username.encode()) and
                    hmac.compare_digest(password, self.config.socks_password.encode()))
                writer.write(bytes([1, 0 if valid else 1]))
                await writer.drain()
                if not valid:
                    return
            version, command, reserved, atyp = await reader.readexactly(4)
            if version != 5 or reserved != 0:
                return
            if command != 1:
                writer.write(REPLY[:1] + b'\x07' + REPLY[2:])
                await writer.drain()
                return
            try:
                host, port = await read_address(reader, atyp)
            except (ValueError, UnicodeError):
                writer.write(REPLY[:1] + b'\x08' + REPLY[2:])
                await writer.drain()
                return
        lease = None
        upstream = None
        connected = False
        try:
            lease = await self.pool.acquire()
            async with asyncio.timeout(self.config.connect_timeout):
                upstream_reader, upstream = await socks_connect(lease.candidate.port, host, port)
            await self.pool.mark_success(lease)
            writer.write(REPLY)
            await writer.drain()
            connected = True
            await relay(reader, writer, upstream_reader, upstream, self.config.relay_idle_timeout)
        except asyncio.CancelledError:
            raise
        except (PoolUnavailable, OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
            if lease and not connected:
                await self.pool.mark_failure(lease)
            if not connected:
                writer.write(REPLY[:1] + b'\x01' + REPLY[2:])
                await writer.drain()
        finally:
            if upstream:
                upstream.close()
            if lease:
                await self.pool.release(lease)

    async def admin(self, reader, writer):
        async with asyncio.timeout(5):
            raw = await reader.readuntil(b'\r\n\r\n')
            if len(raw) > 16384:
                return
            lines = raw.decode('iso-8859-1').split('\r\n')
            method, target, version = lines[0].split(' ', 2)
            path = urlsplit(target).path
            headers = {}
            for line in lines[1:]:
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.lower()] = v.strip()
            status, result = 404, {'error': 'not_found'}
            token = self.config.admin_token
            authorized = bool(token and hmac.compare_digest(
                headers.get('authorization', '').encode(), ('Bearer ' + token).encode()))
            if method == 'GET' and path == '/healthz':
                status = 200 if self.pool.ready() else 503
                result = {'ready': self.pool.ready()}
            elif not authorized:
                status, result = 401, {'error': 'unauthorized'}
            elif method == 'GET' and path == '/status':
                status, result = 200, self.pool.status()
            elif method == 'POST' and path == '/refresh':
                if self.pool.update_lock.locked() or (self.manual_refresh and not self.manual_refresh.done()):
                    status, result = 409, {'error': 'update_in_progress'}
                else:
                    self.manual_refresh = asyncio.create_task(self.pool.refresh())
                    status, result = 202, {'status': 'accepted'}
            body = json.dumps(result, ensure_ascii=False).encode()
            reason = {200: 'OK', 202: 'Accepted', 401: 'Unauthorized', 404: 'Not Found',
                      409: 'Conflict', 503: 'Service Unavailable'}[status]
            writer.write((f'HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\n'
                          f'Content-Length: {len(body)}\r\nConnection: close\r\n'
                          'Cache-Control: no-store\r\n\r\n').encode() + body)
            await writer.drain()

    async def close(self):
        for listener in self.listeners:
            listener.close()
            await listener.wait_closed()
        if self.manual_refresh and not self.manual_refresh.done():
            self.manual_refresh.cancel()
            await asyncio.gather(self.manual_refresh, return_exceptions=True)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
