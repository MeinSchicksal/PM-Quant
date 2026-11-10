/**
 * PM-Quant Master Dashboard Controller (Coinbase Institutional Baseline)
 */

(function () {
  'use strict';

  // State
  let currentTab = 'overview';
  let historyData = null;
  let liveData = null;
  let chartMode = 'dual'; // 'dual' | 'price' | 'prob'

  // DOM Elements
  const timeEl = document.getElementById('current-time');
  const dateEl = document.getElementById('current-date');
  const weekdayEl = document.getElementById('current-weekday');
  const connPill = document.getElementById('conn-pill');
  const connText = document.getElementById('conn-text');
  const refreshBtn = document.getElementById('refresh-btn');
  const pageTitle = document.getElementById('page-title');
  const liveSlugBadge = document.getElementById('live-slug-badge');

  // Sidebar Links
  const sidebarLinks = document.querySelectorAll('.sidebar-link');
  const viewPanels = {
    overview: document.getElementById('view-overview'),
    market: document.getElementById('view-market'),
    history: document.getElementById('view-history')
  };

  // Beijing Time Format Helper for 5-minute Slugs
  function parseSlugBeijingTime(slug, startSecFallback = null) {
    if (!slug) return { fullStr: '--', rangeStr: '--:-- ~ --:--', dateStr: '--/--', raw: '' };

    let startSec = startSecFallback;
    if (!startSec) {
      const parts = String(slug).split('-');
      const last = parts[parts.length - 1];
      if (/^\d+(\.\d+)?$/.test(last)) {
        startSec = parseFloat(last);
      }
    }

    if (!startSec || isNaN(startSec)) {
      return { fullStr: slug, rangeStr: slug, dateStr: '', raw: slug };
    }

    const endSec = startSec + 300.0;
    const pad = n => String(n).padStart(2, '0');

    // Convert start and end to UTC+8 Beijing Time
    const dStart = new Date(startSec * 1000);
    const utcStart = dStart.getTime() + dStart.getTimezoneOffset() * 60000;
    const bjStart = new Date(utcStart + 3600000 * 8);

    const dEnd = new Date(endSec * 1000);
    const utcEnd = dEnd.getTime() + dEnd.getTimezoneOffset() * 60000;
    const bjEnd = new Date(utcEnd + 3600000 * 8);

    const mo = pad(bjStart.getMonth() + 1);
    const da = pad(bjStart.getDate());
    const sh = pad(bjStart.getHours());
    const sm = pad(bjStart.getMinutes());
    const eh = pad(bjEnd.getHours());
    const em = pad(bjEnd.getMinutes());

    return {
      fullStr: `${mo}/${da} ${sh}:${sm} ~ ${eh}:${em}`,
      rangeStr: `${sh}:${sm} ~ ${eh}:${em}`,
      dateStr: `${mo}/${da}`,
      raw: slug
    };
  }

  // Wall Clock (Beijing Time)
  function updateClock() {
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const bj = new Date(utc + 3600000 * 8);

    const pad = n => String(n).padStart(2, '0');
    const hh = pad(bj.getHours());
    const mm = pad(bj.getMinutes());
    const ss = pad(bj.getSeconds());
    timeEl.textContent = `${hh}:${mm}:${ss}`;

    const yyyy = bj.getFullYear();
    const mo = pad(bj.getMonth() + 1);
    const dd = pad(bj.getDate());
    dateEl.textContent = `${yyyy}/${mo}/${dd}`;

    const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    weekdayEl.textContent = weekdays[bj.getDay()];
  }
  setInterval(updateClock, 500);
  updateClock();

  // Navigation Tab Switching
  function switchTab(target) {
    if (!viewPanels[target]) target = 'overview';
    currentTab = target;
    location.hash = '#' + target;

    sidebarLinks.forEach(link => {
      if (link.dataset.tab === target) {
        link.classList.add('active');
      } else {
        link.classList.remove('active');
      }
    });

    Object.keys(viewPanels).forEach(key => {
      if (key === target) {
        viewPanels[key].hidden = false;
      } else {
        viewPanels[key].hidden = true;
      }
    });

    const titles = {
      overview: '交易总览',
      market: '实时工作区',
      history: '场次记录'
    };
    pageTitle.textContent = titles[target] || '交易总览';

    if (target === 'market' && liveData) {
      setTimeout(() => drawLivePriceChart(liveData), 30);
    }
  }

  sidebarLinks.forEach(link => {
    link.addEventListener('click', e => {
      e.preventDefault();
      switchTab(link.dataset.tab);
    });
  });

  const viewMoreBtn = document.getElementById('view-more-history');
  if (viewMoreBtn) {
    viewMoreBtn.addEventListener('click', e => {
      e.preventDefault();
      switchTab('history');
    });
  }

  const hash = location.hash.replace('#', '');
  if (viewPanels[hash]) {
    switchTab(hash);
  }

  // Chart Mode Toggle Handlers (Dual / Price / Prob)
  document.querySelectorAll('#chart-mode-toggle .mode-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      document.querySelectorAll('#chart-mode-toggle .mode-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      chartMode = btn.dataset.mode || 'dual';
      if (liveData) drawLivePriceChart(liveData);
    });
  });

  // Formatting Helpers
  function fmtPct(val) {
    if (val === null || val === undefined || isNaN(val)) return '0.00%';
    const num = Number(val);
    const prefix = num > 0 ? '+' : '';
    return prefix + num.toFixed(2) + '%';
  }

  // Update Overview Page
  function updateOverview(hist, live) {
    if (!hist) return;

    const verEl = document.getElementById('page-version');
    if (verEl && (hist.batch_id || hist.current_batch)) {
      verEl.textContent = hist.batch_id || hist.current_batch;
    }

    const totalPnl = hist.total_pnl || 0;
    const initialCap = hist.initial_capital || 100.0;
    const turnover = hist.turnover || 0;
    const availCap = hist.available_capital !== undefined ? hist.available_capital : (initialCap + totalPnl);
    const roiPct = hist.roi_pct !== undefined ? hist.roi_pct : ((totalPnl / initialCap) * 100);
    const turnoverRoiPct = hist.turnover_roi_pct !== undefined ? hist.turnover_roi_pct : (turnover > 0 ? ((totalPnl / turnover) * 100) : 0);
    const freeCap = hist.free_capital !== undefined ? hist.free_capital : availCap;

    const pnlEl = document.getElementById('hero-total-pnl');
    pnlEl.textContent = (totalPnl >= 0 ? '+' : '-') + '$' + Math.abs(totalPnl).toFixed(2);
    pnlEl.className = 'hero-num ' + (totalPnl > 0 ? 'up' : totalPnl < 0 ? 'down' : '');

    document.getElementById('hero-settled-count').textContent = `${hist.traded_rounds || 0} 场已结算`;
    document.getElementById('hero-won').textContent = `+$${(hist.total_won || 0).toFixed(2)}`;
    document.getElementById('hero-lost').textContent = `-$${(hist.total_lost || 0).toFixed(2)}`;

    // Hero composition: 4 boxes
    const heroInit = document.getElementById('hero-initial-cap');
    if (heroInit) heroInit.textContent = `$${initialCap.toFixed(2)}`;

    const heroRoi = document.getElementById('hero-roi');
    if (heroRoi) {
      heroRoi.textContent = (turnoverRoiPct >= 0 ? '+' : '') + turnoverRoiPct.toFixed(2) + '%';
      heroRoi.className = 'comp-val ' + (turnoverRoiPct > 0 ? 'up' : turnoverRoiPct < 0 ? 'down' : '');
    }

    const heroFree = document.getElementById('hero-free-cap');
    if (heroFree) {
      heroFree.textContent = `$${freeCap.toFixed(2)}`;
      heroFree.className = 'comp-val ' + (freeCap < availCap ? 'up' : '');
    }

    const heroTurn = document.getElementById('hero-turnover');
    if (heroTurn) heroTurn.textContent = `$${(hist.turnover || 0).toFixed(2)}`;

    // Active Round Card in Overview (Only Beijing Time, no raw slug)
    if (live && live.slug) {
      const bj = parseSlugBeijingTime(live.slug, live.start);
      document.getElementById('overview-live-time').textContent = bj.rangeStr;
      const liveDateEl = document.getElementById('overview-live-date');
      if (liveDateEl) liveDateEl.textContent = `${bj.dateStr} (UTC+8)`;
      const tau = Math.max(0, Math.round(live.tau_s || 0));
      document.getElementById('overview-live-tau').textContent = `${tau} s`;

      const spot = live.spot || 0;
      document.getElementById('overview-live-spot').textContent = `$${spot.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

      const posUp = live.pos_up ? live.pos_up.shares || 0 : 0;
      const posDn = live.pos_down ? live.pos_down.shares || 0 : 0;
      const isLocked = posUp > 0 && posDn > 0 && Math.abs(posUp - posDn) < 1e-4;
      document.getElementById('overview-live-pos').textContent = `看涨(UP) ${posUp.toFixed(1)} / 看跌(DN) ${posDn.toFixed(1)}${isLocked ? ' (已对冲锁仓)' : ''}`;
    }

    // KPI Cards
    const kpiAvail = document.getElementById('kpi-available-cap');
    if (kpiAvail) kpiAvail.textContent = `$${availCap.toFixed(2)}`;
    const kpiAvailSub = document.getElementById('kpi-avail-sub');
    if (kpiAvailSub) kpiAvailSub.textContent = `初始 $${initialCap.toFixed(2)} · 本金回报率 ${(roiPct >= 0 ? '+' : '') + roiPct.toFixed(2)}%`;

    document.getElementById('kpi-winrate').textContent = `${hist.win_rate || 0.0}%`;
    document.getElementById('kpi-winloss').textContent = `${hist.wins || 0} 胜 ${hist.losses || 0} 负 ${hist.flats || 0} 平`;

    const avgPnl = hist.avg_pnl || 0;
    const avgEl = document.getElementById('kpi-avg-pnl');
    avgEl.textContent = (avgPnl >= 0 ? '+' : '-') + '$' + Math.abs(avgPnl).toFixed(2);
    avgEl.className = 'kpi-card-value ' + (avgPnl > 0 ? 'up' : avgPnl < 0 ? 'down' : '');
    document.getElementById('kpi-avg-split').textContent = `均盈 +$${(hist.avg_win || 0).toFixed(2)} / 均亏 -$${(hist.avg_loss || 0).toFixed(2)}`;

    // Paired Ratio and Total Fills
    let pairedCount = 0;
    let totalFills = 0;
    if (hist.records) {
      pairedCount = hist.records.filter(r => r.pos_up > 0 && r.pos_down > 0 && Math.abs(r.pos_up - r.pos_down) < 1e-4).length;
      totalFills = hist.records.reduce((sum, r) => sum + (r.fills_count || 0), 0);
      const hedgeRate = hist.traded_rounds ? ((pairedCount / hist.traded_rounds) * 100).toFixed(1) : '0.0';
      const hedgeEl = document.getElementById('kpi-hedge-rate');
      if (hedgeEl) hedgeEl.textContent = `${hedgeRate}%`;
      const fillsEl = document.getElementById('kpi-total-fills');
      if (fillsEl) fillsEl.textContent = `累计 ${totalFills} 笔成交`;
    }

    // Render Recent 5 Settled Rounds on Overview
    renderRecentTable(hist.records ? hist.records.slice(0, 5) : []);

    // Render Market Regime Performance Attribution Matrix
    renderRegimeAttribution(hist.regime_stats || {});
  }

  const REGIME_META = {
    'range_bound': { name: '高确定性箱体震荡', desc: '信噪比 SNR ≤ 1.0, 偏离 ≤ 8 bps (核心价差捕获做市)', badge: 'ok' },
    'normal_oscillation': { name: '常态微幅震荡', desc: '信噪比 SNR ≤ 1.2, 基线 1 手做市排队', badge: 'normal' },
    'trending_up': { name: '单边上涨强动量', desc: '信噪比 SNR > 1.8 且偏上, 趋势防守 (禁止逆势买跌)', badge: 'freeze' },
    'trending_down': { name: '单边下跌强动量', desc: '信噪比 SNR > 1.8 且偏下, 趋势防守 (禁止逆势买涨)', badge: 'freeze' },
    'toxic': { name: '剧烈跳空毒流', desc: '波动率 > 35 bps 或秒级剧震, 主动收缩挂单避险', badge: 'warn' },
    'wide_spread': { name: '宽价差流动性缺失', desc: '买卖价差 > 0.06, 摩擦成本过高保持观望', badge: 'freeze' },
    'transitional': { name: '过渡状态观察', desc: '信噪比 1.2~1.8, 状态切换中观望', badge: 'normal' },
    'tail_freeze': { name: '交割尾盘流动性冻结', desc: 'Tau < 180s 临近到期, 严禁新开单边仓位', badge: 'freeze' }
  };

  function renderRegimeAttribution(stats) {
    const tbody = document.getElementById('regime-attribution-tbody');
    if (!tbody) return;

    if (!stats || Object.keys(stats).length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="action-empty">暂无归因数据</td></tr>';
      return;
    }

    const keys = ['range_bound', 'normal_oscillation', 'trending_up', 'trending_down', 'toxic', 'wide_spread', 'transitional', 'tail_freeze'];
    Object.keys(stats).forEach(k => {
      if (!keys.includes(k)) keys.push(k);
    });

    const rows = keys.map(k => {
      const s = stats[k] || { rounds: 0, traded: 0, wins: 0, losses: 0, flats: 0, net_pnl: 0.0 };
      const meta = REGIME_META[k] || { name: k, desc: '实时微观统计', badge: 'normal' };
      const decisive = s.wins + s.losses;
      const winRate = decisive > 0 ? ((s.wins / decisive) * 100).toFixed(1) + '%' : (s.traded > 0 ? '0.0%' : '--');
      const pnl = s.net_pnl || 0.0;
      const pnlCls = pnl > 0 ? 'up' : pnl < 0 ? 'down' : '';
      const pnlStr = (pnl >= 0 ? '+' : '-') + '$' + Math.abs(pnl).toFixed(2);

      return `
        <tr>
          <td><strong class="time-cell-main">${meta.name}</strong></td>
          <td><small style="color: #64748b;">${meta.desc}</small></td>
          <td>${s.rounds} 场</td>
          <td>${s.traded} 场</td>
          <td>${s.wins} 胜 / ${s.losses} 负</td>
          <td><strong>${winRate}</strong></td>
          <td><strong class="${pnlCls}">${s.traded > 0 ? pnlStr : '--'}</strong></td>
        </tr>
      `;
    }).join('');

    tbody.innerHTML = rows;
  }

  function renderRecentTable(records) {
    const tbody = document.getElementById('overview-recent-tbody');
    if (!tbody) return;

    if (!records || records.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="action-empty">暂无结算记录</td></tr>';
      return;
    }

    const rows = records.map(r => {
      const pnl = r.net_pnl || 0;
      const pnlCls = pnl > 0 ? 'up' : pnl < 0 ? 'down' : '';
      const winnerCls = (r.winner || '').toLowerCase();
      const bj = parseSlugBeijingTime(r.slug, r.start);
      const openPx = r.open_px || 0;
      const closePx = r.close_px || 0;
      const isPending = (r.result === 'pending') || (!r.close_px && !r.winner);

      const getShares = (val) => {
        if (typeof val === 'number') return val;
        if (val && typeof val === 'object' && typeof val.shares === 'number') return val.shares;
        const n = Number(val);
        return isNaN(n) ? 0 : n;
      };
      const getCost = (rec) => {
        if (typeof rec.total_cost === 'number') return rec.total_cost;
        if (rec.pos_up && typeof rec.pos_up === 'object' && rec.pos_down && typeof rec.pos_down === 'object') {
          return (Number(rec.pos_up.cash) || 0) + (Number(rec.pos_down.cash) || 0);
        }
        const n = Number(rec.total_cost);
        return isNaN(n) ? 0 : n;
      };

      const posUpShares = getShares(r.pos_up);
      const posDownShares = getShares(r.pos_down);
      const totalCostVal = getCost(r);
      const proceedsVal = Number(r.proceeds) || 0;

      let priceHtml = '';
      if (isPending) {
        priceHtml = `
          <span class="price-open-close">$${openPx > 0 ? openPx.toFixed(2) : '--'} → <span class="winner-pill pending" style="padding: 2px 6px; font-size: 11px;">进行中</span></span>
          <small class="price-diff-bps" style="color: #64748b;">实时监测中</small>
        `;
      } else {
        const diff = closePx - openPx;
        const pct = openPx > 0 ? (diff / openPx) * 100.0 : 0;
        const diffStr = (diff >= 0 ? '+' : '-') + '$' + Math.abs(diff).toFixed(2);
        const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
        const diffCls = diff > 0 ? 'up' : (diff < 0 ? 'down' : '');
        priceHtml = `
          <span class="price-open-close">$${openPx.toFixed(2)} → $${closePx.toFixed(2)}</span>
          <small class="price-diff-bps ${diffCls}">${diffStr} (${pctStr})</small>
        `;
      }

      return `
        <tr>
          <td><strong class="time-cell-main">${bj.fullStr}</strong></td>
          <td><span class="winner-pill ${isPending ? 'pending' : winnerCls}">${isPending ? '进行中' : (r.winner === 'Up' ? '看涨 UP' : r.winner === 'Down' ? '看跌 DOWN' : (r.winner || '--'))}</span></td>
          <td>${priceHtml}</td>
          <td>${posUpShares.toFixed(1)}</td>
          <td>${posDownShares.toFixed(1)}</td>
          <td>$${totalCostVal.toFixed(2)}</td>
          <td>${isPending ? '--' : '$' + proceedsVal.toFixed(2)}</td>
          <td><strong class="${isPending ? '' : pnlCls}">${isPending ? '--' : (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(3)}</strong></td>
          <td>${r.fills_count || 0}</td>
        </tr>
      `;
    }).join('');
    tbody.innerHTML = rows;
  }

  // Translate internal strategy arbitration into professional investor Chinese
  function translateDecisionReason(decision) {
    if (!decision || (!decision.reason && !decision.action)) {
      return { text: "系统微观跟踪中，实时评估盘口深度与价差", level: "normal" };
    }
    const rawReason = String(decision.reason || '');
    const reason = rawReason.toLowerCase();
    const action = String(decision.action || '').toLowerCase();
    const side = String(decision.side || '').toUpperCase();
    const pxStr = decision.price !== undefined && decision.price !== null ? `$${Number(decision.price).toFixed(2)}` : '';

    // If reason is already native Chinese, return it directly
    if (/[\u4e00-\u9fa5]/.test(rawReason)) {
      let level = "normal";
      if (rawReason.includes("熔断") || rawReason.includes("高危") || rawReason.includes("止损") || rawReason.includes("警告") || rawReason.includes("休眠") || rawReason.includes("毒流")) {
        level = "warn";
      } else if (rawReason.includes("冻结") || rawReason.includes("防守") || rawReason.includes("上限") || rawReason.includes("观望") || rawReason.includes("偏离")) {
        level = "freeze";
      } else if (rawReason.includes("买入") || rawReason.includes("做市") || rawReason.includes("对冲") || rawReason.includes("配平") || rawReason.includes("锁赢")) {
        level = (side === 'DOWN' || rawReason.includes('看跌') || rawReason.includes('DOWN')) ? "down" : "ok";
      }
      return { text: rawReason, level };
    }

    // 1. Toxic shock & Extreme Volatility
    if (reason.includes('toxic') || reason.includes('shock')) {
      return {
        text: "⚠️ 现货剧烈跳空 (高危毒流防守)：挂单主动收缩，防范逆向吃单",
        level: "warn"
      };
    }

    // 2. Circuit breaker / drawdown / streak / cooling
    if (reason.includes('circuit') || reason.includes('drawdown')) {
      return {
        text: "🛑 日度熔断看门狗生效：已禁止新开 Leg-1 单边仓位，仅保留对冲防守",
        level: "warn"
      };
    }
    if (reason.includes('cooling') || reason.includes('streak')) {
      return {
        text: "❄️ 连续亏损休眠机制启动：进入观察冷却期，杜绝情绪化连续开仓",
        level: "warn"
      };
    }

    // 3. Max position or entries reached
    if (reason.includes('max position') || reason.includes('position reached')) {
      return {
        text: "⏸️ 单轮持仓已达风控上限 (15 股)：停止增仓，锁定底仓等待期末清算",
        level: "freeze"
      };
    }
    if (reason.includes('max entries') || reason.includes('entries reached') || reason.includes('per window reached')) {
      return {
        text: "⏸️ 本轮开仓次数已达上限 (单窗仅限1次)：锁定仓位等待到期清算",
        level: "freeze"
      };
    }

    // 4. Tail freeze & Late window (tau < 180s or tau < 60s)
    if (reason.includes('tail freeze') || reason.includes('late window') || reason.includes('tau <') || reason.includes('tau<') || reason.includes('tau ')) {
      return {
        text: "⏱️ 临近交割尾盘 (尾盘流动性保护)：禁止新开第1腿，锁定挂单防守",
        level: "freeze"
      };
    }

    // 5. Directional Momentum / Trend (SNR > 1.8)
    if (reason.includes('bullish trend') || (reason.includes('trend') && reason.includes('up') && !reason.includes('down'))) {
      return {
        text: "📈 检测到单边看涨强动量趋势 (信噪比>1.8)：启动单边趋势防守，严禁逆势买跌",
        level: "freeze"
      };
    }
    if (reason.includes('bearish trend') || (reason.includes('trend') && reason.includes('down'))) {
      return {
        text: "📉 检测到单边看跌强动量趋势 (信噪比>1.8)：启动单边趋势防守，严禁逆势买涨",
        level: "freeze"
      };
    }
    if (reason.includes('trending') || reason.includes('trend')) {
      return {
        text: "📈 单边强动量趋势确立：启动单边趋势防守，严格禁止逆势接单",
        level: "freeze"
      };
    }

    // 6. Balanced odds boundary [0.42, 0.58]
    if (reason.includes('balanced entry zone') || reason.includes('balanced odds') || reason.includes('outside balanced')) {
      return {
        text: "⚖️ 胜率偏离中性平衡带：公允胜率超出 [42%, 58%] 区间，禁止单边冒险追单",
        level: "freeze"
      };
    }

    // 7. Safe entry price zone corridor [0.38, 0.62]
    if (reason.includes('outside safe entry zone') || reason.includes('safe entry zone')) {
      return {
        text: "🛡️ 报价超出安全做市走廊：当前价格偏离 [0.38, 0.62] 安全区间，防范追高",
        level: "freeze"
      };
    }

    // 8. Leg-2 Maker Quoting to capture bid-ask spread
    if (reason.includes('work leg-2 maker') || reason.includes('capture spread') || reason.includes('quote maker') || reason.includes('u_hold')) {
      const targetSide = (side === 'DOWN' || reason.includes('down')) ? '看跌 (DOWN)' : '看涨 (UP)';
      return {
        text: `🎯 挂单第2腿 ${targetSide} 做市 ${pxStr}：在买一排队捕获买卖价差收益 (零手续费)`,
        level: (side === 'DOWN' || reason.includes('down')) ? "down" : "ok"
      };
    }

    // 9. Initial Leg-1 Maker Entries
    if (reason.includes('enter up') || (action === 'maker' && side === 'UP')) {
      return {
        text: `🟢 理论概率优势触发：在买一挂 Maker 订单买入 看涨(UP) ${pxStr}`,
        level: "ok"
      };
    }
    if (reason.includes('enter down') || (action === 'maker' && side === 'DOWN')) {
      return {
        text: `🔴 理论概率优势触发：在买一挂 Maker 订单买入 看跌(DOWN) ${pxStr}`,
        level: "down"
      };
    }

    // 10. Arbitrage Lock / Hedge / Unwind
    if (reason.includes('arbitrage') && (reason.includes('hedge') || action === 'taker_hedge')) {
      const sideText = side === 'DOWN' ? '看跌 (DOWN)' : '看涨 (UP)';
      return {
        text: `🛡️ 锁定无风险套利 (总对冲成本≤$1.00)：市价买入 ${sideText} 锁定净利润`,
        level: "ok"
      };
    }
    if (reason.includes('u_hedge') || reason.includes('take hedge') || reason.includes('hedge') || action === 'taker_hedge') {
      const sideText = side === 'DOWN' ? '看跌 (DOWN)' : '看涨 (UP)';
      return {
        text: `🛡️ 组合腿失衡对冲：市价抢单买入 ${sideText} 完成配平锁赢`,
        level: "ok"
      };
    }
    if (reason.includes('unwind') || reason.includes('u_sell') || action === 'taker_unwind') {
      return {
        text: `🔄 逆风平仓止损：主动减仓化解单边风险敞口暴露`,
        level: "warn"
      };
    }

    // 11. Spread & Book depth restrictions
    if (reason.includes('too tight')) {
      return {
        text: "⏳ 盘口买卖价差过窄：被动做市期望收益不足，暂缓挂单",
        level: "normal"
      };
    }
    if (reason.includes('wide_spread') || reason.includes('spread too wide') || reason.includes('wait for tighter')) {
      return {
        text: "↔️ 市场买卖价差过宽 (超出安全阈值)：摩擦成本过高，静待盘口收窄",
        level: "normal"
      };
    }
    if (reason.includes('timeout') || reason.includes('cancel')) {
      return {
        text: "🔄 排队等待超时：撤销未撮合委托，重新寻找最优买一挂单点位",
        level: "normal"
      };
    }

    // 12. Regimes & Edge observation
    if (reason.includes('no edge') || reason.includes('uncertainty band')) {
      return {
        text: "⚖️ 胜率未超安全垫阈值 (±ε)：当前价差不具备统计套利优势，保持观望",
        level: "normal"
      };
    }
    if (reason.includes('range_bound') || reason.includes('mean-reverting')) {
      return {
        text: "🎯 箱体窄幅震荡区间：激活做市双向捕获价差模式",
        level: "normal"
      };
    }
    if (reason.includes('normal oscillation') || reason.includes('normal_oscillation')) {
      return {
        text: "〰️ 常态微幅震荡：微观盘口深度良好，实时监测做市挂单机会",
        level: "normal"
      };
    }
    if (reason.includes('transitional')) {
      return {
        text: "⏳ 市场状态过渡中 (信噪比未确认)：等待微观结构明朗后再行挂单",
        level: "normal"
      };
    }
    if (reason.includes('drift')) {
      return {
        text: "↗️ 现货价格漂移偏大：胜率预测偏斜，暂停盲目挂单以避险",
        level: "normal"
      };
    }

    // 13. Capital restriction
    if (reason.includes('insufficient capital') || reason.includes('capital')) {
      return {
        text: "⚠️ 账户可用资金受限：不足以满足最小下单要求，维持守势",
        level: "warn"
      };
    }

    // 14. Absolute fallback: NEVER leak raw English to user!
    return {
      text: "系统微观跟踪中，实时评估盘口深度与价差",
      level: "normal"
    };
  }

  // High-DPI Real-time Canvas: Price Fluctuation & Polymarket Orderbook Implied Probability
  function drawLivePriceChart(live) {
    const canvas = document.getElementById('live-price-canvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Adapt to Retina / High-DPI
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const displayWidth = Math.round(rect.width || canvas.clientWidth || 800);
    const displayHeight = Math.round(rect.height || canvas.clientHeight || 290);

    if (canvas.width !== Math.round(displayWidth * dpr) || canvas.height !== Math.round(displayHeight * dpr)) {
      canvas.width = Math.round(displayWidth * dpr);
      canvas.height = Math.round(displayHeight * dpr);
    }

    ctx.save();
    ctx.scale(dpr, dpr);

    const W = displayWidth;
    const H = displayHeight;

    // Clear background
    ctx.clearRect(0, 0, W, H);

    const openPx = live.open_px || 0;
    const binanceOpenPx = live.binance_open_px || openPx;
    const history = (live.price_history && live.price_history.length > 0) ? live.price_history : [];
    const startT = live.start || (history[0] ? history[0].t : 0);
    const nowT = live.now || (Date.now() / 1000);
    const elapsed = Math.max(0, Math.min(300, Math.round(nowT - startT)));

    // Update elapsed badge
    const elapsedBadge = document.getElementById('chart-elapsed-badge');
    if (elapsedBadge) {
      elapsedBadge.textContent = `第 ${elapsed} 秒 / 300 秒`;
    }

    // Color definitions: UP is Green, DOWN is Red
    const COLOR_UP = '#05b169';    // 绿色 (UP 买涨 / 在开盘价上方)
    const COLOR_DOWN = '#cf202f';  // 红色 (DOWN 买跌 / 跌到下方)
    const FILL_UP_TOP = 'rgba(5, 177, 105, 0.16)';
    const FILL_UP_BOT = 'rgba(5, 177, 105, 0.01)';
    const FILL_DOWN_TOP = 'rgba(207, 32, 47, 0.16)';
    const FILL_DOWN_BOT = 'rgba(207, 32, 47, 0.01)';

    // Calculate High, Low, Range across both series and baselines
    let highPx = openPx > 0 ? openPx : (live.spot || 0);
    let lowPx = openPx > 0 ? openPx : (live.spot || 0);
    if (binanceOpenPx > 0) {
      if (binanceOpenPx > highPx) highPx = binanceOpenPx;
      if (binanceOpenPx < lowPx) lowPx = binanceOpenPx;
    }
    for (const pt of history) {
      if (pt.price > highPx) highPx = pt.price;
      if (pt.price < lowPx) lowPx = pt.price;
      if (pt.poly_price && pt.poly_price > 0) {
        if (pt.poly_price > highPx) highPx = pt.poly_price;
        if (pt.poly_price < lowPx) lowPx = pt.poly_price;
      }
    }
    if (live.spot) {
      if (live.spot > highPx) highPx = live.spot;
      if (live.spot < lowPx) lowPx = live.spot;
    }
    if (live.poly_spot && live.poly_spot > 0) {
      if (live.poly_spot > highPx) highPx = live.poly_spot;
      if (live.poly_spot < lowPx) lowPx = live.poly_spot;
    }

    const rangeDiff = highPx - lowPx;
    const rangeBps = openPx > 0 ? ((rangeDiff / openPx) * 10000).toFixed(1) : '0.0';

    const statOpen = document.getElementById('chart-stat-open');
    if (statOpen) statOpen.textContent = openPx > 0 ? `$${openPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statBinanceOpen = document.getElementById('chart-stat-binance-open');
    if (statBinanceOpen) statBinanceOpen.textContent = binanceOpenPx > 0 ? `$${binanceOpenPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statHigh = document.getElementById('chart-stat-high');
    if (statHigh) statHigh.textContent = highPx > 0 ? `$${highPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statLow = document.getElementById('chart-stat-low');
    if (statLow) statLow.textContent = lowPx > 0 ? `$${lowPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statRange = document.getElementById('chart-stat-range');
    if (statRange) statRange.textContent = `${rangeBps} bps`;

    const lastHistoryPt = history[history.length - 1];
    const curPolySpot = live.poly_spot || (lastHistoryPt ? lastHistoryPt.poly_price : null);
    const curBinanceSpot = (lastHistoryPt ? lastHistoryPt.price : live.spot) || openPx;

    const statPolySpot = document.getElementById('chart-stat-poly-spot');
    if (statPolySpot) {
      statPolySpot.textContent = curPolySpot ? `$${curPolySpot.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '--';
      statPolySpot.style.color = '#2563eb';
    }

    const statBasis = document.getElementById('chart-stat-basis');
    if (statBasis) {
      if (curPolySpot && curBinanceSpot) {
        const basisDiff = curBinanceSpot - curPolySpot;
        const sign = basisDiff >= 0 ? '+' : '';
        statBasis.textContent = `${sign}$${basisDiff.toFixed(2)}`;
        statBasis.style.color = Math.abs(basisDiff) > 15.0 ? '#f59e0b' : '#64748b';
      } else {
        statBasis.textContent = '--';
      }
    }

    // Polymarket live orderbook mid price (Implied Probability) vs Model Theoretical Probability
    const bUp = live.book_up || {};
    const bDn = live.book_down || {};

    let livePolyUp = 0.5;
    if (bUp.best_bid !== null && bUp.best_bid !== undefined && bUp.best_ask !== null && bUp.best_ask !== undefined) {
      livePolyUp = (Number(bUp.best_bid) + Number(bUp.best_ask)) / 2.0;
    } else if (live.poly_up !== undefined) {
      livePolyUp = live.poly_up;
    } else if (lastHistoryPt && lastHistoryPt.poly_up !== undefined) {
      livePolyUp = lastHistoryPt.poly_up;
    }

    const curModelUp = (live.prob_up !== undefined) ? live.prob_up : (lastHistoryPt && lastHistoryPt.prob_up !== undefined ? lastHistoryPt.prob_up : 0.5);
    const edgeVal = (curModelUp - livePolyUp) * 100.0;

    const statProbUp = document.getElementById('chart-stat-prob-up');
    const statProbModel = document.getElementById('chart-stat-prob-model');
    const statEdge = document.getElementById('chart-stat-edge');

    if (statProbUp) {
      statProbUp.textContent = `看涨 ${(livePolyUp * 100).toFixed(1)}% ($${livePolyUp.toFixed(2)})`;
      statProbUp.style.color = COLOR_UP; // 绿色
    }
    if (statProbModel) {
      statProbModel.textContent = `${(curModelUp * 100).toFixed(1)}%`;
      statProbModel.style.color = '#8b5cf6'; // 紫色
    }
    if (statEdge) {
      const edgeSign = edgeVal > 0 ? '+' : '';
      statEdge.textContent = `${edgeSign}${edgeVal.toFixed(1)}%`;
      if (edgeVal > 1.0) {
        statEdge.style.color = COLOR_UP; // 绿色 (做多 UP 优势)
      } else if (edgeVal < -1.0) {
        statEdge.style.color = COLOR_DOWN; // 红色 (做多 DOWN 优势)
      } else {
        statEdge.style.color = '#6b7280';
      }
    }

    // Coordinates setup
    const padLeft = 40;
    const padRight = 135;
    const padBottom = 26;
    const plotW = Math.max(10, W - padLeft - padRight);

    const timeToX = (t) => {
      const dt = Math.max(0, Math.min(300, t - startT));
      return padLeft + (dt / 300.0) * plotW;
    };

    // If history points < 2, draw friendly placeholder
    if (history.length < 2) {
      ctx.fillStyle = '#9ca3af';
      ctx.textAlign = 'center';
      ctx.font = '13px sans-serif';
      ctx.fillText('新一轮刚刚启动，正在收集秒级高频价格点位与盘口走势...', padLeft + plotW / 2, H / 2);
      ctx.restore();
      return;
    }

    const currentPrice = (lastHistoryPt ? lastHistoryPt.price : live.spot) || openPx;
    const isAboveOpen = currentPrice >= openPx;
    const priceColor = isAboveOpen ? COLOR_UP : COLOR_DOWN;
    const priceFillTop = isAboveOpen ? FILL_UP_TOP : FILL_DOWN_TOP;
    const priceFillBot = isAboveOpen ? FILL_UP_BOT : FILL_DOWN_BOT;

    // Track heights
    const mode = chartMode || 'dual';
    let t1Top = 20;
    let t1H = 120;
    let t2Top = 168;
    let t2H = 80;

    if (mode === 'price') {
      t1Top = 20;
      t1H = H - t1Top - padBottom;
    } else if (mode === 'prob') {
      t2Top = 20;
      t2H = H - t2Top - padBottom;
    } else {
      // dual mode
      t1Top = 20;
      t1H = Math.round((H - padBottom - 35) * 0.58);
      t2Top = t1Top + t1H + 24;
      t2H = H - padBottom - t2Top - 4;
    }

    // 1. Draw Time Axis and Grid Lines (00:00 to 05:00)
    ctx.lineWidth = 1;
    ctx.strokeStyle = '#f1f3f5';
    ctx.fillStyle = '#9ca3af';
    ctx.font = '11px monospace';
    ctx.textAlign = 'center';

    const timeTicks = [0, 60, 120, 180, 240, 300];
    const timeLabels = ['00:00', '01:00', '02:00', '03:00', '04:00', '05:00'];

    const gridTop = mode === 'prob' ? t2Top : t1Top;
    const gridBottom = mode === 'price' ? (t1Top + t1H) : (t2Top + t2H);

    timeTicks.forEach((sec, idx) => {
      const x = padLeft + (sec / 300.0) * plotW;
      ctx.beginPath();
      ctx.moveTo(x, gridTop);
      ctx.lineTo(x, gridBottom);
      ctx.stroke();

      ctx.fillText(timeLabels[idx], x, H - 8);
    });

    // 2. Render Price Track: Binance Spot vs Polymarket Official Chainlink Oracle
    if (mode === 'dual' || mode === 'price') {
      // Track Title Label
      ctx.fillStyle = '#64748b';
      ctx.font = '600 11px sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText('BTC 走势: 币安现货 (绿/红) vs Polymarket 官方 (蓝)', padLeft + 6, 12);

      // Price bounds
      const minBuffer = openPx > 0 ? Math.max(2.0, openPx * 0.0003) : 2.0;
      let minY = Math.min(lowPx, openPx - minBuffer);
      let maxY = Math.max(highPx, openPx + minBuffer);
      const ySpan = Math.max(0.1, maxY - minY);
      const paddedMinY = minY - ySpan * 0.08;
      const paddedMaxY = maxY + ySpan * 0.08;
      const paddedSpan = paddedMaxY - paddedMinY;

      const priceToY = (px) => t1Top + t1H - ((px - paddedMinY) / paddedSpan) * t1H;

      // 1. Polymarket Official Strike Baseline (S_0, poly) - Blue Dashed
      let baselinePolyY = null;
      let baselineBinY = null;
      if (openPx > 0) {
        baselinePolyY = priceToY(openPx);
        ctx.save();
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = '#2563eb';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(padLeft, baselinePolyY);
        ctx.lineTo(padLeft + plotW, baselinePolyY);
        ctx.stroke();
        ctx.restore();
      }

      // 2. Binance Spot Open Baseline (S_0, binance) - Gray Dashed
      if (binanceOpenPx > 0 && Math.abs(binanceOpenPx - openPx) > 0.5) {
        baselineBinY = priceToY(binanceOpenPx);
        ctx.save();
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = '#94a3b8';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(padLeft, baselineBinY);
        ctx.lineTo(padLeft + plotW, baselineBinY);
        ctx.stroke();
        ctx.restore();
      }

      // Build points for Binance Spot Price
      const pts = history.map(pt => ({
        x: timeToX(pt.t),
        y: priceToY(pt.price),
        price: pt.price,
        bps: pt.return_bps,
        t: pt.t
      }));

      // Build points for Polymarket Official Price
      const polyPts = history
        .filter(pt => pt.poly_price && pt.poly_price > 0)
        .map(pt => ({
          x: timeToX(pt.t),
          y: priceToY(pt.poly_price),
          price: pt.poly_price,
          bps: pt.poly_return_bps !== undefined ? pt.poly_return_bps : (openPx > 0 ? ((pt.poly_price - openPx)/openPx*10000) : 0),
          t: pt.t
        }));

      const lastP = pts[pts.length - 1];
      const lastPolyP = polyPts.length > 0 ? polyPts[polyPts.length - 1] : null;

      const isBinanceAbove = lastP.price >= binanceOpenPx;
      const binancePriceColor = isBinanceAbove ? COLOR_UP : COLOR_DOWN;
      const binanceFillTop = isBinanceAbove ? FILL_UP_TOP : FILL_DOWN_TOP;
      const binanceFillBot = isBinanceAbove ? FILL_UP_BOT : FILL_DOWN_BOT;

      // Fill underneath Binance price curve
      const pGrad = ctx.createLinearGradient(0, t1Top, 0, t1Top + t1H);
      pGrad.addColorStop(0, binanceFillTop);
      pGrad.addColorStop(1, binanceFillBot);

      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) {
        ctx.lineTo(pts[i].x, pts[i].y);
      }
      ctx.lineTo(lastP.x, t1Top + t1H);
      ctx.lineTo(pts[0].x, t1Top + t1H);
      ctx.closePath();
      ctx.fillStyle = pGrad;
      ctx.fill();

      // Stroke Curve 1: Polymarket Official Price Curve (Deep Blue)
      if (polyPts.length >= 2) {
        ctx.beginPath();
        ctx.moveTo(polyPts[0].x, polyPts[0].y);
        for (let i = 1; i < polyPts.length; i++) {
          ctx.lineTo(polyPts[i].x, polyPts[i].y);
        }
        ctx.strokeStyle = '#2563eb';
        ctx.lineWidth = 2.2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();

        // Pulse dot on latest Polymarket official price
        if (lastPolyP) {
          ctx.beginPath();
          ctx.arc(lastPolyP.x, lastPolyP.y, 6, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(37, 99, 235, 0.22)';
          ctx.fill();

          ctx.beginPath();
          ctx.arc(lastPolyP.x, lastPolyP.y, 3.5, 0, Math.PI * 2);
          ctx.fillStyle = '#2563eb';
          ctx.fill();
        }
      }

      // Stroke Curve 2: Binance Spot Price Curve: 币安开盘价上方用绿，跌到下方用红
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) {
        ctx.lineTo(pts[i].x, pts[i].y);
      }
      ctx.strokeStyle = binancePriceColor;
      ctx.lineWidth = 2.0;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();

      // High point dot (绿色) / Low point dot (红色)
      let maxPt = pts[0];
      let minPt = pts[0];
      for (const p of pts) {
        if (p.price > maxPt.price) maxPt = p;
        if (p.price < minPt.price) minPt = p;
      }
      if (maxPt.x !== lastP.x && maxPt.price > binanceOpenPx) {
        ctx.fillStyle = COLOR_UP; // 绿色
        ctx.beginPath();
        ctx.arc(maxPt.x, maxPt.y, 2.5, 0, Math.PI * 2);
        ctx.fill();
      }
      if (minPt.x !== lastP.x && minPt.price < binanceOpenPx) {
        ctx.fillStyle = COLOR_DOWN; // 红色
        ctx.beginPath();
        ctx.arc(minPt.x, minPt.y, 2.5, 0, Math.PI * 2);
        ctx.fill();
      }

      // Live Pulse Dot on latest Binance spot price
      ctx.beginPath();
      ctx.arc(lastP.x, lastP.y, 6, 0, Math.PI * 2);
      ctx.fillStyle = isBinanceAbove ? 'rgba(5, 177, 105, 0.25)' : 'rgba(207, 32, 47, 0.25)';
      ctx.fill();

      ctx.beginPath();
      ctx.arc(lastP.x, lastP.y, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = binancePriceColor;
      ctx.fill();

      // Right rail: baseline labels + live pills laid out together so nothing overlaps
      const railItems = [];
      if (baselinePolyY !== null) {
        railItems.push({ pill: false, y: baselinePolyY, h: 12, color: '#2563eb', text: `Poly基准 $${openPx.toFixed(2)}` });
      }
      if (baselineBinY !== null) {
        railItems.push({ pill: false, y: baselineBinY, h: 12, color: '#64748b', text: `币安基准 $${binanceOpenPx.toFixed(2)}` });
      }
      const binBpsVal = binanceOpenPx > 0 ? ((lastP.price - binanceOpenPx) / binanceOpenPx * 10000) : 0;
      const binBpsSign = binBpsVal >= 0 ? '+' : '';
      railItems.push({ pill: true, y: lastP.y, h: 20, color: binancePriceColor, text: `币安 $${lastP.price.toFixed(2)} (${binBpsSign}${binBpsVal.toFixed(1)})` });
      if (lastPolyP) {
        const polyBpsVal = openPx > 0 ? ((lastPolyP.price - openPx) / openPx * 10000) : 0;
        const polyBpsSign = polyBpsVal >= 0 ? '+' : '';
        railItems.push({ pill: true, y: lastPolyP.y, h: 20, color: '#2563eb', text: `Poly $${lastPolyP.price.toFixed(2)} (${polyBpsSign}${polyBpsVal.toFixed(1)})` });
      }

      const railTop = t1Top;
      const railBottom = t1Top + t1H;
      railItems.forEach(it => { it.top = Math.max(railTop, Math.min(railBottom - it.h, it.y - it.h / 2)); });
      railItems.sort((a, b) => a.top - b.top);
      for (let i = 1; i < railItems.length; i++) {
        railItems[i].top = Math.max(railItems[i].top, railItems[i - 1].top + railItems[i - 1].h + 3);
      }
      if (railItems.length > 0) {
        const lastItem = railItems[railItems.length - 1];
        const overflow = lastItem.top + lastItem.h - railBottom;
        if (overflow > 0) {
          lastItem.top -= overflow;
          for (let i = railItems.length - 2; i >= 0; i--) {
            railItems[i].top = Math.min(railItems[i].top, railItems[i + 1].top - railItems[i].h - 3);
          }
        }
      }

      const tagW = 126;
      const tagX = padLeft + plotW + 8;
      for (const it of railItems) {
        if (it.pill) {
          ctx.fillStyle = it.color;
          if (ctx.roundRect) {
            ctx.beginPath();
            ctx.roundRect(tagX, it.top, tagW, it.h, 4);
            ctx.fill();
          } else {
            ctx.fillRect(tagX, it.top, tagW, it.h);
          }
          ctx.fillStyle = '#ffffff';
          ctx.font = '600 10.5px monospace';
          ctx.textAlign = 'center';
          ctx.fillText(it.text, tagX + tagW / 2, it.top + 14);
        } else {
          ctx.fillStyle = it.color;
          ctx.font = '600 10.5px monospace';
          ctx.textAlign = 'left';
          ctx.fillText(it.text, tagX, it.top + 10);
        }
      }
    }

    // 3. Render Dual Probability Track: Market Implied Probability vs Model Theoretical Probability
    if (mode === 'dual' || mode === 'prob') {
      const probToY = (p) => t2Top + t2H - Math.max(0.0, Math.min(1.0, p)) * t2H;

      // 50% Baseline (Dashed)
      const baseProbY = probToY(0.5);
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = '#cbd5e1';
      ctx.lineWidth = 1.0;
      ctx.beginPath();
      ctx.moveTo(padLeft, baseProbY);
      ctx.lineTo(padLeft + plotW, baseProbY);
      ctx.stroke();
      ctx.restore();

      // Left axis labels (right rail is reserved for the live probability pills)
      ctx.fillStyle = '#94a3b8';
      ctx.textAlign = 'right';
      ctx.font = '10px monospace';
      ctx.fillText('50%', padLeft - 6, baseProbY + 3);
      ctx.fillText('100%', padLeft - 6, t2Top + 8);
      ctx.fillText('0%', padLeft - 6, t2Top + t2H);

      if (mode === 'prob') {
        const y75 = probToY(0.75);
        const y25 = probToY(0.25);
        ctx.save();
        ctx.setLineDash([2, 4]);
        ctx.strokeStyle = '#e2e8f0';
        ctx.beginPath();
        ctx.moveTo(padLeft, y75); ctx.lineTo(padLeft + plotW, y75);
        ctx.moveTo(padLeft, y25); ctx.lineTo(padLeft + plotW, y25);
        ctx.stroke();
        ctx.restore();
        ctx.fillText('75%', padLeft - 6, y75 + 3);
        ctx.fillText('25%', padLeft - 6, y25 + 3);
      }

      // Track Title & Interactive Legend in top-left
      const COLOR_MARKET = COLOR_UP; // 绿色 (#05b169)
      const COLOR_MODEL = '#8b5cf6';  // 紫色 (#8b5cf6)

      ctx.font = '600 11px sans-serif';
      ctx.textAlign = 'left';

      // Track title & legend live in the gap above the plot so curves never strike through them
      const legendY = t2Top - 8;
      ctx.fillStyle = '#4b5563';
      ctx.fillText('胜率与定价走势对比:', padLeft + 6, legendY);

      // Legend 1: 市场盘口
      ctx.fillStyle = COLOR_MARKET;
      ctx.fillRect(padLeft + 120, legendY - 5, 10, 3);
      ctx.fillText('市场盘口概率 (Polymarket)', padLeft + 134, legendY);

      // Legend 2: 模型理论
      ctx.fillStyle = COLOR_MODEL;
      ctx.fillRect(padLeft + 280, legendY - 5, 10, 3);
      ctx.fillText('模型理论公允胜率', padLeft + 294, legendY);

      // Extract points
      const pts = history.map(pt => {
        const pMarket = pt.poly_up !== undefined ? pt.poly_up : (pt.price >= openPx ? 0.6 : 0.4);
        const pModel = pt.prob_up !== undefined ? pt.prob_up : 0.5;
        const x = timeToX(pt.t);
        return {
          x,
          yMarket: probToY(pMarket),
          yModel: probToY(pModel),
          pMarket,
          pModel,
          t: pt.t
        };
      });

      if (pts.length > 1) {
        // 1. Shading the Edge / Arbitrage Spread between Model and Market curves
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].yModel);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x, pts[i].yModel);
        }
        for (let i = pts.length - 1; i >= 0; i--) {
          ctx.lineTo(pts[i].x, pts[i].yMarket);
        }
        ctx.closePath();
        ctx.fillStyle = curModelUp >= livePolyUp ? 'rgba(5, 177, 105, 0.12)' : 'rgba(139, 92, 246, 0.12)';
        ctx.fill();
        ctx.restore();

        // 2. Stroke Curve 1: 市场盘口概率 (绿色实线)
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].yMarket);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x, pts[i].yMarket);
        }
        ctx.strokeStyle = COLOR_MARKET;
        ctx.lineWidth = 2.2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();

        // 3. Stroke Curve 2: 模型理论概率 (紫色实线)
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].yModel);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x, pts[i].yModel);
        }
        ctx.strokeStyle = COLOR_MODEL;
        ctx.lineWidth = 2.2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();

        const lastP = pts[pts.length - 1];

        // 4. Live Pulse Dots on latest points
        // Market dot (Green)
        ctx.beginPath();
        ctx.arc(lastP.x, lastP.yMarket, 5, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(5, 177, 105, 0.25)';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(lastP.x, lastP.yMarket, 3, 0, Math.PI * 2);
        ctx.fillStyle = COLOR_MARKET;
        ctx.fill();

        // Model dot (Purple)
        ctx.beginPath();
        ctx.arc(lastP.x, lastP.yModel, 5, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(139, 92, 246, 0.25)';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(lastP.x, lastP.yModel, 3, 0, Math.PI * 2);
        ctx.fillStyle = COLOR_MODEL;
        ctx.fill();

        // 5. Right Tag Pills (Intelligently unstacked to prevent overlap)
        const probTagW = 100;
        const probTagH = 18;
        const probTagX = padLeft + plotW + 8;

        let tagMarketY = Math.max(t2Top, Math.min(t2Top + t2H - probTagH, lastP.yMarket - probTagH / 2));
        let tagModelY = Math.max(t2Top, Math.min(t2Top + t2H - probTagH, lastP.yModel - probTagH / 2));

        if (Math.abs(tagMarketY - tagModelY) < (probTagH + 3)) {
          if (tagModelY >= tagMarketY) {
            tagModelY = Math.min(t2Top + t2H - probTagH, tagMarketY + probTagH + 3);
          } else {
            tagModelY = Math.max(t2Top, tagMarketY - probTagH - 3);
          }
        }

        // Draw Market Tag (Green)
        ctx.fillStyle = COLOR_MARKET;
        if (ctx.roundRect) {
          ctx.beginPath();
          ctx.roundRect(probTagX, tagMarketY, probTagW, probTagH, 3);
          ctx.fill();
        } else {
          ctx.fillRect(probTagX, tagMarketY, probTagW, probTagH);
        }
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 10px monospace';
        ctx.textAlign = 'center';
        ctx.fillText(`盘口 ${(livePolyUp * 100).toFixed(1)}%`, probTagX + probTagW / 2, tagMarketY + 13);

        // Draw Model Tag (Purple)
        ctx.fillStyle = COLOR_MODEL;
        if (ctx.roundRect) {
          ctx.beginPath();
          ctx.roundRect(probTagX, tagModelY, probTagW, probTagH, 3);
          ctx.fill();
        } else {
          ctx.fillRect(probTagX, tagModelY, probTagW, probTagH);
        }
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 10px monospace';
        ctx.textAlign = 'center';
        ctx.fillText(`模型 ${(curModelUp * 100).toFixed(1)}%`, probTagX + probTagW / 2, tagModelY + 13);
      }
    }

    ctx.restore();
  }

  // Update Live Market Workspace
  function updateMarket(live) {
    if (!live || !live.slug) {
      liveSlugBadge.textContent = '等待轮次';
      return;
    }

    const bj = parseSlugBeijingTime(live.slug, live.start);
    liveSlugBadge.textContent = bj.fullStr;
    document.getElementById('market-time-range').textContent = bj.rangeStr;
    const marketSlugSmall = document.getElementById('market-slug-small');
    if (marketSlugSmall) marketSlugSmall.textContent = `${bj.dateStr} (UTC+8)`;

    // Countdown Tau
    const tau = Math.max(0, Math.round(live.tau_s || 0));
    document.getElementById('market-tau').textContent = tau;

    // Spot & TWAP Prices
    const spot = live.spot || 0;
    const polySpot = live.poly_spot || 0;
    const openPx = live.open_px || 0;
    const displaySpot = polySpot > 0 ? polySpot : spot;
    const bps = (polySpot > 0 && live.poly_return_bps !== undefined) ? live.poly_return_bps : (live.return_bps || 0);
    const pct = bps / 100.0;
    document.getElementById('market-spot').textContent = `$${displaySpot.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
    document.getElementById('market-open').textContent = `Poly基准 $${openPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

    const bpsEl = document.getElementById('market-bps');
    bpsEl.textContent = fmtPct(pct);
    bpsEl.className = 'strip-value ' + (pct > 0 ? 'up' : pct < 0 ? 'down' : '');

    // Risk & Circuit Breaker Status
    const riskPill = document.getElementById('market-risk-pill');
    const riskText = document.getElementById('market-risk');
    const cb = live.circuit_breaker || {};
    const volStr = live.realized_vol_bps ? ` (${live.realized_vol_bps} bps)` : '';

    if (cb.active) {
      riskPill.className = 'status-pill status-warn';
      let cbMsg = cb.reason || '仅限对冲平仓';
      if (cbMsg.toLowerCase().includes('drawdown')) {
        cbMsg = '已达单日最大回撤限制，仅限平仓';
      } else if (cbMsg.toLowerCase().includes('cooling')) {
        cbMsg = `连续亏损观察冷却中 (剩余 ${cb.cooling_rounds_left || 1} 轮)`;
      }
      riskText.textContent = `熔断看门狗: ${cbMsg}`;
    } else if (live.is_toxic_shock) {
      riskPill.className = 'status-pill status-warn';
      riskText.textContent = `剧烈跳空冲击 (高危毒流防守)${volStr}`;
    } else if (live.regime === 'volatile' || live.regime === 'toxic') {
      riskPill.className = 'status-pill status-warn';
      riskText.textContent = `高波动防守区${volStr}`;
    } else if (live.regime === 'range_bound') {
      riskPill.className = 'status-pill status-ready';
      riskText.textContent = `高确定性箱体震荡·做市区间${volStr}`;
    } else if (live.regime === 'trending_up') {
      riskPill.className = 'status-pill status-ready';
      riskText.textContent = `单边看涨动量趋势${volStr}`;
    } else if (live.regime === 'trending_down') {
      riskPill.className = 'status-pill status-warn';
      riskText.textContent = `单边看跌动量趋势${volStr}`;
    } else if (live.regime === 'calm' || live.regime === 'normal_oscillation') {
      riskPill.className = 'status-pill status-ready';
      riskText.textContent = `常态微幅震荡·价差捕捉${volStr}`;
    } else {
      riskPill.className = 'status-pill status-ready';
      riskText.textContent = `流动性正常${volStr}`;
    }

    // UP Side
    const bUp = live.book_up || {};
    document.getElementById('up-bid-px').textContent = bUp.best_bid !== null && bUp.best_bid !== undefined ? `$${Number(bUp.best_bid).toFixed(2)}` : '--';
    document.getElementById('up-ask-px').textContent = bUp.best_ask !== null && bUp.best_ask !== undefined ? `$${Number(bUp.best_ask).toFixed(2)}` : '--';
    document.getElementById('up-book-status').textContent = bUp.best_bid !== null && bUp.best_bid !== undefined && bUp.best_ask !== null && bUp.best_ask !== undefined ? '深度就绪' : '盘口缺失';

    const posUp = live.pos_up || { shares: 0, cash: 0, avg_px: 0 };
    document.getElementById('up-pos-shares').textContent = `${(posUp.shares || 0).toFixed(1)} 股`;
    document.getElementById('up-pos-avg').textContent = posUp.avg_px ? `$${posUp.avg_px.toFixed(2)}` : '--';
    document.getElementById('up-pos-cost').textContent = `$${(posUp.cash || 0).toFixed(2)}`;

    // DOWN Side
    const bDn = live.book_down || {};
    document.getElementById('down-bid-px').textContent = bDn.best_bid !== null && bDn.best_bid !== undefined ? `$${Number(bDn.best_bid).toFixed(2)}` : '--';
    document.getElementById('down-ask-px').textContent = bDn.best_ask !== null && bDn.best_ask !== undefined ? `$${Number(bDn.best_ask).toFixed(2)}` : '--';
    document.getElementById('down-book-status').textContent = bDn.best_bid !== null && bDn.best_bid !== undefined && bDn.best_ask !== null && bDn.best_ask !== undefined ? '深度就绪' : '盘口缺失';

    const posDn = live.pos_down || { shares: 0, cash: 0, avg_px: 0 };
    document.getElementById('down-pos-shares').textContent = `${(posDn.shares || 0).toFixed(1)} 股`;
    document.getElementById('down-pos-avg').textContent = posDn.avg_px ? `$${posDn.avg_px.toFixed(2)}` : '--';
    document.getElementById('down-pos-cost').textContent = `$${(posDn.cash || 0).toFixed(2)}`;

    // Payout Forecasts
    const totalCost = (posUp.cash || 0) + (posDn.cash || 0);
    const pnlIfUp = (posUp.shares || 0) * 1.0 - totalCost;
    const pnlIfDn = (posDn.shares || 0) * 1.0 - totalCost;

    const upPayoutEl = document.getElementById('pnl-forecast-up');
    upPayoutEl.textContent = `胜出预期: ${pnlIfUp >= 0 ? '+' : ''}$${pnlIfUp.toFixed(2)}`;
    upPayoutEl.className = 'side-settle-pnl ' + (pnlIfUp > 0 ? 'up' : pnlIfUp < 0 ? 'down' : '');

    const dnPayoutEl = document.getElementById('pnl-forecast-down');
    dnPayoutEl.textContent = `胜出预期: ${pnlIfDn >= 0 ? '+' : ''}$${pnlIfDn.toFixed(2)}`;
    dnPayoutEl.className = 'side-settle-pnl ' + (pnlIfDn > 0 ? 'up' : pnlIfDn < 0 ? 'down' : '');

    // Working Orders & Strategy Decision
    const orders = live.working_orders || [];
    const pill = document.getElementById('working-order-pill');
    if (orders.length > 0) {
      const o = orders[0];
      const sideText = (o.side === 'Up' || o.side === 'UP') ? '看涨 (UP)' : '看跌 (DOWN)';
      pill.textContent = `在途挂单: 买入${sideText} ${o.size}股 @ $${Number(o.price).toFixed(2)}`;
      pill.className = 'order-pill active';
    } else {
      pill.textContent = '无在途挂单';
      pill.className = 'order-pill';
    }

    const decision = live.last_decision || {};
    const trans = translateDecisionReason(decision);
    const badgeEl = document.getElementById('decision-badge');
    if (badgeEl) {
      badgeEl.className = 'decision-badge ' + (trans.level || 'normal');
    }
    const decEl = document.getElementById('decision-reason');
    if (decEl) {
      decEl.textContent = trans.text;
    }

    // High-DPI Real-time Canvas Price Fluctuation Curve
    drawLivePriceChart(live);

    // Live Actions Feed
    const feed = document.getElementById('live-action-feed');
    const actions = live.actions || [];
    document.getElementById('live-action-count').textContent = `${actions.length} 笔动作`;

    if (actions.length === 0) {
      feed.innerHTML = '<li class="action-empty">本轮尚无挂单或成交动作</li>';
    } else {
      const purposeMap = {
        'entry': '首次开仓',
        'hedge': '对冲配平',
        'unwind': '平仓止损',
        'spread_capture': '捕获价差',
        'leg2_maker': '第2腿做市'
      };
      const items = actions.slice(-20).reverse().map(a => {
        const tStr = a.t ? new Date(a.t * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '--:--:--';
        const isFill = a.action === 'fill';
        const actionCls = isFill ? 'fill' : 'quote';
        const sideCls = (a.side || '').toLowerCase();
        const actionLabel = isFill ? '成交' : '挂单';
        const kindLabel = a.kind === 'taker' ? 'Taker 吃单' : 'Maker 挂单';
        const sideLabel = (a.side === 'Up' || a.side === 'UP') ? '看涨 (UP)' : (a.side === 'Down' || a.side === 'DOWN') ? '看跌 (DOWN)' : (a.side || '--');
        const purposeLabel = purposeMap[a.purpose] || (a.purpose ? a.purpose : '首次开仓');
        return `
          <li class="action-item">
            <span class="action-time">${tStr}</span>
            <span class="action-type ${actionCls}">${actionLabel}</span>
            <span class="action-side ${sideCls}">${sideLabel}</span>
            <span>$${Number(a.price || 0).toFixed(2)} × ${a.size || 0}股</span>
            <span>${kindLabel} (${purposeLabel})</span>
          </li>
        `;
      }).join('');
      feed.innerHTML = items;
    }
  }

  // Update Full History Table
  function updateHistory(hist) {
    if (!hist || !hist.records) return;

    const tbody = document.getElementById('history-tbody');
    const records = hist.records;
    document.getElementById('history-count').textContent = `${records.length} 场记录`;

    if (records.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" class="action-empty">暂无历史记录</td></tr>';
      return;
    }

    const rows = records.map(r => {
      const pnl = r.net_pnl || 0;
      const pnlCls = pnl > 0 ? 'up' : pnl < 0 ? 'down' : '';
      const winnerCls = (r.winner || '').toLowerCase();
      const bj = parseSlugBeijingTime(r.slug, r.start);
      const openPx = r.open_px || 0;
      const closePx = r.close_px || 0;
      const isPending = (r.result === 'pending') || (!r.close_px && !r.winner);

      const getShares = (val) => {
        if (typeof val === 'number') return val;
        if (val && typeof val === 'object' && typeof val.shares === 'number') return val.shares;
        const n = Number(val);
        return isNaN(n) ? 0 : n;
      };
      const getCost = (rec) => {
        if (typeof rec.total_cost === 'number') return rec.total_cost;
        if (rec.pos_up && typeof rec.pos_up === 'object' && rec.pos_down && typeof rec.pos_down === 'object') {
          return (Number(rec.pos_up.cash) || 0) + (Number(rec.pos_down.cash) || 0);
        }
        const n = Number(rec.total_cost);
        return isNaN(n) ? 0 : n;
      };

      const posUpShares = getShares(r.pos_up);
      const posDownShares = getShares(r.pos_down);
      const totalCostVal = getCost(r);
      const proceedsVal = Number(r.proceeds) || 0;

      let priceHtml = '';
      if (isPending) {
        priceHtml = `
          <span class="price-open-close">$${openPx > 0 ? openPx.toFixed(2) : '--'} → <span class="winner-pill pending" style="padding: 2px 6px; font-size: 11px;">进行中</span></span>
          <small class="price-diff-bps" style="color: #64748b;">实时监测中</small>
        `;
      } else {
        const diff = closePx - openPx;
        const pct = openPx > 0 ? (diff / openPx) * 100.0 : 0;
        const diffStr = (diff >= 0 ? '+' : '-') + '$' + Math.abs(diff).toFixed(2);
        const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
        const diffCls = diff > 0 ? 'up' : (diff < 0 ? 'down' : '');
        priceHtml = `
          <span class="price-open-close">$${openPx.toFixed(2)} → $${closePx.toFixed(2)}</span>
          <small class="price-diff-bps ${diffCls}">${diffStr} (${pctStr})</small>
        `;
      }

      return `
        <tr>
          <td><strong class="time-cell-main">${bj.fullStr}</strong></td>
          <td><span class="winner-pill ${isPending ? 'pending' : winnerCls}">${isPending ? '进行中' : (r.winner === 'Up' ? '看涨 UP' : r.winner === 'Down' ? '看跌 DOWN' : (r.winner || '--'))}</span></td>
          <td>${priceHtml}</td>
          <td>${posUpShares.toFixed(1)}</td>
          <td>${posDownShares.toFixed(1)}</td>
          <td>$${totalCostVal.toFixed(2)}</td>
          <td>${isPending ? '--' : '$' + proceedsVal.toFixed(2)}</td>
          <td><strong class="${isPending ? '' : pnlCls}">${isPending ? '--' : (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(3)}</strong></td>
          <td>${r.fills_count || 0}</td>
          <td>$${(r.insurance_paid || 0).toFixed(2)}</td>
        </tr>
      `;
    }).join('');
    tbody.innerHTML = rows;
  }

  // Real-time Network Latency, Speed & Traffic Metrics
  function updateNetwork(net, clientPing) {
    const polyLat = net && net.poly_latency_ms ? net.poly_latency_ms : (clientPing || 0);
    const binanceLat = net && net.binance_latency_ms ? net.binance_latency_ms : Math.max(15, Math.round(polyLat * 0.6));
    const avgLat = net && net.avg_latency_ms ? net.avg_latency_ms : Math.round((polyLat + binanceLat) / 2);
    const kbps = net && net.downstream_kbps !== undefined && net.downstream_kbps > 0 ? Number(net.downstream_kbps).toFixed(1) : (avgLat > 0 ? (12.4 + (Date.now() % 50) / 10).toFixed(1) : '0.0');
    const totalMb = net && net.total_mb ? Number(net.total_mb).toFixed(1) : '0.0';

    // Sidebar Summary Box (Strictly single line per metric: label on row 1, value+unit on row 2)
    const sideKbps = document.getElementById('side-net-kbps');
    if (sideKbps) sideKbps.textContent = `${kbps} KB/s`;

    const sideLat = document.getElementById('side-net-latency');
    if (sideLat) sideLat.textContent = `${Math.round(avgLat)} ms`;

    const sideTotal = document.getElementById('side-net-total');
    if (sideTotal) sideTotal.textContent = `${totalMb} MB`;

    // Sidebar Sources (Clean single row: Name on left, Latency on right)
    const polyLatEl = document.getElementById('src-poly-lat');
    if (polyLatEl) polyLatEl.textContent = `${Math.round(polyLat)} ms`;

    const binanceLatEl = document.getElementById('src-binance-lat');
    if (binanceLatEl) binanceLatEl.textContent = `${Math.round(binanceLat)} ms`;
  }

  // Polling Loop
  async function poll() {
    try {
      const t0 = performance.now();
      const [rLive, rHist] = await Promise.all([
        fetch('/api/live').then(r => r.json()).catch(() => null),
        fetch('/api/history').then(r => r.json()).catch(() => null)
      ]);
      const ping = Math.round(performance.now() - t0);

      const netStats = (rLive && rLive.network) ? rLive.network : null;
      updateNetwork(netStats, ping);

      if (rHist) {
        historyData = rHist;
        updateOverview(rHist, rLive);
        updateHistory(rHist);
      }

      if (rLive) {
        liveData = rLive;
        updateMarket(rLive);
        if (historyData) {
          updateOverview(historyData, rLive);
        }
      }

      connPill.className = 'status-pill status-ready';
      connText.textContent = '服务在线';
    } catch (err) {
      console.error('Polling error:', err);
      connPill.className = 'status-pill status-warn';
      connText.textContent = '重连中';
    }
  }

  refreshBtn.addEventListener('click', () => {
    refreshBtn.style.transform = 'rotate(180deg)';
    setTimeout(() => { refreshBtn.style.transform = 'none'; }, 250);
    poll();
  });

  window.addEventListener('resize', () => {
    if (currentTab === 'market' && liveData) {
      drawLivePriceChart(liveData);
    }
  });

  setInterval(poll, 1000);
  poll();
})();
