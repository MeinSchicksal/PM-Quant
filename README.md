# PM-Quant (`v1.1`)

面向 **Polymarket BTC 5 分钟二元预测市场**的极简、可证伪的**纸面/仿真（Paper）量化研究系统**。当前版本**没有实盘下单适配器**，不会向交易所提交任何真实订单；所有盈亏均为录制行情上的仿真结果。

完整的系统设计、总体目标与四阶段演进规划请参见 **[PLAN.md](file:///D:/pm-quant/PLAN.md)**。
交易哲学、历史亏损病根解剖与“三做三不做”军规请参见 **[docs/CORE_STRATEGY_PHILOSOPHY.md](file:///D:/pm-quant/docs/CORE_STRATEGY_PHILOSOPHY.md)**。

---

## 1. 核心设计原则

1. **真实性优先**：拒绝用合成假行情或脱离排队深度的“触价即成交”自欺欺人。撮合严格基于 Polymarket 官方 WebSocket 逐笔成交流（`trades`）：只有真实打印价格扫到我们的挂单价才消耗 FIFO 队列并成交，没有成交流时**不**凭空推进队列；进攻腿按盘口逐档吃深度并计算 VWAP 与手续费，不会按买一价无限量成交。结算优先使用 Chainlink TWAP / Gamma 官方真值。
2. **单一事件驱动内核**：回放（Replay）与纸面（Paper）共用同一套无副作用的核心决策与执行状态机（`core/`）；实盘（Live）适配器尚未实现，接入前不会产生真实下单。
3. **语义化版本与账本隔离**：采用 `v1.0`、`v1.1`、`v2.0` 专业语义化版本管理。每个版本对应独立账本 `data/2_ledgers/<VERSION>/`，绝不混写或污染历史成绩。
4. **资方极简看板**：看板仅呈现核心财务结果（累计净收益、胜率、单场均利）与每一轮 5 分钟机器人的具体动作流水（挂单、对冲、成交均价、结算输赢与落袋盈亏）。

---

## 2. 目录结构

```text
D:\pm-quant/
├── config.py                 # 全局唯一真理源（版本标识 v1.1 与核心参数字典）
├── main.py                   # 统一命令行主入口
├── PLAN.md                   # 总体目标、架构设计与四阶段路线图
├── CHANGELOG.md              # 语义化版本变更记录
├── core/                     # 纯函数数学与经济学内核
│   ├── types.py              # 强类型数据结构（OrderBook, SpotState, Position, Order, Decision）
│   ├── estimator.py          # 高频 EWMA 动态波动率、自适应价差、连续胜率 Φ(d) 与凯利配资
│   ├── circuit_breaker.py    # 账户级日度最大回撤与连亏熔断看门狗（Reduce-only）
│   ├── policy.py             # Avellaneda-Stoikov 库存保留定价与三方效用实时仲裁
│   └── execution.py          # 离散量子化与报价滞回防抖（Hysteresis）
├── sim/                      # 真实微观结构模拟与历史回放
│   ├── matcher.py            # 300ms 延迟 + FIFO 队列消耗 + 逐档深度/VWAP 撮合器
│   └── replay.py             # 事件驱动回放：重放录制的盘口/成交并重新调用真实决策内核
├── runtime/                  # 实时数据流、纸面执行与进程封存
│   ├── trade_stream.py       # Polymarket 官方 WebSocket 逐笔成交流（RTDS）
│   ├── market_feed.py        # 现货/预言机/盘口流与 Chainlink TWAP 结算核验
│   ├── runner.py             # 窗口生命周期记账、live.json 原子写与 summary.jsonl 追加
│   ├── pipeline.py           # 5 分钟窗口全自动轮转与跨轮次平滑重叠守护进程
│   └── supervisor.py         # 版本封存（code_snapshot.zip + manifest.json）
├── web/                      # 资方极简看板（零第三方重型依赖）
│   ├── server.py             # 原生 HTTP 服务与净收益/动作流水 API
│   └── static/               # 前端页面（index.html, style.css, app.js）
├── tests/                    # 数学不变量、撮合、风控与回放单元测试（59 项用例）
└── data/                     # 四层冷热分离数据目录
    ├── 1_market/             # 只读原始市场录制资产（events, oracle, settlements）
    ├── 2_ledgers/            # 按版本隔离的运行账本（如 v1.1/）
    ├── 3_reports/            # 样本外回放与优势校准报告
    └── 4_runtime/            # 进程 PID 与运行日志
```

---

## 3. 核心参数字典（`config.py`）

| 参数名 | 默认值 | 类别 | 经济学与物理含义 |
|---|---|---|---|
| `LOT_SIZE` | `5.0` | 离散物理约束 | 交易所最小下单股数单位（所有订单与持仓均为其整数倍） |
| `MIN_LOTS` | `1` | 仓位底限 | 最小入场挂单批数（1 批 = 5 股） |
| `MAX_LOTS` | `3` | 仓位上限 | 最大持仓批数（3 批 = 15 股，分数凯利仓位上限） |
| `LATENCY_S` | `0.30` | 网络物理延迟 | 下单至撮合引擎生效的往返延迟（300ms） |
| `BASE_EDGE` | `0.025` | 安全边际 | 跨越不确定性带宽后要求的基准净优势（2.5 档） |
| `UNCERTAINTY_K` | `1.2` | 概率置信带宽 | 实时价格不确定性包络 $\epsilon_t$ 的缩放乘数 |
| `BASELINE_VOL_BPS` | `15.0` | 基准波动率 | 冷启动或数据不足时的 5m 基准已实现波动率（bps） |
| `KELLY_FRACTION` | `0.25` | 仓位管理 | 四分之一凯利（Quarter-Kelly）风险放大乘数 |
| `RISK_AVERSION_GAMMA` | `0.15` | 库存风险厌恶 | Avellaneda-Stoikov 单腿库存方差惩罚系数 |
| `MAX_QUEUE_WAIT_S` | `15.0` | 排队动力学 | 基于盘口成交流速的最大可接受被动排队等待时间 |
| `TOXICITY_SHOCK_BPS` | `3.5` | 逆向选择保护 | 3 秒内现货剧烈偏移阈值（3.5 bps），触发防知情毒性保护 |
| `TAIL_FREEZE_S` | `60.0` | 生命周期边界 | 距离结算前停止新开第一腿被动单的保护时间（60s） |
| `MAX_WINDOW_LOSS_USD` | `3.00` | 尾部风险上限 | 单窗口硬止损包络；必须 ≥ `LOT_SIZE × MAX_MAKER_ENTRY_PRICE`（=5×0.60），启动时校验 |
| `MAX_DAILY_DRAWDOWN_USD` | `8.00` | 账户级风控 | 单日累计最大亏损硬熔断保护阈值（触发仅允许平仓） |
| `LOSS_STREAK_COOLING` | `3` | 账户级风控 | 连续亏损轮次触发冷却锁定的阈值（3 轮） |
| `COOLING_ROUNDS` | `3` | 冷却锁定周期 | 连亏后强制休眠观察的轮次数 |

---

## 4. 常用命令

```bash
# 查看当前版本状态与核心参数
python main.py status

# 运行全部单元测试（59 项用例）
python main.py test

# 事件驱动回放：读取 data/1_market/ 录制的盘口/成交/预言机数据，
# 逐 tick 重新调用真实决策与撮合，结果写入 data/3_reports/replay_*.jsonl
python main.py replay

# 闸门反事实：同一批窗口回放两次（开闸门 / 关闸门），
# 直接给出 regime 闸门的净贡献与被拦窗口的反事实盈亏
python main.py replay --no-gates

# 启动实时运行流水线（纸面，不会提交真实订单）
python main.py run

# 启动资方极简监控看板（默认端口 8900）
python main.py web 8900

# 封存当前版本代码快照与参数清单
python main.py seal
```

> 回放只覆盖具备完整录制数据的窗口；报告同时给出“同窗口账本实际盈亏”作为对照，二者差异来自旧账本撮合缺陷，而不是回放被手工调参。
> 看板的行情形态归因矩阵只统计“入场时刻”打标的已成交窗口，并给出样本量与 95% 置信区间；样本 n < 30 的格子会标记为“样本不足”，不应据此下结论。历史窗口没有入场标签，统一显示为 `unlabeled`。

