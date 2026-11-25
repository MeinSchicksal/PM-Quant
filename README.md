# PM-Quant (`v3.3`)

面向 **Polymarket BTC 5 分钟 Up/Down 二元预测市场**的自动化纸面（Paper）量化研究与监控系统。当前版本仅包含纸面仿真执行器，不含实盘下单适配器，所有交易记录均基于真实实时行情或录制历史事件流的拟真撮合结果。

## 1. 核心设计目标

1. **真实性优先（零自欺欺人）**：撮合与记账严格对齐 Polymarket 官方规则——最小下单量 5 股、按真实 RTT 动态建模的网络+引擎延迟（无 600ms 人工削峰）、下单前基于可见 L2 深度精确计算原生 IOC 限价（彻底移除到达后事后拒单/闪崩反悔）、毫秒级时钟校验与 `signal_minus_venue_ms` 相对延迟差监测、以及每窗独立记录 **Brier Score（模型 `P*` vs 盘口 `mid`）** 基准评估。
2. **单一内核**：回放（`sim/replay.py`）与实时纸面（`runtime/pipeline.py`）共用同一套策略决策与执行状态机（`core/` 与 `runtime/runner.py`）。
3. **版本与账本隔离**：所有策略参数与版本身份统一由 `config.py` 定义，账本按版本隔离存储在 `data/2_ledgers/<VERSION>/`。
4. **轻量 7×24 运行**：极简依赖（仅 `requests` 与 `websockets`），面向低配 Linux 服务器（2C / 1G~2G RAM / 30G 硬盘）长期无人值守运行，自带 Web 监控看板、数据自动保留清理与可选手机告警。

## 2. 策略概览（`v3.3`：多因子权重执行 + 节奏控仓 + 终局导向调仓）

- **定价模型（`core/estimator.py`）**：基于实时现货（默认 Coinbase BTC/USD）、跨链基差 EMA 校准与最后 60 秒已锁定均值，采用**方差归一化**胖尾 Student-t（`df=5`，按 $\sqrt{5/3}$ 缩放消除系统性欠自信）估计到期 Chainlink 60s TWAP 胜率 `P*`，并叠加快慢双周期（20s / 5m 取大）滚动波动率（下限 14 bps/5m）与高胜率过度领先边际抑制（`P* > 0.78`）。
- **场景权重执行路由（`core/policy.py`, `core/execution.py`）**：
  - 保留 **Maker 与 Taker 双执行通道**，按现货动能/平静度、盘口点差、价格赔率与剩余时间动态评分路由：现货平静且点差宽（`≥ $0.02`）且价格处于中低区（`≤ $0.62`）时加权零费 Maker 挂单（厚安全垫 `≥ 0.055`）；顺势启动或中后段紧迫时加权原生 IOC 吃单（扣费净边际 `≥ 0.038`）；
  - 不唯“强领先模型”论：允许在胶着区（`P* ∈ [0.46, 0.53)`）但盘口出现极高赔率低价错杀（`≤ $0.42`、净边际 `≥ 0.055`）时轻仓 5 股参与；
  - 维持方向性防接飞刀（逆向 `≥ 1.5 bps` 拦截、顺势 Taker 放行）、极端波动风暴暂停（`> 75 bps/5m`）、以及任何 `≥ 2.5 bps` 现货冲击即撤全部被动挂单。
- **时间节奏控仓与终局导向反手**：
  - **开盘校准（前 20s）→ 早盘轻仓试探（20~120s，硬限 5 股严禁早加仓）→ 中盘质量控仓（120~255s）→ 尾盘收官（最后 45s 停开新仓、最后 15s 冻结）**；
  - **控均价质量加仓**：中盘阶段且冷却 `≥ 12s`、加仓价 `≤ 持仓均价 + $0.05` 且 `≤ $0.65`、扣费净边际 `≥ 4.0%`、己侧 `P* ≥ 0.56` 且较首仓衰减 `≤ 6%`（严禁追高拉烂均价、严禁反转途中低价送钱）；
  - **终局导向动态整数股反手**：确认反转且对侧具备独立正 EV（价格 `≤ $0.68`）时，按最终结算保本微盈（`+$0.30`）一次性反算买入 **5~10 整数股**，受单窗最多 1 次翻面、单窗累计成本 `$10` 封顶与风暴保护约束。

## 3. 目录结构

```text
pm-quant/
├── config.py                 # 全局唯一参数与版本真理源（v3.3）
├── main.py                   # 统一命令行入口
├── requirements.txt          # Python 依赖（requests, websockets）
├── core/                     # 纯策略与风控内核
│   ├── types.py              # 核心数据结构（OrderBook, SpotState, Position, Order, Decision）
│   ├── estimator.py          # 胖尾 Student-t(5) 亚式 TWAP 定价与双周期波动率
│   ├── policy.py             # 多因子动态权重决策与终局导向持仓管理引擎（v3.3）
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
