"""真实 Mihomo + 本地假上游；不连接互联网，不依赖真实订阅。"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subpool.config import Config
from subpool.core import Generation
from subpool.net import read_address, reserve_ports, socks_connect
from subpool.pool import Pool
from subpool.server import Servers

ROOT=Path(__file__).resolve().parent.parent


class FakeUpstream:
    def __init__(self,label,ip):
        self.label,self.ip=label,ip
        self.server=None;self.tasks=set();self.targets=[]

    async def start(self):
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        self.port=self.server.sockets[0].getsockname()[1]

    async def handle(self,reader,writer):
        task=asyncio.current_task();self.tasks.add(task)
        try:
            version,n=await reader.readexactly(2);await reader.readexactly(n)
            writer.write(b'\x05\x00');await writer.drain()
            head=await reader.readexactly(4)
            self.targets.append(await read_address(reader,head[3]))
            writer.write(b'\x05\x00\x00\x01'+b'\0'*6);await writer.drain()
            while line:=await reader.readline():
                if line.startswith(b'GET '):
                    await reader.readuntil(b'\r\n\r\n')
                    body=self.ip.encode()
                    writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
                    await writer.drain();return
                writer.write(self.label.encode()+b':'+line);await writer.drain()
        except (OSError,ValueError,asyncio.IncompleteReadError):pass
        finally:
            writer.close();self.tasks.discard(task)

    async def close(self):
        self.server.close();await self.server.wait_closed()
        tasks=list(self.tasks)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


@unittest.skipUnless((ROOT/'bin/mihomo').exists(),'未安装真实 Mihomo 内核')
class CoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT/'.cache/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.cache/tests')
        ports=reserve_ports(2)
        self.config=Config('https://example.invalid/sub',data_dir=Path(self.temp.name),
                           socks_port=ports[0],admin_port=ports[1],admin_token='test-token',
                           probe_url='http://127.0.0.1/ip',probe_timeout=3,probe_concurrency=1)
        (self.config.data_dir/'tmp').mkdir()
        self.nodes=[FakeUpstream('A','8.8.8.8'),FakeUpstream('B','8.8.4.4'),FakeUpstream('C','8.8.8.8')]
        for node in self.nodes:await node.start()
        self.raw=json.dumps({'proxies':[{'name':f'{node.label} [test]+ x-y','type':'socks5',
                            'server':'127.0.0.1','port':node.port} for node in self.nodes]}).encode()
        self.pool=Pool(self.config);self.servers=Servers(self.pool)

    async def asyncTearDown(self):
        await self.servers.close();await self.pool.close()
        for node in self.nodes:await node.close()
        self.temp.cleanup()

    async def refresh(self):
        with patch('subpool.pool.fetch_subscription',return_value=self.raw):
            self.assertTrue(await self.pool.refresh())

    async def test_real_core_dedup_rotation_remote_dns_and_live_refresh(self):
        await self.refresh();await self.servers.start()
        self.assertEqual(len(self.pool.members),2)
        self.assertEqual(self.pool.last_probe_stats['duplicates_removed'],1)
        observed=[]
        for _ in range(6):
            reader,writer=await socks_connect(self.config.socks_port,'not-in-local-dns.invalid',443)
            writer.write(b'PING\n');await writer.drain()
            observed.append((await asyncio.wait_for(reader.readline(),3)).split(b':')[0])
            writer.close();await writer.wait_closed()
        self.assertTrue(all(a!=b for a,b in zip(observed,observed[1:])))
        self.assertEqual(len(set(observed)),2)
        self.assertTrue(any(('not-in-local-dns.invalid',443) in n.targets for n in self.nodes))
        # 在同一连接上发送多个逻辑请求，不会轮换；连接跨更新仍可继续。
        reader,writer=await socks_connect(self.config.socks_port,'stream.invalid',443)
        writer.write(b'FIRST\n');await writer.drain()
        first=(await reader.readline()).split(b':')[0]
        old=self.pool.generation
        await self.refresh()
        self.assertFalse(old.closed)
        writer.write(b'SECOND\n');await writer.drain()
        second=(await reader.readline()).split(b':')[0]
        self.assertEqual(first,second)
        writer.close();await writer.wait_closed()
        for _ in range(50):
            if old.closed:break
            await asyncio.sleep(.02)
        self.assertTrue(old.closed)

    async def test_real_core_health_probe_changes_and_deduplicates_ip(self):
        await self.refresh()
        for node in self.nodes:node.ip='9.9.9.9'
        await self.pool.health_check()
        self.assertEqual(len(self.pool.members),1)
        self.assertTrue(self.pool.ready())
        self.assertTrue(self.pool.status()['single_ip_fallback'])

    async def test_base64_vless_subscription_parses(self):
        # 只验证内核 URI 解析，不向这个占位节点发起连接。
        uri='vless://00000000-0000-4000-8000-000000000001@127.0.0.1:9?encryption=none&security=none&type=tcp#test'
        gen=Generation(self.config)
        try:
            await gen.start(base64.b64encode(uri.encode()))
            self.assertEqual(len(gen.candidates),1)
        finally:await gen.close()

    async def test_full_cli_start_health_status_and_graceful_shutdown(self):
        async def subscription(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {len(self.raw)}\r\nConnection: close\r\n\r\n'.encode()+self.raw)
                await writer.drain()
            finally:
                writer.close()
        sub_server=await asyncio.start_server(subscription,'127.0.0.1',0)
        sub_port=sub_server.sockets[0].getsockname()[1]
        env={**os.environ,'SUBSCRIPTION_URL':f'http://127.0.0.1:{sub_port}/sub',
             'CORE_PATH':str(ROOT/'bin/mihomo'),'DATA_DIR':str(self.config.data_dir),
             'SOCKS_HOST':'127.0.0.1','SOCKS_PORT':str(self.config.socks_port),
             'SOCKS_USERNAME':'','SOCKS_PASSWORD':'',
             'ADMIN_HOST':'127.0.0.1','ADMIN_PORT':str(self.config.admin_port),
             'ADMIN_TOKEN':'test-cli-secret','PROBE_URL':'http://127.0.0.1/ip',
             'PROBE_CONCURRENCY':'1','PROBE_TIMEOUT':'3'}
        process=await asyncio.create_subprocess_exec(sys.executable,'-m','subpool',cwd=ROOT,
                    env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        try:
            async def request(path):
                reader,writer=await asyncio.open_connection('127.0.0.1',self.config.admin_port)
                writer.write(f'GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-cli-secret\r\n\r\n'.encode())
                await writer.drain()
                result=await reader.read();writer.close();await writer.wait_closed();return result
            ready=False
            for _ in range(100):
                if process.returncode is not None:break
                try:
                    raw=await request('/healthz')
                    if b'200 OK' in raw:
                        ready=True;break
                except OSError:pass
                await asyncio.sleep(.05)
            self.assertTrue(ready)
            status=await request('/status')
            self.assertIn(b'"distinct_observed_ips": 2',status)
            self.assertNotIn(b'test-cli-secret',status)
            reader,writer=await socks_connect(self.config.socks_port,'cli.invalid',443)
            writer.write(b'CLI\n');await writer.drain()
            self.assertIn(b':CLI',await asyncio.wait_for(reader.readline(),3))
            writer.close();await writer.wait_closed()
        finally:
            if process.returncode is None:process.terminate()
            stdout,_=await asyncio.wait_for(process.communicate(),10)
            sub_server.close();await sub_server.wait_closed()
        self.assertEqual(process.returncode,0)
        self.assertNotIn(b'test-cli-secret',stdout)
        self.assertFalse(list(self.config.data_dir.glob('generation-*')))
