# 项目协作约定

- 用户已于2026-10-24授权用GitHub同步代码，供笔记本开发；本仓库初始运行代码来自正式服务器，而非旧本地目录。
- 阅读README.md和SERVER_SOURCE_MANIFEST.json。运行 `python scripts/verify-server-source.py` 可校验导入时的服务器字节基线。
- 多台电脑都可能更新服务器；修改或发布前同时核对GitHub最新提交和服务器当前哈希，避免覆盖别处的新改动。
- 正式目录为 `/opt/mushroom-king-timer`，正式服务为 `mushroom-timer.service`，socket为 `mushroom-timer.socket`；预发目录和服务分别为 `/opt/mushroom-king-timer-pre`、`mushroom-pre.service`，使用独立数据库。
- 日常开发使用本地SQLite或独立测试库，不能复制正式数据库或在正式环境制造测试击杀。用户要求不压测。
- 发布须有授权、备份及基线校验；保留socket，只重启应用服务。线上数据、账号和权限不是代码的一部分。
- `archive/legacy-ops` 与 `docs/legacy-server-readme.md` 是服务器遗留文件，保留仅为溯源，不能直接当作当前部署脚本执行。
- 不提交环境文件、数据库、密码、登录Cookie、密钥或历史数据备份。
