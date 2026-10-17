#!/usr/bin/env python3
"""显式联网验收：读取私密 .env，探测真实节点，不调用 AI API，不输出凭据。"""
import asyncio
from dataclasses import replace
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from subpool.config import Config
from subpool.core import Generation
from subpool.net import fetch_subscription, probe_exit_ip, reserve_ports
from subpool.pool import Pool
from subpool.server import Servers


async def main():
    config = Config.from_env()
    ports = reserve_ports(2)
    # 验收使用随机回环端口，不打断已运行的正式服务。
    config = replace(config, socks_host='127.0.0.1', socks_port=ports[0],
                     admin_host='127.0.0.1', admin_port=ports[1],
                     socks_username='', socks_password='')
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (config.data_dir / 'tmp').mkdir(exist_ok=True)
    pool = Pool(config)
    servers = Servers(pool)
    generation = Generation(config)
    start = time.monotonic()
    try:
        raw = await asyncio.to_thread(fetch_subscription, config.subscription_url)
        await generation.start(raw)
        unique = await pool.probe(generation.candidates)
        frontdoor = []
        expected_ips = []
        samples = []
        errors = []
        if len(unique) >= config.min_distinct_ips:
            generation.loaded_at = time.monotonic()
            pool.generation = generation
            pool.members = unique
            await servers.start()
            for _ in range(12):
                try:
                    actual = await probe_exit_ip(config.socks_port, config.probe_url, config.probe_timeout + 5)
                    expected_ips.append(pool.last_ip)
                    frontdoor.append(actual)
                    samples.append({'allocation': len(samples)+1, 'expected_observed_ip': pool.last_ip,
                                    'actual_echo_ip': actual, 'matches_last_probe': actual == pool.last_ip})
                except Exception as exc:
                    errors.append(type(exc).__name__)
        result = {'core_started': True, 'tested_subscription_nodes': len(generation.candidates),
                  'probe': pool.last_probe_stats, 'pool_ready': len(unique) >= config.min_distinct_ips,
                  'frontdoor': {'attempts': 12 if len(unique) >= config.min_distinct_ips else 0,
                                'successes': len(frontdoor),
                                'distinct_observed_ips': len(set(frontdoor)),
                                'adjacent_duplicate_pairs': sum(a == b for a,b in zip(frontdoor,frontdoor[1:])),
                                'error_types': errors,
                                'differs_from_admitted_ip': sum(a != b for a,b in zip(frontdoor,expected_ips)),
                                'samples': samples},
                  'best_effort_connectivity_passed': bool(len(frontdoor) == 12 and not errors),
                  'actual_exit_rotation_passed': bool(len(frontdoor) == 12 and not errors and
                      all(a != b for a,b in zip(frontdoor,frontdoor[1:]))),
                  'elapsed_seconds': round(time.monotonic() - start, 1)}
    except Exception as exc:
        result = {'pool_ready': False, 'error_type': type(exc).__name__}
    finally:
        await servers.close()
        await pool.close()
        await generation.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    out = ROOT / 'artifacts/live-check.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    historical = out.with_name('live-check-' + str(time.time_ns()) + '.json')
    historical.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return 0 if result.get('best_effort_connectivity_passed') else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
