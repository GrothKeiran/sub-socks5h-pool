from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys
from dataclasses import replace

from .config import Config
from .pool import Pool
from .server import Servers


async def run(config: Config):
    import fcntl
    os.umask(0o077)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.data_dir.chmod(0o700)
    (config.data_dir / 'tmp').mkdir(exist_ok=True, mode=0o700)
    lock = (config.data_dir / 'service.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('此数据目录已有运行中的实例') from None
    # 配置未给管理令牌时生成并保存；不输出到日志。
    if not config.admin_token:
        path = config.data_dir / 'admin.token'
        token = path.read_text().strip() if path.exists() else secrets.token_urlsafe(32)
        path.write_text(token + '\n')
        path.chmod(0o600)
        config = replace(config, admin_token=token)
    if not config.core_path.is_file() or not os.access(config.core_path, os.X_OK):
        raise ValueError('未找到可执行的 Mihomo；请先运行 python3 tools/install_core.py')
    pool = Pool(config)
    servers = Servers(pool)
    tasks = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await servers.start()
        logging.info('SOCKS5 入口 %s:%d；管理接口 %s:%d；未就绪时拒绝转发',
                     config.socks_host, config.socks_port, config.admin_host, config.admin_port)
        tasks = [asyncio.create_task(pool.refresh_loop()), asyncio.create_task(pool.health_loop())]
        await stop.wait()
    finally:
        pool.closing = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await servers.close()
        await pool.close()
        lock.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        asyncio.run(run(Config.from_env()))
    except (ValueError, OSError) as exc:
        # 只打印程序自身的受控配置错误，不输出第三方网络异常详情。
        if isinstance(exc, ValueError):
            logging.error('%s', exc)
        else:
            logging.error('启动失败：%s', type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
