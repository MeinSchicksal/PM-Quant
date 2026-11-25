# PM-Quant (`v3.2`)

面向 **Polymarket BTC 5 分钟 Up/Down 二元预测市场**的自动化纸面（Paper）量化研究与监控系统。当前版本仅包含纸面仿真执行器，不含实盘下单适配器，所有交易记录均基于真实实时行情或录制历史事件流的拟真撮合结果。

## 1. 核心设计目标

1. **真实性优先（零自欺欺人）**：撮合与记账严格对齐 Polymarket 官方规则——最小下单量 5 股、按真实 RTT 动态建模的网络+引擎延迟（无 600ms 人工削峰）、下单前基于可见 L2 深度精确计算原生 IOC 限价（彻底移除到达后事后拒单/闪崩反悔）、毫秒级时钟校验与 `signal_minus_venue_ms` 相对延迟差监测、以及每窗独立记录 **Brier Score（模型 `P*` vs 盘口 `mid`）** 基准评估。
2. **单一内核**：回放（`sim/replay.py`）与实时纸面（`runtime/pipeline.py`）共用同一套策略决策与执行状态机（`core/` 与 `runtime/runner.py`）。
3. **版本与账本隔离**：所有策略参数与版本身份统一由 `config.py` 定义，账本按版本隔离存储在 `data/2_ledgers/<VERSION>/`。
4. **轻量 7×24 运行**：极简依赖（仅 `requests` 与 `websockets`），面向低配 Linux 服务器（2C / 1G~2G RAM / 30G 硬盘）长期无人值守运行，自带 Web 监控看板、数据自动保留清理与可选手机告警。

## 2. 策略概览（`v3.2`：概率控仓多买引擎）

- **定价模型（`core/estimator.py`）**：基于实时现货（当前默认源 Coinbase BTC/USD，实测验证中）、跨链基差 EMA 校准与最后 60 秒已锁定均值，采用胖尾 Student-t（`df=5`）分布估计到期 Chainlink 60s TWAP 结算胜率 `P*`，并结合快慢双周期（20s / 5m 取大）滚动实现波动率（下限 14 bps/5m）。
- **开仓与挂撤单（`core/policy.py`, `core/execution.py`）**：
  - 活跃入场窗口 `tau ∈ [15s, 285s]`，价格走廊 `[$0.20, $0.74]`，只买领先侧 `P* ≥ 0.53`；
  - Taker 优先：扣费净边际达标（基准 `≥ 0.035`）即以精确预计算原生 IOC 限价吃单；价格高于 `$0.55` 后每 +$0.10 额外要求 +0.5% 边际（早盘时间溢价已归零）；
  - 吃单边际不足且现货平静时退为零费率 Maker 挂单（基准 `≥ 0.045`），配备 2.5s 追价冷却、边际衰减撤单（< 1.5%）、8s 排队超时撤单与顶 5 档巨额卖墙拦截；
  - 三道保护：方向性防接飞刀（现货 2-3s 逆向急动 `≥ 1.5 bps` 禁止逆势开仓、顺势 Taker 放行）、极端波动风暴暂停（`> 75 bps/5m`）、任何 `≥ 2.5 bps` 现货冲击即撤全部被动挂单。
- **持仓管理（严守买入即持有至结算）**：锁利 / 残值出清 / 对冲开关全部关闭，保留到期 `$1.00` 完整赔率凸性；顺势概率优势分批加仓（领先侧 `P* ≥ 0.54`、Taker-only、4s 冷却，净敞口上限 10 股、单侧累计 50 股、每窗最多 10 笔）；确认反转调仓（己侧 `P* ≤ 0.42` 且对侧 `≥ 0.58` 并对侧独立正 EV，纯 Taker、每窗最多 2 次、8s 冷却）；尾盘最后 15s 冻结撤单。
- **实验开关**：高价单边顺势模式 B（`ENABLE_DRIFT_MODE_B`）已关闭（高价负赔率易放大尾部回撤）；纸面验证阶段暂关闭账户熔断与单窗预算截断，完整暴露策略原生盈亏下限。

## 3. 目录结构

```text
pm-quant/
├── config.py                 # 全局唯一参数与版本真理源（v3.2）
├── main.py                   # 统一命令行入口
├── requirements.txt          # Python 依赖（requests, websockets）
├── core/                     # 纯策略与风控内核
│   ├── types.py              # 核心数据结构（OrderBook, SpotState, Position, Order, Decision）
│   ├── estimator.py          # 胖尾 Student-t(5) 亚式 TWAP 定价与双周期波动率
│   ├── policy.py             # 统一净边际决策与持仓管理引擎（v3.2 概率控仓多买）
│   ├── execution.py          # 5 股离散化、手续费与挂单生命周期管理
│   ├── circuit_breaker.py    # 账户级日度回撤与连亏冷却看门狗
│   └── regime.py             # 市场形态归因分类（仅统计与展示，不拦截开仓）
├── sim/                      # 拟真撮合、历史回放、压力测试与科学体检
│   ├── matcher.py            # 动态 RTT 延迟 + FIFO 排队消耗 + 逐档 VWAP 撮合器
│   ├── replay.py             # 事件驱动历史回放器（含 Walk-Forward 按日分片与多延迟对比）
│   ├── audit.py              # 账本科学体检引擎（t统计量 / Brier Score / 校准度 / 结构拆解）
│   └── stress.py             # 合成行情压力测试套件
├── runtime/                  # 实时行情、纸面执行、运维清理与告警
│   ├── ws_feeds.py           # 多源低延迟现货（Binance/Kraken/Coinbase/OKX/auto）+ Polymarket L2 WS
│   ├── trade_stream.py       # Polymarket RTDS 逐笔成交流 WebSocket
│   ├── market_feed.py        # 多源行情聚合、REST 兜底、时钟监测与接口协议守卫
│   ├── runner.py             # 单窗状态机、原子账本落盘与官方结算对账
│   ├── pipeline.py           # 5 分钟全自动轮转守护驱动
│   ├── retention.py          # 过期原始行情/回放定期清理与磁盘水位保护
│   ├── alerts.py             # 轻量非阻塞手机告警（Telegram / Bark / Webhook）
│   └── supervisor.py         # 版本代码快照封存（seal）
├── web/                      # 零重型依赖投资人监控看板
│   ├── server.py             # HTTP API、Basic Auth 鉴权与 /healthz 探针
│   └── static/               # 响应式前端（index.html, style.css, app.js）
├── deploy/                   # Ubuntu 7×24 生产部署脚本、systemd 与 Caddy 反代模板
├── tests/                    # 单元测试与回归测试套件
└── data/                     # 运行数据（不纳入版本控制）
    ├── 1_market/             # 原始行情流与官方结算记录（定期滚动清理）
    ├── 2_ledgers/            # 分版本账本（summary.jsonl 永久保留、live.json、windows/*.json.gz）
    ├── 3_reports/            # 离线回放与压测报告
    └── 4_runtime/            # 进程锁、PID 与运行状态
```

## 4. 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行全部单元测试
python main.py test

# 启动完整系统（交易引擎 + Web 看板 :8900）
python main.py start 8900 --no-browser

# 双进程分别启动（推荐生产环境）
python main.py run
python main.py web 8900 --no-browser

# 查看当前版本与参数 / 一键网络测速 / 账本科学体检 / 手动清理过期行情 / 历史回放 / 压力测试
python main.py status
python main.py probe
python main.py audit [账本目录或.tar.gz]
python main.py cleanup --dry-run
python main.py replay --latencies=0.05,0.20,0.50
python main.py stress

# 现货源当前默认 Coinbase (BTC/USD) 实测验证；可用环境变量切换 (binance / coinbase / okx / kraken / auto)
PM_SPOT_WS_PROVIDER=binance python main.py run
```

## 5. Linux 服务器生产部署

Ubuntu 22.04 / 24.04（2C / 1G~2G RAM / 30G 硬盘）的完整部署步骤、双进程 systemd 服务、Caddy HTTPS 反代、自动备份与告警配置，见 [deploy/README_LINUX.md](deploy/README_LINUX.md)。
