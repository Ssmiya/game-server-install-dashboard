# GameDeck

单台 Linux 服务器上的多游戏服务端 Dashboard。第一版预置：

- 幻兽帕鲁 Dedicated Server
- Minecraft Java Server

## 本地预览

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

访问 `http://127.0.0.1:5000`。

默认运行在安全演示模式，安装、更新、启停操作会展示完整流程，但不会执行系统命令。

## Linux 正式安装

支持 Ubuntu、Debian、CentOS Stream、Rocky Linux 和 AlmaLinux：

```bash
sudo \
  SERVER_IP="你的公网IP" \
  DASHBOARD_PORT=8443 \
  bash deploy/install.sh
```

安装器会创建：

- `gameserver` 非 root 运行账户
- `/srv/games/palworld` 与 `/srv/games/minecraft`
- SteamCMD、Java、Python、Gunicorn、Nginx
- Dashboard、Palworld、Minecraft systemd 服务
- 限定到两个游戏服务的 sudo 权限
- 密码验证、CSRF 防护与 HTTPS 自签名证书

正式模式下不会预装任何游戏。进入 Dashboard 后按需点击“开始安装”；首次安装 Minecraft 时在页面确认官方 EULA。

## 更新已有服务器

将新版项目覆盖上传到一个临时目录，然后从新版项目根目录重新运行：

```bash
sudo \
  SERVER_IP="你的公网IP" \
  DASHBOARD_PORT=8443 \
  bash deploy/install.sh
```

游戏文件、存档、配置和备份位于 `/srv/games`，不会被应用更新覆盖。

## 生产数据来源

- systemd：运行状态、启停和主进程
- psutil：CPU、内存和运行时间
- Palworld REST API：在线玩家；需要在配置中启用
- Minecraft Server List Ping：在线玩家
- Steam appmanifest / SteamCMD：帕鲁版本
- Mojang 官方版本清单：Minecraft 版本、下载地址和 SHA-1

不要把 Palworld REST、RCON、Minecraft RCON、Gunicorn 8000 或 Flask 5000 直接开放到公网。
