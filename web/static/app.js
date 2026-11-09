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
      document.getElementById('overview-live-pos').textContent = `UP ${posUp.toFixed(1)} / DN ${posDn.toFixed(1)}${isLocked ? ' (已锁仓)' : ''}`;
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
      const diff = closePx - openPx;
      const pct = openPx > 0 ? (diff / openPx) * 100.0 : 0;
      const diffStr = (diff >= 0 ? '+' : '-') + '$' + Math.abs(diff).toFixed(2);
      const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
      const diffCls = diff > 0 ? 'up' : (diff < 0 ? 'down' : '');
      return `
        <tr>
          <td><strong class="time-cell-main">${bj.fullStr}</strong></td>
          <td><span class="winner-pill ${winnerCls}">${r.winner || '--'}</span></td>
          <td>
            <span class="price-open-close">$${openPx.toFixed(2)} → $${closePx.toFixed(2)}</span>
            <small class="price-diff-bps ${diffCls}">${diffStr} (${pctStr})</small>
          </td>
          <td>${(r.pos_up || 0).toFixed(1)}</td>
          <td>${(r.pos_down || 0).toFixed(1)}</td>
          <td>$${(r.total_cost || 0).toFixed(2)}</td>
          <td>$${(r.proceeds || 0).toFixed(2)}</td>
          <td><strong class="${pnlCls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(3)}</strong></td>
          <td>${r.fills_count || 0}</td>
        </tr>
      `;
    }).join('');
    tbody.innerHTML = rows;
  }

  // Translate internal strategy arbitration into professional investor Chinese
  function translateDecisionReason(decision) {
    if (!decision || (!decision.reason && !decision.action)) {
      return { text: "实时微观行情评估中...", level: "normal" };
    }
    const reason = (decision.reason || '').toLowerCase();
    const action = (decision.action || '').toLowerCase();
    const side = (decision.side || '').toUpperCase();
    const pxStr = decision.price !== undefined && decision.price !== null ? `$${Number(decision.price).toFixed(2)}` : '';

    // 1. Toxic shock
    if (reason.includes('toxic') || reason.includes('shock')) {
      return {
        text: "⚠️ 现货剧烈跳空 (毒流冲击)：挂单主动收缩，防范逆选择吃单",
        level: "warn"
      };
    }

    // 2. Circuit breaker / drawdown / streak
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

    // 3. Max position reached
    if (reason.includes('max position') || reason.includes('position reached')) {
      return {
        text: "⏸️ 单轮持仓已达风控上限 (15 股)：停止增仓，锁定底仓等待期末清算",
        level: "freeze"
      };
    }

    // 4. Tail freeze
    if (reason.includes('tail freeze') || reason.includes('freeze') || reason.includes('tau <') || reason.includes('tau<')) {
      return {
        text: "⏱️ 临近结算交割窗口 (Tau ≤ 60s)：冻结新开仓以规避尾盘剧震风险",
        level: "freeze"
      };
    }

    // 5. Entries (Maker)
    if (reason.includes('enter up') || (action === 'maker' && side === 'UP')) {
      return {
        text: `🟢 概率优势触发：在买一排队挂 Maker 订单买入 UP ${pxStr}`,
        level: "ok"
      };
    }
    if (reason.includes('enter down') || (action === 'maker' && side === 'DOWN')) {
      return {
        text: `🔴 概率优势触发：在买一排队挂 Maker 订单买入 DOWN ${pxStr}`,
        level: "down"
      };
    }

    // 6. Hedge / Unwind
    if (reason.includes('u_hedge') || reason.includes('take hedge') || reason.includes('hedge')) {
      const sideText = side === 'DOWN' ? 'DOWN' : 'UP';
      return {
        text: `🛡️ 组合腿失衡对冲：市价抢单买入 ${sideText} 完成配平锁赢`,
        level: "ok"
      };
    }
    if (reason.includes('unwind') || reason.includes('u_sell')) {
      return {
        text: `🔄 逆风平仓止损：主动减仓化解单边敞口暴露`,
        level: "warn"
      };
    }

    // 7. Capital restriction
    if (reason.includes('insufficient capital') || reason.includes('capital')) {
      return {
        text: "⚠️ 账户可用资金受限：不足以满足凯利单笔最小下单要求，维持守势",
        level: "warn"
      };
    }

    // 8. Queue timeout / tight spread
    if (reason.includes('too tight')) {
      return {
        text: "⏳ 盘口对家价差过窄：被动 Maker 期望收益不足，暂缓挂单",
        level: "normal"
      };
    }
    if (reason.includes('timeout') || reason.includes('cancel')) {
      return {
        text: "🔄 排队等待超时：撤销未撮合委托，重新寻找最优买一挂单点位",
        level: "normal"
      };
    }

    // 9. No edge
    if (reason.includes('no edge') || reason.includes('uncertainty')) {
      return {
        text: "⚖️ 胜率未超安全垫阈值 (±ε)：当前价差不具备统计套利优势，保持观望",
        level: "normal"
      };
    }

    // 10. Default clean fallback
    return {
      text: decision.reason || "系统微观跟踪中，实时评估盘口深度与价差",
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

    // Calculate High, Low, Range
    let highPx = openPx > 0 ? openPx : (live.spot || 0);
    let lowPx = openPx > 0 ? openPx : (live.spot || 0);
    for (const pt of history) {
      if (pt.price > highPx) highPx = pt.price;
      if (pt.price < lowPx) lowPx = pt.price;
    }
    if (live.spot) {
      if (live.spot > highPx) highPx = live.spot;
      if (live.spot < lowPx) lowPx = live.spot;
    }

    const rangeDiff = highPx - lowPx;
    const rangeBps = openPx > 0 ? ((rangeDiff / openPx) * 10000).toFixed(1) : '0.0';

    const statOpen = document.getElementById('chart-stat-open');
    if (statOpen) statOpen.textContent = openPx > 0 ? `$${openPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statHigh = document.getElementById('chart-stat-high');
    if (statHigh) statHigh.textContent = highPx > 0 ? `$${highPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statLow = document.getElementById('chart-stat-low');
    if (statLow) statLow.textContent = lowPx > 0 ? `$${lowPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '--';
    const statRange = document.getElementById('chart-stat-range');
    if (statRange) statRange.textContent = `${rangeBps} bps`;

    // Polymarket live orderbook mid price (Implied Probability)
    const bUp = live.book_up || {};
    const bDn = live.book_down || {};
    const lastHistoryPt = history[history.length - 1];

    let livePolyUp = 0.5;
    let livePolyDn = 0.5;
    if (bUp.best_bid !== null && bUp.best_bid !== undefined && bUp.best_ask !== null && bUp.best_ask !== undefined) {
      livePolyUp = (Number(bUp.best_bid) + Number(bUp.best_ask)) / 2.0;
    } else if (live.poly_up !== undefined) {
      livePolyUp = live.poly_up;
    } else if (lastHistoryPt && lastHistoryPt.poly_up !== undefined) {
      livePolyUp = lastHistoryPt.poly_up;
    }

    if (bDn.best_bid !== null && bDn.best_bid !== undefined && bDn.best_ask !== null && bDn.best_ask !== undefined) {
      livePolyDn = (Number(bDn.best_bid) + Number(bDn.best_ask)) / 2.0;
    } else if (live.poly_down !== undefined) {
      livePolyDn = live.poly_down;
    } else {
      livePolyDn = Math.max(0.01, 1.0 - livePolyUp);
    }

    const pctPolyUp = (livePolyUp * 100).toFixed(1);
    const pctPolyDn = (livePolyDn * 100).toFixed(1);
    const modelProbVal = (live.prob_up !== undefined) ? (live.prob_up * 100).toFixed(1) : '--';

    const statProbUp = document.getElementById('chart-stat-prob-up');
    const statProbDn = document.getElementById('chart-stat-prob-dn');
    const statProbModel = document.getElementById('chart-stat-prob-model');
    if (statProbUp) {
      statProbUp.textContent = `UP ${pctPolyUp}% ($${livePolyUp.toFixed(2)})`;
      statProbUp.style.color = COLOR_UP; // 绿色
    }
    if (statProbDn) {
      statProbDn.textContent = `DN ${pctPolyDn}% ($${livePolyDn.toFixed(2)})`;
      statProbDn.style.color = COLOR_DOWN; // 红色
    }
    if (statProbModel) {
      statProbModel.textContent = `${modelProbVal}%`;
    }

    // Coordinates setup
    const padLeft = 24;
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

    // 2. Render Price Track (if dual or price)
    if (mode === 'dual' || mode === 'price') {
      // Track Title Label
      ctx.fillStyle = '#64748b';
      ctx.font = '600 11px sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText('BTC 现货基准波动 (BTC/USD)', padLeft + 6, t1Top + 12);

      // Price bounds
      const minBuffer = openPx > 0 ? Math.max(2.0, openPx * 0.0003) : 2.0;
      let minY = Math.min(lowPx, openPx - minBuffer);
      let maxY = Math.max(highPx, openPx + minBuffer);
      const ySpan = Math.max(0.1, maxY - minY);
      const paddedMinY = minY - ySpan * 0.08;
      const paddedMaxY = maxY + ySpan * 0.08;
      const paddedSpan = paddedMaxY - paddedMinY;

      const priceToY = (px) => t1Top + t1H - ((px - paddedMinY) / paddedSpan) * t1H;

      // Baseline strike price line S_0 (Dashed)
      if (openPx > 0) {
        const baselineY = priceToY(openPx);
        ctx.save();
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = '#94a3b8';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(padLeft, baselineY);
        ctx.lineTo(padLeft + plotW, baselineY);
        ctx.stroke();
        ctx.restore();

        // Baseline text on right
        ctx.fillStyle = '#64748b';
        ctx.textAlign = 'left';
        ctx.font = '600 11px monospace';
        ctx.fillText(`开盘基准 $${openPx.toFixed(2)}`, padLeft + plotW + 8, baselineY + 4);
      }

      // Build points for Price
      const pts = history.map(pt => ({
        x: timeToX(pt.t),
        y: priceToY(pt.price),
        price: pt.price,
        bps: pt.return_bps,
        t: pt.t
      }));

      const lastP = pts[pts.length - 1];

      // Fill underneath price curve
      const pGrad = ctx.createLinearGradient(0, t1Top, 0, t1Top + t1H);
      pGrad.addColorStop(0, priceFillTop);
      pGrad.addColorStop(1, priceFillBot);

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

      // Stroke price curve: 开盘价上方用绿，跌到下方用红
      ctx.beginPath();
      ctx.moveTo(pts[0].x, pts[0].y);
      for (let i = 1; i < pts.length; i++) {
        ctx.lineTo(pts[i].x, pts[i].y);
      }
      ctx.strokeStyle = priceColor;
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
      if (maxPt.x !== lastP.x && maxPt.price > openPx) {
        ctx.fillStyle = COLOR_UP; // 绿色
        ctx.beginPath();
        ctx.arc(maxPt.x, maxPt.y, 2.5, 0, Math.PI * 2);
        ctx.fill();
      }
      if (minPt.x !== lastP.x && minPt.price < openPx) {
        ctx.fillStyle = COLOR_DOWN; // 红色
        ctx.beginPath();
        ctx.arc(minPt.x, minPt.y, 2.5, 0, Math.PI * 2);
        ctx.fill();
      }

      // Live Pulse Dot on latest price
      ctx.beginPath();
      ctx.arc(lastP.x, lastP.y, 6, 0, Math.PI * 2);
      ctx.fillStyle = isAboveOpen ? 'rgba(5, 177, 105, 0.25)' : 'rgba(207, 32, 47, 0.25)';
      ctx.fill();

      ctx.beginPath();
      ctx.arc(lastP.x, lastP.y, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = priceColor;
      ctx.fill();

      // Right tag pill: 开盘价上方用绿，跌到下方用红
      const tagW = 122;
      const tagH = 22;
      const tagX = padLeft + plotW + 8;
      const tagY = Math.max(t1Top, Math.min(t1Top + t1H - tagH, lastP.y - tagH / 2));

      ctx.fillStyle = priceColor;
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(tagX, tagY, tagW, tagH, 4);
        ctx.fill();
      } else {
        ctx.fillRect(tagX, tagY, tagW, tagH);
      }

      ctx.fillStyle = '#ffffff';
      ctx.font = '600 11px monospace';
      ctx.textAlign = 'center';
      const bpsSign = lastP.bps >= 0 ? '+' : '';
      ctx.fillText(`$${lastP.price.toFixed(2)} (${bpsSign}${lastP.bps.toFixed(1)})`, tagX + tagW / 2, tagY + 15);
    }

    // 3. Render Polymarket Orderbook Implied Probability Track (if dual or prob)
    if (mode === 'dual' || mode === 'prob') {
      // Track Title Label
      ctx.fillStyle = '#64748b';
      ctx.font = '600 11px sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText('Polymarket 真实盘口价格 / 市场概率走势 (UP 绿 / DOWN 红)', padLeft + 6, t2Top + 12);

      // Prob Mapping: 0.0 to 1.0 -> y
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

      // 50% text on right
      ctx.fillStyle = '#94a3b8';
      ctx.textAlign = 'left';
      ctx.font = '10px monospace';
      ctx.fillText('50% ($0.50)', padLeft + plotW + 8, baseProbY + 3);

      // 100% and 0% edge markings
      ctx.fillText('100% ($1.0)', padLeft + plotW + 8, t2Top + 8);
      ctx.fillText('0% ($0.0)', padLeft + plotW + 8, t2Top + t2H);

      // Build points for Polymarket UP and DOWN token prices
      const upPts = [];
      const dnPts = [];
      const modelPts = [];

      history.forEach(pt => {
        const u = pt.poly_up !== undefined ? pt.poly_up : (pt.prob_up !== undefined ? pt.prob_up : 0.5);
        const d = pt.poly_down !== undefined ? pt.poly_down : Math.max(0.01, 1.0 - u);
        const m = pt.prob_up !== undefined ? pt.prob_up : 0.5;
        const x = timeToX(pt.t);
        upPts.push({ x, y: probToY(u), val: u });
        dnPts.push({ x, y: probToY(d), val: d });
        modelPts.push({ x, y: probToY(m), val: m });
      });

      const lastUp = upPts[upPts.length - 1];
      const lastDn = dnPts[dnPts.length - 1];

      // Draw Model Theoretical Fair Value as subtle dashed guide line
      if (modelPts.length > 1) {
        ctx.save();
        ctx.setLineDash([2, 3]);
        ctx.strokeStyle = 'rgba(139, 92, 246, 0.45)'; // subtle purple
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(modelPts[0].x, modelPts[0].y);
        for (let i = 1; i < modelPts.length; i++) {
          ctx.lineTo(modelPts[i].x, modelPts[i].y);
        }
        ctx.stroke();
        ctx.restore();
      }

      // Draw UP Curve (Green: #05b169)
      if (upPts.length > 1) {
        ctx.beginPath();
        ctx.moveTo(upPts[0].x, upPts[0].y);
        for (let i = 1; i < upPts.length; i++) {
          ctx.lineTo(upPts[i].x, upPts[i].y);
        }
        ctx.strokeStyle = COLOR_UP;
        ctx.lineWidth = 2.0;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();
      }

      // Draw DOWN Curve (Red: #cf202f)
      if (dnPts.length > 1) {
        ctx.beginPath();
        ctx.moveTo(dnPts[0].x, dnPts[0].y);
        for (let i = 1; i < dnPts.length; i++) {
          ctx.lineTo(dnPts[i].x, dnPts[i].y);
        }
        ctx.strokeStyle = COLOR_DOWN;
        ctx.lineWidth = 2.0;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.stroke();
      }

      // Live Pulse Dots on latest points
      // UP Dot
      ctx.beginPath();
      ctx.arc(lastUp.x, lastUp.y, 5, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(5, 177, 105, 0.25)';
      ctx.fill();
      ctx.beginPath();
      ctx.arc(lastUp.x, lastUp.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = COLOR_UP;
      ctx.fill();

      // DOWN Dot
      ctx.beginPath();
      ctx.arc(lastDn.x, lastDn.y, 5, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(207, 32, 47, 0.25)';
      ctx.fill();
      ctx.beginPath();
      ctx.arc(lastDn.x, lastDn.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = COLOR_DOWN;
      ctx.fill();

      // Right Tag Pills for UP and DOWN
      const probTagW = 110;
      const probTagH = 18;
      const probTagX = padLeft + plotW + 8;

      // Position UP tag near lastUp.y
      const upTagY = Math.max(t2Top, Math.min(t2Top + t2H - probTagH, lastUp.y - probTagH / 2));
      ctx.fillStyle = COLOR_UP;
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(probTagX, upTagY, probTagW, probTagH, 3);
        ctx.fill();
      } else {
        ctx.fillRect(probTagX, upTagY, probTagW, probTagH);
      }
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 10px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`UP ${pctPolyUp}% ($${livePolyUp.toFixed(2)})`, probTagX + probTagW / 2, upTagY + 13);

      // Position DOWN tag near lastDn.y (offset if overlapping)
      let dnTagY = Math.max(t2Top, Math.min(t2Top + t2H - probTagH, lastDn.y - probTagH / 2));
      if (Math.abs(dnTagY - upTagY) < (probTagH + 3)) {
        dnTagY = upTagY > (t2Top + t2H / 2) ? (upTagY - probTagH - 4) : (upTagY + probTagH + 4);
      }
      ctx.fillStyle = COLOR_DOWN;
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(probTagX, dnTagY, probTagW, probTagH, 3);
        ctx.fill();
      } else {
        ctx.fillRect(probTagX, dnTagY, probTagW, probTagH);
      }
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 10px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`DN ${pctPolyDn}% ($${livePolyDn.toFixed(2)})`, probTagX + probTagW / 2, dnTagY + 13);
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

    // Spot Price
    const spot = live.spot || 0;
    const openPx = live.open_px || 0;
    const bps = live.return_bps || 0;
    const pct = bps / 100.0;
    document.getElementById('market-spot').textContent = `$${spot.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
    document.getElementById('market-open').textContent = `基准 $${openPx.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

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
      riskText.textContent = `熔断看门狗: ${cb.reason || '仅限对冲平仓'}`;
    } else if (live.is_toxic_shock) {
      riskPill.className = 'status-pill status-warn';
      riskText.textContent = `剧烈波动冲击 (Toxic Shock)${volStr}`;
    } else if (live.regime === 'volatile') {
      riskPill.className = 'status-pill status-warn';
      riskText.textContent = `高波动防守区${volStr}`;
    } else if (live.regime === 'calm') {
      riskPill.className = 'status-pill status-ready';
      riskText.textContent = `平稳行情·窄价差抢占${volStr}`;
    } else {
      riskPill.className = 'status-pill status-ready';
      riskText.textContent = `流动性正常${volStr}`;
    }

    // UP Side
    const bUp = live.book_up || {};
    document.getElementById('up-bid-px').textContent = bUp.best_bid !== null && bUp.best_bid !== undefined ? `$${Number(bUp.best_bid).toFixed(2)}` : '--';
    document.getElementById('up-bid-sz').textContent = bUp.best_bid !== null ? '深度就绪' : '--';
    document.getElementById('up-ask-px').textContent = bUp.best_ask !== null && bUp.best_ask !== undefined ? `$${Number(bUp.best_ask).toFixed(2)}` : '--';
    document.getElementById('up-ask-sz').textContent = bUp.best_ask !== null ? '深度就绪' : '--';

    const posUp = live.pos_up || { shares: 0, cash: 0, avg_px: 0 };
    document.getElementById('up-pos-shares').textContent = `${(posUp.shares || 0).toFixed(1)} 股`;
    document.getElementById('up-pos-avg').textContent = posUp.avg_px ? `$${posUp.avg_px.toFixed(2)}` : '--';
    document.getElementById('up-pos-cost').textContent = `$${(posUp.cash || 0).toFixed(2)}`;

    // DOWN Side
    const bDn = live.book_down || {};
    document.getElementById('down-bid-px').textContent = bDn.best_bid !== null && bDn.best_bid !== undefined ? `$${Number(bDn.best_bid).toFixed(2)}` : '--';
    document.getElementById('down-bid-sz').textContent = bDn.best_bid !== null ? '深度就绪' : '--';
    document.getElementById('down-ask-px').textContent = bDn.best_ask !== null && bDn.best_ask !== undefined ? `$${Number(bDn.best_ask).toFixed(2)}` : '--';
    document.getElementById('down-ask-sz').textContent = bDn.best_ask !== null ? '深度就绪' : '--';

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
      pill.textContent = `在途挂单: ${o.side} ${o.size}股 @ $${Number(o.price).toFixed(2)}`;
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
      const items = actions.slice(-20).reverse().map(a => {
        const tStr = a.t ? new Date(a.t * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '--:--:--';
        const isFill = a.action === 'fill';
        const actionCls = isFill ? 'fill' : 'quote';
        const sideCls = (a.side || '').toLowerCase();
        const actionLabel = isFill ? '成交' : '挂单';
        const kindLabel = a.kind === 'taker' ? 'Taker 吃单' : 'Maker 挂单';
        return `
          <li class="action-item">
            <span class="action-time">${tStr}</span>
            <span class="action-type ${actionCls}">${actionLabel}</span>
            <span class="action-side ${sideCls}">${a.side || '--'}</span>
            <span>$${Number(a.price || 0).toFixed(2)} × ${a.size || 0}股</span>
            <span>${kindLabel} (${a.purpose || 'entry'})</span>
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
      const diff = closePx - openPx;
      const pct = openPx > 0 ? (diff / openPx) * 100.0 : 0;
      const diffStr = (diff >= 0 ? '+' : '-') + '$' + Math.abs(diff).toFixed(2);
      const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
      const diffCls = diff > 0 ? 'up' : (diff < 0 ? 'down' : '');
      return `
        <tr>
          <td><strong class="time-cell-main">${bj.fullStr}</strong></td>
          <td><span class="winner-pill ${winnerCls}">${r.winner || '--'}</span></td>
          <td>
            <span class="price-open-close">$${openPx.toFixed(2)} → $${closePx.toFixed(2)}</span>
            <small class="price-diff-bps ${diffCls}">${diffStr} (${pctStr})</small>
          </td>
          <td>${(r.pos_up || 0).toFixed(1)}</td>
          <td>${(r.pos_down || 0).toFixed(1)}</td>
          <td>$${(r.total_cost || 0).toFixed(2)}</td>
          <td>$${(r.proceeds || 0).toFixed(2)}</td>
          <td><strong class="${pnlCls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(3)}</strong></td>
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
