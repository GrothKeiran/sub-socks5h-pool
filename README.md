# Sub SOCKS5h Pool

[![Docker 镜像构建与发布](https://github.com/GrothKeiran/sub-socks5h-pool/actions/workflows/docker.yml/badge.svg)](https://github.com/GrothKeiran/sub-socks5h-pool/actions/workflows/docker.yml)

把 V2Ray / Clash 订阅变成一个固定的 **SOCKS5h 轮询代理入口**，适用于希望每次 AI API
请求尽量更换出口的客户端。API 地址和密钥不变，不需要 HTTP 反向网关。

```text
AI 客户端（每次请求新建 TCP 连接）
        │ socks5h://用户名:密码@127.0.0.1:1080
        ▼
全局轮询网关：优先选择不同的已探测出口
        ▼
Mihomo：VLESS / VMess / Trojan / Shadowsocks 等协议
        ▼
订阅节点的公网出口 → AI API
```

## 轮换策略：尽力换 IP，允许重复

- 每个新的 SOCKS5 CONNECT 轮询一个出口，优先避开上次已探测的 IP。
- 多个节点同 IP 时保留本轮探测延迟较低者；去重依据真实公网回显，不是节点名称。
- **默认只剩一个可用 IP 时仍继续服务**，不会因为不能换 IP 而中断请求。
- 默认接纳会动态换出口的节点；双次探测不同 IP 只标记为动态节点，不因此剔除。
- 没有健康节点、内核退出或订阅超过最大陈旧期限时拒绝新连接，绝不回退到本机直连。
- 有限池会循环使用 IP；不保证每次实际 IP 不同，也不保证回显出口与目标 API 所见出口一致。
- SSE / 流式响应从开始到结束固定使用本次连接，不在中途强制断开换 IP。
- 并发轮询按**代理分配顺序**计算，不保证目标服务器收到请求或返回响应的顺序。

**SOCKS5 不能识别 TLS 长连接里面的每次 HTTP 请求。**
客户端必须关闭连接复用和 HTTP/2 多路复用，才能让每次 API 请求触发一次新的代理分配。
`socks5h` 的 `h` 表示客户端把域名交给代理处理；服务端仍是标准 SOCKS5。

## Docker 镜像

```text
ghcr.io/grothkeiran/sub-socks5h-pool:latest
```

支持 `linux/amd64`、`linux/arm64`。推送 `main` 后工作流自动完成：

1. 两种架构分别在原生 GitHub runner 构建镜像。
2. 在每个镜像内运行离线单元测试和真实 Mihomo 本地集成测试。
3. 通过测试后推送架构镜像，最后合并为多架构清单。
4. 发布 `latest` 和 `sha-<提交前12位>`；推送 `v*` 标签时额外发布版本标签。
5. PR 只构建和测试，不登录注册表、不推送镜像。

Actions 固定到完整提交 SHA，发布只使用 GitHub 临时 `GITHUB_TOKEN`。
工作流**不需要也不会读取真实订阅**。构建证明与 SBOM 随镜像一起发布。

> 首次发布的 GHCR 包可能默认为 Private。仓库所有者需在包页面的
> **Package settings → Change visibility → Public** 设置公开，才能匿名拉取。
> 公开 Git 仓库不等于 GHCR 包自动公开；没有公开前需使用具备 `read:packages` 的凭据登录。

## 快速启动

```bash
git clone https://github.com/GrothKeiran/sub-socks5h-pool.git
cd sub-socks5h-pool
cp .env.example .env
# 编辑 .env，填写自己的 SUBSCRIPTION_URL。
python3 tools/init_env.py
# 生成的 SOCKS 密码和管理令牌只保存在本地 .env，不会打印。
docker compose pull
docker compose up -d
docker compose logs -f
```

默认仅映射宿主机 `127.0.0.1:1080` 和 `127.0.0.1:8090`；容器内非回环监听强制认证。
代理地址中的用户名、密码从本地 `.env` 读取：

```text
socks5h://用户名:URL编码后的密码@127.0.0.1:1080
```

初始化会记录宿主 UID/GID，绑定的 `./data` 目录归当前用户所有，容器不需要 root 权限。
建议通过 VPN / SSH 隧道使用，不要直接把未受保护的代理暴露到公网。

### 本地构建镜像

```bash
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

### 不使用 Docker

Linux + Python **3.11+**；服务端仅依赖标准库：

```bash
cp .env.example .env
# 编辑 .env，填写订阅地址。已有文件不要覆盖。
python3 tools/install_core.py
./run.sh
```

自动安装固定 Mihomo **v1.19.32**，支持 Linux amd64 / arm64，并校验固定 SHA-256。
节点协议、URI/Base64 与 Clash `proxies:` 格式的兼容性以该内核为准。
订阅在本地解析，不发送给第三方转换站。

## AI 客户端：关闭连接复用

以下是 HTTPX 配置，不需要修改 AI API 的 base URL：

```python
import httpx

client = httpx.Client(
    proxy="socks5h://用户名:密码@127.0.0.1:1080",
    limits=httpx.Limits(max_keepalive_connections=0, max_connections=20),
    http2=False,
    follow_redirects=False,
    timeout=httpx.Timeout(300, connect=30, pool=30),
)
# 交给支持自定义 HTTP 客户端的 AI SDK；异步 SDK 使用 httpx.AsyncClient。
# 结束后调用 client.close() / await client.aclose()。
```

示例使用 HTTPX **0.28.1** 的 SOCKS 扩展：

```bash
python3 -m venv .venv
PIP_CACHE_DIR="$PWD/.cache/pip" .venv/bin/python -m pip install -r examples/requirements.txt
```

完整的通用流式示例在 [`examples/ai_request.py`](examples/ai_request.py)，读取
`AI_API_URL`（完整 URL）、`AI_API_KEY`、`AI_MODEL`、可选 `POOL_PROXY`。
不同服务商的请求路径、鉴权和字段需自行适配。

注意：

- 只设置代理地址或 `ALL_PROXY` 不代表 SDK 支持 SOCKS，也不代表已禁用连接池。
- 应用如果没有禁用连接复用的设置，就无法保证逐 API 请求轮换。
- SDK 自动重试可能把一次调用变成多次请求；涉及计费、非幂等请求时请谨慎。
- 不要关闭 TLS 证书验证，不需要安装订阅节点提供的证书。
- 本项目不解密 HTTPS、看不到 401 / 403 / 429，也不根据这些状态重发 POST。
- 回显可用不等于 AI API 可用；账号、地域、权限和服务商规则仍然适用。

## 配置

完整模板见 [`.env.example`](.env.example)。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SUBSCRIPTION_URL` | 必填 | HTTPS 订阅；仅回环测试地址允许 HTTP |
| `REFRESH_SECONDS` | `7200` | 更新订阅的间隔；失败后最多每分钟重试 |
| `HEALTH_SECONDS` | `300` | 重测全部候选节点，失败节点有机会恢复 |
| `MAX_STALE_SECONDS` | `10800` | 距上次成功加载订阅超过此时间，拒绝新连接 |
| `MIN_DISTINCT_IPS` | `1` | 默认允许单出口；设为 `2` 可恢复双出口门槛 |
| `REQUIRE_STABLE_EXIT` | `false` | 默认允许动态 IP；`true` 要求双次探测一致 |
| `MAX_NODES` | `256` | 处理订阅前 N 个合格节点，硬上限 1024 |
| `PROBE_CONCURRENCY` | `8` | 出口探测并发，硬上限 64 |
| `PROBE_TIMEOUT` | `12` | 单次回显超时，秒 |
| `PROBE_URL` | `https://api4.ipify.org` | 纯文本公网 IP 或 `{"ip":"公网IP"}` |
| `FAILURE_THRESHOLD` | `2` | 连续建连失败达到门槛后暂时摘除 |
| `CONNECT_TIMEOUT` | `20` | 建立上游连接超时，秒 |
| `RELAY_IDLE_TIMEOUT` | `300` | 单方向无数据超时；超长推理可调大 |
| `SOCKS_USERNAME` / `SOCKS_PASSWORD` | 本地可为空 | 非回环监听必须同时设置 |
| `ADMIN_TOKEN` | 本地可自动生成 | 容器部署通过初始化脚本生成 |

每个节点两次独立请求 IP 回显。公共回显可能限流；较大代理池建议使用自有的稳定回显服务。
新订阅先在新一代内核中验证，合格才替换；旧连接在结束前仍由旧内核承载。
建连失败也可能来自目标地址本身，不一定代表节点永久失效。

## 管理接口

```bash
curl -fsS http://127.0.0.1:8090/healthz
python3 tools/status.py
```

- `GET /healthz`：无需令牌，仅返回就绪状态；不满足健康出口门槛时为 503。
- `GET /status`：需要 `Authorization: Bearer <ADMIN_TOKEN>`；显示匿名节点编号、最近已探测
  IP、动态节点标记、探测统计、订阅更新时间、活动连接数和单 IP 降级状态。
- `POST /refresh`：同样需要令牌；异步刷新返回 202，已有更新进行时返回 409。

本地未设置管理令牌时生成 `data/admin.token`（0600）；显式配置时使用 `.env` 的值。
状态工具自动读取令牌，不把令牌放进命令行参数。

## 测试与联网检查

```bash
# 离线测试，包括真实 Mihomo + 本地假上游。
python3 tools/install_core.py
./test.sh

# 可选：真实订阅和公网回显。不调用任何付费 AI API。
TMPDIR="$PWD/.cache/tmp" XDG_CACHE_HOME="$PWD/.cache" \
PYTHONPYCACHEPREFIX="$PWD/.cache/pycache" python3 tools/live_check.py
```

测试缓存、Python 字节码、临时目录都放在项目内。未安装内核时，集成测试明确跳过；
CI 则在已内置内核的镜像内执行，不跳过。

联网检查输出 12 次独立连接的成功率、不同 IP 数和相邻重复次数；**允许 IP 重复**，
返回状态只判断是否完成全部连接。`actual_exit_rotation_passed` 仍单独记录严格不重复的样本结果，
不把“尽力轮换可用”误写成“严格换 IP 通过”。检查使用随机回环端口，结束后清理子进程。

## 隐私、安全和局限

- `.env`、原始订阅、节点凭据、缓存、内核二进制、`data/` 与整个 `artifacts/` 都不会提交，
  也不进入 Docker 构建上下文。公开仓库及工作流只包含占位配置和本地假节点。
- 临时订阅和内核配置含节点凭据，位于权限 0700 的代际目录，正常退出时删除；未加密存储。
- 日志不打印订阅 URL、节点真实名称、密码或第三方异常详情。
- HTTPS 订阅必须直接返回内容；为防止令牌随跳转泄漏，默认拒绝重定向。
- 仅支持 TCP 的 SOCKS5 CONNECT，不支持 UDP / QUIC。
- 不提供分布式游标；多个实例不自动共享轮询顺序。
- 换出口、重做 TLS 握手会增加延迟，也可能影响服务商风控。
- 请遵守订阅提供方和目标服务的使用条款，不用于绕过访问授权。

## 许可证与第三方组件

项目以 [GPL-3.0](LICENSE) 发布，内核及客户端示例依赖见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。镜像附带许可证、上游固定版本源码链接，
未把第三方内核重新声称为自有代码；自行再分发时请遵守对应许可证。

参考：[Mihomo](https://github.com/MetaCubeX/mihomo)、
[Provider](https://wiki.metacubex.one/config/proxy-providers/)、
[HTTPX SOCKS](https://www.python-httpx.org/advanced/proxies/#socks)、
[HTTPX 连接限制](https://www.python-httpx.org/advanced/resource-limits/)。
