# PM-Quant (`v2.4`)

面向 **Polymarket BTC 5 分钟 Up/Down 二元预测市场**的纸面（Paper）量化研究系统。系统没有实盘下单适配器，不会向交易所提交任何真实订单；所有盈亏都是录制行情或实时行情上的仿真结果。

## 1. 核心设计原则

**真实性优先。** 撮合严格基于 Polymarket 官方 WebSocket 逐笔成交流：只有真实打印价格扫到挂单价时才消耗 FIFO 队列并成交，没有成交流就不凭空推进队列；进攻腿按盘口逐档吃深度、计算 VWAP 与手续费，不按买一价无限量成交。结算优先采用 Chainlink TWAP / Gamma 官方真值。

**单一事件驱动内核。** 回放（Replay）与纸面（Paper）共用同一套无副作用的决策与执行状态机（`core/` 与 `runtime/runner.py`）。回放逐 tick 重新调用真实 `plan_decision` 与撮合器，队列位置、延迟、深度、手续费全部计入，不读取事后盈亏。实盘适配器尚未实现。

**版本与账本隔离。** 采用语义化版本 `v<Major>.<Minor>`，每个版本对应独立账本 `data/2_ledgers/<VERSION>/`，绝不混写或污染历史成绩。

**资方极简看板。** 看板只呈现核心财务结果（累计净收益、胜率、单场均利）与每轮 5 分钟的具体动作流水（挂单、对冲、成交均价、结算输赢与落袋盈亏）。

## 2. 策略概览：`v2.4` 纯物理量双轨融合引擎（双边动态做市 + 单侧趋势预测）

`v2.4` 彻底剔除了黑盒正态分布胜率算命模型（`p_fair` / `P*`），完全基于客观物理量（基差校准现价偏离、最后 60 秒已锁定 TWAP 均值、15 秒短期价格与盘口拐点动量、双边真实盘口成本），融合两条互补盈利轨道：

- **第一轨：前半程拐点/顺势低吸 + 动态追踪双边做市（$\tau \in (120\text{s}, 240\text{s}]$）**：
  - **第一腿低吸（$\le \$0.45$）**：① **水下/高位止跌抬头低吸**：当价格仍在开盘价下方/上方（`8.5 bps` 内，折价票 $\le \$0.45$），且最近 15 秒现货回升（$\ge 1.0\text{ bps}$）与盘口概率抬升（$\ge +1.5\%$）同步确认拐点时挂单买入；② **顺势折价低吸**：客观领先 $\ge 1.5\text{ bps}$ 且票价 $\le \$0.45$ 时顺势挂单；③ **防飞刀保护**：15 秒盘口暴跌 $> \$0.08$ 且现货反向跳水时强制拦截。
  - **第二腿动态追踪锁利（`Trailing Pair Lock`）**：第一腿成交后实时追踪对侧卖一价最低点 `min_opp_ask_seen`。当最佳配对成本进入 $\le \$0.95$ 后，对侧继续下跌时让利润奔跑；一旦势头停顿、对侧从最低点反弹 $\ge \$0.02$（且总成本 $\le \$0.97$）立即吃单补买第二腿锁定无风险收益；若遇深折价暴利（`pair_cost <= $0.90`）则直接锁仓。
- **第二轨：单侧高确信趋势预测 + 大突破单边吃满**：
  - **单边大突破豁免**：若低吸第一腿后行情演变为单边大突破（客观领先 $\ge 12.0\text{ bps}$），自动豁免浅层追踪锁利，直接单侧持有到期兑现完整 $\$1.00$。
  - **尾盘高确信单侧预测（LWAA，$\tau \in [15\text{s}, 120\text{s}]$）**：当客观物理领先（含最后 60 秒锁定 TWAP 均值）达到 $\ge 5.0\text{ bps}$（Maker）或 $\ge 8.0\text{ bps}$（Taker IOC），且市场盘口共识同向（中间价 $\ge 0.52$）时，在 $[\$0.20, \$0.65]$（硬顶 $\$0.70$）内单侧进场并持有到期。
- **尾盘残值挽回与冻结**：$\tau \le 30\text{s}$ 若持仓方向相反且盘口买一崩塌至 $[\$0.06, \$0.15]$ 允许防御性出清；$\tau < 15\text{s}$ 停止新开仓并撤回未成交挂单。

每窗最多开仓 1 次（1 手 = 5 股）。行情形态分类（regime）只用于事后归因与看板展示，**不作为开仓硬闸门**。

## 3. 目录结构

```text
D:\pm-quant/
├── config.py                 # 全局唯一真理源（版本标识 v2.2 与核心参数字典）
├── main.py                   # 统一命令行主入口
├── CHANGELOG.md              # 语义化版本变更记录
├── core/                     # 纯函数数学与经济学内核
│   ├── types.py              # 强类型数据结构（OrderBook, SpotState, Position, Order, Decision）
│   ├── estimator.py          # EWMA 动态波动率、自适应价差、连续结算胜率 Φ(d)
│   ├── regime.py             # 行情形态分类（仅用于归因与看板，不参与开仓决策）
│   ├── circuit_breaker.py    # 账户级日度最大回撤与连亏熔断看门狗
│   ├── policy.py             # v2.2 双阶段动态吸收决策（MWDE 顺势低吸 + LWAA 尾盘吸收）
│   └── execution.py          # 离散量子化、手续费与报价滞回防抖（Hysteresis）
├── sim/                      # 真实微观结构模拟与历史回放
│   ├── matcher.py            # 300ms 延迟 + FIFO 队列消耗 + 逐档深度/VWAP 撮合器
│   ├── replay.py             # 事件驱动回放：重放录制盘口/成交并重新调用真实决策内核
│   └── stress.py             # 合成行情压力测试（鲁棒性/风险包络，非盈利证明）
├── runtime/                  # 实时数据流、纸面执行与进程封存
│   ├── trade_stream.py       # Polymarket 官方 WebSocket 逐笔成交流（RTDS）
│   ├── market_feed.py        # 现货/预言机/盘口流与 Chainlink TWAP 结算核验
│   ├── runner.py             # 窗口生命周期记账、live.json 原子写与 summary.jsonl 追加
│   ├── pipeline.py           # 5 分钟窗口全自动轮转与跨轮次平滑重叠守护进程
│   └── supervisor.py         # 版本封存（code_snapshot.zip + manifest.json）
├── web/                      # 资方极简看板（零第三方重型依赖）
│   ├── server.py             # 原生 HTTP 服务与净收益/动作流水 API
│   └── static/               # 前端页面（index.html, style.css, app.js）
├── tests/                    # 数学不变量、撮合、风控、回放与审计回归测试（100 项用例）
└── data/                     # 四层冷热分离数据目录
    ├── 1_market/             # 只读原始市场录制资产（events, oracle, settlements）
    ├── 2_ledgers/            # 按版本隔离的运行账本（如 v2.2/）
    ├── 3_reports/            # 样本外回放与压力测试报告
    └── 4_runtime/            # 进程 PID 与运行日志
```

## 4. 核心参数

所有参数集中在 `config.py` 的 `PARAMS` 字典，启动时由 `validate_params()` 校验自洽性。关键口径如下：

仓位与物理约束以 5 股为 1 手（`LOT_SIZE`），每窗 1~2 手（`MIN_LOTS`/`MAX_LOTS`），初始本金 $100（`INITIAL_CAPITAL_USD`），下单往返延迟 300ms（`LATENCY_S`）。手续费采用 Polymarket 官方加密合约口径：吃单每股 `费率 × p × (1-p)`（`TAKER_FEE_RATE=0.07`），挂单零费率。

两阶段入场阈值见第 2 节：MWDE 折价上限 `MWDE_MAX_ENTRY_PRICE=0.45`、大涨大跌门槛 `POST_SHOCK_MIN_MOVE=40.0`；LWAA 高确信门槛 `LWAA_MIN_PROB=0.80`、最小净优势 `LWAA_MIN_EDGE=0.02`、入场走廊 `LWAA_MIN_ENTRY_PRICE=0.35` 至 `LWAA_MAX_ENTRY_PRICE=0.65`、追高硬顶 `LWAA_HARD_CHASE_CEILING=0.70`。两阶段在 tau=120 秒无缝衔接，尾盘冻结线 `LWAA_MIN_TAU_S=15.0`。

风控看门狗：单窗硬亏损包络 `MAX_WINDOW_LOSS_USD=5.00`，单日累计回撤熔断 `MAX_DAILY_DRAWDOWN_USD=15.00`，连亏 3 轮触发冷却（`LOSS_STREAK_COOLING`/`COOLING_ROUNDS`），断流死人开关 `DEADMAN_TIMEOUT_S=3.0`，被动排队超时 `MAX_QUEUE_WAIT_S=15.0`。

财务口径统一为：`net_pnl = 结算兑现 proceeds − 两腿总成本 total_cost`；`pair_cost = Up 均价 + Down 均价`；只有 `pair_cost > 1.00` 的补腿才计入 `insurance_paid`（买入了到期必亏的溢价）。

## 5. 常用命令

```bash
python main.py status        # 查看当前版本身份与核心参数
python main.py test          # 运行全部单元测试（100 项用例）
python main.py replay        # 事件驱动回放录制数据，结果写入 data/3_reports/replay_*.jsonl
python main.py stress        # 合成行情压力测试（鲁棒性/风险包络）
python main.py run           # 启动实时纸面交易管道（不提交真实订单）
python main.py web 8900      # 启动资方极简监控看板（默认端口 8900）
python main.py start         # 一键启动交易管道 + 看板 + 浏览器
python main.py stop          # 一键停止所有交易进程并释放 8900 端口
python main.py reset         # 归档并重置当前版本账本为干净初始状态（$100.00）
python main.py seal          # 封存当前批次代码快照与参数清单
```

回放只覆盖具备完整录制数据的窗口，报告同时给出“同窗口账本实际盈亏”作为对照。看板的行情形态归因矩阵只统计“入场时刻”打标的已成交窗口，并给出样本量与 95% 置信区间；样本 n < 30 的格子标记为“样本不足”，不应据此下结论。没有入场标签的历史窗口被单独归入 `unlabeled`/`legacy` 桶，不折叠进任何真实形态。

## 6. 价格曲线口径（2026-11-12 修复）

此前 `price_history.price` 与 `price_history.poly_price` 被写入**同一个** 60 秒 Chainlink TWAP 值，`live.spot` 也被 TWAP 覆盖，导致看板把一条滞后 20~60 秒的平滑曲线当成「BTC 实时价格」展示，与真实行情相差可达数十美元；官方开盘价校准又只改 `poly_price` 不改 `price`，造成实时图与历史图起点不一致、同一时点两个收益率相差约 13 bps。

修复后：

- `price` / `live.spot` = **Binance 实时点值**；`poly_price` / `live.poly_spot` = **Chainlink 60s TWAP**，明确标注为滞后对照。前端主曲线画 Binance，蓝色虚线画 TWAP，图例与 hover 提示均标明来源。
- 两条序列各自对自己的开盘基准计算 bps：`binance_open_px` 与 `open_px` 分别保存，不再相互覆盖；`update_open_price()` 同步首点的两个锚点，收益率不再矛盾。
- TWAP 缓存超过 `POLY_PRICE_MAX_AGE_S` 时写 `poly_price: null` + `poly_stale: true`，概率模型立即切到 Binance 同源链（`binance_open_price`），**不再续用陈旧 TWAP**；`MIN_MODEL_VOL_BPS` 防止低波动读数让 Φ(d) 饱和。
- 结算真值优先级 `gamma` > `twap` > `spot_fallback` 写入账本，TWAP 后台对账只能刷新价格，不能覆盖 UMA/Gamma 已裁决的 winner。
