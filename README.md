# Boss频道协作计时器

本仓库运行代码直接导出自正式服务 `https://mushroom-king-timer.online/`，采集于北京时间2026-10-24 21:32。对应版本 `20261023-snowman100`。

## 线上版本核对

```sh
python scripts/verify-server-source.py
```

`SERVER_SOURCE_MANIFEST.json` 保存每个导入文件的SHA-256和原服务器路径。运行时Python、JavaScript、HTML、CSS均与采集时的服务器逐字节一致；服务器dist静态文件亦与根目录对应文件一致。后续开发修改文件后，校验报告差异是正常现象；不要为了通过校验而覆盖新代码。

当前包括小白兔/红螃蟹区服及7个Boss。蘑菇王/僵尸蘑菇王38–60分钟，多尔/浮士德/艾利杰45分钟，驮狼雪人58–100分钟，国际服蘑菇王58–90分钟。账号、权限、击杀和频道状态属于数据库数据，不随代码导出。

## 笔记本本地启动

需要Python 3.12或更新版本。SQLite开发模式仅用标准库；连接MySQL时需安装 `requirements.txt`。Node.js仅用于复制前端构建文件，开发可直接使用根目录静态文件。

macOS / Linux：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
unset DATABASE_URL
export APP_ENV=development
export DB_ENGINE=sqlite
export DB_PATH=./timer.db
export HOST=127.0.0.1
export PORT=8000
export ADMIN_PASSWORD="$(python -c 'import getpass; print(getpass.getpass("本地root密码（至少16位）: "))')"
python server.py
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
$env:APP_ENV = 'development'
$env:DB_ENGINE = 'sqlite'
$env:DB_PATH = './timer.db'
$env:HOST = '127.0.0.1'
$env:PORT = '8000'
$localPassword = Read-Host '本地root密码（至少16位）' -AsSecureString
$env:ADMIN_PASSWORD = [System.Net.NetworkCredential]::new('', $localPassword).Password
.\.venv\Scripts\python.exe server.py
```

打开 `http://127.0.0.1:8000`，使用root及刚设置的本地密码登录。首次启动会建立独立本地数据库。已有数据库时，修改ADMIN_PASSWORD环境变量不会重置既有root密码。`.env.example` 是生产环境变量模板，程序不会自动加载.env文件。

## 验证与前端构建

```sh
python tests/run_faust_regressions.py test_snowman.SnowmanTests test_global_mushroom.GlobalMushroomTests
node --check app.js
node scripts/build-frontend.mjs
```

随仓库提供的是已验证的雪人/国际服20项回归及其API测试依赖，来自本地开发目录；它们使用临时SQLite数据库，不连接正式库。`tests/test_api.py`另含历史用例，本次未把这些旧用例全部作为当前版本的验收套件。

构建只是将4个前端文件复制到dist；dist不提交，笔记本可随时重新生成。

## 发布说明

GitHub提交不会自动部署。上线前核对服务器当前版本并保留备份；不要把本地SQLite或示例环境变量上传为正式数据。现网通过Nginx、MySQL和systemd服务/socket运行。archive中的首次部署文件源自旧服务器时代，不能直接用于当前线上服务。
