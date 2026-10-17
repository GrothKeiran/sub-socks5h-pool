import asyncio
import dataclasses
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from subpool.config import Config, validate_url
from subpool.core import Candidate, exact_filter
from subpool.net import address_bytes, read_address, read_http_response, socks_connect
from subpool.pool import Pool, PoolUnavailable, deduplicate
from subpool.server import Servers


def fake_generation():
    gen = type('FakeGeneration', (), {})()
    gen.process = SimpleNamespace(returncode=None)
    gen.closed = False
    gen.active_connections = 0
    gen.loaded_at = time.monotonic()
    gen.retired = False
    gen.candidates = []
    gen.close = AsyncMock()
    return gen


def make_pool(count=3, **kwargs):
    config = Config(subscription_url='https://example.invalid/sub', **kwargs)
    pool = Pool(config)
    pool.generation = fake_generation()
    pool.members = [Candidate(f'node-{i}', 10000+i, f'8.8.8.{i+1}', i+1) for i in range(count)]
    pool.generation.candidates = pool.members.copy()
    return pool


class ConfigTests(unittest.TestCase):
    def test_valid_defaults(self):
        Config('https://example.invalid/sub').validate()

    def test_reject_public_http_subscription(self):
        with self.assertRaises(ValueError):
            validate_url('http://example.com/sub')

    def test_allow_local_test_http(self):
        validate_url('http://127.0.0.1:1234/sub')

    def test_no_url_credentials(self):
        with self.assertRaises(ValueError):
            validate_url('https://user:password@example.com/sub')

    def test_public_socks_requires_auth(self):
        with self.assertRaises(ValueError):
            Config('https://example.invalid', socks_host='0.0.0.0').validate()

    def test_public_admin_requires_token(self):
        with self.assertRaises(ValueError):
            Config('https://example.invalid', admin_host='0.0.0.0').validate()

    def test_both_credentials_required(self):
        with self.assertRaises(ValueError):
            Config('https://example.invalid', socks_username='abc').validate()

    def test_bad_intervals(self):
        with self.assertRaises(ValueError):
            Config('https://example.invalid', refresh_seconds=0).validate()

    def test_filter_escapes_re2_metacharacters(self):
        self.assertEqual(exact_filter('A [US]+ x-y'), r'^A \[US\]\+ x-y$')

    def test_dedup_uses_lowest_latency(self):
        a = Candidate('a', 1, '8.8.8.8', 50)
        b = Candidate('b', 2, '8.8.8.8', 10)
        c = Candidate('c', 3, '1.1.1.1', 20)
        self.assertEqual(deduplicate([a,b,c]), [b,c])

    def test_dedup_ignores_unprobed(self):
        self.assertEqual(deduplicate([Candidate('a', 1)]), [])


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_encoding_no_local_dns(self):
        self.assertEqual(address_bytes('unresolved.invalid',443),
                         b'\x03\x12unresolved.invalid\x01\xbb')

    async def test_ipv6_encoding(self):
        self.assertEqual(address_bytes('::1',80)[0],4)

    async def test_zero_bound_port_is_valid(self):
        reader=asyncio.StreamReader();reader.feed_data(b'\0'*6);reader.feed_eof()
        self.assertEqual(await read_address(reader,1,True),('0.0.0.0',0))

    async def test_zero_target_port_rejected(self):
        reader=asyncio.StreamReader();reader.feed_data(b'\0'*6);reader.feed_eof()
        with self.assertRaises(ValueError):await read_address(reader,1)

    async def test_http_content_length(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc');reader.feed_eof()
        self.assertEqual(await read_http_response(reader),b'abc')

    async def test_http_chunked(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n');reader.feed_eof()
        self.assertEqual(await read_http_response(reader),b'abc')

    async def test_http_body_limit(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n');reader.feed_eof()
        with self.assertRaises(ValueError):await read_http_response(reader,10)

    async def test_http_non_200(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'HTTP/1.1 429 Too Many Requests\r\n\r\n');reader.feed_eof()
        with self.assertRaises(ValueError):await read_http_response(reader)


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_robin(self):
        pool=make_pool(); ips=[]
        for _ in range(7):
            lease=await pool.acquire();ips.append(lease.candidate.exit_ip);await pool.release(lease)
        self.assertEqual(ips,['8.8.8.1','8.8.8.2','8.8.8.3','8.8.8.1','8.8.8.2','8.8.8.3','8.8.8.1'])

    async def test_concurrent_allocations(self):
        pool=make_pool()
        leases=await asyncio.gather(*(pool.acquire() for _ in range(60)))
        ips=[n.candidate.exit_ip for n in leases]
        self.assertTrue(all(a!=b for a,b in zip(ips,ips[1:])))
        await asyncio.gather(*(pool.release(n) for n in leases))
        self.assertEqual(pool.generation.active_connections,0)

    async def test_one_ip_fails_closed_in_strict_mode(self):
        pool=make_pool(1,min_distinct_ips=2)
        with self.assertRaises(PoolUnavailable):await pool.acquire()

    async def test_single_ip_fallback_keeps_service_available(self):
        pool=make_pool(1)
        for _ in range(3):
            lease=await pool.acquire()
            self.assertEqual(lease.candidate.exit_ip,'8.8.8.1')
            await pool.release(lease)
        self.assertTrue(pool.ready())
        self.assertTrue(pool.status()['single_ip_fallback'])

    async def test_no_healthy_nodes_still_fails_closed(self):
        pool=make_pool(1,failure_threshold=1)
        lease=await pool.acquire()
        await pool.mark_failure(lease)
        await pool.release(lease)
        self.assertFalse(pool.ready())
        with self.assertRaises(PoolUnavailable):await pool.acquire()

    async def test_default_refresh_accepts_one_exit(self):
        pool=make_pool();staged=fake_generation();staged.start=AsyncMock()
        with patch('subpool.pool.fetch_subscription',return_value=b'test'), \
             patch('subpool.pool.Generation',return_value=staged), \
             patch.object(pool,'probe',new=AsyncMock(return_value=[pool.members[0]])):
            self.assertTrue(await pool.refresh())
        self.assertTrue(pool.ready())
        self.assertEqual(len(pool.members),1)

    async def test_empty_fails_closed(self):
        pool=make_pool(0)
        with self.assertRaises(PoolUnavailable):await pool.acquire()

    async def test_stale_fails_closed(self):
        pool=make_pool(max_stale_seconds=1);pool.generation.loaded_at-=2
        with self.assertRaises(PoolUnavailable):await pool.acquire()

    async def test_dead_core_fails_closed(self):
        pool=make_pool();pool.generation.process.returncode=1
        with self.assertRaises(PoolUnavailable):await pool.acquire()

    async def test_cross_refresh_skips_last_ip(self):
        pool=make_pool();lease=await pool.acquire();await pool.release(lease)
        pool.index=0
        next_lease=await pool.acquire()
        self.assertNotEqual(lease.candidate.exit_ip,next_lease.candidate.exit_ip)
        await pool.release(next_lease)

    async def test_quarantine_failures(self):
        pool=make_pool(2,failure_threshold=2,min_distinct_ips=2);lease=await pool.acquire()
        await pool.mark_failure(lease);self.assertTrue(pool.ready())
        await pool.mark_failure(lease);self.assertFalse(pool.ready())
        await pool.release(lease)

    async def test_success_resets_failure_streak(self):
        pool=make_pool();lease=await pool.acquire()
        await pool.mark_failure(lease);await pool.mark_success(lease)
        self.assertEqual(lease.candidate.failures,0);await pool.release(lease)

    async def test_retired_core_drains(self):
        pool=make_pool();lease=await pool.acquire();old=pool.generation
        pool.generation=fake_generation();old.retired=True;pool.retired.add(old)
        await pool.cleanup_retired();old.close.assert_not_awaited()
        await pool.release(lease);old.close.assert_awaited_once()

    async def test_probe_requires_stable_ip_when_enabled(self):
        pool=make_pool(2,require_stable_exit=True)
        with patch('subpool.pool.probe_exit_ip',new=AsyncMock(side_effect=['8.8.8.8','1.1.1.1','9.9.9.9','9.9.9.9'])):
            self.assertEqual(len(await pool.probe(pool.members)),1)

    async def test_default_probe_accepts_dynamic_exit(self):
        pool=make_pool(1)
        with patch('subpool.pool.probe_exit_ip',new=AsyncMock(side_effect=['8.8.8.8','1.1.1.1'])):
            nodes=await pool.probe(pool.members)
        self.assertEqual(len(nodes),1)
        self.assertEqual(nodes[0].exit_ip,'1.1.1.1')
        self.assertTrue(nodes[0].dynamic_exit)
        self.assertEqual(pool.last_probe_stats['dynamic'],1)

    async def test_probe_deduplicates_actual_exit(self):
        pool=make_pool(3)
        with patch('subpool.pool.probe_exit_ip',new=AsyncMock(return_value='8.8.8.8')):
            self.assertEqual(len(await pool.probe(pool.members)),1)
            self.assertEqual(pool.last_probe_stats['duplicates_removed'],2)

    async def test_bad_refresh_preserves_current_pool(self):
        pool=make_pool();old=pool.generation
        with patch('subpool.pool.fetch_subscription',side_effect=ValueError('private token')):
            self.assertFalse(await pool.refresh())
        self.assertIs(pool.generation,old)
        self.assertEqual(pool.last_error,'ValueError')
        self.assertNotIn('token',json.dumps(pool.status()))

    async def test_single_exit_refresh_does_not_replace_pool_in_strict_mode(self):
        pool=make_pool(min_distinct_ips=2);old=pool.generation
        staged=fake_generation();staged.start=AsyncMock()
        with patch('subpool.pool.fetch_subscription',return_value=b'test'), \
             patch('subpool.pool.Generation',return_value=staged), \
             patch.object(pool,'probe',new=AsyncMock(return_value=[pool.members[0]])):
            self.assertFalse(await pool.refresh())
        self.assertIs(pool.generation,old);staged.close.assert_awaited_once()

    async def test_successful_refresh_drains_old_generation(self):
        pool=make_pool();lease=await pool.acquire();old=pool.generation
        staged=fake_generation();staged.start=AsyncMock()
        with patch('subpool.pool.fetch_subscription',return_value=b'test'), \
             patch('subpool.pool.Generation',return_value=staged), \
             patch.object(pool,'probe',new=AsyncMock(return_value=pool.members.copy())):
            self.assertTrue(await pool.refresh())
        self.assertIs(pool.generation,staged);old.close.assert_not_awaited()
        await pool.release(lease);old.close.assert_awaited_once()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool=make_pool(admin_token='test-admin-token')
        self.servers=Servers(self.pool)
        self.listener=await asyncio.start_server(self.servers.track(self.servers.socks),'127.0.0.1',0)
        self.port=self.listener.sockets[0].getsockname()[1]
        self.writers=[]

    async def asyncTearDown(self):
        for writer in self.writers:writer.close()
        self.listener.close();await self.listener.wait_closed();await self.servers.close()

    async def connect(self):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.port)
        self.writers.append(writer);return reader,writer

    async def test_noauth_handshake(self):
        reader,writer=await self.connect();writer.write(b'\x05\x01\x00');await writer.drain()
        self.assertEqual(await reader.readexactly(2),b'\x05\x00')

    async def test_no_supported_auth_method(self):
        reader,writer=await self.connect();writer.write(b'\x05\x01\x02');await writer.drain()
        self.assertEqual(await reader.readexactly(2),b'\x05\xff')

    async def test_auth_required_and_password_checked(self):
        self.servers.config=dataclasses.replace(self.servers.config,socks_username='u',socks_password='p')
        reader,writer=await self.connect();writer.write(b'\x05\x02\x00\x02');await writer.drain()
        self.assertEqual(await reader.readexactly(2),b'\x05\x02')
        writer.write(b'\x01\x01u\x01x');await writer.drain()
        self.assertEqual(await reader.readexactly(2),b'\x01\x01')

    async def test_auth_success(self):
        self.servers.config=dataclasses.replace(self.servers.config,socks_username='u',socks_password='p')
        reader,writer=await self.connect();writer.write(b'\x05\x01\x02');await writer.drain()
        await reader.readexactly(2);writer.write(b'\x01\x01u\x01p');await writer.drain()
        self.assertEqual(await reader.readexactly(2),b'\x01\x00')

    async def test_udp_rejected(self):
        reader,writer=await self.connect();writer.write(b'\x05\x01\x00');await writer.drain()
        await reader.readexactly(2);writer.write(b'\x05\x03\x00\x01');await writer.drain()
        self.assertEqual((await reader.readexactly(10))[1],7)

    async def test_unready_pool_never_connects_direct(self):
        self.pool.members=[]
        with patch('subpool.server.socks_connect',new=AsyncMock()) as connector:
            reader,writer=await self.connect();writer.write(b'\x05\x01\x00');await writer.drain()
            await reader.readexactly(2)
            writer.write(b'\x05\x01\x00'+address_bytes('api.invalid',443));await writer.drain()
            self.assertEqual((await reader.readexactly(10))[1],1)
            connector.assert_not_awaited()

    async def test_admin_requires_token(self):
        admin=await asyncio.start_server(self.servers.track(self.servers.admin),'127.0.0.1',0)
        try:
            reader,writer=await asyncio.open_connection('127.0.0.1',admin.sockets[0].getsockname()[1]);self.writers.append(writer)
            writer.write(b'GET /status HTTP/1.1\r\nHost: localhost\r\n\r\n');await writer.drain()
            self.assertIn(b'401 Unauthorized',await reader.readline())
        finally:admin.close();await admin.wait_closed()

    async def test_admin_authorized_status(self):
        admin=await asyncio.start_server(self.servers.track(self.servers.admin),'127.0.0.1',0)
        try:
            reader,writer=await asyncio.open_connection('127.0.0.1',admin.sockets[0].getsockname()[1]);self.writers.append(writer)
            writer.write(b'GET /status HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-admin-token\r\n\r\n');await writer.drain()
            raw=await reader.read();self.assertIn(b'200 OK',raw)
            self.assertNotIn(b'example.invalid',raw)
        finally:admin.close();await admin.wait_closed()
