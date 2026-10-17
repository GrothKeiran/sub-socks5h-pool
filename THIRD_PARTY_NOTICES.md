# 第三方组件说明

## Mihomo

- 项目：https://github.com/MetaCubeX/mihomo
- 本项目固定版本：v1.19.32
- 对应源码：https://github.com/MetaCubeX/mihomo/tree/v1.19.32
- 上游许可证：GPL-3.0
- 许可证原文：https://github.com/MetaCubeX/mihomo/blob/v1.19.32/LICENSE

本项目通过独立子进程调用 Mihomo，不把它改名或重新声称为自有代码。
安装脚本从官方发布下载二进制并校验 SHA-256；二进制不作为源码仓库的提交内容。
如果自行分发包含内核的镜像或软件包，请按上游 GPL 许可证提供所需许可证和对应源码等材料。

## HTTPX（仅客户端示例）

- 项目：https://github.com/encode/httpx
- 示例固定版本：0.28.1
- 许可证：BSD-3-Clause；SOCKS 扩展还包含自己的传递依赖与许可证。

代理池服务本身不依赖 HTTPX。
