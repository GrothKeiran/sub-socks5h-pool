from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .config import Config
from .core import Candidate, Generation
from .net import fetch_subscription, probe_exit_ip

log = logging.getLogger('subpool')


class PoolUnavailable(Exception):
    pass


@dataclass
class Lease:
    generation: Generation
    candidate: Candidate


def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    by_ip = {}
    for candidate in candidates:
        if not candidate.exit_ip:
            continue
        current = by_ip.get(candidate.exit_ip)
        if current is None or candidate.latency_ms < current.latency_ms:
            by_ip[candidate.exit_ip] = candidate
    return sorted(by_ip.values(), key=lambda n: n.alias)


class Pool:
    def __init__(self, config: Config):
        self.config = config
        self.generation = None
        self.retired = set()
        self.members: list[Candidate] = []
        self.lock = asyncio.Lock()
        self.update_lock = asyncio.Lock()
        self.index = 0
        self.last_ip = None
        self.selected_connections = 0
        self.failed_connections = 0
        self.last_update = None
        self.last_probe = None
        self.last_probe_stats = None
        self.last_error = None
        self.last_rejected_refresh = None
        self.closing = False

    def ready(self):
        gen = self.generation
        return bool(not self.closing and gen and not gen.closed and gen.process
                    and gen.process.returncode is None and len(self.members) >= self.config.min_distinct_ips
                    and time.monotonic() - gen.loaded_at <= self.config.max_stale_seconds)

    async def probe(self, candidates: list[Candidate]) -> list[Candidate]:
        semaphore = asyncio.Semaphore(self.config.probe_concurrency)

        async def check(candidate):
            async with semaphore:
                start = time.monotonic()
                try:
                    first = await probe_exit_ip(candidate.port, self.config.probe_url,
                                                self.config.probe_timeout)
                    second = await probe_exit_ip(candidate.port, self.config.probe_url,
                                                 self.config.probe_timeout)
                    if first != second and self.config.require_stable_exit:
                        return None
                    return Candidate(candidate.alias, candidate.port, second,
                                     (time.monotonic() - start) * 500,
                                     dynamic_exit=first != second)
                except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError,
                        asyncio.LimitOverrunError, UnicodeError, IndexError):
                    return None
        checked = await asyncio.gather(*(check(n) for n in candidates))
        stable = [n for n in checked if n is not None]
        unique = deduplicate(stable)
        self.last_probe_stats = {'tested': len(candidates), 'reachable': len(stable),
                                 'stable': sum(not n.dynamic_exit for n in stable),
                                 'dynamic': sum(n.dynamic_exit for n in stable),
                                 'distinct_ips': len(unique), 'duplicates_removed': len(stable)-len(unique)}
        return unique

    async def refresh(self) -> bool:
        async with self.update_lock:
            if self.closing:
                return False
            staged = None
            try:
                raw = await asyncio.to_thread(fetch_subscription, self.config.subscription_url)
                staged = Generation(self.config)
                await staged.start(raw)
                unique = await self.probe(staged.candidates)
                self.last_probe = time.time()
                if len(unique) < self.config.min_distinct_ips:
                    self.last_rejected_refresh = {'observed_distinct_ips': len(unique),
                                                  'at': time.time()}
                    raise PoolUnavailable('独立出口数量未达到配置的最小值')
                staged.loaded_at = time.monotonic()
                async with self.lock:
                    old = self.generation
                    self.generation = staged
                    self.members = unique
                    self.index = 0
                    self.last_update = time.time()
                    self.last_error = None
                    self.last_rejected_refresh = None
                    if old:
                        old.retired = True
                        self.retired.add(old)
                staged = None
                await self.cleanup_retired()
                log.info('订阅更新成功：%d 个已验证独立出口', len(unique))
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 绝不输出异常字符串，它们可能包含订阅 token、节点密码或 URL。
                self.last_error = type(exc).__name__
                log.warning('订阅更新失败（%s），不替换现有池', self.last_error)
                return False
            finally:
                if staged:
                    await staged.close()

    async def health_check(self):
        async with self.update_lock:
            gen = self.generation
            if not gen or self.closing:
                return
            if not gen.process or gen.process.returncode is not None:
                async with self.lock:
                    self.members = []
                    self.last_error = 'CoreExited'
                return
            checked = await self.probe(gen.candidates)
            async with self.lock:
                self.members = checked
                self.index = 0
                self.last_probe = time.time()
            log.info('健康检查完成：%d 个已验证独立出口', len(checked))

    async def acquire(self) -> Lease:
        async with self.lock:
            if not self.ready():
                raise PoolUnavailable('没有足够的未过期健康出口')
            # 全局锁使并发客户端也按分配顺序轮询，并跨订阅更新避开上次 IP。
            for _ in range(len(self.members)):
                candidate = self.members[self.index % len(self.members)]
                self.index = (self.index + 1) % len(self.members)
                if candidate.exit_ip != self.last_ip or len(self.members) == 1:
                    self.last_ip = candidate.exit_ip
                    self.generation.active_connections += 1
                    self.selected_connections += 1
                    return Lease(self.generation, candidate)
            raise PoolUnavailable('没有不同于上次的出口')

    async def release(self, lease: Lease):
        async with self.lock:
            lease.generation.active_connections -= 1
        await self.cleanup_retired()

    async def mark_failure(self, lease: Lease):
        async with self.lock:
            self.failed_connections += 1
            if lease.generation is not self.generation:
                return
            for candidate in self.members:
                if candidate.port == lease.candidate.port:
                    candidate.failures += 1
                    if candidate.failures >= self.config.failure_threshold:
                        self.members.remove(candidate)
                    break

    async def mark_success(self, lease: Lease):
        async with self.lock:
            if lease.generation is self.generation:
                for candidate in self.members:
                    if candidate.port == lease.candidate.port:
                        candidate.failures = 0
                        break

    async def cleanup_retired(self):
        async with self.lock:
            done = [g for g in self.retired if g.active_connections == 0]
            self.retired.difference_update(done)
        for gen in done:
            await gen.close()

    def status(self):
        return {
            'ready': self.ready(), 'rotation_unit': 'socks5_tcp_connection',
            'distinct_observed_ips': len(self.members),
            'min_distinct_ips': self.config.min_distinct_ips,
            'require_stable_exit': self.config.require_stable_exit,
            'rotation_policy': 'best_effort',
            'single_ip_fallback': len(self.members) == 1,
            'subscription_nodes': len(self.generation.candidates) if self.generation else 0,
            'refresh_seconds': self.config.refresh_seconds,
            'health_seconds': self.config.health_seconds,
            'max_stale_seconds': self.config.max_stale_seconds,
            'last_subscription_success': self.last_update, 'last_probe': self.last_probe,
            'last_error_type': self.last_error, 'last_probe_stats': self.last_probe_stats, 'last_rejected_refresh': self.last_rejected_refresh,
            'refresh_or_probe_running': self.update_lock.locked(),
            'selected_connections': self.selected_connections,
            'failed_connections': self.failed_connections,
            'active_connections': sum(g.active_connections for g in
                                      ([self.generation] if self.generation else []) + list(self.retired)),
            'nodes': [{'alias': n.alias, 'observed_exit_ip': n.exit_ip,
                       'latency_ms': round(n.latency_ms, 1), 'dynamic_exit': n.dynamic_exit} for n in self.members],
            'guarantee': '优先分配不同的已探测出口；只剩一个出口时允许复用，不保证每次实际 IP 不同',
        }

    async def refresh_loop(self):
        while not self.closing:
            ok = await self.refresh()
            await asyncio.sleep(self.config.refresh_seconds if ok else min(60, self.config.refresh_seconds))

    async def health_loop(self):
        while not self.closing:
            await asyncio.sleep(self.config.health_seconds)
            try:
                await self.health_check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning('健康检查失败（%s）', self.last_error)

    async def close(self):
        self.closing = True
        generations = list(self.retired)
        if self.generation:
            generations.append(self.generation)
        for gen in generations:
            await gen.close()
