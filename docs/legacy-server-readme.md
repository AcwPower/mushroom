# 蘑菇王频道计时器

第一版是单台腾讯云新加坡 Ubuntu CVM 的**原生部署**：Nginx 发布构建后的前端并反向代理 API；Python 后端由 systemd 开机启动和崩溃重启；MySQL、实时 SSE 和超时回收任务都在同一台 CVM 上。**不使用 Docker，不依赖本地 Windows 持续运行。** Redis 目前不需要。现在仅生成项目和部署文件，尚未连接服务器。

## 本地开发与测试

本地可以用 Python 3.12 的 SQLite 模式：先设置至少 16 位的 `ADMIN_PASSWORD` 环境变量，再运行 `python server.py`，浏览器打开 `http://127.0.0.1:8000`。首次运行建立 60 条频道，管理员用户名为 `root`。测试：`python -m unittest discover -s tests -v`。前端构建：`node scripts/build-frontend.mjs`，构建产物为 `dist/`。生产模式强制 MySQL；代码中没有默认管理员密码。

## 在腾讯云新加坡 Ubuntu CVM 上从零部署（后续执行）

以下命令在 **CVM 的 Ubuntu 终端**执行，除非明确写了“本机”。有域名时把 `timer.example.com` 换成自己的域名。建议至少 2 GB 内存和足够的数据库/备份空间；目前已在 Ubuntu 26.04 的 1 GB CVM 上部署验证。先在腾讯云控制台将安全组入站限制为管理所需的 TCP 22、网站 TCP 80/443。SSH 最好限制为你的固定公网 IP；不要开放 3306 或 8000。无固定 IP 可使用腾讯云控制台登录服务器。

### 1. 安装系统软件

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs nginx mysql-server mysql-client certbot python3-certbot-nginx
sudo systemctl enable --now mysql nginx
```

在腾讯云安全组设置完成后，再启用 Ubuntu 防火墙；务必先允许 SSH，避免断开连接：

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

如果 SSH 使用自定义端口，第一行要改为实际端口。将项目代码从本机通过 Git 或 SCP 上传到 `/opt/mushroom-king-timer`。确保执行 `ls /opt/mushroom-king-timer/server.py` 能看到文件。创建专用服务用户并赋予项目读取权限：

```bash
sudo useradd --system --home /opt/mushroom-king-timer --shell /usr/sbin/nologin mushroom
sudo chown -R root:mushroom /opt/mushroom-king-timer
sudo chmod -R g+rX /opt/mushroom-king-timer
cd /opt/mushroom-king-timer
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo node scripts/build-frontend.mjs
sudo chown -R root:mushroom .venv dist
sudo chmod -R g+rX .venv dist
sudo chmod o+x /opt/mushroom-king-timer
sudo chmod -R a+rX dist
```

### 2. 配置只监听本机的 MySQL

```bash
sudo cp ops/mysql-local.cnf /etc/mysql/mysql.conf.d/99-mushroom-local.cnf
sudo systemctl restart mysql
sudo ss -lntp | grep 3306
```

`3306` 应显示 `127.0.0.1`，不应显示 `0.0.0.0` 或公网 IP。生成两个**不同**的强密码，分别供数据库和初始管理员使用：

```bash
openssl rand -base64 36
openssl rand -base64 36
sudo install -m 600 -o root -g root .env.example /etc/mushroom-king-timer.env
sudo nano /etc/mushroom-king-timer.env
```

在编辑器中设置 `APP_BASE_URL=https://你的域名`、`ADMIN_PASSWORD`、`DB_PASSWORD`。其他变量按实际情况调整。不要把真实密码提交到 Git。数据库连接来自 `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` 环境变量；服务地址来自 `HOST/PORT/APP_BASE_URL`。`DB_ENGINE=mysql` 必须保留。用 `sudo mysql` 进入 MySQL，下面两处密码必须与 `/etc/mushroom-king-timer.env` 中的 `DB_PASSWORD` 完全一致：

```sql
CREATE DATABASE mushroom_timer CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mushroom_app'@'127.0.0.1' IDENTIFIED BY '在此填入你的数据库密码';
GRANT ALL PRIVILEGES ON mushroom_timer.* TO 'mushroom_app'@'127.0.0.1';
FLUSH PRIVILEGES;
EXIT;
```

这个账户只能访问本机 MySQL 中的本项目数据库。初始化表和 60 条频道由后端首次启动自动完成。

### 3. 配置后端开机运行

```bash
sudo cp ops/mushroom-timer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mushroom-timer
sudo systemctl status mushroom-timer --no-pager
curl http://127.0.0.1:8000/api/health
```

最后一条应返回 `{"ok": true}`。如失败，查看 `sudo journalctl -u mushroom-timer -n 100 --no-pager`。后端只监听 `127.0.0.1:8000`；公网不能直接访问。

### 4. 配置 Nginx 和 HTTPS

```bash
sudo cp ops/nginx-site.conf /etc/nginx/sites-available/mushroom-timer
sudo sed -i 's/timer.example.com/你的域名/g' /etc/nginx/sites-available/mushroom-timer
sudo ln -s /etc/nginx/sites-available/mushroom-timer /etc/nginx/sites-enabled/mushroom-timer
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d 你的域名 --redirect
```

运行 `curl -I https://你的域名/` 和 `curl https://你的域名/api/health` 验证。Certbot 会配置 TLS 证书及自动续期。Nginx 只把 `/api/` 转发到本机后端；前端静态文件从 CVM 的 `dist/` 读取。

**暂时没有域名的 IP 部署：**当前 CVM 使用 `https://43.160.242.5/`。Let’s Encrypt 的 IP 证书为约六天短期证书，需 Certbot 5.4+；系统自带的旧 Certbot 不能直接完成此流程。项目的 `ops/nginx-ip-https.conf` 和 `ops/mushroom-cert-renew.timer` 用于 IP 证书及每日两次续期检查。首次部署可运行 `ops/deploy-first.sh`，再用新版本 Certbot 的 `certonly --preferred-profile shortlived --webroot --webroot-path /opt/mushroom-king-timer/dist --ip-address 你的IP` 签发证书，最后运行 `ops/enable-ip-https.sh`。须在腾讯云安全组同时放行 TCP 80 和 443；仅配置 Ubuntu UFW 不够。可用 `sudo /opt/mushroom-certbot/bin/certbot renew --dry-run` 检查续期。已在本次部署中完成签发与续期模拟测试。官方背景：[Let’s Encrypt IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

### 5. 配置每日自动备份

```bash
sudo install -d -m 700 -o mushroom -g mushroom /var/backups/mushroom-king-timer
sudo cp ops/mushroom-backup.service ops/mushroom-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mushroom-backup.timer
sudo systemctl start mushroom-backup.service
sudo systemctl status mushroom-backup.service --no-pager
sudo ls -lh /var/backups/mushroom-king-timer/
```

systemd 每天 03:15（CVM 本地时区）运行备份，错过时补跑，保留约 14 天。备份是压缩 SQL 文件；建议再把备份加密复制到另一处存储，避免 CVM 故障连备份一起丢失。定期用测试数据库演练恢复。**恢复会覆盖现有数据**，先另做一份当前备份并停止服务：

```bash
sudo systemctl stop mushroom-timer
sudo systemctl stop mushroom-backup.timer
sudo ls -lh /var/backups/mushroom-king-timer/
```

选择要恢复的 `mysql-日期.sql.gz`。在终端执行 `sudo mysql`，输入 `DROP DATABASE mushroom_timer; CREATE DATABASE mushroom_timer CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; EXIT;`。再执行以下命令；它临时读取环境变量，密码不写进命令行或仓库：

```bash
sudo bash -c 'set -a; source /etc/mushroom-king-timer.env; gzip -dc /var/backups/mushroom-king-timer/mysql-日期.sql.gz | MYSQL_PWD="$DB_PASSWORD" mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$DB_NAME"'
sudo systemctl start mushroom-timer mushroom-backup.timer
curl http://127.0.0.1:8000/api/health
```

### 6. 更新与检查

以后在 CVM 更新代码后执行：

```bash
cd /opt/mushroom-king-timer
sudo .venv/bin/pip install -r requirements.txt
sudo node scripts/build-frontend.mjs
sudo chown -R root:mushroom dist
sudo chmod -R g+rX dist
sudo chmod -R a+rX dist
sudo systemctl restart mushroom-timer
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl status mushroom-timer mushroom-backup.timer --no-pager
```

不要运行多个后端副本；当前实时事件通知位于单个进程内。MySQL 仅绑定本机，Redis 不需要。会员日卡/周卡目前由管理员测试激活，未接真实支付。当前关闭自助注册与短信验证码；管理员可通过页面上的“创建成员”或 `/api/admin/create-user` 创建成员并给予 1 天或 7 天测试使用期。待短信服务资质完成后再设计并上线手机号验证。普通成员主动释放频道后，第 1 次冷却 10 分钟、每次增加 10 分钟、最多 60 分钟；冷却期间不能重新认领或接受 SOS 接管，但仍可登录查看；管理员释放任意频道不触发惩罚。认领超时的 5 分钟限制是另一条规则。推荐优先选择其他成员击杀且无人认领的已知频道；若没有，则回退到自己最近击杀且无人认领的频道。推荐卡片倒计时指向第 38 分钟进刷时间，认领后指向第 60 分钟最终保底时间。“未知频道”只列出未记录击杀且未被认领的频道，按编号升序完整展示。登录采用 HttpOnly/Secure/SameSite Cookie；普通成员权限由服务端接口执行，不靠前端隐藏。
