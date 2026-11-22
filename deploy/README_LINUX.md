# PM-Quant Ubuntu 7×24 生产部署指南

面向低配海外 Linux 云服务器（Ubuntu 22.04 / 24.04，2C / 1G~2G 内存 / 30G 硬盘）。

## 1. 拉取代码并创建虚拟环境

```bash
sudo mkdir -p /opt/pm-quant && sudo chown "$USER:$USER" /opt/pm-quant
cd /opt/pm-quant
# 上传或 git clone 项目到此目录后：
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py test
```

## 2. 执行主机安全与防 OOM / 防磁盘满初始化

```bash
chmod +x deploy/setup_ubuntu.sh
APP_DIR=/opt/pm-quant ./deploy/setup_ubuntu.sh
```

脚本会自动完成：
- 创建并挂载 **2GB Swap**（防止 1G~2G 机器内存峰值 OOM）；
- 限制 `systemd-journald` 最大占用 **200MB**（避免 stdout 日志长期写爆 30G 硬盘）；
- 检查并启用 NTP 时钟同步（推荐安装 `chrony` 避免时钟步进回拨）；
- 生成 `/etc/pm-quant.env`（`chmod 600`），含强密码、手机告警（Telegram / Bark / 飞书 / 钉钉 Webhook）与 `rclone` 异地备份配置项；
- 安装跨版本自动匹配的错峰备份定时任务（UTC `00:17`，避开 5 分钟整点结算边界，保留 30 天并支持 `rclone` 异地同步）。

## 3. 设置密码与告警并安装 systemd 守护服务（推荐双进程隔离）

```bash
sudo nano /etc/pm-quant.env   # 必填：将 PM_WEB_PASS 改为 >=12 位强密码；可选配置手机告警
```

**推荐：交易引擎与 Web 看板双进程隔离部署**（Web 内存波动或重启绝不影响交易与持仓）：

```bash
sudo cp deploy/pm-quant-engine.service /etc/systemd/system/
sudo cp deploy/pm-quant-web.service /etc/systemd/system/
# 如服务器用户名不是 ubuntu，请编辑两个 service 文件中的 User= / Group=：
sudo systemctl daemon-reload
sudo systemctl enable --now pm-quant-engine pm-quant-web
sudo systemctl status pm-quant-engine pm-quant-web
```

（若只需单进程部署，也可使用 `deploy/pm-quant.service`。）

常用运维命令：
```bash
journalctl -u pm-quant-engine -f -n 100   # 实时查看交易引擎日志
journalctl -u pm-quant-web -f -n 100      # 实时查看 Web 鉴权与看板日志
sudo systemctl restart pm-quant-web       # 仅重启看板，零中断交易
sudo systemctl restart pm-quant-engine    # 平滑重启引擎（同窗自动恢复、跨窗自动补结算）
```

## 4. 公网 HTTPS 安全访问（推荐 Caddy 反代）

**请勿直接把 8900 端口暴露到公网明文 HTTP**。保持 `PM_WEB_HOST=127.0.0.1`，在云安全组 / UFW 仅开放 `22`、`80` 和 `443` 端口，用 Caddy 反代提供自动 HTTPS 与慢连接吸收：

```bash
sudo apt update && sudo apt install -y caddy
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile            # 将 quant.example.com 替换为你的域名
sudo systemctl reload caddy
```

## 5. 外部真探活监控（可选强烈推荐）

在 UptimeRobot / BetterStack / Healthchecks.io 添加免登录监控端点：
- URL：`https://<你的域名>/healthz`
- 该端点不返回任何资金、持仓或策略信息，仅校验交易引擎 `live.json` 心跳是否 `<= 60s`；心跳停滞或引擎挂掉立即返回 `HTTP 503` 触发手机告警。
