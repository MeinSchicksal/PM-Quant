// ============================================================================
// PM-QUANT · Institutional Quantitative Trading Terminal Logic (v3.1.0)
// ============================================================================

const $ = (id) => document.getElementById(id);

// Prevent unnecessary DOM rebuilds on 500ms polling so :hover states and scroll positions never flicker
const _lastHtmlCache = new WeakMap();
function setHtmlIfChanged(el, html) {
  if (!el) return false;
  if (_lastHtmlCache.get(el) === html) return false;
  _lastHtmlCache.set(el, html);
  el.innerHTML = html;
  return true;
}

const PAGE_TITLES = {
  overview: "交易总览",
  market: "实时工作区",
  history: "场次记录",
};

const REGIME_META = {
  range_bound: {
    label: "窄幅震荡 · 均值回归 (Range Bound)",
    desc: "SNR ≤ 1.0 且距基准偏离 ≤ 8bps，盘口双边高配平期望区间",
  },
  normal_oscillation: {
    label: "常态双向波动 (Normal Oscillation)",
    desc: "标准微观震荡市况，双阶段被动挂单吸筹与对冲主战场",
  },
  skewed_drift: {
    label: "温和偏态漂移 (Skewed Drift)",
    desc: "存在轻度方向性漂移但尚未形成单边趋势突破",
  },
  strong_trend: {
    label: "单边强趋势动量 (Strong Trend)",
    desc: "SNR > 1.8 强动量突破，严格禁止逆势挂单以防逆向选择",
  },
  wide_spread: {
    label: "宽价差 · 低流动性 (Wide Spread)",
    desc: "盘口价差 > $0.06，造市商撤退或流动性枯竭窗口",
  },
  toxic_shock: {
    label: "知情交易毒性冲击 (Toxic Shock)",
    desc: "3 秒内价格剧烈跳变 > 3.5bps，触发瞬时防逆向选择保护",
  },
  tail_freeze: {
    label: "尾盘伽马冻结区 (Tail Freeze)",
    desc: "距交割不足 15 秒，冻结新开仓仅保留残值防守",
  },
  legacy: {
    label: "早期未标注历史样本 (Legacy)",
    desc: "形态归因版本上线前已交割的历史实盘/模拟成交记录",
  },
  unlabeled: {
    label: "未触发开仓观测窗口 (Unlabeled)",
    desc: "全程处于观望状态、未发生实际开仓的监控窗口",
  },
};

const SVG_ICONS = {
  search: `<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3"/></svg>`,
  chart: `<svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 2.5v13h13M5.5 11.5l3-3.5 2.5 2 4-5"/></svg>`,
  list: `<svg width="15" height="15" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4.5h9.5M6 9h9.5M6 13.5h9.5M2.5 4.5h.01M2.5 9h.01M2.5 13.5h.01"/></svg>`,
  up: `<svg width="9" height="9" viewBox="0 0 12 12" fill="currentColor"><path d="M6 2.2 10.4 9.5H1.6L6 2.2Z"/></svg>`,
  down: `<svg width="9" height="9" viewBox="0 0 12 12" fill="currentColor"><path d="M6 9.8 1.6 2.5h8.8L6 9.8Z"/></svg>`,
};

// Dynamic theme-aware canvas palette
function getPalette() {
  const isDark = document.documentElement.dataset.theme === "dark";
  if (isDark) {
    return {
      isDark: true,
      spot: "#3b82f6",
      spotFillTop: "rgba(59, 130, 246, 0.22)",
      spotFillBot: "rgba(59, 130, 246, 0.01)",
      twap: "#38bdf8",
      model: "#fbbf24",
      up: "#10b981",
      upSoft: "rgba(16, 185, 129, 0.20)",
      down: "#f43f5e",
      downSoft: "rgba(244, 63, 94, 0.20)",
      amber: "#f59e0b",
      ink: "#f8fafc",
      label: "#94a3b8",
      axis: "#64748b",
      grid: "rgba(148, 163, 184, 0.14)",
      gridSoft: "rgba(148, 163, 184, 0.08)",
      mask: "rgba(17, 24, 39, 0.88)",
      cardBg: "#111827",
      divider: "rgba(148, 163, 184, 0.18)",
    };
  }
  return {
    isDark: false,
    spot: "#2563eb",
    spotFillTop: "rgba(37, 99, 235, 0.14)",
    spotFillBot: "rgba(37, 99, 235, 0.00)",
    twap: "#0284c7",
    model: "#d97706",
    up: "#059669",
    upSoft: "rgba(5, 150, 105, 0.14)",
    down: "#dc2626",
    downSoft: "rgba(220, 38, 38, 0.14)",
    amber: "#d97706",
    ink: "#0f172a",
    label: "#475569",
    axis: "#64748b",
    grid: "rgba(15, 23, 42, 0.07)",
    gridSoft: "rgba(15, 23, 42, 0.04)",
    mask: "rgba(255, 255, 255, 0.90)",
    cardBg: "#ffffff",
    divider: "rgba(15, 23, 42, 0.10)",
  };
}

let currentPage = "overview";
let chartViewMode = "dual"; // "dual" | "price" | "prob"
let historyFilterMode = "all"; // "all" | "traded" | "win" | "loss"
let cachedLive = null;
let cachedHistory = null;
let cachedModalWindow = null;
let cachedModalRecord = null;
let liveChartHoverIdx = null;
let liveChartGeom = null;
let equityHoverIdx = null;
let equityChartGeom = null;
let modalHoverIdx = null;
let modalSelectedActionIdx = null;
let modalChartGeom = null;

// ============================================================================
// TOAST NOTIFICATION SYSTEM
// ============================================================================
function showToast(message, tone = "brand") {
  const container = $("toast-container");
  if (!container) return;
  const item = document.createElement("div");
  item.className = "toast-item";
  const dotColor =
    tone === "up" ? "var(--up)" : tone === "down" ? "var(--down)" : "var(--accent)";
  item.innerHTML = `<span style="width:7px;height:7px;border-radius:50%;background:${dotColor};display:inline-block"></span><span>${escapeHtml(message)}</span>`;
  container.appendChild(item);
  setTimeout(() => {
    item.style.transition = "opacity 0.2s ease, transform 0.2s ease";
    item.style.opacity = "0";
    item.style.transform = "translateY(6px)";
    setTimeout(() => item.remove(), 220);
  }, 2100);
}

// ============================================================================
// THEME MANAGEMENT (LIGHT / DARK)
// ============================================================================
function initTheme() {
  try {
    const saved = localStorage.getItem("pm_quant_theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (_) {
    // ignore storage errors
  }
}

function toggleTheme() {
  const curr = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  const next = curr === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("pm_quant_theme", next);
  } catch (_) {
    // ignore
  }
  if (cachedHistory) drawEquityCurve(cachedHistory);
  if (cachedLive) drawLivePriceChart(cachedLive);
  if (cachedModalWindow && !$("round-detail-modal")?.hidden) {
    drawModalHistoryChart(cachedModalWindow, cachedModalRecord);
  }
  showToast(next === "dark" ? "已切换至深色终端模式" : "已切换至浅色机构模式", "brand");
}

// ============================================================================
// FORMATTING & RECORD NORMALIZATION HELPERS
// ============================================================================
function fmtUsd(v, signed = false) {
  const num = Number(v || 0);
  const abs = Math.abs(num).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (!signed) return `$${abs}`;
  if (num > 1e-6) return `+$${abs}`;
  if (num < -1e-6) return `-$${abs}`;
  return `$0.00`;
}

function fmtPct(v, signed = true) {
  const num = Number(v || 0);
  const sign = signed && num > 0 ? "+" : "";
  return `${sign}${num.toFixed(2)}%`;
}

function fmtShares(v) {
  return Number(v || 0).toFixed(1);
}

function fmtPriceCents(v) {
  if (v === null || v === undefined || Number(v) <= 0) return "--";
  return `$${Number(v).toFixed(2)}`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function normalizeRecord(r) {
  if (!r) return null;
  const posUpRaw = r.pos_up;
  const posDnRaw = r.pos_down;
  const upShares =
    r.up_shares !== undefined
      ? Number(r.up_shares)
      : typeof posUpRaw === "object" && posUpRaw !== null
      ? Number(posUpRaw.shares || 0)
      : Number(posUpRaw || 0);
  const dnShares =
    r.down_shares !== undefined
      ? Number(r.down_shares)
      : typeof posDnRaw === "object" && posDnRaw !== null
      ? Number(posDnRaw.shares || 0)
      : Number(posDnRaw || 0);

  let cost = Number(r.total_cost ?? r.cost ?? 0);
  if (!cost && typeof posUpRaw === "object" && posUpRaw !== null) {
    cost = Number(posUpRaw.cash || 0) + Number(posDnRaw?.cash || 0);
  }
  const payout = Number(r.proceeds ?? r.payout ?? 0);
  const pnl = Number(r.net_pnl ?? r.pnl ?? 0);
  const fillsCount = Number(r.fills_count || 0);
  const insurance = Number(r.insurance_paid ?? r.insurance_Prem ?? 0);
  const startTs = Number(r.start ?? r.start_ts ?? 0);
  const endTs = Number(r.end ?? r.end_ts ?? (startTs ? startTs + 300 : 0));

  return {
    slug: r.slug || "",
    start_ts: startTs,
    end_ts: endTs,
    winner: r.winner || "PENDING",
    open_px: Number(r.open_px || 0),
    close_px: Number(r.close_px || 0),
    up_shares: upShares,
    down_shares: dnShares,
    cost,
    payout,
    pnl,
    fills_count: fillsCount,
    insurance,
    traded: Boolean(r.traded || fillsCount > 0 || cost > 0 || Math.abs(pnl) > 1e-6),
  };
}

function slugToWindowText(slug, startTs = 0, endTs = 0) {
  let s = Number(startTs || 0);
  let e = Number(endTs || 0);
  if (!s && slug) {
    const m = String(slug).match(/(\d{10})$/);
    if (m) {
      s = parseInt(m[1], 10);
      e = s + 300;
    }
  }
  if (!s) {
    return {
      range: slug || "--:-- ~ --:--",
      dateStr: "--",
      fullLabel: slug || "--",
      shortTime: "--:--",
    };
  }
  if (!e) e = s + 300;

  const fmtHm = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const fmtMd = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
  });

  const sDate = new Date(s * 1000);
  const eDate = new Date(e * 1000);
  const hmStart = fmtHm.format(sDate);
  const hmEnd = fmtHm.format(eDate);
  const md = fmtMd.format(sDate);
  return {
    range: `${hmStart} ~ ${hmEnd}`,
    dateStr: `${md} (北京时间)`,
    fullLabel: `${md} ${hmStart}~${hmEnd}`,
    shortTime: hmStart,
  };
}

// ============================================================================
// NAVIGATION & VIEW ROUTING
// ============================================================================
function switchPage(page) {
  const target = PAGE_TITLES[page] ? page : "overview";
  currentPage = target;
  document.body.dataset.page = target;

  document.querySelectorAll(".view-panel").forEach((panel) => {
    const isMatch = panel.id === `view-${target}`;
    panel.hidden = !isMatch;
  });

  document.querySelectorAll(".sidebar-link").forEach((link) => {
    link.classList.toggle("active", link.dataset.tab === target);
  });

  const titleEl = $("page-title");
  if (titleEl) titleEl.textContent = PAGE_TITLES[target];

  requestAnimationFrame(() => {
    if (target === "overview" && cachedHistory) {
      drawEquityCurve(cachedHistory);
    }
    if (target === "market" && cachedLive) {
      drawLivePriceChart(cachedLive);
    }
  });
}

function initNavigation() {
  const applyHash = () => {
    const raw = (window.location.hash || "#overview").replace("#", "").trim();
    switchPage(PAGE_TITLES[raw] ? raw : "overview");
  };
  window.addEventListener("hashchange", applyHash);
  applyHash();

  window.addEventListener("keydown", (e) => {
    if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    const modal = $("round-detail-modal");
    if (e.key === "Escape" && modal && !modal.hidden) {
      closeRoundModal();
      return;
    }
    if (modal && !modal.hidden) return;
    if (e.key === "1") window.location.hash = "#overview";
    else if (e.key === "2") window.location.hash = "#market";
    else if (e.key === "3") window.location.hash = "#history";
    else if (e.key === "r" || e.key === "R") {
      fetchAll(true);
    } else if (e.key === "t" || e.key === "T") {
      toggleTheme();
    }
  });
}

// ============================================================================
// REAL-TIME BEIJING WALL CLOCK
// ============================================================================
function updateWallClock() {
  const now = new Date();
  const timeFmt = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const dateFmt = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
  });
  const weekFmt = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    weekday: "short",
  });

  const timeEl = $("current-time");
  const dateEl = $("current-date");
  const weekEl = $("current-weekday");

  if (timeEl) timeEl.textContent = timeFmt.format(now);
  if (dateEl) dateEl.textContent = dateFmt.format(now);
  if (weekEl) weekEl.textContent = weekFmt.format(now);
}

// ============================================================================
// TELEMETRY & NETWORK BAR
// ============================================================================
function updateNetworkTelemetry(live) {
  const net = (live && live.network) || {};
  const cb = (live && live.circuit_breaker) || {};

  const kbpsEl = $("side-net-kbps");
  const latEl = $("side-net-latency");
  const totalEl = $("side-net-total");
  const polyLatEl = $("src-poly-lat");
  const binLatEl = $("src-binance-lat");
  const netCountEl = $("net-count");

  const rxKbps = net.downstream_kbps ?? net.rx_kbps ?? net.kbps ?? 0;
  const polyMs = net.poly_latency_ms ?? cb.poly_latency_ms ?? net.latency_ms ?? 0;
  const binMs = net.binance_latency_ms ?? cb.spot_latency_ms ?? 0;
  const rttMs = net.avg_latency_ms ?? net.latency_ms ?? (polyMs && binMs ? (polyMs + binMs) / 2 : polyMs || binMs);

  if (kbpsEl) kbpsEl.textContent = `${Number(rxKbps || 0).toFixed(1)} KB/s`;
  if (latEl) latEl.textContent = rttMs ? `${Math.round(rttMs)} ms` : "-- ms";
  if (totalEl) totalEl.textContent = `${Number(net.total_mb || 0).toFixed(2)} MB`;

  if (polyLatEl) polyLatEl.textContent = polyMs ? `${Math.round(polyMs)} ms` : "就绪";
  if (binLatEl) binLatEl.textContent = binMs ? `${Math.round(binMs)} ms` : "就绪";

  const tripped = Boolean(cb.active || cb.tripped);
  const nowSec = live && live.server_time ? Number(live.server_time) : (Date.now() / 1000);
  const liveAge = (live && live.now) ? Math.abs(nowSec - Number(live.now)) : Infinity;
  const isIdle = !live || live.status === "idle" || liveAge > 10;
  const spotStale = Boolean(live && live.spot_stale);
  const polyStale = Boolean(live && live.poly_spot_stale);

  if (netCountEl) {
    if (isIdle) {
      netCountEl.textContent = "管道待机/离线";
      netCountEl.style.color = "var(--text-muted)";
      netCountEl.style.background = "var(--surface-2, rgba(148,163,184,0.14))";
    } else if (tripped) {
      netCountEl.textContent = "风控告警";
      netCountEl.style.color = "var(--down)";
      netCountEl.style.background = "var(--down-soft)";
    } else if (spotStale) {
      netCountEl.textContent = "现货断流";
      netCountEl.style.color = "var(--down)";
      netCountEl.style.background = "var(--down-soft)";
    } else if (polyStale) {
      netCountEl.textContent = "TWAP 滞后";
      netCountEl.style.color = "#d97706";
      netCountEl.style.background = "rgba(245,158,11,0.14)";
    } else {
      netCountEl.textContent = "4 源正常";
      netCountEl.style.color = "var(--up)";
      netCountEl.style.background = "var(--up-soft)";
    }
  }
}

// ============================================================================
// STRATEGY & CIRCUIT BREAKER REASON TRANSLATOR (CLEAN CHINESE PHRASING)
// ============================================================================
function translateCircuitBreakerReason(rawReason) {
  if (!rawReason) return "微观风控熔断保护中";
  const s = String(rawReason).trim();
  let m;
  if ((m = s.match(/Loss streak cooling\s*\((\d+)\s*rounds?\s*remaining\)/i))) {
    return `连亏冷却保护（剩余 ${m[1]} 轮）`;
  }
  if ((m = s.match(/Loss streak:\s*(\d+)\s*consecutive losing rounds/i))) {
    return `触发连亏保护（连续 ${m[1]} 轮亏损）`;
  }
  if ((m = s.match(/Daily drawdown.*?\$([\d.]+)/i)) || /daily\s*(drawdown|loss)/i.test(s)) {
    const amt = m && m[1] ? `（$${m[1]}）` : "";
    return `触发单日最大回撤熔断保护${amt}`;
  }
  if (/cooling/i.test(s)) {
    return "连亏风控冷却保护中";
  }
  if (/[\u4e00-\u9fa5]/.test(s)) {
    return s;
  }
  return "微观风控熔断保护中";
}

function translateDecisionReason(rawReason) {
  if (!rawReason) return "持续监控双边盘口赔率与预言机偏离度...";
  let s = String(rawReason).trim();

  // v2.3 English reason translations (concise Chinese financial phrasing)
  let m;
  if ((m = s.match(/Holding\s+([\d.]+)\s+(Up|Down)\s+@\s*([\d.]+)\s+to settlement\s*\(p_fair=([\d.]+)\)/i))) {
    const pct = (parseFloat(m[4]) * 100).toFixed(1);
    return `持仓等待交割：持有 ${m[1]} 股 ${m[2]}（均价 $${m[3]}），当前模型胜率 ${pct}%`;
  }
  if ((m = s.match(/Taker IOC:\s*(Up|Down)\s+ask=([\d.]+),\s*fee=([\d.]+),\s*P\*=([\d.]+),\s*net_edge=([\d.-]+)/i))) {
    const pPct = (parseFloat(m[4]) * 100).toFixed(1);
    const ePct = (parseFloat(m[5]) * 100).toFixed(1);
    return `触发主动吃单（IOC）：买入 ${m[1]} @ $${m[2]}（手续费 $${m[3]}，胜率 ${pPct}%，净优势 +${ePct}%）`;
  }
  if ((m = s.match(/Maker Quote:\s*(Up|Down)\s+bid=([\d.]+),\s*P\*=([\d.]+),\s*edge=([\d.-]+)/i))) {
    const pPct = (parseFloat(m[3]) * 100).toFixed(1);
    const ePct = (parseFloat(m[4]) * 100).toFixed(1);
    return `零费率挂单排队：挂买 ${m[1]} @ $${m[2]}（模型胜率 ${pPct}%，优势 +${ePct}%）`;
  }
  if ((m = s.match(/Observation period\s*\(tau\s*([\d.]+)s/i))) {
    return `开盘观察蓄势期（剩余 ${Math.round(parseFloat(m[1]))}s）：前 60 秒只采集基差与波动率，暂不开仓`;
  }
  if ((m = s.match(/Tail freeze\s*\(tau\s*([\d.]+)s/i))) {
    return `尾盘冻结（剩余 ${Math.round(parseFloat(m[1]))}s）：最后 15 秒停止新开仓并撤回未成挂单，持有到期`;
  }
  if ((m = s.match(/No executable Net-Edge\s*\(P\*\(Up\)=([\d.]+),\s*Up=([^,]+),\s*Dn=([^)]+)\)/i))) {
    const upPct = (parseFloat(m[1]) * 100).toFixed(1);
    return `暂无满足净优势的报价（模型 UP 胜率 ${upPct}%，Up 盘口 ${m[2]} / Dn 盘口 ${m[3]}），继续盯盘`;
  }
  if ((m = s.match(/Anti-toxicity spot shock\s*\(([\d.]+)\s*bps\)/i))) {
    return `现货突发急剧跳变（${m[1]} bps），防毒流保护已紧急撤回在途挂单`;
  }
  if (s.includes("Inconsistent book snapshot")) {
    return "双边盘口瞬时异步交叉或价差异常，跳过本轮防止误判";
  }
  if (s.includes("Incomplete orderbook")) {
    return "盘口买卖档位不完整（单边空盘），暂停报单等待流动性恢复";
  }
  if (s.includes("Waiting for official strike")) {
    return "正在等待官方开盘基准价与稳态基差校准完成...";
  }
  if (s.includes("Spot price stale") || s.includes("Dead-man switch")) {
    return "行情流延迟或断流触发死人开关保护，已撤单并暂停开仓";
  }
  if (s.includes("Max entries per window")) {
    return "本轮已达到单窗开仓上限（1 次），锁定仓位等待结算";
  }
  if (s.includes("Fully hedged pair") || s.includes("Max position reached")) {
    return "已达本轮最大持仓或双边配平，静待到期交割";
  }
  if (s.includes("Insufficient capital")) {
    return "账户可用资金不足，暂停开新仓";
  }
  if (s.includes("Circuit breaker active")) {
    return "风控熔断器生效中（触发单日回撤或连亏冷却），暂停新开第一腿";
  }
  if (/Loss streak cooling|Loss streak:|daily\s*(drawdown|loss)/i.test(s)) {
    return translateCircuitBreakerReason(s);
  }

  // Legacy mixed-English normalization
  s = s
    .replace(/LWAA\s+Maker\s+Absorption\s*:/gi, "被动做市吸筹：")
    .replace(/MWDE\s+Maker\s+Absorption\s*:/gi, "顺势被动挂单：")
    .replace(/LWAA\s+Taker\s+Absorption\s*:/gi, "主动吃单吸筹：")
    .replace(/MWDE\s+Taker\s+Absorption\s*:/gi, "顺势主动吃单：")
    .replace(/Arbitrage\s+lock\s*:/gi, "双边套利锁仓：")
    .replace(/Defensive\s+salvage\s*:/gi, "尾盘防守回收：")
    .replace(/Maker\s+Absorption\s*:/gi, "被动做市吸筹：")
    .replace(/Taker\s+Absorption\s*:/gi, "主动吃单吸筹：");

  if (/[\u4e00-\u9fa5]/.test(s)) return s;

  if (s.includes("Waiting for warmup") || s.includes("warmup")) {
    return "开盘冷静期观察中，等待价格基准稳定";
  }
  if (s.includes("No positive edge") || s.includes("edge too small")) {
    return "当前盘口隐含赔率无正期望边际，保持观望";
  }
  if (s.includes("cutoff") || s.includes("tau too low")) {
    return "临近交割窗口期，停止新开单以规避尾盘伽马风险";
  }
  if (s.includes("Circuit breaker") || s.includes("stale")) {
    return "数据源延迟或价差触发微观风控保护，暂停报单";
  }
  if (s.includes("Max capital") || s.includes("budget")) {
    return "本轮风险敞口已达上限，不再追加裸头寸";
  }
  return s;
}

// ============================================================================
// VIEW 1: TRADING OVERVIEW (交易总览)
// ============================================================================
function updateOverview(live, hist) {
  if (!hist) return;

  const totalPnl = Number(hist.total_pnl || 0);
  const totalWon = Number(hist.total_won || 0);
  const totalLost = Math.abs(Number(hist.total_lost || 0));
  const initialCap = Number(hist.initial_capital || 100);
  const availCap = Number(hist.available_capital ?? initialCap + totalPnl);
  const freeCap = Number(hist.free_capital ?? availCap);
  const turnover = Number(hist.turnover || 0);
  const equityRoi = Number(hist.roi_pct || 0);
  const turnoverRoi = Number(hist.turnover_roi_pct || 0);
  const totalRounds = Number(hist.total_rounds || 0);
  const tradedRounds = Number(hist.traded_rounds || 0);
  const wins = Number(hist.wins || 0);
  const losses = Number(hist.losses || 0);
  const flats = Number(hist.flats || 0);
  const winRate = Number(hist.win_rate || 0);
  const avgPnl = Number(hist.avg_pnl || 0);
  const avgWin = Number(hist.avg_win || 0);
  const avgLoss = Math.abs(Number(hist.avg_loss || 0));

  // Hero Net PnL
  const pnlEl = $("hero-total-pnl");
  if (pnlEl) {
    pnlEl.textContent = fmtUsd(totalPnl, true);
    pnlEl.className = `hero-num ${totalPnl > 0 ? "up" : totalPnl < 0 ? "down" : ""}`;
  }

  const settledBadge = $("hero-settled-count");
  if (settledBadge) settledBadge.textContent = `${totalRounds} 场已结算`;

  const roiSub = $("kpi-avail-sub");
  const roiPill = $("hero-equity-roi-pill");
  if (roiSub) roiSub.textContent = fmtPct(equityRoi, true);
  if (roiPill) {
    roiPill.style.color = equityRoi >= 0 ? "var(--up)" : "var(--down)";
    roiPill.style.background = equityRoi >= 0 ? "var(--up-soft)" : "var(--down-soft)";
    roiPill.style.borderColor = equityRoi >= 0 ? "var(--up-border)" : "var(--down-border)";
  }

  // Gross Won vs Lost bar
  const wonEl = $("hero-won");
  const lostEl = $("hero-lost");
  const winBarEl = $("hero-win-bar");
  if (wonEl) wonEl.textContent = fmtUsd(totalWon, true);
  if (lostEl) lostEl.textContent = `-$${totalLost.toFixed(2)}`;
  if (winBarEl) {
    const grossSum = totalWon + totalLost;
    const ratio = grossSum > 0 ? Math.min(98, Math.max(2, (totalWon / grossSum) * 100)) : 50;
    winBarEl.style.width = `${ratio.toFixed(1)}%`;
  }

  // Capital Matrix
  if ($("kpi-available-cap")) $("kpi-available-cap").textContent = fmtUsd(availCap);
  if ($("hero-free-cap")) $("hero-free-cap").textContent = fmtUsd(freeCap);
  if ($("hero-initial-cap")) $("hero-initial-cap").textContent = fmtUsd(initialCap);
  if ($("hero-turnover")) $("hero-turnover").textContent = fmtUsd(turnover);
  const heroRoiEl = $("hero-roi");
  if (heroRoiEl) {
    heroRoiEl.textContent = fmtPct(turnoverRoi, true);
    heroRoiEl.className = `cap-sub ${turnoverRoi >= 0 ? "up" : "down"}`;
  }

  // Active Round Card in Overview
  if (live && live.slug) {
    const winInfo = slugToWindowText(live.slug, live.start, live.end);
    if ($("overview-live-time")) $("overview-live-time").textContent = winInfo.range;
    if ($("overview-live-date")) $("overview-live-date").textContent = winInfo.dateStr;

    const tau = Math.max(0, Math.round(Number(live.tau_s || 0)));
    if ($("overview-live-tau")) {
    setHtmlIfChanged($("overview-live-tau"), `${tau}<span class="val-unit">s</span>`);
  }
    const elapsedPct = Math.min(100, Math.max(0, ((300 - tau) / 300) * 100));
    if ($("overview-tau-bar")) $("overview-tau-bar").style.width = `${elapsedPct.toFixed(1)}%`;

    if ($("overview-live-spot")) {
      const spotNum = Number(live.spot || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      setHtmlIfChanged($("overview-live-spot"), `<span class="val-currency">$</span>${spotNum}`);
    }
    const upS = Number(live.pos_up?.shares || 0);
    const dnS = Number(live.pos_down?.shares || 0);
    if ($("overview-live-pos")) {
      setHtmlIfChanged(
        $("overview-live-pos"),
        `<span class="pos-chip up">UP ${fmtShares(upS)}</span><span class="pos-sep">/</span><span class="pos-chip down">DN ${fmtShares(dnS)}</span>`
      );
    }
  }

  // 4 Core Non-Redundant KPI Strip Cards
  if ($("kpi-winrate")) $("kpi-winrate").textContent = `${winRate.toFixed(1)}%`;
  if ($("kpi-winloss")) $("kpi-winloss").textContent = `${wins} 胜 · ${losses} 负 · ${flats} 平`;

  const avgPnlEl = $("kpi-avg-pnl");
  if (avgPnlEl) {
    avgPnlEl.textContent = fmtUsd(avgPnl, true);
    avgPnlEl.className = `kpi-box-val ${avgPnl > 0 ? "up" : avgPnl < 0 ? "down" : ""}`;
  }
  if ($("kpi-avg-split")) {
    $("kpi-avg-split").textContent = `均盈 +$${avgWin.toFixed(2)} · 均亏 -$${avgLoss.toFixed(2)}`;
  }

  // Compute hedge lock rate & total fills across settled records
  const rawRecords = Array.isArray(hist.records) ? hist.records : [];
  const normRecords = rawRecords.map(normalizeRecord).filter(Boolean);
  let pairedCount = 0;
  let totalFills = 0;
  normRecords.forEach((r) => {
    totalFills += r.fills_count;
    if (r.up_shares > 0 && r.down_shares > 0) pairedCount += 1;
  });
  const hedgeRate = tradedRounds > 0 ? (pairedCount / tradedRounds) * 100 : 0;
  if ($("kpi-hedge-rate")) $("kpi-hedge-rate").textContent = `${hedgeRate.toFixed(1)}%`;
  if ($("kpi-total-fills")) {
    $("kpi-total-fills").textContent = `配平 ${pairedCount}/${tradedRounds} 场 · ${totalFills} 笔成交`;
  }

  const partRate = totalRounds > 0 ? (tradedRounds / totalRounds) * 100 : 0;
  const capTurnMultiple = initialCap > 0 ? turnover / initialCap : 0;
  if ($("kpi-participation-rate")) $("kpi-participation-rate").textContent = `${partRate.toFixed(1)}%`;
  if ($("kpi-participation-sub")) {
    $("kpi-participation-sub").textContent = `${tradedRounds}/${totalRounds} 场成交 · 周转 ${capTurnMultiple.toFixed(2)}x`;
  }

  // Render Cumulative Equity Curve
  drawEquityCurve(hist);

  // Render Regime Attribution Table
  renderRegimeAttribution(hist.regime_stats, tradedRounds, totalRounds);
}

// ============================================================================
// INTERACTIVE CUMULATIVE EQUITY CURVE CANVAS
// ============================================================================
function drawEquityCurve(hist) {
  const canvas = $("equity-curve-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(260, rect.width || 420);
  const h = Math.max(175, rect.height || 200);

  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pal = getPalette();
  const rawCurve = Array.isArray(hist?.equity_curve) ? hist.equity_curve : [];

  // Backend equity_curve has { slug, pnl (cumulative!), start }
  const pts = [{ idx: 0, cum_pnl: 0, round_pnl: 0, slug: "", ts: 0 }];
  let prevCum = 0;
  rawCurve.forEach((p, idx) => {
    const cum = Number(p.cum_pnl ?? p.pnl ?? 0);
    const delta = p.round_pnl !== undefined ? Number(p.round_pnl) : cum - prevCum;
    prevCum = cum;
    pts.push({
      idx: idx + 1,
      cum_pnl: cum,
      round_pnl: delta,
      slug: p.slug || "",
      ts: Number(p.start ?? p.ts ?? 0),
    });
  });

  let peak = 0;
  pts.forEach((p) => {
    if (p.cum_pnl > peak) peak = p.cum_pnl;
  });
  const peakBadge = $("equity-peak-badge");
  if (peakBadge) peakBadge.textContent = `峰值 ${fmtUsd(peak, true)}`;

  if (pts.length < 2) {
    equityChartGeom = null;
    ctx.fillStyle = pal.axis;
    ctx.font = "500 12px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("暂无已结算净值点", w / 2, h / 2);
    return;
  }

  const padL = 52;
  const padR = 20;
  const padT = 26;
  const padB = 26;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const vals = pts.map((p) => p.cum_pnl);
  let minV = Math.min(0, ...vals);
  let maxV = Math.max(0, ...vals);
  if (Math.abs(maxV - minV) < 1e-6) {
    maxV += 1;
    minV -= 1;
  } else {
    const span = maxV - minV;
    maxV += span * 0.16;
    minV -= span * 0.10;
  }

  const xOf = (i) => padL + (i / (pts.length - 1)) * plotW;
  const yOf = (v) => padT + ((maxV - v) / (maxV - minV)) * plotH;

  const coords = pts.map((p, i) => ({
    ...p,
    x: xOf(i),
    y: yOf(p.cum_pnl),
  }));

  equityChartGeom = {
    w,
    h,
    padL,
    padR,
    padT,
    padB,
    plotW,
    plotH,
    pts: coords,
  };

  // Crisp straight-segment path helper
  const tracePolyline = () => {
    coords.forEach((pt, i) => {
      if (i === 0) ctx.moveTo(pt.x, pt.y);
      else ctx.lineTo(pt.x, pt.y);
    });
  };

  // Minimal horizontal dashed grid lines
  const steps = 3;
  ctx.font = "500 10.5px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  for (let s = 0; s <= steps; s++) {
    const val = minV + ((maxV - minV) * s) / steps;
    const y = yOf(val);
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(148, 163, 184, 0.10)" : "rgba(15, 23, 42, 0.055)";
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.restore();

    ctx.fillStyle = pal.axis;
    ctx.fillText(fmtUsd(val, true), padL - 8, y);
  }

  // Zero baseline
  const zeroY = yOf(0);
  if (zeroY >= padT && zeroY <= padT + plotH) {
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(148, 163, 184, 0.28)" : "rgba(15, 23, 42, 0.16)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, zeroY);
    ctx.lineTo(w - padR, zeroY);
    ctx.stroke();
    ctx.restore();
  }

  const lastVal = vals[vals.length - 1];
  const strokeColor = lastVal >= 0 ? pal.up : pal.down;
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  if (lastVal >= 0) {
    grad.addColorStop(0, pal.isDark ? "rgba(16, 185, 129, 0.22)" : "rgba(5, 150, 105, 0.15)");
    grad.addColorStop(0.85, "rgba(16, 185, 129, 0.01)");
    grad.addColorStop(1, "rgba(16, 185, 129, 0.00)");
  } else {
    grad.addColorStop(0, pal.isDark ? "rgba(244, 63, 94, 0.22)" : "rgba(225, 29, 72, 0.15)");
    grad.addColorStop(0.85, "rgba(244, 63, 94, 0.01)");
    grad.addColorStop(1, "rgba(244, 63, 94, 0.00)");
  }

  // Area fill
  ctx.beginPath();
  tracePolyline();
  ctx.lineTo(coords[coords.length - 1].x, padT + plotH);
  ctx.lineTo(coords[0].x, padT + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Main crisp line
  ctx.beginPath();
  tracePolyline();
  ctx.strokeStyle = strokeColor;
  ctx.lineWidth = 2.2;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke();

  // Terminal glowing endpoint dot
  const lastPt = coords[coords.length - 1];
  ctx.save();
  ctx.beginPath();
  ctx.arc(lastPt.x, lastPt.y, 8, 0, Math.PI * 2);
  ctx.fillStyle = lastVal >= 0
    ? (pal.isDark ? "rgba(16, 185, 129, 0.24)" : "rgba(5, 150, 105, 0.16)")
    : (pal.isDark ? "rgba(244, 63, 94, 0.24)" : "rgba(225, 29, 72, 0.16)");
  ctx.fill();

  ctx.beginPath();
  ctx.arc(lastPt.x, lastPt.y, 4, 0, Math.PI * 2);
  ctx.fillStyle = strokeColor;
  ctx.fill();
  ctx.strokeStyle = pal.cardBg;
  ctx.lineWidth = 1.8;
  ctx.stroke();
  ctx.restore();

  // Hover crosshair on equity curve
  if (equityHoverIdx !== null && equityChartGeom.pts[equityHoverIdx]) {
    const hp = equityChartGeom.pts[equityHoverIdx];
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(248, 250, 252, 0.28)" : "rgba(15, 23, 42, 0.20)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hp.x, padT);
    ctx.lineTo(hp.x, padT + plotH);
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.arc(hp.x, hp.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = strokeColor;
    ctx.fill();
    ctx.strokeStyle = pal.cardBg;
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  // X-axis labels
  ctx.fillStyle = pal.axis;
  ctx.font = "500 10px 'JetBrains Mono', monospace";
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  ctx.fillText("起点 #0", padL, padT + plotH + 7);
  ctx.textAlign = "right";
  ctx.fillText(`最新 #${pts.length - 1}`, w - padR, padT + plotH + 7);
}

function initEquityCurveInteractions() {
  const canvas = $("equity-curve-canvas");
  const tooltip = $("equity-tooltip");
  if (!canvas || !tooltip) return;

  canvas.addEventListener("mousemove", (e) => {
    if (!equityChartGeom || !equityChartGeom.pts?.length) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;

    let bestIdx = 0;
    let bestDist = Infinity;
    equityChartGeom.pts.forEach((p, idx) => {
      const d = Math.abs(p.x - mx);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = idx;
      }
    });

    equityHoverIdx = bestIdx;
    drawEquityCurve(cachedHistory);

    const pt = equityChartGeom.pts[bestIdx];
    if (bestIdx === 0) {
      tooltip.innerHTML = `
        <div class="tt-time">账户初始起点 (#0)</div>
        <div class="tt-row"><span>初始本金</span><b>${fmtUsd(cachedHistory?.initial_capital || 100)}</b></div>
        <div class="tt-row"><span>累计净收益</span><b>$0.00</b></div>
      `;
    } else {
      const winInfo = slugToWindowText(pt.slug, pt.ts);
      const delta = Number(pt.round_pnl || 0);
      const cum = Number(pt.cum_pnl || 0);
      tooltip.innerHTML = `
        <div class="tt-time">第 #${bestIdx} 场 · ${escapeHtml(winInfo.range)}</div>
        <div class="tt-row">
          <span>单场净损益</span>
          <b class="${delta > 0 ? "up" : delta < 0 ? "down" : ""}">${fmtUsd(delta, true)}</b>
        </div>
        <div class="tt-row">
          <span>累计已结算净利</span>
          <b class="${cum > 0 ? "up" : cum < 0 ? "down" : ""}">${fmtUsd(cum, true)}</b>
        </div>
      `;
    }

    tooltip.hidden = false;
    const ttW = 225;
    const leftPos = Math.min(rect.width - ttW - 10, Math.max(10, pt.x - ttW / 2));
    tooltip.style.left = `${leftPos}px`;
    tooltip.style.top = `10px`;
  });

  canvas.addEventListener("mouseleave", () => {
    equityHoverIdx = null;
    tooltip.hidden = true;
    if (cachedHistory) drawEquityCurve(cachedHistory);
  });

  canvas.addEventListener("click", () => {
    if (equityHoverIdx !== null && equityChartGeom?.pts?.[equityHoverIdx]?.slug) {
      window.openRoundModal(equityChartGeom.pts[equityHoverIdx].slug);
    }
  });
}

// ============================================================================
// REGIME ATTRIBUTION MATRIX (SUPPORTS DICT OR ARRAY FROM /api/history)
// ============================================================================
function renderRegimeAttribution(regimeStats, tradedRounds = 0, totalRounds = 0) {
  const tbody = $("regime-attribution-tbody");
  const badge = $("regime-sample-badge");
  const covEl = $("regime-coverage");
  if (!tbody) return;

  if (!regimeStats || typeof regimeStats !== "object") {
    tbody.innerHTML = `<tr><td colspan="6" class="action-empty">暂无归因样本数据</td></tr>`;
    return;
  }

  // Convert dictionary { [regime]: { rounds, traded, wins, losses, flats, net_pnl, mean, ci_low, ci_high, sample_ok } } to sorted array
  let rows = [];
  if (Array.isArray(regimeStats.rows)) {
    rows = regimeStats.rows;
  } else {
    const order = [
      "range_bound",
      "normal_oscillation",
      "skewed_drift",
      "strong_trend",
      "wide_spread",
      "toxic_shock",
      "tail_freeze",
      "legacy",
    ];
    const keys = Object.keys(regimeStats).filter((k) => {
      const b = regimeStats[k];
      return b && (Number(b.traded || 0) > 0 || k !== "unlabeled");
    });
    keys.sort((a, b) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      if (ia !== -1 && ib !== -1) return ia - ib;
      if (ia !== -1) return -1;
      if (ib !== -1) return 1;
      return a.localeCompare(b);
    });

    rows = keys.map((k) => {
      const b = regimeStats[k] || {};
      const n = Number(b.traded || 0);
      const wins = Number(b.wins || 0);
      const losses = Number(b.losses || 0);
      const flats = Number(b.flats || 0);
      const decisive = wins + losses;
      const meta = REGIME_META[k] || { label: k, desc: "" };
      return {
        regime: k,
        label: meta.label,
        desc: meta.desc,
        rounds: Number(b.rounds || n),
        n,
        wins,
        losses,
        flats,
        win_rate_pct: decisive > 0 ? (wins / decisive) * 100 : null,
        total_pnl: Number(b.net_pnl || 0),
        mean_pnl: Number(b.mean || 0),
        ci95_low: n >= 2 ? Number(b.ci_low || 0) : null,
        ci95_high: n >= 2 ? Number(b.ci_high || 0) : null,
        low_sample: !b.sample_ok,
      };
    });
  }

  if (rows.length === 0) {
    setHtmlIfChanged(tbody, `<tr><td colspan="6" class="action-empty">暂无已成交形态归因数据</td></tr>`);
    return;
  }

  const legacyRow = rows.find((r) => r.regime === "legacy");
  const legacyCount = legacyRow ? legacyRow.n : 0;
  const attributedCount = Math.max(0, tradedRounds - legacyCount);

  if (badge) {
    badge.textContent = `实时归因 ${attributedCount} 场 · 共 ${tradedRounds} 成交场`;
  }

  setHtmlIfChanged(tbody, rows
    .map((r) => {
      const n = Number(r.n || 0);
      const totalPnl = Number(r.total_pnl || 0);
      const meanPnl = Number(r.mean_pnl || 0);
      const wr =
        r.win_rate_pct !== null && r.win_rate_pct !== undefined
          ? `${Number(r.win_rate_pct).toFixed(1)}%`
          : "--";
      const ciStr =
        r.ci95_low !== null && r.ci95_high !== null
          ? `[${fmtUsd(r.ci95_low, true)}, ${fmtUsd(r.ci95_high, true)}]`
          : "样本不足 (n<2)";

      const lowSampleTag =
        r.low_sample && n > 0
          ? `<span class="regime-sample-tag">样本 n&lt;30</span>`
          : "";

      return `
        <tr>
          <td class="regime-name-cell">
            <div class="regime-name-top">
              <strong>${escapeHtml(r.label || r.regime)}</strong>
              ${lowSampleTag}
            </div>
            ${r.desc ? `<small>${escapeHtml(r.desc)}</small>` : ""}
          </td>
          <td class="num">
            <div class="n-cell">
              <strong>${n} 场成交</strong>
              <span class="n-sub">共出现 ${r.rounds || n} 窗</span>
            </div>
          </td>
          <td class="num">
            <div class="n-cell">
              <strong>${wr}</strong>
              <span class="n-sub">${r.wins}胜 · ${r.losses}负 · ${r.flats}平</span>
            </div>
          </td>
          <td class="num ${totalPnl > 0 ? "up" : totalPnl < 0 ? "down" : ""}" style="font-weight:700">
            ${n > 0 ? fmtUsd(totalPnl, true) : "$0.00"}
          </td>
          <td class="num ${meanPnl > 0 ? "up" : meanPnl < 0 ? "down" : ""}">
            ${n > 0 ? fmtUsd(meanPnl, true) : "--"}
          </td>
          <td class="num ci-cell">${ciStr}</td>
        </tr>
      `;
    })
    .join(""));

  if (covEl) {
    covEl.hidden = false;
    setHtmlIfChanged(covEl, `
      <span class="coverage-label">归因覆盖率说明：</span>
      <span class="coverage-chip">监测总窗口 <b>${totalRounds}</b></span>
      <span class="coverage-chip">已成交窗口 <b>${tradedRounds}</b></span>
      <span class="coverage-chip">携带开仓实时形态标签 <b>${attributedCount}</b></span>
      ${legacyCount > 0 ? `<span class="coverage-chip">早期未标注历史成交 <b>${legacyCount}</b></span>` : ""}
    `);
  }
}

// ============================================================================
// TABLE ROW BUILDER FOR SETTLED ROUNDS (OVERVIEW & HISTORY)
// ============================================================================
function buildRoundTableRow(rawRec, includeInsuranceCol = false) {
  const r = rawRec.up_shares !== undefined ? rawRec : normalizeRecord(rawRec);
  const winInfo = slugToWindowText(r.slug, r.start_ts, r.end_ts);
  const winner = String(r.winner || "PENDING").toUpperCase();
  let winnerBadge = `<span class="winner-pill pending">平局 / 未定</span>`;
  if (winner === "UP") {
    winnerBadge = `<span class="winner-pill up">${SVG_ICONS.up}UP 胜出</span>`;
  } else if (winner === "DOWN") {
    winnerBadge = `<span class="winner-pill down">${SVG_ICONS.down}DOWN 胜出</span>`;
  }

  const openPx = Number(r.open_px || 0);
  const closePx = Number(r.close_px || 0);
  const diff = closePx - openPx;
  const diffPct = openPx > 0 ? (diff / openPx) * 100 : 0;
  const diffClass = diff > 0 ? "up" : diff < 0 ? "down" : "";
  const diffSign = diff > 0 ? "+" : "";

  const pnl = Number(r.pnl || 0);
  const pnlClass = pnl > 0 ? "up" : pnl < 0 ? "down" : "";

  const upShares = Number(r.up_shares || 0);
  const dnShares = Number(r.down_shares || 0);
  const costVal = Number(r.cost || 0);
  const payoutVal = Number(r.payout || 0);
  const fillsVal = Number(r.fills_count || 0);
  const insVal = Number(r.insurance || 0);

  const upCls = upShares > 1e-6 ? "cell-up" : "";
  const dnCls = dnShares > 1e-6 ? "cell-down" : "";
  const costCls = costVal > 1e-6 ? "cell-cost" : "";
  const payoutCls = payoutVal > 1e-6 ? "cell-payout" : "";
  const pnlStyle = Math.abs(pnl) > 1e-6 ? "font-weight:700" : "font-weight:600";

  const insCol = includeInsuranceCol
    ? `<td class="center">${fmtUsd(insVal)}</td>`
    : "";

  return `
    <tr class="history-row-clickable" data-slug="${escapeHtml(r.slug)}" title="点击调阅 ${escapeHtml(winInfo.fullLabel)} 的 5 分钟行情与交易复盘">
      <td>
        <span class="time-cell-main">${escapeHtml(winInfo.range)}</span>
        <small style="display:block;font-size:11.5px;color:var(--caption)">${escapeHtml(winInfo.dateStr)}</small>
      </td>
      <td class="center">${winnerBadge}</td>
      <td class="center col-price-cell">
        <span class="price-open-close">${fmtUsd(openPx)} → ${fmtUsd(closePx)}</span>
        <span class="price-diff-bps ${diffClass}">
          ${diffSign}${diff.toFixed(2)} (${diffSign}${diffPct.toFixed(3)}%)
        </span>
      </td>
      <td class="center ${upCls}">${fmtShares(upShares)}</td>
      <td class="center ${dnCls}">${fmtShares(dnShares)}</td>
      <td class="center ${costCls}">${fmtUsd(costVal)}</td>
      <td class="center ${payoutCls}">${fmtUsd(payoutVal)}</td>
      <td class="center ${pnlClass}" style="${pnlStyle}">${fmtUsd(pnl, true)}</td>
      <td class="center">${fillsVal}</td>
      ${insCol}
    </tr>
  `;
}

// ============================================================================
// VIEW 3: FULL HISTORY LEDGER & FILTERING
// ============================================================================
function renderHistoryTable(hist) {
  const tbody = $("history-tbody");
  const countEl = $("history-count");
  if (!tbody) return;

  const rawRecords = Array.isArray(hist?.records) ? hist.records : [];
  const allRecords = rawRecords.map(normalizeRecord).filter(Boolean);
  const filtered = allRecords.filter((r) => {
    if (historyFilterMode === "traded") return r.traded;
    if (historyFilterMode === "win") return r.pnl > 1e-6;
    if (historyFilterMode === "loss") return r.pnl < -1e-6;
    return true;
  });

  if (countEl) {
    const statHtml = historyFilterMode === "all"
      ? `共 <strong>${allRecords.length}</strong> 场`
      : `已筛选 <strong>${filtered.length}</strong> / ${allRecords.length} 场`;
    setHtmlIfChanged(countEl, statHtml);
  }

  if (filtered.length === 0) {
    setHtmlIfChanged(tbody, `<tr><td colspan="10" class="action-empty">当前筛选条件下暂无场次记录</td></tr>`);
    return;
  }
  setHtmlIfChanged(tbody, filtered.map((r) => buildRoundTableRow(r, true)).join(""));
}

// ============================================================================
// VIEW 2: LIVE MARKET WORKSPACE (实时工作区)
// ============================================================================
function updateMarket(live) {
  if (!live || live.status === "idle") return;

  const winInfo = slugToWindowText(live.slug, live.start, live.end);
  if ($("market-time-range")) $("market-time-range").textContent = winInfo.range;

  const spot = Number(live.spot || 0);
  const polySpot = Number(live.poly_spot || spot);
  const openPx = Number(live.open_px || 0);
  // Display-only: all visible price readouts use the Polymarket / Chainlink chain.
  // Backend return_bps (Binance-based) still drives the model and is untouched.
  const bps = openPx > 0 ? ((polySpot - openPx) / openPx) * 10000 : 0;
  const pct = bps / 100;
  const diffUsd = openPx > 0 ? (polySpot - openPx) : 0;

  if ($("market-spot")) $("market-spot").textContent = fmtUsd(polySpot);
  if ($("market-open")) $("market-open").textContent = `基准 ${fmtUsd(openPx)}`;

  const bpsEl = $("market-bps");
  const bpsSubEl = $("market-bps-sub");
  if (bpsEl) {
    const sign = diffUsd > 0 ? "+" : (diffUsd < 0 ? "-" : "");
    bpsEl.textContent = `${sign}$${Math.abs(diffUsd).toFixed(2)}`;
    bpsEl.className = `mc-val ${bps > 0 ? "up" : bps < 0 ? "down" : ""}`;
  }
  if (bpsSubEl) {
    const bpsSign = bps > 0 ? "+" : "";
    bpsSubEl.textContent = `${bpsSign}${pct.toFixed(3)}% · ${bpsSign}${bps.toFixed(1)} bps`;
  }

  // Risk & Circuit Breaker Status
  const cb = live.circuit_breaker || {};
  const riskPill = $("market-risk-pill");
  const riskText = $("market-risk");
  if (riskPill && riskText) {
    if (cb.active || cb.tripped) {
      riskPill.className = "status-pill status-critical";
      riskText.textContent = translateCircuitBreakerReason(cb.reason);
    } else {
      const regKey = live.regime || live.micro_regime || "normal_oscillation";
      const regLabel = REGIME_META[regKey]?.label?.split(" ")[0] || "流动性正常";
      riskPill.className = "status-pill status-ready";
      riskText.textContent = `${regLabel} · 双源同步`;
    }
  }

  // Unified Game-Style Strategy Decision Bulletin (single source of truth)
  renderDecisionBulletin(live);

  // UP / DOWN Positions & Orderbook (handles best_bid/best_ask AND bid/ask, cash AND cost)
  const posUp = live.pos_up || {};
  const posDn = live.pos_down || {};
  const bookUp = live.book_up || {};
  const bookDn = live.book_down || {};

  const upShares = Number(posUp.shares || 0);
  const dnShares = Number(posDn.shares || 0);
  const upCost = Number(posUp.cash ?? posUp.cost ?? 0);
  const dnCost = Number(posDn.cash ?? posDn.cost ?? 0);
  const totalCost = upCost + dnCost;

  if ($("up-pos-shares")) $("up-pos-shares").textContent = `${fmtShares(upShares)} 股`;
  if ($("up-pos-avg")) $("up-pos-avg").textContent = upShares > 0 ? fmtPriceCents(posUp.avg_px) : "--";
  if ($("up-pos-cost")) $("up-pos-cost").textContent = fmtUsd(upCost);

  if ($("down-pos-shares")) $("down-pos-shares").textContent = `${fmtShares(dnShares)} 股`;
  if ($("down-pos-avg")) $("down-pos-avg").textContent = dnShares > 0 ? fmtPriceCents(posDn.avg_px) : "--";
  if ($("down-pos-cost")) $("down-pos-cost").textContent = fmtUsd(dnCost);

  const upWinNet = upShares * 1.0 - totalCost;
  const dnWinNet = dnShares * 1.0 - totalCost;

  const fUpEl = $("pnl-forecast-up");
  if (fUpEl) {
    fUpEl.textContent = totalCost > 0 ? `若 UP 胜出: ${fmtUsd(upWinNet, true)}` : "当前空仓观望";
    fUpEl.style.color = totalCost > 0 ? (upWinNet >= 0 ? "var(--up)" : "var(--down)") : "var(--label)";
  }
  const fDnEl = $("pnl-forecast-down");
  if (fDnEl) {
    fDnEl.textContent = totalCost > 0 ? `若 DOWN 胜出: ${fmtUsd(dnWinNet, true)}` : "当前空仓观望";
    fDnEl.style.color = totalCost > 0 ? (dnWinNet >= 0 ? "var(--up)" : "var(--down)") : "var(--label)";
  }

  const upBid = bookUp.best_bid ?? bookUp.bid ?? null;
  const upAsk = bookUp.best_ask ?? bookUp.ask ?? null;
  const dnBid = bookDn.best_bid ?? bookDn.bid ?? null;
  const dnAsk = bookDn.best_ask ?? bookDn.ask ?? null;

  if ($("up-bid-px")) $("up-bid-px").textContent = fmtPriceCents(upBid);
  if ($("up-ask-px")) $("up-ask-px").textContent = fmtPriceCents(upAsk);
  const upMid =
    upBid && upAsk ? (Number(upBid) + Number(upAsk)) / 2 : Number(live.poly_up ?? upBid ?? upAsk ?? 0.5);
  if ($("up-prob-bar")) {
    $("up-prob-bar").style.width = `${Math.min(100, Math.max(0, upMid * 100)).toFixed(1)}%`;
  }
  if ($("up-book-status")) {
    if (upBid && upAsk) {
      const sp = Math.max(0, Number(upAsk) - Number(upBid));
      $("up-book-status").textContent = `隐含 ${(upMid * 100).toFixed(1)}% · 价差 $${sp.toFixed(2)}`;
    } else {
      $("up-book-status").textContent = "盘口深度就绪";
    }
  }

  if ($("down-bid-px")) $("down-bid-px").textContent = fmtPriceCents(dnBid);
  if ($("down-ask-px")) $("down-ask-px").textContent = fmtPriceCents(dnAsk);
  const dnMid =
    dnBid && dnAsk ? (Number(dnBid) + Number(dnAsk)) / 2 : Number(live.poly_down ?? dnBid ?? dnAsk ?? 0.5);
  if ($("down-prob-bar")) {
    $("down-prob-bar").style.width = `${Math.min(100, Math.max(0, dnMid * 100)).toFixed(1)}%`;
  }
  if ($("down-book-status")) {
    if (dnBid && dnAsk) {
      const sp = Math.max(0, Number(dnAsk) - Number(dnBid));
      $("down-book-status").textContent = `隐含 ${(dnMid * 100).toFixed(1)}% · 价差 $${sp.toFixed(2)}`;
    } else {
      $("down-book-status").textContent = "盘口深度就绪";
    }
  }

  renderWorkingOrders(live.working_orders || []);
  renderActionFeed(live.actions || [], live.start);

  updateLiveChartReadouts(live, spot, polySpot, openPx, upMid);
  drawLivePriceChart(live);
}

function updateLiveChartReadouts(live, spot, polySpot, openPx, upMid) {
  const hist = Array.isArray(live.price_history) ? live.price_history : [];
  // Display-only: high / low / amplitude follow the Chainlink 60s TWAP series.
  let hi = polySpot;
  let lo = polySpot;
  hist.forEach((p) => {
    const v = Number(p.poly_price ?? p.poly_spot ?? 0);
    if (v > 0) {
      if (hi === 0 || v > hi) hi = v;
      if (lo === 0 || v < lo) lo = v;
    }
  });
  const rangeBps = openPx > 0 && hi > 0 && lo > 0 ? ((hi - lo) / openPx) * 10000 : 0;

  if ($("chart-stat-open")) $("chart-stat-open").textContent = fmtUsd(openPx);
  if ($("chart-stat-spot")) $("chart-stat-spot").textContent = fmtUsd(polySpot);
  if ($("chart-stat-poly-spot")) $("chart-stat-poly-spot").textContent = fmtUsd(polySpot);
  if ($("chart-stat-high")) $("chart-stat-high").textContent = fmtUsd(hi);
  if ($("chart-stat-low")) $("chart-stat-low").textContent = fmtUsd(lo);
  if ($("chart-stat-range")) $("chart-stat-range").textContent = `${rangeBps.toFixed(1)} bps`;

  const lastPt = hist.length > 0 ? hist[hist.length - 1] : null;
  const probUpMarket = lastPt?.poly_up ?? live.poly_up ?? upMid ?? null;
  const probUpModel = lastPt?.prob_up ?? live.prob_up ?? null;
  const edge =
    probUpModel !== null && probUpMarket !== null ? Number(probUpModel) - Number(probUpMarket) : 0;

  if ($("chart-stat-prob-up")) {
    $("chart-stat-prob-up").textContent =
      probUpMarket !== null ? `${(Number(probUpMarket) * 100).toFixed(1)}%` : "--%";
  }
  if ($("chart-stat-prob-model")) {
    $("chart-stat-prob-model").textContent =
      probUpModel !== null ? `${(Number(probUpModel) * 100).toFixed(1)}%` : "--%";
  }
  const edgeEl = $("chart-stat-edge");
  if (edgeEl) {
    const edgePct = edge * 100;
    edgeEl.textContent = `${edgePct >= 0 ? "+" : ""}${edgePct.toFixed(1)}%`;
    edgeEl.className = edgePct > 0 ? "up" : edgePct < 0 ? "down" : "";
  }
}

function renderWorkingOrders(orders) {
  const pill = $("working-order-pill");
  const box = $("live-working-orders-box");
  if (!pill || !box) return;

  if (!Array.isArray(orders) || orders.length === 0) {
    pill.textContent = "当前无在途排队挂单";
    pill.className = "order-pill";
    box.hidden = true;
    setHtmlIfChanged(box, "");
    return;
  }

  pill.textContent = `${orders.length} 笔挂单排队撮合中`;
  pill.className = "order-pill active";
  box.hidden = false;

  setHtmlIfChanged(box, orders
    .map((o) => {
      const side = String(o.side || "Up").toUpperCase();
      const px = Number(o.price ?? o.px ?? 0);
      const rem = Number(o.remaining_shares ?? o.size ?? o.shares ?? 0);
      const qAhead = Number(o.queue_ahead ?? 0);

      return `
        <div class="queue-status-card active ${side === "UP" ? "up" : "down"}">
          <div class="queue-card-top">
            <span class="queue-badge pulsing">FIFO 排队中</span>
            <span class="queue-order-id">#${escapeHtml(o.order_id || "Maker")}</span>
          </div>
          <div class="queue-card-body">
            <div class="queue-order-spec">
              <span class="queue-side ${side === "UP" ? "up" : "down"}">${side} 限价买单</span>
              <span class="queue-price">@ $${px.toFixed(2)}</span>
              <span class="queue-size">${rem.toFixed(1)} 股</span>
            </div>
            <div class="queue-progress-info">
              <span>前方排队深度: <strong class="highlight-q">${qAhead.toFixed(1)} 股</strong></span>
            </div>
          </div>
        </div>
      `;
    })
    .join(""));
}

function formatActionItem(a, startTs = 0) {
  if (typeof a === "string") {
    return {
      timeStr: "实时",
      typeLabel: "记录",
      typeClass: "quote",
      sideLabel: "--",
      sideClass: "",
      specStr: "--",
      reasonStr: translateDecisionReason(a),
    };
  }
  const t = Number(a.t || a.ts || 0);
  let timeStr = "--:--:--";
  if (startTs > 0 && t > 1e9) {
    const sec = Math.max(0, Math.round(t - startTs));
    const m = Math.floor(sec / 60);
    const s = String(sec % 60).padStart(2, "0");
    timeStr = `+0${m}:${s} (${sec}s)`;
  } else if (t > 1e9) {
    const fmt = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    timeStr = fmt.format(new Date(t * 1000));
  }

  const act = String(a.action || a.kind || "fill").toLowerCase();
  let typeLabel = "撮合成交";
  let typeClass = "fill";
  if (act === "quote") {
    typeLabel = "限价报单";
    typeClass = "quote";
  } else if (act === "cancel") {
    typeLabel = "队列撤单";
    typeClass = "cancel";
  }

  const side = String(a.side || "--").toUpperCase();
  const sideClass = side === "UP" ? "up" : side === "DOWN" ? "down" : "";
  const px = Number(a.price ?? a.px ?? 0);
  const sz = Number(a.size ?? a.shares ?? 0);
  const specStr = px > 0 && sz > 0 ? `$${px.toFixed(2)} × ${sz.toFixed(1)}股` : "--";
  const reasonStr = translateDecisionReason(a.reason || a.msg || "");

  return { timeStr, typeLabel, typeClass, sideLabel: side, sideClass, specStr, reasonStr };
}

function renderActionFeed(actions, startTs = 0) {
  const list = $("live-action-feed");
  const countEl = $("live-action-count");
  if (!list) return;

  const arr = Array.isArray(actions) ? actions : [];
  if (countEl) countEl.textContent = `${arr.length} 笔动作`;

  if (arr.length === 0) {
    setHtmlIfChanged(list, `<li class="action-empty">本轮暂无报单或撮合动作，策略持续监控盘口边际中...</li>`);
    return;
  }

  setHtmlIfChanged(list, arr
    .slice()
    .reverse()
    .slice(0, 30)
    .map((raw) => {
      const item = formatActionItem(raw, startTs);
      return `
        <li class="action-item ${item.typeClass === "cancel" ? "cancel" : ""}">
          <span class="action-time">${escapeHtml(item.timeStr)}</span>
          <span class="action-type ${item.typeClass}">${escapeHtml(item.typeLabel)}</span>
          <span class="action-side ${item.sideClass}">${escapeHtml(item.sideLabel)}</span>
          <span>${escapeHtml(item.specStr)}</span>
          <span class="action-desc">${escapeHtml(item.reasonStr)}</span>
        </li>
      `;
    })
    .join(""));
}

// ============================================================================
// NORMALIZE PRICE HISTORY POINT (SUPPORTS BOTH live.json AND window.json.gz)
// ============================================================================
function normalizePricePoint(p, startTs, openPx, idx, totalLen) {
  const t = Number(p.t ?? p.ts ?? 0);
  const elapsed =
    startTs > 0 && t > 1e9
      ? Math.min(300, Math.max(0, t - startTs))
      : (idx / Math.max(1, totalLen - 1)) * 300;
  const spot = Number(p.spot_price ?? p.price ?? p.spot ?? openPx);
  const polySpot = Number(p.poly_price ?? p.poly_spot ?? spot);
  const upMid =
    p.poly_up !== undefined && p.poly_up !== null
      ? Number(p.poly_up)
      : p.up_mid !== undefined && p.up_mid !== null
      ? Number(p.up_mid)
      : null;
  const dnMid =
    p.poly_down !== undefined && p.poly_down !== null
      ? Number(p.poly_down)
      : p.dn_mid !== undefined && p.dn_mid !== null
      ? Number(p.dn_mid)
      : null;
  const pModel =
    p.prob_up !== undefined && p.prob_up !== null
      ? Number(p.prob_up)
      : p.p_model !== undefined && p.p_model !== null
      ? Number(p.p_model)
      : null;

  return { elapsed, spot, poly_spot: polySpot, up_mid: upMid, dn_mid: dnMid, p_model: pModel };
}

// ============================================================================
// LIVE 5-MINUTE DUAL-PANE SYNCHRONIZED CHART (PRICE + PROBABILITY)
// ============================================================================
function drawLivePriceChart(live) {
  const canvas = $("live-price-canvas");
  if (!canvas || !live) return;

  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(320, rect.width || 800);
  const h = Math.max(280, rect.height || 390);

  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pal = getPalette();
  const hist = Array.isArray(live.price_history) ? live.price_history : [];
  const startTs = Number(live.start || 0);
  // Display-only: chart strike is the Polymarket / Chainlink opening price.
  const openPx = Number(live.open_px || live.spot || 0);

  if (hist.length < 2 || !startTs) {
    liveChartGeom = null;
    ctx.fillStyle = pal.axis;
    ctx.font = "500 13px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("正在采集本轮秒级行情与胜率轨迹...", w / 2, h / 2);
    return;
  }

  const padL = 66;
  const padR = 58;
  const padT = 18;
  const padB = 28;
  const plotW = w - padL - padR;
  const totalPlotH = h - padT - padB;

  let priceTop = padT;
  let priceH = totalPlotH;
  let probTop = padT;
  let probH = 0;

  if (chartViewMode === "dual") {
    priceH = Math.round(totalPlotH * 0.63);
    probTop = padT + priceH + 18;
    probH = totalPlotH - priceH - 18;
  } else if (chartViewMode === "prob") {
    priceH = 0;
    probTop = padT;
    probH = totalPlotH;
  }

  // Display-only: the price pane renders the recorded Chainlink 60s TWAP series.
  // Forward-fill stale oracle ticks instead of letting normalizePricePoint fall
  // back to a Binance print, so no Binance price can leak into this chart.
  let lastTwap = openPx;
  const pts = hist.map((p, i) => {
    const np = normalizePricePoint(p, startTs, openPx, i, hist.length);
    const twap = Number(p.poly_price ?? 0);
    if (twap > 0) lastTwap = twap;
    np.poly_spot = lastTwap;
    return np;
  });
  const xOfSec = (sec) => padL + (Math.min(300, Math.max(0, sec)) / 300) * plotW;

  const priceVals = [openPx];
  pts.forEach((p) => {
    if (p.poly_spot > 0) priceVals.push(p.poly_spot);
  });
  let minPx = Math.min(...priceVals);
  let maxPx = Math.max(...priceVals);
  if (Math.abs(maxPx - minPx) < 4) {
    maxPx += 4;
    minPx -= 4;
  } else {
    const span = maxPx - minPx;
    maxPx += span * 0.14;
    minPx -= span * 0.14;
  }

  const yOfPrice = (px) => priceTop + ((maxPx - px) / (maxPx - minPx)) * priceH;
  const yOfProb = (prob) => probTop + (1 - Math.min(1, Math.max(0, prob))) * probH;

  liveChartGeom = {
    w,
    h,
    padL,
    padR,
    padT,
    padB,
    plotW,
    priceTop,
    priceH,
    probTop,
    probH,
    openPx,
    pts,
    xOfSec,
    yOfPrice,
    yOfProb,
  };

  // Vertical time grid (every 60s: 0:00 .. 5:00)
  ctx.font = "500 10.5px 'JetBrains Mono', monospace";
  for (let sec = 0; sec <= 300; sec += 60) {
    const x = xOfSec(sec);
    ctx.strokeStyle = pal.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, h - padB);
    ctx.stroke();

    const mins = Math.floor(sec / 60);
    ctx.fillStyle = pal.axis;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(`0${mins}:00`, x, h - padB + 8);
  }

  // -------------------- UPPER PANE: PRICE TRAJECTORY --------------------
  if (priceH > 0) {
    const pSteps = 4;
    for (let i = 0; i <= pSteps; i++) {
      const val = minPx + ((maxPx - minPx) * i) / pSteps;
      const y = yOfPrice(val);
      ctx.strokeStyle = pal.grid;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();

      ctx.fillStyle = pal.axis;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(`$${val.toFixed(1)}`, padL - 8, y);

      if (openPx > 0) {
        const bps = ((val - openPx) / openPx) * 10000;
        const sign = bps > 0 ? "+" : "";
        ctx.textAlign = "left";
        ctx.fillText(`${sign}${bps.toFixed(0)}bp`, w - padR + 6, y);
      }
    }

    if (openPx >= minPx && openPx <= maxPx) {
      const openY = yOfPrice(openPx);
      ctx.save();
      ctx.strokeStyle = pal.amber;
      ctx.lineWidth = 1.2;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, openY);
      ctx.lineTo(w - padR, openY);
      ctx.stroke();
      ctx.restore();
    }

    // Chainlink 60s TWAP — the only displayed price series (display-only change).
    // Binance spot stays in the recorded data and in every backend calculation.
    const twapGrad = ctx.createLinearGradient(0, priceTop, 0, priceTop + priceH);
    twapGrad.addColorStop(0, pal.spotFillTop);
    twapGrad.addColorStop(1, pal.spotFillBot);

    ctx.beginPath();
    pts.forEach((p, idx) => {
      const x = xOfSec(p.elapsed);
      const y = yOfPrice(p.poly_spot);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOfSec(pts[pts.length - 1].elapsed), priceTop + priceH);
    ctx.lineTo(xOfSec(pts[0].elapsed), priceTop + priceH);
    ctx.closePath();
    ctx.fillStyle = twapGrad;
    ctx.fill();

    ctx.beginPath();
    pts.forEach((p, idx) => {
      const x = xOfSec(p.elapsed);
      const y = yOfPrice(p.poly_spot);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = pal.twap;
    ctx.lineWidth = 2.2;
    ctx.lineJoin = "round";
    ctx.stroke();
  }

  // -------------------- LOWER PANE: IMPLIED PROBABILITY & MODEL P* --------------------
  if (probH > 0) {
    if (chartViewMode === "dual") {
      ctx.strokeStyle = pal.divider;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, probTop - 9);
      ctx.lineTo(w - padR, probTop - 9);
      ctx.stroke();
    }

    [0.2, 0.5, 0.8].forEach((prob) => {
      const y = yOfProb(prob);
      ctx.strokeStyle = prob === 0.5 ? pal.divider : pal.gridSoft;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();

      ctx.fillStyle = pal.axis;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(`${Math.round(prob * 100)}%`, padL - 8, y);
    });

    ctx.beginPath();
    let startedUp = false;
    pts.forEach((p) => {
      if (p.up_mid === null) return;
      const x = xOfSec(p.elapsed);
      const y = yOfProb(p.up_mid);
      if (!startedUp) {
        ctx.moveTo(x, y);
        startedUp = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    if (startedUp) {
      ctx.strokeStyle = pal.up;
      ctx.lineWidth = 1.8;
      ctx.stroke();
    }

    ctx.beginPath();
    let startedModel = false;
    pts.forEach((p) => {
      if (p.p_model === null) return;
      const x = xOfSec(p.elapsed);
      const y = yOfProb(p.p_model);
      if (!startedModel) {
        ctx.moveTo(x, y);
        startedModel = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    if (startedModel) {
      ctx.strokeStyle = pal.model;
      ctx.lineWidth = 1.8;
      ctx.stroke();
    }
  }

  // Hover Crosshair
  if (liveChartHoverIdx !== null && pts[liveChartHoverIdx]) {
    const hp = pts[liveChartHoverIdx];
    const hx = xOfSec(hp.elapsed);
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(248, 250, 252, 0.35)" : "rgba(15, 23, 42, 0.25)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hx, padT);
    ctx.lineTo(hx, h - padB);
    ctx.stroke();
    ctx.restore();

    if (priceH > 0) {
      const hy = yOfPrice(hp.poly_spot);
      ctx.beginPath();
      ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = pal.twap;
      ctx.fill();
      ctx.strokeStyle = pal.cardBg;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
  }
}

function initLiveChartInteractions() {
  const canvas = $("live-price-canvas");
  const tooltip = $("chart-tooltip");
  if (!canvas || !tooltip) return;

  canvas.addEventListener("mousemove", (e) => {
    if (!liveChartGeom || !liveChartGeom.pts?.length) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;

    let bestIdx = 0;
    let bestDist = Infinity;
    liveChartGeom.pts.forEach((p, idx) => {
      const px = liveChartGeom.xOfSec(p.elapsed);
      const d = Math.abs(px - mx);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = idx;
      }
    });

    liveChartHoverIdx = bestIdx;
    drawLivePriceChart(cachedLive);

    const pt = liveChartGeom.pts[bestIdx];
    const openPx = liveChartGeom.openPx || pt.poly_spot;
    const bps = openPx > 0 ? ((pt.poly_spot - openPx) / openPx) * 10000 : 0;
    const sign = bps > 0 ? "+" : "";

    tooltip.innerHTML = `
      <div class="tt-time" style="display:flex;align-items:center;justify-content:space-between;gap:10px">
        <span>第 ${Math.round(pt.elapsed)} 秒切片</span>
        <span class="${bps >= 0 ? "up" : "down"}">${sign}${bps.toFixed(1)} bps</span>
      </div>
      <div class="tt-row"><span>Chainlink 60s TWAP</span><b>${fmtUsd(pt.poly_spot)}</b></div>
      <div class="tt-row"><span>UP 盘口隐含概率</span><b class="up">${pt.up_mid !== null ? (pt.up_mid * 100).toFixed(1) + "%" : "--"}</b></div>
      <div class="tt-row"><span>模型公允胜率 P*</span><b style="color:var(--model)">${pt.p_model !== null ? (pt.p_model * 100).toFixed(1) + "%" : "--"}</b></div>
    `;
    tooltip.hidden = false;

    const hx = liveChartGeom.xOfSec(pt.elapsed);
    const ttW = 225;
    const leftPos = Math.min(rect.width - ttW - 12, Math.max(12, hx + 14));
    tooltip.style.left = `${leftPos}px`;
    tooltip.style.top = `16px`;
  });

  canvas.addEventListener("mouseleave", () => {
    liveChartHoverIdx = null;
    tooltip.hidden = true;
    if (cachedLive) drawLivePriceChart(cachedLive);
  });

  const toggleGroup = $("chart-mode-toggle");
  if (toggleGroup) {
    toggleGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".mode-btn");
      if (!btn) return;
      chartViewMode = btn.dataset.mode || "dual";
      toggleGroup.querySelectorAll(".mode-btn").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      if (cachedLive) drawLivePriceChart(cachedLive);
    });
  }
}

// ============================================================================
// 5-MINUTE ROUND REVIEW MODAL (CLICK ANY SETTLED ROUND OR EQUITY POINT)
// ============================================================================
async function openRoundModal(slug) {
  if (!slug) return;
  const modal = $("round-detail-modal");
  const body = $("modal-round-body");
  const slugEl = $("modal-round-slug");
  const winnerEl = $("modal-round-winner");
  const titleEl = $("modal-round-title");

  if (!modal || !body) return;
  modal.hidden = false;
  modalHoverIdx = null;
  modalSelectedActionIdx = null;

  const rawRec = (cachedHistory?.records || []).find((r) => r.slug === slug) || null;
  const rec = normalizeRecord(rawRec);
  cachedModalRecord = rec;
  const winInfo = slugToWindowText(slug, rec?.start_ts, rec?.end_ts);

  if (titleEl) titleEl.textContent = `${winInfo.fullLabel} 轮次复盘详情`;
  if (slugEl) slugEl.textContent = slug;
  if (winnerEl) {
    const w = String(rec?.winner || "SETTLED").toUpperCase();
    winnerEl.className = `winner-pill ${w === "UP" ? "up" : w === "DOWN" ? "down" : "pending"}`;
    winnerEl.textContent = w === "UP" ? "UP 胜出" : w === "DOWN" ? "DOWN 胜出" : "平局";
  }

  body.innerHTML = `<div class="action-empty">正在加载 ${escapeHtml(slug)} 的 5 分钟秒级复盘数据...</div>`;

  try {
    const res = await fetch(`/api/window?slug=${encodeURIComponent(slug)}`, { cache: "no-store" });
    const winData = await res.json();
    cachedModalWindow = winData;
    renderModalDetailContent(winData, rec);
  } catch (err) {
    body.innerHTML = `<div class="action-empty">无法加载该轮次详细轨迹数据 (${escapeHtml(err.message)})</div>`;
  }
}

function closeRoundModal() {
  const modal = $("round-detail-modal");
  if (modal) modal.hidden = true;
  cachedModalWindow = null;
  cachedModalRecord = null;
  modalHoverIdx = null;
  modalSelectedActionIdx = null;
}

window.openRoundModal = openRoundModal;
window.closeRoundModal = closeRoundModal;

function renderModalDetailContent(winData, rec) {
  const body = $("modal-round-body");
  if (!body) return;

  const normWin = normalizeRecord(winData);
  const openPx = Number(rec?.open_px || normWin?.open_px || 0);
  const closePx = Number(rec?.close_px || normWin?.close_px || 0);
  const upShares = Number(rec?.up_shares ?? normWin?.up_shares ?? 0);
  const dnShares = Number(rec?.down_shares ?? normWin?.down_shares ?? 0);
  const cost = Number(rec?.cost ?? normWin?.cost ?? 0);
  const payout = Number(rec?.payout ?? normWin?.payout ?? 0);
  const pnl = Number(rec?.pnl ?? normWin?.pnl ?? 0);
  const startTs = Number(winData?.start || rec?.start_ts || 0);
  const actions = Array.isArray(winData?.actions) ? winData.actions : [];

  const diffPx = closePx - openPx;
  const diffPct = openPx > 0 ? (diffPx / openPx) * 100 : 0;
  const diffSign = diffPx > 0 ? "+" : "";
  const diffCls = diffPx > 0 ? "up" : diffPx < 0 ? "down" : "";
  const openShort = openPx.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const closeShort = closePx.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  body.innerHTML = `
    <div class="modal-summary-grid">
      <div class="modal-kpi modal-kpi-wide">
        <span class="modal-kpi-label">开盘基准 → 交割收盘</span>
        <strong class="modal-kpi-val">$${openShort} → $${closeShort}</strong>
        <span class="modal-kpi-sub ${diffCls}">${diffSign}$${diffPx.toFixed(2)} (${diffSign}${diffPct.toFixed(3)}%)</span>
      </div>
      <div class="modal-kpi">
        <span class="modal-kpi-label">UP 期末持仓</span>
        <strong class="modal-kpi-val ${upShares > 0 ? "up" : ""}">${fmtShares(upShares)} <span class="val-unit">股</span></strong>
      </div>
      <div class="modal-kpi">
        <span class="modal-kpi-label">DN 期末持仓</span>
        <strong class="modal-kpi-val ${dnShares > 0 ? "down" : ""}">${fmtShares(dnShares)} <span class="val-unit">股</span></strong>
      </div>
      <div class="modal-kpi">
        <span class="modal-kpi-label">总成本 → 期末兑现</span>
        <strong class="modal-kpi-val">${fmtUsd(cost)} → ${fmtUsd(payout)}</strong>
      </div>
      <div class="modal-kpi">
        <span class="modal-kpi-label">本轮已实现净损益</span>
        <strong class="modal-kpi-val ${pnl > 0 ? "up" : pnl < 0 ? "down" : ""}">${fmtUsd(pnl, true)}</strong>
      </div>
    </div>

    <div class="detail-card">
      <div class="detail-card-head">
        <span class="detail-card-title">${SVG_ICONS.chart}<span>5 分钟秒级价格与胜率复盘图</span></span>
        <div class="detail-card-legend">
          <span><i class="legend-dot btc"></i>Binance 现货</span>
          <span><i class="legend-dot twap"></i>60s TWAP</span>
          <span><i class="legend-dot up-prob"></i>UP 盘口隐含</span>
          <span><i class="legend-dot fair-prob"></i>模型胜率 P*</span>
        </div>
      </div>
      <div class="history-canvas-box">
        <canvas id="modal-hist-canvas" height="360"></canvas>
        <div class="chart-tooltip" id="modal-chart-tooltip" hidden></div>
      </div>
    </div>

    <div class="detail-card table-card">
      <div class="detail-card-head">
        <span class="detail-card-title">${SVG_ICONS.list}<span>撮合与策略决策流水 (${actions.length} 条)</span></span>
      </div>
      <div class="table-wrap">
        <table class="detail-actions-table">
          <thead>
            <tr>
              <th>窗口时间</th>
              <th>事件类型</th>
              <th>方向</th>
              <th class="num">价格</th>
              <th class="num">股数</th>
              <th class="num">金额</th>
              <th>策略决策依据</th>
            </tr>
          </thead>
          <tbody>
            ${
              actions.length === 0
                ? `<tr><td colspan="7" class="action-empty">该窗口内策略未触发开仓动作 (全程观望)</td></tr>`
                : actions
                    .map((a, idx) => {
                      const item = formatActionItem(a, startTs);
                      const px = Number(a.price ?? a.px ?? 0);
                      const sz = Number(a.size ?? a.shares ?? 0);
                      const notional = px * sz;
                      const actT = Number(a.t ?? a.ts ?? a.timestamp ?? startTs);
                      const elapsed = Math.min(300, Math.max(0, startTs > 0 ? actT - startTs : actT));
                      const evtCls = item.typeClass === "fill" ? "fill" : item.typeClass === "cancel" ? "cancel" : "quote";
                      return `
                        <tr class="modal-action-row" data-action-idx="${idx}" data-elapsed="${elapsed.toFixed(2)}">
                          <td>${escapeHtml(item.timeStr)}</td>
                          <td><span class="evt-tag ${evtCls}">${escapeHtml(item.typeLabel)}</span></td>
                          <td><span class="side-txt ${item.sideClass || ""}">${escapeHtml(item.sideLabel)}</span></td>
                          <td class="num">${px > 0 ? "$" + px.toFixed(2) : "--"}</td>
                          <td class="num">${sz > 0 ? sz.toFixed(1) : "--"}</td>
                          <td class="num">${notional > 0 ? fmtUsd(notional) : "--"}</td>
                          <td class="reason-cell">${escapeHtml(item.reasonStr || "--")}</td>
                        </tr>
                      `;
                    })
                    .join("")
            }
          </tbody>
        </table>
      </div>
    </div>
  `;

  requestAnimationFrame(() => {
    drawModalHistoryChart(winData, rec);
    bindModalChartHover();
    bindModalActionRowClicks(winData, startTs);
  });
}

function drawModalHistoryChart(winData, rec) {
  const canvas = $("modal-hist-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(320, rect.width || 760);
  const h = Math.max(280, rect.height || 360);

  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pal = getPalette();
  const hist = Array.isArray(winData?.price_history) ? winData.price_history : [];
  if (hist.length < 2) {
    modalChartGeom = null;
    ctx.fillStyle = pal.axis;
    ctx.font = "500 12.5px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("该历史场次未保存秒级行情切片（仅记录交割汇总账本）", w / 2, h / 2);
    return;
  }

  const startTs = Number(winData.start || winData.start_ts || rec?.start_ts || hist[0].t || hist[0].ts || 0);
  const openPx = Number(
    winData.binance_open_px || rec?.open_px || winData.open_px || hist[0].price || hist[0].spot || 0
  );

  const padL = 62;
  const padR = 24;
  const padT = 16;
  const padB = 26;
  const plotW = w - padL - padR;
  const totalH = h - padT - padB;
  const priceH = Math.round(totalH * 0.64);
  const probTop = padT + priceH + 14;
  const probH = totalH - priceH - 14;

  const pts = hist.map((p, i) => normalizePricePoint(p, startTs, openPx, i, hist.length));

  const vals = [openPx];
  pts.forEach((p) => {
    if (p.spot > 0) vals.push(p.spot);
    if (p.poly_spot > 0) vals.push(p.poly_spot);
  });
  let minPx = Math.min(...vals);
  let maxPx = Math.max(...vals);
  if (Math.abs(maxPx - minPx) < 4) {
    maxPx += 4;
    minPx -= 4;
  } else {
    const span = maxPx - minPx;
    maxPx += span * 0.12;
    minPx -= span * 0.12;
  }

  const xOfSec = (s) => padL + (Math.min(300, Math.max(0, s)) / 300) * plotW;
  const yOfPrice = (px) => padT + ((maxPx - px) / (maxPx - minPx)) * priceH;
  const yOfProb = (prob) => probTop + (1 - Math.min(1, Math.max(0, prob))) * probH;

  modalChartGeom = {
    w,
    h,
    padL,
    padR,
    padT,
    padB,
    priceH,
    probTop,
    probH,
    openPx,
    pts,
    xOfSec,
    yOfPrice,
    yOfProb,
  };

  ctx.font = "500 10px 'JetBrains Mono', monospace";
  for (let s = 0; s <= 300; s += 60) {
    const x = xOfSec(s);
    ctx.strokeStyle = pal.grid;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, h - padB);
    ctx.stroke();

    ctx.fillStyle = pal.axis;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(`0${Math.floor(s / 60)}:00`, x, h - padB + 6);
  }

  // Price grid
  for (let i = 0; i <= 3; i++) {
    const val = minPx + ((maxPx - minPx) * i) / 3;
    const y = yOfPrice(val);
    ctx.strokeStyle = pal.grid;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();

    ctx.fillStyle = pal.axis;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(`$${val.toFixed(1)}`, padL - 6, y);
  }

  // Open benchmark
  if (openPx >= minPx && openPx <= maxPx) {
    const oy = yOfPrice(openPx);
    ctx.save();
    ctx.strokeStyle = pal.amber;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, oy);
    ctx.lineTo(w - padR, oy);
    ctx.stroke();
    ctx.restore();
  }

  // TWAP dashed line
  ctx.save();
  ctx.strokeStyle = pal.twap;
  ctx.lineWidth = 1.4;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  pts.forEach((p, idx) => {
    const x = xOfSec(p.elapsed);
    const y = yOfPrice(p.poly_spot);
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.restore();

  // Spot line
  ctx.beginPath();
  pts.forEach((p, idx) => {
    const x = xOfSec(p.elapsed);
    const y = yOfPrice(p.spot);
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = pal.spot;
  ctx.lineWidth = 2;
  ctx.stroke();

  // Prob pane
  [0.2, 0.5, 0.8].forEach((pr) => {
    const y = yOfProb(pr);
    ctx.strokeStyle = pal.gridSoft;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
  });

  ctx.beginPath();
  let hasUp = false;
  pts.forEach((p) => {
    if (p.up_mid === null) return;
    const x = xOfSec(p.elapsed);
    const y = yOfProb(p.up_mid);
    if (!hasUp) {
      ctx.moveTo(x, y);
      hasUp = true;
    } else ctx.lineTo(x, y);
  });
  if (hasUp) {
    ctx.strokeStyle = pal.up;
    ctx.lineWidth = 1.6;
    ctx.stroke();
  }

  ctx.beginPath();
  let hasMod = false;
  pts.forEach((p) => {
    if (p.p_model === null) return;
    const x = xOfSec(p.elapsed);
    const y = yOfProb(p.p_model);
    if (!hasMod) {
      ctx.moveTo(x, y);
      hasMod = true;
    } else ctx.lineTo(x, y);
  });
  if (hasMod) {
    ctx.strokeStyle = pal.model;
    ctx.lineWidth = 1.6;
    ctx.stroke();
  }

  // Action event markers along timeline (subtle baseline dots + selected action spotlight)
  const actions = Array.isArray(winData?.actions) ? winData.actions : [];
  actions.forEach((a, idx) => {
    const actT = Number(a.t ?? a.ts ?? a.timestamp ?? startTs);
    const elSec = Math.min(300, Math.max(0, startTs > 0 ? actT - startTs : actT));
    const ax = xOfSec(elSec);
    const actType = String(a.action || "").toLowerCase();
    const isSel = modalSelectedActionIdx === idx;
    const evtColor = actType === "fill" ? pal.up : actType === "cancel" ? pal.down : pal.spot;

    ctx.save();
    if (isSel) {
      ctx.strokeStyle = evtColor;
      ctx.lineWidth = 1.6;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(ax, padT);
      ctx.lineTo(ax, h - padB);
      ctx.stroke();

      // Spotlight marker on nearest price point
      let nearest = pts[0];
      let minDt = Infinity;
      pts.forEach((pt) => {
        const dt = Math.abs(pt.elapsed - elSec);
        if (dt < minDt) {
          minDt = dt;
          nearest = pt;
        }
      });
      if (nearest) {
        const ny = yOfPrice(nearest.spot);
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(ax, ny, 7.5, 0, Math.PI * 2);
        ctx.fillStyle = actType === "fill" ? "rgba(16, 185, 129, 0.22)" : actType === "cancel" ? "rgba(244, 63, 94, 0.22)" : "rgba(37, 99, 235, 0.20)";
        ctx.fill();

        ctx.beginPath();
        ctx.arc(ax, ny, 4.2, 0, Math.PI * 2);
        ctx.fillStyle = evtColor;
        ctx.fill();
        ctx.strokeStyle = pal.cardBg;
        ctx.lineWidth = 1.8;
        ctx.stroke();
      }
    } else {
      ctx.strokeStyle = pal.isDark ? "rgba(148, 163, 184, 0.16)" : "rgba(15, 23, 42, 0.08)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(ax, padT);
      ctx.lineTo(ax, h - padB);
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(ax, h - padB, 3, 0, Math.PI * 2);
      ctx.fillStyle = evtColor;
      ctx.fill();
    }
    ctx.restore();
  });

  // Hover crosshair in modal
  if (modalHoverIdx !== null && pts[modalHoverIdx]) {
    const hp = pts[modalHoverIdx];
    const hx = xOfSec(hp.elapsed);
    const hy = yOfPrice(hp.spot);
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(248, 250, 252, 0.35)" : "rgba(15, 23, 42, 0.25)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hx, padT);
    ctx.lineTo(hx, h - padB);
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = pal.spot;
    ctx.fill();
    ctx.strokeStyle = pal.cardBg;
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

function showModalTooltipForElapsed(elapsedSec, actionMeta = null) {
  const canvas = $("modal-hist-canvas");
  const tooltip = $("modal-chart-tooltip");
  if (!canvas || !tooltip || !modalChartGeom || !modalChartGeom.pts?.length) return;

  const rect = canvas.getBoundingClientRect();
  let bestIdx = 0;
  let bestDist = Infinity;
  modalChartGeom.pts.forEach((p, idx) => {
    const d = Math.abs(p.elapsed - elapsedSec);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = idx;
    }
  });

  const pt = modalChartGeom.pts[bestIdx];
  const openPx = modalChartGeom.openPx || pt.spot;
  const bps = openPx > 0 ? ((pt.spot - openPx) / openPx) * 10000 : 0;
  const sign = bps > 0 ? "+" : "";

  const actionBanner = actionMeta
    ? `<div class="tt-row" style="padding-bottom:4px;margin-bottom:4px;border-bottom:1px dashed var(--border)">
         <span>${escapeHtml(actionMeta.typeLabel)}</span>
         <b class="${actionMeta.sideClass || ""}">${escapeHtml(actionMeta.specStr || actionMeta.sideLabel)}</b>
       </div>`
    : "";

  tooltip.innerHTML = `
    <div class="tt-time" style="display:flex;align-items:center;justify-content:space-between;gap:10px">
      <span>窗口第 ${Math.round(elapsedSec)} 秒</span>
      <span class="${bps >= 0 ? "up" : "down"}">${sign}${bps.toFixed(1)} bps</span>
    </div>
    ${actionBanner}
    <div class="tt-row"><span>Binance 实时现货</span><b>${fmtUsd(pt.spot)}</b></div>
    <div class="tt-row"><span>Chainlink 60s TWAP</span><b>${fmtUsd(pt.poly_spot)}</b></div>
    <div class="tt-row"><span>UP 盘口隐含概率</span><b class="up">${pt.up_mid !== null ? (pt.up_mid * 100).toFixed(1) + "%" : "--"}</b></div>
    <div class="tt-row"><span>模型公允胜率 P*</span><b style="color:var(--model)">${pt.p_model !== null ? (pt.p_model * 100).toFixed(1) + "%" : "--"}</b></div>
  `;
  tooltip.hidden = false;

  const hx = modalChartGeom.xOfSec(elapsedSec);
  const ttW = 235;
  const leftPos = Math.min(rect.width - ttW - 10, Math.max(10, hx + 14));
  tooltip.style.left = `${leftPos}px`;
  tooltip.style.top = `12px`;
}

function bindModalActionRowClicks(winData, startTs) {
  const rows = document.querySelectorAll("#modal-round-body .modal-action-row");
  const actions = Array.isArray(winData?.actions) ? winData.actions : [];
  const tooltip = $("modal-chart-tooltip");

  rows.forEach((row) => {
    row.addEventListener("click", () => {
      const idx = Number(row.dataset.actionIdx);
      const elapsed = Number(row.dataset.elapsed || 0);
      if (modalSelectedActionIdx === idx) {
        modalSelectedActionIdx = null;
        rows.forEach((r) => r.classList.remove("is-selected"));
        if (tooltip) tooltip.hidden = true;
        drawModalHistoryChart(cachedModalWindow, cachedModalRecord);
        return;
      }

      modalSelectedActionIdx = idx;
      rows.forEach((r) => r.classList.toggle("is-selected", Number(r.dataset.actionIdx) === idx));
      drawModalHistoryChart(cachedModalWindow, cachedModalRecord);

      const act = actions[idx];
      const item = act ? formatActionItem(act, startTs) : null;
      showModalTooltipForElapsed(elapsed, item);
    });
  });
}

function bindModalChartHover() {
  const canvas = $("modal-hist-canvas");
  const tooltip = $("modal-chart-tooltip");
  if (!canvas || !tooltip) return;

  canvas.addEventListener("mousemove", (e) => {
    if (!modalChartGeom || !modalChartGeom.pts?.length) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;

    let bestIdx = 0;
    let bestDist = Infinity;
    modalChartGeom.pts.forEach((p, idx) => {
      const px = modalChartGeom.xOfSec(p.elapsed);
      const d = Math.abs(px - mx);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = idx;
      }
    });

    modalHoverIdx = bestIdx;
    drawModalHistoryChart(cachedModalWindow, cachedModalRecord);
    const pt = modalChartGeom.pts[bestIdx];
    showModalTooltipForElapsed(pt.elapsed, null);
  });

  canvas.addEventListener("mouseleave", () => {
    modalHoverIdx = null;
    if (cachedModalWindow) drawModalHistoryChart(cachedModalWindow, cachedModalRecord);
    if (modalSelectedActionIdx !== null && cachedModalWindow) {
      const actions = Array.isArray(cachedModalWindow.actions) ? cachedModalWindow.actions : [];
      const startTs = Number(cachedModalWindow.start || cachedModalWindow.start_ts || cachedModalRecord?.start_ts || 0);
      const act = actions[modalSelectedActionIdx];
      if (act) {
        const actT = Number(act.t ?? act.ts ?? act.timestamp ?? startTs);
        const elSec = Math.min(300, Math.max(0, startTs > 0 ? actT - startTs : actT));
        showModalTooltipForElapsed(elSec, formatActionItem(act, startTs));
        return;
      }
    }
    tooltip.hidden = true;
  });
}

const DECISION_LOG_STORAGE_KEY = "pmquant_decision_audit_log_v1";
let decisionAuditSlug = null;
let decisionAuditLog = [];
try {
  const savedLog = sessionStorage.getItem(DECISION_LOG_STORAGE_KEY);
  if (savedLog) {
    const parsed = JSON.parse(savedLog);
    if (Array.isArray(parsed)) {
      decisionAuditLog = parsed;
      if (parsed.length > 0) decisionAuditSlug = parsed[parsed.length - 1].slug || null;
    } else if (parsed && Array.isArray(parsed.entries)) {
      decisionAuditSlug = parsed.slug || null;
      decisionAuditLog = parsed.entries;
    }
  }
} catch {}
const decisionAuditKeys = new Set(decisionAuditLog.map((e) => e._uid));
let lastFallbackDecisionState = { slug: null, key: null, t: 0 };

function clearDecisionAuditLog(newSlug) {
  decisionAuditSlug = newSlug || null;
  decisionAuditLog = [];
  decisionAuditKeys.clear();
  lastFallbackDecisionState = { slug: newSlug || null, key: null, t: 0 };
  try {
    sessionStorage.setItem(DECISION_LOG_STORAGE_KEY, JSON.stringify({ slug: decisionAuditSlug, entries: [] }));
  } catch {}
}

function appendDecisionAuditEntry(entry) {
  if (!entry || !entry.reason) return;
  const uid = `${entry.slug || ""}|${Number(entry.t || 0).toFixed(2)}|${entry.action || ""}|${entry.reason.slice(0, 48)}`;
  if (decisionAuditKeys.has(uid)) return;
  decisionAuditKeys.add(uid);
  decisionAuditLog.push({ ...entry, _uid: uid });
  if (decisionAuditLog.length > 200) {
    const removed = decisionAuditLog.splice(0, decisionAuditLog.length - 200);
    removed.forEach((r) => decisionAuditKeys.delete(r._uid));
  }
  try {
    sessionStorage.setItem(DECISION_LOG_STORAGE_KEY, JSON.stringify({ slug: decisionAuditSlug, entries: decisionAuditLog }));
  } catch {}
}

function ingestLiveDecisions(live) {
  if (!live || live.status === "idle") return;
  const slug = live.slug || "";
  if (slug && decisionAuditSlug !== slug) {
    clearDecisionAuditLog(slug);
  }
  const winInfo = slugToWindowText(slug, live.start, live.end);
  const winRange = winInfo?.range || "";

  // 1. Prefer authoritative backend decision_timeline when available
  if (Array.isArray(live.decision_timeline) && live.decision_timeline.length > 0) {
    live.decision_timeline.forEach((item) => {
      appendDecisionAuditEntry({
        slug,
        winRange,
        t: Number(item.t || live.now || 0),
        tau_s: item.tau_s != null ? Number(item.tau_s) : null,
        action: item.action || "wait",
        side: item.side || null,
        price: item.price != null ? Number(item.price) : null,
        reason: item.reason || "",
      });
    });
  } else {
    // 2. Fallback accumulation when backend process hasn't been restarted yet:
    //    record all order actions (submit/fill/cancel) + significant decision transitions
    if (Array.isArray(live.actions)) {
      live.actions.forEach((act) => {
        const actType = act.action || "";
        const mappedAction =
          actType === "fill" ? "taker_buy" :
          actType === "submit" ? "quote" :
          actType === "cancel" ? "taker_unwind" : "wait";
        const pxTxt = act.price != null ? ` @ $${Number(act.price).toFixed(2)}` : "";
        const szTxt = act.size != null ? ` ${Number(act.size)}股` : "";
        const sideTxt = act.side || "";
        const labelPrefix =
          actType === "fill" ? `成交${sideTxt}${szTxt}${pxTxt}` :
          actType === "submit" ? `委托${sideTxt}${szTxt}${pxTxt}` :
          actType === "cancel" ? `撤单${sideTxt}${szTxt}${pxTxt}` : actType;
        appendDecisionAuditEntry({
          slug,
          winRange,
          t: Number(act.t || live.now || 0),
          tau_s: live.end && act.t ? Math.max(0, Math.round(Number(live.end) - Number(act.t))) : null,
          action: mappedAction,
          side: act.side || null,
          price: act.price != null ? Number(act.price) : null,
          reason: act.reason ? `${labelPrefix}：${act.reason}` : labelPrefix,
        });
      });
    }

    if (live.last_decision && live.last_decision.reason) {
      const ld = live.last_decision;
      const nowT = Number(ld.t || live.now || Date.now() / 1000);
      const coreReason = String(ld.reason).split("(")[0].slice(0, 36);
      const stateKey = `${ld.action || ""}|${ld.side || ""}|${coreReason}`;
      const isActionable = ["taker_buy", "taker_hedge", "taker_unwind", "quote"].includes(ld.action);
      const changed = lastFallbackDecisionState.slug !== slug || lastFallbackDecisionState.key !== stateKey;
      const timedOut = (nowT - lastFallbackDecisionState.t) >= 20;
      if (changed || isActionable || timedOut) {
        appendDecisionAuditEntry({
          slug,
          winRange,
          t: nowT,
          tau_s: live.tau_s != null ? Number(live.tau_s) : null,
          action: ld.action || "wait",
          side: ld.side || null,
          price: ld.price != null ? Number(ld.price) : null,
          reason: ld.reason,
        });
        lastFallbackDecisionState = { slug, key: stateKey, t: nowT };
      }
    }
  }
}

function renderDecisionBulletin(live) {
  const listEl = $("decision-bulletin-list");
  const countEl = $("bulletin-count");
  const modalCountEl = $("modal-bulletin-count");
  const previewEl = $("bulletin-latest-preview");
  if (!listEl) return;

  ingestLiveDecisions(live);

  const timeline = decisionAuditLog;
  if (!timeline.length) {
    if (countEl) countEl.textContent = "0 条";
    if (modalCountEl) modalCountEl.textContent = "0 条记录";
    if (previewEl) previewEl.textContent = "等待策略研判记录...";
    setHtmlIfChanged(listEl, `
      <div class="bulletin-empty">
        <div class="bulletin-empty-title">暂无策略研判记录</div>
        <div class="bulletin-empty-sub">策略引擎每次产生关键研判、开仓、挂单或撤单决策时，将按时间顺序永久追加记录在此</div>
      </div>`);
    return;
  }

  if (countEl) countEl.textContent = `${timeline.length} 条`;
  if (modalCountEl) modalCountEl.textContent = `${timeline.length} 条记录`;
  const tagMap = {
    taker_buy: ["吃单", "tag-taker"],
    taker_hedge: ["锁仓", "tag-taker"],
    taker_unwind: ["防守", "tag-warn"],
    quote: ["挂单", "tag-quote"],
    wait: ["研判", "tag-hold"],
  };

  const latest = timeline[timeline.length - 1];
  if (previewEl && latest) {
    const lts = latest.t ? new Date(Number(latest.t) * 1000).toLocaleTimeString("zh-CN", {
      hour12: false, timeZone: "Asia/Shanghai"
    }) : "--:--:--";
    const ltau = latest.tau_s != null ? `剩${Math.max(0, Math.round(Number(latest.tau_s)))}s` : "";
    const [lTag] = tagMap[latest.action] || ["状态"];
    previewEl.textContent = `[${lts}${ltau ? " · " + ltau : ""}] [${lTag}] ${translateDecisionReason(latest.reason)}`;
  }

  const rows = [...timeline].reverse().map((item, idx) => {
    const ts = item.t ? new Date(Number(item.t) * 1000).toLocaleTimeString("zh-CN", {
      hour12: false, timeZone: "Asia/Shanghai"
    }) : "--:--:--";
    const winTxt = item.winRange ? `${item.winRange} · ` : "";
    const tauTxt = item.tau_s != null ? `剩${Math.max(0, Math.round(Number(item.tau_s)))}s` : "";
    const [tagLabel, tagCls] = tagMap[item.action] || ["状态", "tag-hold"];
    const text = translateDecisionReason(item.reason);
    const latestCls = idx === 0 ? " is-latest" : "";
    return `
      <div class="bulletin-row${latestCls}">
        <span class="bulletin-time">[${ts} · ${winTxt}${tauTxt}]</span>
        <span class="bulletin-tag ${tagCls}">${escapeHtml(tagLabel)}</span>
        <span class="bulletin-text">${escapeHtml(text)}</span>
      </div>`;
  }).join("");
  setHtmlIfChanged(listEl, rows);
}

let stableServerOffsetMs = null;
let lastCountdownSlug = null;
let lastCountdownTau = null;

function syncServerClockOffset(serverTimeSec) {
  if (!serverTimeSec) return;
  const rawOffset = Number(serverTimeSec) * 1000 - Date.now();
  if (stableServerOffsetMs === null || Math.abs(rawOffset - stableServerOffsetMs) > 1500) {
    stableServerOffsetMs = rawOffset;
  } else {
    stableServerOffsetMs = stableServerOffsetMs * 0.9 + rawOffset * 0.1;
  }
}

function updateSmoothCountdowns() {
  if (!cachedLive || cachedLive.status === "idle" || !cachedLive.end) return;
  const slug = cachedLive.slug || "";
  if (slug !== lastCountdownSlug) {
    lastCountdownSlug = slug;
    lastCountdownTau = null;
  }

  const offset = stableServerOffsetMs !== null ? stableServerOffsetMs : 0;
  const nowServerSec = (Date.now() + offset) / 1000;
  let tau = Math.max(0, Math.floor(Number(cachedLive.end) - nowServerSec));
  if (lastCountdownTau !== null && tau > lastCountdownTau) {
    tau = lastCountdownTau;
  }
  lastCountdownTau = tau;
  const elapsed = Math.min(300, Math.max(0, 300 - tau));

  if ($("market-tau")) $("market-tau").textContent = String(tau);
  if ($("chart-elapsed-badge")) $("chart-elapsed-badge").textContent = `已运行 ${elapsed}s / 300s`;
  if ($("overview-live-tau")) {
    setHtmlIfChanged($("overview-live-tau"), `${tau}<span class="val-unit">s</span>`);
  }
  const bar = $("overview-tau-bar");
  if (bar) bar.style.width = `${((elapsed / 300) * 100).toFixed(1)}%`;
}

// ============================================================================
// DATA POLLING & EVENT BINDINGS
// ============================================================================
async function fetchAll(isManual = false) {
  const connPill = $("conn-pill");
  const connText = $("conn-text");
  const refreshBtn = $("refresh-btn");

  if (isManual && refreshBtn) {
    refreshBtn.classList.add("spinning");
  }

  try {
    const [liveRes, histRes] = await Promise.all([
      fetch("/api/live", { cache: "no-store" }),
      fetch("/api/history", { cache: "no-store" }),
    ]);

    if (!liveRes.ok || !histRes.ok) {
      throw new Error(`HTTP ${liveRes.status}/${histRes.status}`);
    }

    const [live, hist] = await Promise.all([liveRes.json(), histRes.json()]);
    cachedLive = live;
    cachedHistory = hist;
    if (live && live.server_time) {
      syncServerClockOffset(live.server_time);
    }

    if (connPill) connPill.className = "status-pill status-ready";
    if (connText) connText.textContent = "服务在线";

    const versionEl = $("page-version");
    const activeVer = (hist && (hist.current_batch || hist.batch_id)) || (live && live.batch_id);
    if (versionEl && activeVer) {
      versionEl.textContent = activeVer;
    }

    updateNetworkTelemetry(live);
    updateOverview(live, hist);
    updateMarket(live);
    updateSmoothCountdowns();
    renderHistoryTable(hist);

    if (isManual) {
      showToast("实时行情与历史结算账本已同步", "up");
    }
  } catch (err) {
    if (connPill) connPill.className = "status-pill status-critical";
    if (connText) connText.textContent = "连接中断";
    if (isManual) {
      showToast("数据同步失败，请检查本地服务状态", "down");
    }
  } finally {
    if (isManual && refreshBtn) {
      setTimeout(() => refreshBtn.classList.remove("spinning"), 420);
    }
  }
}

function initInteractions() {
  const themeBtn = $("theme-btn");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => toggleTheme());
  }

  const refreshBtn = $("refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => fetchAll(true));
  }

  const filterGroup = $("history-filter-group");
  if (filterGroup) {
    filterGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".filter-btn");
      if (!btn) return;
      historyFilterMode = btn.dataset.filter || "all";
      filterGroup.querySelectorAll(".filter-btn").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      if (cachedHistory) renderHistoryTable(cachedHistory);
    });
  }

  document.addEventListener("click", (e) => {
    const row = e.target.closest("tr.history-row-clickable[data-slug]");
    if (row && row.dataset.slug) {
      openRoundModal(row.dataset.slug);
    }
  });

  const closeBtn = $("modal-close-btn");
  if (closeBtn) closeBtn.addEventListener("click", closeRoundModal);

  const modalBackdrop = $("round-detail-modal");
  if (modalBackdrop) {
    modalBackdrop.addEventListener("click", (e) => {
      if (e.target === modalBackdrop) closeRoundModal();
    });
  }

  const connHelpBtn = $("conn-help-btn");
  const connGuideModal = $("conn-guide-modal");
  const connGuideClose = $("conn-guide-close-btn");
  const closeConnGuide = () => {
    if (connGuideModal) connGuideModal.hidden = true;
  };
  if (connHelpBtn && connGuideModal) {
    connHelpBtn.addEventListener("click", () => {
      connGuideModal.hidden = false;
    });
  }
  if (connGuideClose) {
    connGuideClose.addEventListener("click", closeConnGuide);
  }
  if (connGuideModal) {
    connGuideModal.addEventListener("click", (e) => {
      if (e.target === connGuideModal) closeConnGuide();
    });
  }
  const bulletinToggle = $("bulletin-toggle-btn");
  const timelineModal = $("decision-timeline-modal");
  const timelineClose = $("decision-timeline-close-btn");
  const closeTimelineModal = () => {
    if (timelineModal) timelineModal.hidden = true;
  };
  if (bulletinToggle && timelineModal) {
    const openTimelineModal = () => {
      timelineModal.hidden = false;
    };
    bulletinToggle.addEventListener("click", openTimelineModal);
    bulletinToggle.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openTimelineModal();
      }
    });
  }
  if (timelineClose) {
    timelineClose.addEventListener("click", closeTimelineModal);
  }
  if (timelineModal) {
    timelineModal.addEventListener("click", (e) => {
      if (e.target === timelineModal) closeTimelineModal();
    });
  }

  const regimeHelpBtn = $("regime-help-btn");
  const regimeGuideModal = $("regime-guide-modal");
  const regimeGuideClose = $("regime-guide-close-btn");
  const closeRegimeGuide = () => {
    if (regimeGuideModal) regimeGuideModal.hidden = true;
  };
  if (regimeHelpBtn && regimeGuideModal) {
    regimeHelpBtn.addEventListener("click", () => {
      regimeGuideModal.hidden = false;
    });
  }
  if (regimeGuideClose) {
    regimeGuideClose.addEventListener("click", closeRegimeGuide);
  }
  if (regimeGuideModal) {
    regimeGuideModal.addEventListener("click", (e) => {
      if (e.target === regimeGuideModal) closeRegimeGuide();
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && connGuideModal && !connGuideModal.hidden) {
      closeConnGuide();
    }
    if (e.key === "Escape" && regimeGuideModal && !regimeGuideModal.hidden) {
      closeRegimeGuide();
    }
    if (e.key === "Escape" && timelineModal && !timelineModal.hidden) {
      closeTimelineModal();
    }
  });

  window.addEventListener("resize", () => {
    if (currentPage === "overview" && cachedHistory) drawEquityCurve(cachedHistory);
    if (currentPage === "market" && cachedLive) drawLivePriceChart(cachedLive);
    if (cachedModalWindow && !$("round-detail-modal")?.hidden) {
      drawModalHistoryChart(cachedModalWindow, cachedModalRecord);
    }
  });
}

// ============================================================================
// BOOTSTRAP
// ============================================================================
document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initNavigation();
  initInteractions();
  initEquityCurveInteractions();
  initLiveChartInteractions();
  updateWallClock();
  setInterval(updateWallClock, 1000);
  setInterval(updateSmoothCountdowns, 250);

  fetchAll(false);
  setInterval(() => fetchAll(false), 500);
});
