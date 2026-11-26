// ============================================================================
// PM-QUANT · Institutional Quantitative Trading Terminal Logic (v3.3.0)
// ============================================================================

const $ = (id) => document.getElementById(id);

// Bounded fetch helper: prevents mobile 4G/Wi-Fi handoff half-open sockets from wedging _fetchInFlight
async function fetchWithTimeout(url, options = {}, timeoutMs = 6000) {
  if (typeof AbortController === "undefined") {
    return fetch(url, options);
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

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
  trending_up: {
    label: "单边强势拉升 (Trending Up)",
    desc: "SNR > 1.8 多头动量突破，严格禁止逆势挂单以防逆向选择",
  },
  trending_down: {
    label: "单边强势下挫 (Trending Down)",
    desc: "SNR > 1.8 空头动量突破，严格禁止逆势挂单以防逆向选择",
  },
  strong_trend: {
    label: "单边强趋势动量 (Strong Trend)",
    desc: "SNR > 1.8 强动量突破，严格禁止逆势挂单以防逆向选择",
  },
  wide_spread: {
    label: "宽价差 · 低流动性 (Wide Spread)",
    desc: "盘口价差 > $0.06，造市商撤退或流动性枯竭窗口",
  },
  toxic: {
    label: "知情交易毒性冲击 (Toxic)",
    desc: "3 秒内价格剧烈跳变 > 3.5bps 或极端波动，触发瞬时防逆向选择保护",
  },
  toxic_shock: {
    label: "知情交易毒性冲击 (Toxic Shock)",
    desc: "3 秒内价格剧烈跳变 > 3.5bps，触发瞬时防逆向选择保护",
  },
  transitional: {
    label: "过渡观察区间 (Transitional)",
    desc: "1.5 < SNR ≤ 1.8 过渡带或参考基准待就绪，保守观望",
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
      axis: "#a3b3c6",
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
    axis: "#526071",
    grid: "rgba(15, 23, 42, 0.07)",
    gridSoft: "rgba(15, 23, 42, 0.04)",
    mask: "rgba(255, 255, 255, 0.90)",
    cardBg: "#ffffff",
    divider: "rgba(15, 23, 42, 0.10)",
  };
}

let currentPage = "overview";
let chartViewMode = "dual"; // "dual" | "price" | "prob"
let historyFilterMode = "all"; // "all" | "traded" | "win" | "loss" | "maker" | "taker" | "flip"
let historyTodayOnly = false;
let _lastNotifiedSettledSlug = null;
let cachedLive = null;
let cachedHistory = null;
let cachedModalWindow = null;
let cachedModalRecord = null;
let liveChartHoverIdx = null;
let liveSelectedActionIdx = null;
let liveSelectedActionSlug = null;
let liveActionFilterMode = "key"; // "key" (成交/挂单/撤单) | "fill" | "all"
let liveChartGeom = null;
let equityHoverIdx = null;
let equityChartGeom = null;
let modalHoverIdx = null;
let modalSelectedActionIdx = null;
let modalChartGeom = null;
let modalShowDeBasisSpot = false;
let modalActionFilterMode = "all"; // "all" | "fill" | "order"
let modalCurrentSlug = null;

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
    winner_source: String(r.winner_source || ""),
    regime: String(r.regime || ""),
    entry_mode: String(r.entry_mode || ""),
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
// TELEMETRY & NETWORK BAR
// ============================================================================
function updateNetworkTelemetry(live) {
  const net = (live && live.network) || {};
  const cb = (live && live.circuit_breaker) || {};

  const latEl = $("side-net-latency");
  const modalLatEl = $("modal-net-latency");
  const netCountEl = $("net-count");
  const modalNetCountEl = $("modal-net-count");

  const polyMs = Number(net.poly_latency_ms ?? 0);
  const binMs = Number(net.binance_latency_ms ?? 0);
  const tradesMs = Number(net.trades_latency_ms ?? 0);
  const clMs = Number(net.chainlink_latency_ms ?? 0);
  const clAge = net.chainlink_age_s;
  const realRtts = [polyMs, binMs, tradesMs, clMs].filter((x) => x > 0);
  const rttMs = realRtts.length > 0 ? realRtts.reduce((a, b) => a + b, 0) / realRtts.length : 0;

  const rttDisplay = rttMs ? `${Math.round(rttMs)} ms` : "-- ms";
  if (latEl) latEl.textContent = rttDisplay;
  if (modalLatEl) modalLatEl.textContent = rttDisplay;

  const sigDiffEl = $("side-net-sig-diff");
  const modalSigDiffEl = $("modal-net-sig-diff");
  {
    const rawDiff = net.signal_minus_venue_ms;
    const sigDiff = (rawDiff !== null && rawDiff !== undefined)
      ? Number(rawDiff)
      : (binMs > 0 && polyMs > 0 ? (binMs - polyMs) : null);
    const diffText = (sigDiff !== null && Number.isFinite(sigDiff))
      ? `${sigDiff >= 0 ? "+" : ""}${Math.round(sigDiff)}ms`
      : "--";
    const diffCls = (sigDiff !== null && Number.isFinite(sigDiff))
      ? (sigDiff > 100 ? "is-warn" : sigDiff <= 30 ? "is-ok" : "")
      : "";
    const diffTitle = (sigDiff !== null && Number.isFinite(sigDiff))
      ? (sigDiff > 100
        ? `现货源 (${net.spot_provider || "binance"}) 比 Polymarket 慢 ${Math.round(sigDiff)}ms，建议测速换更近现货源`
        : `现货源 (${net.spot_provider || "binance"}) 与 Polymarket 延迟差 ${diffText}`)
      : "";
    [sigDiffEl, modalSigDiffEl].forEach((el) => {
      if (!el) return;
      el.textContent = diffText;
      el.className = `source-metric-val conn-tile-val ${diffCls}`;
      el.title = diffTitle;
    });
  }

  const nowSec = live && live.server_time ? Number(live.server_time) : (Date.now() / 1000);
  const liveAge = (live && live.now) ? Math.abs(nowSec - Number(live.now)) : Infinity;
  const isIdle = !live || live.status === "idle" || liveAge > 10;
  const spotStale = Boolean(live && live.spot_stale);
  const polyStale = Boolean(live && live.poly_spot_stale);
  const spotSrc = String((live && live.spot_source) || "").toLowerCase();
  const spotWsOk = Boolean(net.spot_ws_connected);
  const bookWsOk = Boolean(net.book_ws_connected);
  const tradesWsOk = Boolean(net.ws_connected);
  const bookUp = (live && live.book_up) || {};
  const bookDn = (live && live.book_down) || {};
  const bookEmpty =
    Boolean(net.book_empty) ||
    (bookUp.best_bid == null && bookUp.best_ask == null) ||
    (bookDn.best_bid == null && bookDn.best_ask == null);

  const setSourceState = (dotId, valId, state, text, title = "") => {
    const stateCls = state === "ok" ? "is-ok" : state === "warn" ? "is-warn" : state === "bad" ? "is-bad" : "";
    [dotId, `modal-${dotId}`].forEach((id) => {
      const dot = $(id);
      if (dot) dot.className = `source-dot ${stateCls}`;
    });
    [valId, `modal-${valId}`].forEach((id) => {
      const val = $(id);
      if (!val) return;
      val.textContent = text;
      val.className = `source-metric-val source-chip ${stateCls}`;
      if (title) val.title = title;
    });
  };

  let okCount = 0;
  let warnCount = 0;
  let badCount = 0;
  const tally = (st) => {
    if (st === "ok") okCount += 1;
    else if (st === "warn") warnCount += 1;
    else badCount += 1;
  };

  const spotShortMap = { binance: "BN", kraken: "KR", coinbase: "CB", okx: "OKX" };

  if (isIdle) {
    setSourceState("dot-spot", "src-spot-val", "bad", "离线");
    setSourceState("dot-book", "src-book-val", "bad", "离线");
    setSourceState("dot-trades", "src-trades-val", "bad", "离线");
    setSourceState("dot-chainlink", "src-chainlink-val", "bad", "离线");
  } else {
    // 1. BTC Spot Source (supports Binance / Kraken / Coinbase / OKX WS)
    if (spotStale) {
      tally("bad");
      setSourceState("dot-spot", "src-spot-val", "bad", "断流", "现货行情超时 >3s");
    } else if (spotWsOk && spotSrc.endsWith("_ws")) {
      tally("ok");
      const rawProv = String(net.spot_provider || spotSrc.replace("_ws", "")).toLowerCase();
      const shortTag = spotShortMap[rawProv] || rawProv.slice(0, 3).toUpperCase();
      setSourceState(
        "dot-spot",
        "src-spot-val",
        "ok",
        binMs > 0 ? `${shortTag} ${Math.round(binMs)}ms` : `${shortTag} WS`,
        `${rawProv.toUpperCase()} WebSocket 实时推送`
      );
    } else {
      tally("warn");
      const fallbackTag = spotSrc === "okx" ? "OKX" : "HTTP";
      setSourceState(
        "dot-spot",
        "src-spot-val",
        "warn",
        binMs > 0 ? `${fallbackTag} ${Math.round(binMs)}ms` : `${fallbackTag} 降级`,
        `现货 WS 断开，当前降级源: ${spotSrc || "REST"}`
      );
    }

    // 2. Polymarket CLOB Book Source
    if (bookEmpty) {
      tally("bad");
      setSourceState("dot-book", "src-book-val", "bad", "空盘口", "Up/Down 订单簿为空");
    } else if (bookWsOk) {
      tally("ok");
      setSourceState("dot-book", "src-book-val", "ok", polyMs > 0 ? `WS ${Math.round(polyMs)}ms` : "WS 正常", "Polymarket CLOB WebSocket 实时增量");
    } else {
      tally("warn");
      setSourceState("dot-book", "src-book-val", "warn", polyMs > 0 ? `HTTP ${Math.round(polyMs)}ms` : "HTTP 降级", "盘口 WS 断开或重连中，当前由实时 HTTP 顶上");
    }

    // 3. Polymarket Trade Prints WS Source
    if (tradesWsOk) {
      tally("ok");
      setSourceState("dot-trades", "src-trades-val", "ok", tradesMs > 0 ? `WS ${Math.round(tradesMs)}ms` : "WS 在线", `累计接收 ${net.ws_trades || 0} 笔成交打印`);
    } else {
      tally("bad");
      setSourceState("dot-trades", "src-trades-val", "bad", "WS 断开", net.ws_last_error || "成交打印 WebSocket 断开重连中");
    }

    // 4. Chainlink 60s TWAP Oracle Source
    if (polyStale) {
      tally("warn");
      const ageLbl = clAge != null ? `滞后 ${Math.round(clAge)}s` : "滞后 >10s";
      setSourceState("dot-chainlink", "src-chainlink-val", "warn", ageLbl, "Chainlink 60s TWAP 超过 10s 未更新，使用现货+基差估算");
    } else {
      tally("ok");
      setSourceState("dot-chainlink", "src-chainlink-val", "ok", clMs > 0 ? `${Math.round(clMs)}ms` : "实时", "Polymarket 官方 Chainlink 60s TWAP");
    }
  }

  const apiGuard = (live && live.api_schema_guard) || {};
  const apiPaused = Boolean(apiGuard.paused);
  let pillCls = "status-pill status-ready";
  let pillLabel = "服务在线";
  if (apiPaused) {
    pillCls = "status-pill status-critical";
    pillLabel = "接口变更暂停";
  } else if (isIdle) {
    pillCls = "status-pill status-warn";
    pillLabel = live && live.status === "idle" ? "等待开盘" : "引擎离线";
  } else if (badCount > 0) {
    pillCls = "status-pill status-critical";
    pillLabel = "数据异常";
  } else if (warnCount > 0) {
    pillCls = "status-pill status-warn";
    pillLabel = "链路降级";
  }
  [
    [$("conn-pill"), $("conn-text")],
    [$("modal-conn-pill"), $("modal-conn-text")],
  ].forEach(([pill, textEl]) => {
    if (pill) pill.className = pillCls;
    if (textEl) textEl.textContent = pillLabel;
  });

  const tripped = Boolean(cb.active || cb.tripped);
  let badgeText = "4/4 正常";
  let badgeColor = "var(--up)";
  let badgeBg = "var(--up-soft)";
  if (apiPaused) {
    badgeText = "接口异常暂停";
    badgeColor = "var(--down)";
    badgeBg = "var(--down-soft)";
  } else if (isIdle) {
    badgeText = "管道离线";
    badgeColor = "var(--caption)";
    badgeBg = "var(--surface-2, rgba(148,163,184,0.14))";
  } else if (tripped) {
    badgeText = "风控熔断";
    badgeColor = "var(--down)";
    badgeBg = "var(--down-soft)";
  } else if (badCount > 0) {
    badgeText = `${okCount}/4 异常`;
    badgeColor = "var(--down)";
    badgeBg = "var(--down-soft)";
  } else if (warnCount > 0) {
    badgeText = `${okCount}/4 降级`;
    badgeColor = "#d97706";
    badgeBg = "rgba(245,158,11,0.14)";
  }
  [netCountEl, modalNetCountEl].forEach((el) => {
    if (!el) return;
    el.textContent = badgeText;
    el.style.color = badgeColor;
    el.style.background = badgeBg;
  });
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
  if (!rawReason) return "正在观察实时行情，等待合适的出手时机...";
  let s = String(rawReason).trim();
  const dirCn = (d) => (/^up$/i.test(String(d || "")) ? "看涨" : /^down$/i.test(String(d || "")) ? "看跌" : String(d || ""));
  const pctStr = (v) => {
    const n = parseFloat(v);
    if (!Number.isFinite(n)) return "";
    return n <= 1.0 ? `${Math.round(n * 100)}%` : `${Math.round(n)}%`;
  };
  const edgePct = (v) => {
    const n = parseFloat(v);
    if (!Number.isFinite(n)) return "";
    return `${(Math.abs(n) * 100).toFixed(1)}%`;
  };

  let m;
  // 1. 持仓等待结算
  if ((m = s.match(/Holding\s+([\d.]+)\s+(Up|Down)\s+@\s*([\d.]+)\s+to settlement\s*\(lead=([\d.+-]+)bps\)/i))) {
    const lead = parseFloat(m[4]);
    const status = lead > 0.5 ? "目前走势占优，静待结算" : lead < -0.5 ? "当前略有落后，等待到期结果" : "走势胶着，等待到期结算";
    return `已持有 ${m[1]} 股${dirCn(m[2])}（均价 $${m[3]}），${status}`;
  }
  if ((m = s.match(/Holding\s+([\d.]+)\s+(Up|Down)\s+@\s*([\d.]+)\s+to settlement/i))) {
    return `已持有 ${m[1]} 股${dirCn(m[2])}（均价 $${m[3]}），安心等待到期结算`;
  }

  // 2. 开盘校准 / 尾盘收官与冻结
  if ((m = s.match(/Observation period\s*\(tau\s*([\d.]+)s/i)) || /开盘观察|开盘校准|warmup/i.test(s)) {
    const waitLeft = m ? Math.max(1, Math.round(parseFloat(m[1]) - 280)) : null;
    const left = waitLeft ? `（还剩约 ${waitLeft} 秒）` : "";
    return `开盘前 20 秒校准基准价，暂不下单${left}`;
  }
  if (/尾盘收官期停止新开仓/i.test(s)) {
    return "进入最后 30 秒尾盘收官期，不再新开仓，准备迎接下一轮";
  }
  if (/极端浮盈落袋 IOC/i.test(s)) {
    const sm = s.match(/卖出\s*([\d.]+)\s*股\s*(Up|Down)\s*@\s*bid=\$?([\d.]+)/i);
    if (sm) {
      return `浮盈已达高位，以 $${sm[3]} 卖出 ${sm[1]} 股${dirCn(sm[2])}提前落袋锁定利润`;
    }
    return "浮盈已达极高价区，主动卖出提前落袋锁定利润";
  }
  if (/本窗已完成极端浮盈落袋/i.test(s)) {
    return "本轮已高位止盈落袋，停止二次开仓，静待下一轮";
  }
  if (/基差瞬变脱锚防灾暂停/i.test(s)) {
    const dm = s.match(/偏离=\$?([\d.]+)/);
    return dm
      ? `现货与基准价突发脱锚（偏离 $${dm[1]}），防灾暂停下单等收敛`
      : "现货与基准价突发脱锚，防灾暂停下单等待收敛";
  }
  if (/持续极端分歧/i.test(s)) {
    return "模型与盘口分歧过大且持续不收敛，暂不追单防异常";
  }
  if (/高波动暂停被动 Maker/i.test(s)) {
    return "当前 BTC 波动剧烈，暂停挂被动买单（仅保留高边际吃单）";
  }
  if ((m = s.match(/Tail freeze\s*\(tau\s*([\d.]+)s/i)) || /尾盘冻结|cutoff|tau too low|Late window/i.test(s)) {
    return "临近本轮结束（最后 15 秒），冻结下单，已有持仓安心等待结算";
  }

  // 3. 模式 B：单边顺势折价
  if (/模式B|MODE_B/i.test(s)) {
    const sm = s.match(/(Up|Down)\s+ask=\$?([\d.]+)/i);
    const pm = s.match(/P\*=([\d.]+)/i);
    if (sm) {
      const pTxt = pm ? `胜率约 ${pctStr(pm[1])}，` : "";
      return `高确定性顺势机会：看好${dirCn(sm[1])}（${pTxt}市场价 $${sm[2]}），直接出手买入`;
    }
    return "发现高胜率顺势折价机会，果断出手买入";
  }

  // 4. 低价赔率错杀 / 权重优选挂单与吃单（最核心的开仓动作）
  if (/低价赔率错杀.*挂单/i.test(s) && /bid=\$?([\d.]+)/i.test(s)) {
    const sm = s.match(/(Up|Down)\s+bid=\$?([\d.]+)/i);
    const pm = s.match(/P\*=([\d.]+)/i);
    if (sm) {
      const pTxt = pm ? `（估算胜率 ${pctStr(pm[1])}）` : "";
      return `发现低价高赔率错杀：以 $${sm[2]} 轻仓挂单买${dirCn(sm[1])}${pTxt}`;
    }
  }
  if (/低价赔率错杀.*吃单/i.test(s) && /ask=\$?([\d.]+)/i.test(s)) {
    const sm = s.match(/(Up|Down)\s+ask=\$?([\d.]+)/i);
    const pm = s.match(/P\*=([\d.]+)/i);
    if (sm) {
      const pTxt = pm ? `（估算胜率 ${pctStr(pm[1])}）` : "";
      return `发现低价高赔率错杀：以 $${sm[2]} 轻仓吃单买${dirCn(sm[1])}${pTxt}`;
    }
  }
  if (/净边际挂单|权重优选挂单|Maker/i.test(s) && /bid=\$?([\d.]+)/i.test(s)) {
    const sm = s.match(/(Up|Down)\s+bid=\$?([\d.]+)/i);
    const pm = s.match(/P\*=([\d.]+)/i);
    const em = s.match(/(?:净边际|零费边际)=([+-]?[\d.]+)/i);
    if (sm) {
      const pTxt = pm ? `预计胜率 ${pctStr(pm[1])}，` : "";
      const eTxt = em ? `，比公允价便宜 ${edgePct(em[1])}` : "";
      return `盘口平稳加权挂单：看好${dirCn(sm[1])}（${pTxt}以 $${sm[2]} 零费排队${eTxt}）`;
    }
  }
  if (/净边际吃单|权重优选吃单|IOC/i.test(s) && /ask=\$?([\d.]+)/i.test(s) && !/加仓|反买/.test(s)) {
    const sm = s.match(/(Up|Down)\s+ask=\$?([\d.]+)/i);
    const pm = s.match(/P\*=([\d.]+)/i);
    const em = s.match(/净边际=([+-]?[\d.]+)/i);
    if (sm) {
      const pTxt = pm ? `预计胜率 ${pctStr(pm[1])}，` : "";
      const eTxt = em ? `，比公允价便宜 ${edgePct(em[1])}` : "";
      return `时机占优直接吃单：看好${dirCn(sm[1])}（${pTxt}市场价 $${sm[2]}${eTxt}）`;
    }
  }

  // 5. 质量控均价加仓 / 翻后顺势加仓 / 终局反转调仓 / 再入场冷却 / 锁利 / 尾盘止损
  if (/优质控均价加仓|翻后顺势加仓|顺势确认加仓|顺势概率优势加仓/i.test(s)) {
    const sm = s.match(/(Up|Down)\s+(?:ask|bid)=\$?([\d.]+)/i);
    const avgm = s.match(/均价=\$?([\d.]+)/i);
    const tag = /翻后顺势加仓/i.test(s) ? "反手确认后顺势加仓" : "优质加仓";
    if (sm) {
      const avgTxt = avgm ? `（原均价 $${avgm[1]}）` : "";
      return `胜率稳健且不追高，以 $${sm[2]} ${tag}${dirCn(sm[1])}${avgTxt}`;
    }
    return "胜率稳健且价格占优，执行控均价加仓";
  }
  if (/反手调仓已完成|反手配平后再加仓|已禁顺势加仓/i.test(s)) {
    return "反手调仓后暂不追加仓位，安心持有至结算";
  }
  if (/终局反转反买|不对劲反买|反买/i.test(s)) {
    const sm = s.match(/(Up|Down)\s+(?:ask|bid)=\$?([\d.]+)/i);
    const qm = s.match(/\((\d+)股\)/);
    if (sm) {
      const qtyTxt = qm ? `一次性买入 ${qm[1]} 股` : "吃单买入";
      return `行情确认反转，按终局盈亏以 $${sm[2]} ${qtyTxt}${dirCn(sm[1])}一步调仓`;
    }
    return "行情确认反转，按终局目标一次性买入反方向调仓";
  }
  if (/波段再入场冷却/i.test(s)) {
    const cdm = s.match(/<\s*([\d.]+)s/i);
    const cdTxt = cdm ? `${Math.round(parseFloat(cdm[1]))} 秒` : "10 秒";
    return `刚完成调仓，短冷却 ${cdTxt}后重新观察领先方向`;
  }
  if (/Deep Arbitrage|黑天鹅锁利|Arbitrage\s+lock|Trailing\s+Pair\s+Lock/i.test(s)) {
    return "发现双边套利机会，买入另一侧提前锁定无风险利润";
  }
  if (/Defensive\s+Salvage|尾盘防守回收/i.test(s)) {
    return "临近结束胜率走弱，提前卖出部分持仓减少损失";
  }

  // 6. 各类风控保护与观望原因（彻底去黑话，精准解析盘口无足够净边际）
  if (/无足够净边际|净边际不足|No positive edge|edge too small|无正期望/i.test(s)) {
    const pm = s.match(/P\*\(?Up\)?=([\d.]+)/i);
    const um = s.match(/Up=([\d.-]+)\/([\d.-]+)/i);
    const dm = s.match(/Dn=([\d.-]+)\/([\d.-]+)/i);
    if (pm) {
      const pUp = parseFloat(pm[1]);
      if (Number.isFinite(pUp)) {
        const favorUp = pUp >= 0.5;
        const winPct = Math.round((favorUp ? pUp : 1 - pUp) * 100);
        const askPx = favorUp ? (um ? um[2] : "") : (dm ? dm[2] : "");
        const sideTxt = favorUp ? "看涨" : "看跌";
        if (askPx && /^[\d.]+$/.test(askPx)) {
          return `模型估计${sideTxt}胜率约 ${winPct}%，但当前市场卖价 $${askPx} 偏贵，不够划算，继续等待`;
        }
        return `模型估计${sideTxt}胜率约 ${winPct}%，当前盘口价格不够划算，继续耐心等待`;
      }
    }
    return "当前盘口价格不够便宜，没有明显性价比，继续耐心观望";
  }
  if (/极端巨震|风暴避险/i.test(s)) {
    return "BTC 近期波动过于剧烈，已启动避险模式，暂停新下单";
  }
  if (/Anti-toxicity|Toxic|shock|剧震/i.test(s)) {
    return "BTC 刚刚突发剧烈波动，为防误判已暂停下单并撤回挂单";
  }
  if (/Post-Only|到达对盘/i.test(s)) {
    return "盘口价格瞬间移动，为防吃亏已自动撤单";
  }
  if (/方向反转|反转撤单/i.test(s)) {
    return "行情方向发生反转，已主动撤回未成交挂单";
  }
  if (/卖墙|ask wall|wall/i.test(s)) {
    return "盘口上方出现大额压单，为避免接盘已暂停挂单";
  }
  if (/延迟|stale|Dead-man|ms\b/i.test(s)) {
    return "行情同步略有延迟，为了交易安全暂缓出手";
  }
  if (/outside balanced entry zone|价格不在区间|极端/i.test(s)) {
    return "当前盘口价格已非常极端（胜负基本已分），不追高，保持观望";
  }
  if (/spread too wide|价差/i.test(s)) {
    return "买卖价差太大，此时买入不划算，继续观望";
  }
  if (/Inconsistent book|Incomplete orderbook|空盘/i.test(s)) {
    return "市场挂单深度不足，暂停下单等待流动性恢复";
  }
  if (/Waiting for official strike|strike price unavailable/i.test(s)) {
    return "正在等待本轮官方开盘价确认...";
  }
  if (/Max entries|Fully hedged|Max position|Max capital|budget|已完成配对|终局调仓/i.test(s)) {
    return "本轮仓位已完成布局或调仓，拿好现有持仓等待结算";
  }
  if (/Insufficient capital/i.test(s)) {
    return "可用资金不足，暂停开新仓";
  }
  if (/Circuit breaker|Loss streak|daily\s*(drawdown|loss)/i.test(s)) {
    return "触发账户风控保护，暂停开新仓，保护本金";
  }
  if (/观望/i.test(s)) {
    return "当前盘口暂无明显性价比，继续耐心观望";
  }
  if (/Strong bullish trend|Strong bearish trend|Mean-reverting|Normal oscillation|Transitional drift/i.test(s)) {
    return "正在持续跟踪实时行情与盘口价格变化...";
  }

  // 7. 兜底清洗：彻底去掉版本号、嵌套英文参数括号，绝不泄露半截公式乱码
  s = s
    .replace(/^v\d+\.\d+:?\s*/i, "")
    .replace(/\((?:[^()]|\([^()]*\))*\)/g, "")
    .replace(/\b(IOC|Maker|Taker|FIFO|MWDE|LWAA)\b/gi, "")
    .replace(/\bUp\b/g, "看涨")
    .replace(/\bDown\b/g, "看跌")
    .replace(/\s{2,}/g, " ")
    .trim();

  return /[\u4e00-\u9fa5]/.test(s) ? s : "正在持续观察实时行情，寻找划算的买入价格...";
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

  // 实验运行时长、开始时间、今日战绩与时均/日均收益速率条（净值曲线底部）
  let expStartTs = Number(hist.experiment_start_ts || 0);
  if (!(expStartTs > 0)) {
    const curveArr = Array.isArray(hist.equity_curve) ? hist.equity_curve : [];
    const recArr = Array.isArray(hist.records) ? hist.records : [];
    const candidates = [];
    curveArr.forEach((p) => { if (Number(p?.start) > 0) candidates.push(Number(p.start)); });
    recArr.forEach((r) => {
      const s = Number(r?.start || 0);
      if (s > 0) candidates.push(s);
      else if (typeof r?.slug === "string") {
        const tail = r.slug.split("-").pop();
        if (/^\d{9,}$/.test(tail)) candidates.push(Number(tail));
      }
    });
    if (candidates.length > 0) expStartTs = Math.min(...candidates);
    else if (live && Number(live.start) > 0) expStartTs = Number(live.start);
  }
  const nowSecForPace = live?.server_time ? Number(live.server_time) : (Date.now() / 1000);
  const isCurrentBatch = !hist.batch_id || !hist.current_batch || hist.batch_id === hist.current_batch;
  let elapsedSec = Number(hist.elapsed_seconds || 0);
  if (expStartTs > 0 && isCurrentBatch) {
    elapsedSec = Math.max(elapsedSec, Math.max(0, nowSecForPace - expStartTs));
  }
  const fmtDuration = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    const days = Math.floor(s / 86400);
    const hrs = Math.floor((s % 86400) / 3600);
    const mins = Math.floor((s % 3600) / 60);
    if (days > 0) return `${days}d ${String(hrs).padStart(2, "0")}h`;
    if (hrs > 0) return `${hrs}h ${String(mins).padStart(2, "0")}m`;
    return `${Math.max(1, mins)}m`;
  };
  const fmtStartBjt = (ts) => {
    if (!ts) return "始于 --";
    const d = new Date((ts + 8 * 3600) * 1000);
    const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
    const da = String(d.getUTCDate()).padStart(2, "0");
    const hh = String(d.getUTCHours()).padStart(2, "0");
    const mm = String(d.getUTCMinutes()).padStart(2, "0");
    return `始于 ${mo}/${da} ${hh}:${mm}`;
  };
  if ($("pace-elapsed")) {
    $("pace-elapsed").textContent = expStartTs > 0 ? fmtDuration(elapsedSec) : "--";
  }
  if ($("pace-start-at")) {
    const stText = fmtStartBjt(expStartTs);
    $("pace-start-at").textContent = stText;
    if ($("cs-runtime-wrap")) {
      $("cs-runtime-wrap").title = `${stText}（累计持续运行）`;
    }
  }
  const tdPnl = Number(hist.today_pnl || 0);
  const tdW = Number(hist.today_wins || 0);
  const tdL = Number(hist.today_losses || 0);
  if ($("pace-today-pnl")) {
    $("pace-today-pnl").textContent = fmtUsd(tdPnl, true);
    $("pace-today-pnl").className = `tempo-val ${tdPnl > 1e-6 ? "up" : tdPnl < -1e-6 ? "down" : ""}`;
  }
  if ($("pace-today-wl")) {
    $("pace-today-wl").textContent = `${tdW}胜 ${tdL}负`;
  }
  const hasTrades = Number(hist.traded_rounds || hist.count || 0) > 0;
  const elapsedH = elapsedSec / 3600;
  const hasServerRates = hist.hourly_pnl !== undefined && hist.hourly_pnl !== null;
  const hrPnl = hasServerRates
    ? Number(hist.hourly_pnl || 0)
    : (hasTrades && expStartTs > 0 ? totalPnl / Math.max(1, elapsedH) : 0);
  const hrProjected = hasServerRates ? Boolean(hist.hourly_projected) : (elapsedH < 1);
  const dayPnl = (hist.daily_pnl !== undefined && hist.daily_pnl !== null)
    ? Number(hist.daily_pnl || 0)
    : (hasTrades && expStartTs > 0 ? (elapsedH >= 24 ? totalPnl / (elapsedH / 24) : hrPnl * 24) : 0);
  const dayProjected = (hist.daily_projected !== undefined && hist.daily_projected !== null)
    ? Boolean(hist.daily_projected)
    : (elapsedH < 24);
  if ($("pace-hourly-pnl")) {
    $("pace-hourly-pnl").textContent = hasTrades ? `${fmtUsd(hrPnl, true)}` : "$0.00";
    $("pace-hourly-pnl").className = `tempo-val ${hasTrades ? (hrPnl > 1e-6 ? "up" : hrPnl < -1e-6 ? "down" : "") : ""}`;
  }
  if ($("pace-hourly-note")) {
    $("pace-hourly-note").textContent = !hasTrades ? "等待成交" : (hrProjected ? "未满1时折算" : "实测时均");
  }
  if ($("pace-daily-pnl")) {
    $("pace-daily-pnl").textContent = hasTrades ? `${fmtUsd(dayPnl, true)}` : "$0.00";
    $("pace-daily-pnl").className = `tempo-val ${hasTrades ? (dayPnl > 1e-6 ? "up" : dayPnl < -1e-6 ? "down" : "") : ""}`;
  }
  if ($("pace-daily-note")) {
    $("pace-daily-note").textContent = !hasTrades ? "等待成交" : (dayProjected ? "按24h推算" : "实测日均");
  }

  const brierTargets = [$("side-brier-score"), $("modal-brier-score")];
  {
    const bCnt = Number(hist.brier_count || 0);
    const bMod = hist.brier_model;
    const bMkt = hist.brier_market;
    if (bCnt > 0 && bMod !== null && bMod !== undefined && bMkt !== null && bMkt !== undefined) {
      const mVal = Number(bMod);
      const kVal = Number(bMkt);
      const bText = `${mVal.toFixed(3)} / ${kVal.toFixed(3)}`;
      const bCls = mVal < kVal - 0.001 ? "is-ok" : mVal > kVal + 0.002 ? "is-warn" : "";
      const bTitle = `已统计 ${bCnt} 窗中段预测：模型 ${mVal.toFixed(5)} vs 盘口 ${kVal.toFixed(5)}（越小越准）`;
      brierTargets.forEach((el) => {
        if (!el) return;
        el.textContent = bText;
        el.className = `source-metric-val conn-tile-val ${bCls}`;
        el.title = bTitle;
      });
    } else {
      brierTargets.forEach((el) => {
        if (!el) return;
        el.textContent = "--";
        el.className = "source-metric-val conn-tile-val";
        el.title = "";
      });
    }
  }

  const roiSub = $("kpi-avail-sub");
  const roiPill = $("hero-equity-roi-pill");
  if (roiSub) roiSub.textContent = fmtPct(equityRoi, true);
  if (roiPill) {
    roiPill.style.color = equityRoi >= 0 ? "var(--up)" : "var(--down)";
    roiPill.style.background = equityRoi >= 0 ? "var(--up-soft)" : "var(--down-soft)";
    roiPill.style.borderColor = equityRoi >= 0 ? "var(--up-border)" : "var(--down-border)";
  }

  // Gross Won vs Lost bar + Profit Factor
  const wonEl = $("hero-won");
  const lostEl = $("hero-lost");
  const winBarEl = $("hero-win-bar");
  const pfEl = $("hero-pf-badge");
  if (wonEl) wonEl.textContent = fmtUsd(totalWon, true);
  if (lostEl) lostEl.textContent = `-$${totalLost.toFixed(2)}`;
  if (winBarEl) {
    const grossSum = totalWon + totalLost;
    const ratio = grossSum > 0 ? Math.min(98, Math.max(2, (totalWon / grossSum) * 100)) : 50;
    winBarEl.style.width = `${ratio.toFixed(1)}%`;
  }
  // PF Visual Gauge (0.0 -> 1.0 breakeven @40% -> 1.5 good @60% -> 2.5+ @100%)
  const pfFillEl = $("kpi-pf-fill");
  const pfMarkerEl = $("kpi-pf-marker");
  const pfVerdictEl = $("kpi-pf-verdict");
  if ($("kpi-quality-samples")) {
    $("kpi-quality-samples").textContent = String(tradedRounds);
  }
  {
    let pfPct = 0;
    let pfTone = "neutral";
    let pfVerdict = "等待样本";
    let pfDisplay = "--";
    let hasPfSample = false;
    if (totalWon > 1e-6 && totalLost <= 1e-6) {
      hasPfSample = true;
      pfPct = 100;
      pfTone = "good";
      pfDisplay = "∞";
      pfVerdict = "暂未亏损";
    } else if (totalLost > 1e-6) {
      hasPfSample = true;
      const pfVal = totalWon / totalLost;
      pfDisplay = pfVal.toFixed(2);
      pfPct = Math.max(0, Math.min(100, (pfVal / 2.5) * 100));
      if (pfVal < 1.0) {
        pfTone = "warn";
        pfVerdict = "低于盈亏线";
      } else if (pfVal < 1.15) {
        pfTone = "mid";
        pfVerdict = "微利平衡";
      } else if (pfVal < 1.5) {
        pfTone = "mid";
        pfVerdict = "健康盈利";
      } else {
        pfTone = "good";
        pfVerdict = "表现优秀";
      }
    }
    if (pfEl) {
      pfEl.textContent = pfDisplay;
      pfEl.className = `pf-num-val is-${pfTone}`;
    }
    if (pfFillEl) {
      pfFillEl.style.width = `${pfPct.toFixed(1)}%`;
      pfFillEl.className = `pf-track-fill is-${pfTone}`;
    }
    if (pfMarkerEl) {
      pfMarkerEl.style.left = `${pfPct.toFixed(1)}%`;
      pfMarkerEl.style.opacity = hasPfSample ? "1" : "0";
      pfMarkerEl.className = `pf-track-marker is-${pfTone}`;
    }
    if (pfVerdictEl) {
      pfVerdictEl.textContent = pfVerdict;
      pfVerdictEl.className = `pf-verdict-chip is-${pfTone}`;
    }
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

  // Lower Right: Execution Structure Attribution (Unified Flat-Card Visuals)
  const eb = hist.execution_breakdown || {};
  const ebTotalCnt = ["maker", "taker", "flip"].reduce((s, k) => s + Number(eb[k]?.count || 0), 0);
  [
    ["maker", "exec-card-maker", "exec-maker-cnt", "exec-maker-pnl", "exec-maker-wr", "exec-maker-bar", "exec-maker-avg", "exec-share-maker", "exec-pct-maker"],
    ["taker", "exec-card-taker", "exec-taker-cnt", "exec-taker-pnl", "exec-taker-wr", "exec-taker-bar", "exec-taker-avg", "exec-share-taker", "exec-pct-taker"],
    ["flip", "exec-card-flip", "exec-flip-cnt", "exec-flip-pnl", "exec-flip-wr", "exec-flip-bar", "exec-flip-avg", "exec-share-flip", "exec-pct-flip"],
  ].forEach(([key, cardId, cntId, pnlId, wrId, barId, avgId, shareId, pctId]) => {
    const item = eb[key] || { count: 0, wins: 0, losses: 0, win_rate: 0, pnl: 0 };
    const c = Number(item.count || 0);
    const w = Number(item.wins || 0);
    const l = Number(item.losses || 0);
    const wr = Number(item.win_rate || 0);
    const p = Number(item.pnl || 0);
    const sharePct = ebTotalCnt > 0 ? (c / ebTotalCnt) * 100 : 0;
    const avgPer = c > 0 ? p / c : 0;

    if ($(shareId)) $(shareId).style.width = `${sharePct.toFixed(1)}%`;
    if ($(pctId)) $(pctId).textContent = `${sharePct.toFixed(0)}%`;
    if ($(cntId)) $(cntId).textContent = `${c} 场`;
    if ($(pnlId)) {
      $(pnlId).textContent = c > 0 ? fmtUsd(p, true) : "$0.00";
      $(pnlId).className = `exec-card-pnl num ${c > 0 ? (p > 1e-6 ? "up" : p < -1e-6 ? "down" : "") : ""}`;
    }
    if ($(barId)) {
      $(barId).style.width = c > 0 ? `${Math.max(4, Math.min(100, wr)).toFixed(1)}%` : "0%";
      $(barId).className = `exec-wr-fill is-${key}`;
    }
    if ($(wrId)) {
      $(wrId).textContent = c > 0 ? `胜率 ${wr.toFixed(1)}% (${w}胜 ${l}负)` : "胜率 --";
    }
    if ($(avgId)) {
      $(avgId).textContent = c > 0 ? `场均 ${fmtUsd(avgPer, true)}` : "场均 --";
      $(avgId).className = `exec-ch-avg num ${c > 0 ? (avgPer > 1e-6 ? "up" : avgPer < -1e-6 ? "down" : "") : ""}`;
    }
    const card = $(cardId);
    if (card) {
      const isAlert = c >= 5 && wr < 45.0 && p < -1e-6;
      card.classList.toggle("is-alert", isAlert);
      card.title = "点击筛选该通道的历史场次记录";
    }
  });

  // Active Round Card in Overview
  const liveStatusPill = $("overview-live-status");
  const nowSecOv = live?.server_time ? Number(live.server_time) : (Date.now() / 1000);
  const hbAgeOv = live?.now ? Math.max(0, nowSecOv - Number(live.now)) : Infinity;
  const apiPausedOv = Boolean(live?.api_schema_guard?.paused);
  if (liveStatusPill) {
    let stCls = "status-pill status-ready";
    let stText = "当前轮次进行中";
    if (!live || !live.slug || hbAgeOv > 60) {
      stCls = "status-pill status-critical";
      stText = "交易引擎离线";
    } else if (apiPausedOv) {
      stCls = "status-pill status-critical";
      stText = "接口变更 · 安全暂停";
    } else if (hbAgeOv > 15) {
      stCls = "status-pill status-warn";
      stText = "行情心跳延迟";
    }
    liveStatusPill.className = stCls;
    setHtmlIfChanged(liveStatusPill, `<span class="status-dot"></span><span>${stText}</span>`);
  }
  if (live && live.slug) {
    if (live.slug && live.slug !== liveSelectedActionSlug) {
    liveSelectedActionIdx = null;
    liveSelectedActionSlug = live.slug;
  }

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
      const displaySpot = Number(live.poly_spot || live.spot || 0);
      const spotNum = displaySpot.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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

  // Lower Left (Primary): Visual 2x2 Strategy Quality Diagnostics
  const rawRecords = Array.isArray(hist.records) ? hist.records : [];
  const normRecords = rawRecords.map(normalizeRecord).filter(Boolean);
  const chronoRecords = normRecords.slice().sort((a, b) => (a.start_ts || 0) - (b.start_ts || 0));

  // Dynamic breakeven win rate: avgLoss / (avgWin + avgLoss)
  const beWr = (avgWin > 1e-6 && avgLoss > 1e-6) ? (avgLoss / (avgWin + avgLoss)) * 100 : null;
  const winrateEl = $("kpi-winrate");
  if (winrateEl) {
    winrateEl.textContent = `${winRate.toFixed(1)}%`;
    winrateEl.className = "qm-val";
  }
  if ($("kpi-winloss")) {
    const beTxt = beWr !== null ? ` · 保本线 ${beWr.toFixed(1)}%` : "";
    $("kpi-winloss").textContent = `${wins} 胜 · ${losses} 负 · ${flats} 平${beTxt}`;
  }
  const winSeqEl = $("qm-win-sequence");
  if (winSeqEl) {
    const recentTraded = chronoRecords.filter((r) => r.traded).slice(-16);
    const seqSlots = 16;
    const padCnt = Math.max(0, seqSlots - recentTraded.length);
    const cells = [];
    for (let i = 0; i < padCnt; i++) {
      cells.push(`<span class="qm-form-cell is-empty" title="待成交"></span>`);
    }
    recentTraded.forEach((r) => {
      const p = Number(r.pnl || 0);
      const cls = p > 1e-4 ? "is-win" : p < -1e-4 ? "is-loss" : "is-flat";
      const lbl = p > 1e-4 ? "胜" : p < -1e-4 ? "负" : "平";
      const winTxt = slugToWindowText(r.slug, r.start_ts, r.end_ts).range || r.slug;
      cells.push(`<span class="qm-form-cell ${cls}" title="${escapeHtml(winTxt)} · ${lbl} ${fmtUsd(p, true)}">${lbl}</span>`);
    });
    setHtmlIfChanged(winSeqEl, cells.join(""));
  }

  const avgPnlEl = $("kpi-avg-pnl");
  if (avgPnlEl) {
    avgPnlEl.textContent = fmtUsd(avgPnl, true);
    avgPnlEl.className = `qm-val ${avgPnl > 0 ? "up" : avgPnl < 0 ? "down" : ""}`;
  }

  // Avg Win vs Avg Loss direct amount bars + ratio line
  const barAvgWinEl = $("qm-bar-avgwin");
  const barAvgLossEl = $("qm-bar-avgloss");
  const avgWinAmtEl = $("kpi-avgwin-amt");
  const avgLossAmtEl = $("kpi-avgloss-amt");
  const maxAvgLeg = Math.max(avgWin, avgLoss, 0);
  if (barAvgWinEl) {
    barAvgWinEl.style.width = maxAvgLeg > 1e-6 ? `${Math.max(4, (avgWin / maxAvgLeg) * 100).toFixed(1)}%` : "0%";
  }
  if (barAvgLossEl) {
    barAvgLossEl.style.width = maxAvgLeg > 1e-6 ? `${Math.max(4, (avgLoss / maxAvgLeg) * 100).toFixed(1)}%` : "0%";
  }
  if (avgWinAmtEl) avgWinAmtEl.textContent = `+$${avgWin.toFixed(2)}`;
  if (avgLossAmtEl) avgLossAmtEl.textContent = `-$${avgLoss.toFixed(2)}`;
  if ($("kpi-avg-split")) {
    if (avgWin > 1e-6 && avgLoss > 1e-6) {
      $("kpi-avg-split").textContent = `单场赚赔比 ${(avgWin / avgLoss).toFixed(2)} : 1`;
    } else if (avgWin > 1e-6) {
      $("kpi-avg-split").textContent = "单场赚赔比 ∞ : 1（暂无亏损）";
    } else {
      $("kpi-avg-split").textContent = "单场赚赔比 --";
    }
  }

  // Full-history Max Drawdown (in Equity Curve Header) & Total Fills (in Engine Attribution Header)
  let totalFills = Number.isFinite(Number(hist.total_fills)) ? Number(hist.total_fills) : 0;
  if (!Number.isFinite(Number(hist.total_fills))) {
    totalFills = normRecords.reduce((acc, r) => acc + r.fills_count, 0);
  }
  const maxDd = Number.isFinite(Number(hist.max_drawdown)) ? Math.max(0, Number(hist.max_drawdown)) : 0;
  const maxDdEl = $("kpi-max-dd");
  if (maxDdEl) {
    maxDdEl.textContent = maxDd > 1e-4 ? `-$${maxDd.toFixed(2)}` : "$0.00";
    maxDdEl.className = `cs-val ${maxDd > 1e-4 ? "down" : ""}`;
  }
  if ($("kpi-total-fills")) {
    $("kpi-total-fills").textContent = `共 ${totalFills} 笔成交`;
  }

  const partRate = totalRounds > 0 ? (tradedRounds / totalRounds) * 100 : 0;
  const skippedRounds = Math.max(0, totalRounds - tradedRounds);
  const capTurnMultiple = initialCap > 0 ? turnover / initialCap : 0;
  if ($("kpi-participation-rate")) $("kpi-participation-rate").textContent = `${partRate.toFixed(1)}%`;
  if ($("kpi-participation-sub")) {
    $("kpi-participation-sub").textContent = `出手 ${tradedRounds} 场 · 观望 ${skippedRounds} 场（共 ${totalRounds} 场）`;
  }
  const partTlEl = $("qm-part-timeline");
  if (partTlEl) {
    const recentAll = chronoRecords.slice(-24);
    const tlSlots = 24;
    const padCnt = Math.max(0, tlSlots - recentAll.length);
    const bars = [];
    for (let i = 0; i < padCnt; i++) {
      bars.push(`<i class="qm-up-bar is-empty"></i>`);
    }
    recentAll.forEach((r) => {
      const winTxt = slugToWindowText(r.slug, r.start_ts, r.end_ts).range || r.slug;
      if (r.traded) {
        bars.push(`<i class="qm-up-bar is-traded" title="${escapeHtml(winTxt)} · 已出手 (${fmtUsd(r.pnl, true)})"></i>`);
      } else {
        bars.push(`<i class="qm-up-bar is-skip" title="${escapeHtml(winTxt)} · 观望未出手"></i>`);
      }
    });
    setHtmlIfChanged(partTlEl, bars.join(""));
  }

  // Primary Bet ROI + Zero-Centered Diverging Bar (-25% .. 0% .. +25%)
  const roiValEl = $("kpi-turnover-roi-pill");
  if (roiValEl) {
    roiValEl.textContent = turnover > 1e-4 ? fmtPct(turnoverRoi, true) : "+0.00%";
    roiValEl.className = `qm-val ${turnover > 1e-4 ? (turnoverRoi > 1e-6 ? "up" : turnoverRoi < -1e-6 ? "down" : "") : ""}`;
  }
  const turnNoteEl = $("kpi-turnover-note");
  if (turnNoteEl) {
    setHtmlIfChanged(
      turnNoteEl,
      `净盈亏 ${fmtUsd(totalPnl, true)} · 下注 ${fmtUsd(turnover)} · 周转 <span id="kpi-turnover-mult">${capTurnMultiple.toFixed(2)}x</span>`
    );
  }
  const roiBarEl = $("qm-roi-bar");
  if (roiBarEl) {
    const spanHalf = Math.min(50, (Math.abs(turnoverRoi) / 25.0) * 50);
    if (turnover > 1e-4 && turnoverRoi > 1e-6) {
      roiBarEl.style.left = "50%";
      roiBarEl.style.width = `${Math.max(2, spanHalf).toFixed(1)}%`;
      roiBarEl.className = "qm-roi-bar is-pos";
    } else if (turnover > 1e-4 && turnoverRoi < -1e-6) {
      const wPct = Math.max(2, spanHalf);
      roiBarEl.style.left = `${(50 - wPct).toFixed(1)}%`;
      roiBarEl.style.width = `${wPct.toFixed(1)}%`;
      roiBarEl.className = "qm-roi-bar is-neg";
    } else {
      roiBarEl.style.left = "50%";
      roiBarEl.style.width = "0%";
      roiBarEl.className = "qm-roi-bar";
    }
  }

  // Render Cumulative Equity Curve
  drawEquityCurve(hist);
}

// ============================================================================
// INTERACTIVE CUMULATIVE EQUITY CURVE CANVAS
// ============================================================================
function drawEquityCurve(hist) {
  const canvas = $("equity-curve-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const wrap = canvas.parentElement;
  const w = Math.max(260, (wrap ? wrap.clientWidth : canvas.clientWidth) || 420);
  const h = Math.max(180, Math.min(320, (wrap ? wrap.clientHeight : 256) || 256));

  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pal = getPalette();
  const rawCurve = Array.isArray(hist?.equity_curve) ? hist.equity_curve : [];

  // Backend equity_curve has { idx, slug, pnl (cumulative!), round_pnl, start }
  const pts = [{ idx: 0, cum_pnl: 0, round_pnl: 0, slug: "", ts: 0 }];
  let prevCum = 0;
  rawCurve.forEach((p, idx) => {
    const cum = Number(p.cum_pnl ?? p.pnl ?? 0);
    const delta = p.round_pnl !== undefined ? Number(p.round_pnl) : cum - prevCum;
    prevCum = cum;
    const realIdx = Number.isFinite(Number(p.idx)) && Number(p.idx) > 0 ? Number(p.idx) : idx + 1;
    pts.push({
      idx: realIdx,
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
  if (peakBadge) peakBadge.textContent = fmtUsd(peak, true);

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
  const padT = 22;
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

  // Adaptive smooth local trend curve (zero-phase centered EMA / rolling trend that genuinely bends with local waves)
  if (coords.length >= 4) {
    const nT = coords.length;
    const span = Math.max(3, Math.min(7, Math.round(nT * 0.28)));
    const alpha = 2 / (span + 1);
    const fwd = new Array(nT);
    fwd[0] = coords[0].cum_pnl;
    for (let i = 1; i < nT; i++) {
      fwd[i] = alpha * coords[i].cum_pnl + (1 - alpha) * fwd[i - 1];
    }
    // Forward-backward blend eliminates phase lag so peaks/valleys align with local regime turns
    const bwd = new Array(nT);
    bwd[nT - 1] = fwd[nT - 1];
    for (let i = nT - 2; i >= 0; i--) {
      bwd[i] = alpha * fwd[i] + (1 - alpha) * bwd[i + 1];
    }
    const tPts = coords.map((c, i) => ({
      x: c.x,
      y: Math.max(padT, Math.min(padT + plotH, yOf(bwd[i]))),
    }));
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(96, 165, 250, 0.72)" : "rgba(37, 99, 235, 0.62)";
    ctx.lineWidth = 1.6;
    ctx.setLineDash([5, 4]);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(tPts[0].x, tPts[0].y);
    for (let i = 1; i < tPts.length - 1; i++) {
      const mx = (tPts[i].x + tPts[i + 1].x) * 0.5;
      const my = (tPts[i].y + tPts[i + 1].y) * 0.5;
      ctx.quadraticCurveTo(tPts[i].x, tPts[i].y, mx, my);
    }
    ctx.lineTo(tPts[tPts.length - 1].x, tPts[tPts.length - 1].y);
    ctx.stroke();
    ctx.restore();
  }

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

  // Clean X-axis time boundary labels (spaced away from Y-axis to prevent overlap)
  const _fmtCurveTs = (p) => {
    const ts = Number(p?.ts || 0);
    if (ts > 1e9) {
      const d = new Date(ts * 1000);
      const mm = String(d.getMonth() + 1).padStart(2, "0");
      const dd = String(d.getDate()).padStart(2, "0");
      const hh = String(d.getHours()).padStart(2, "0");
      const mi = String(d.getMinutes()).padStart(2, "0");
      return `${mm}/${dd} ${hh}:${mi}`;
    }
    return "";
  };
  const xFirstPt = pts.length > 1 ? pts[1] : pts[0];
  const xLastPt = pts[pts.length - 1];
  const leftLbl = _fmtCurveTs(xFirstPt) || "#1";
  const rightLbl = _fmtCurveTs(xLastPt) || `#${xLastPt.idx}`;
  ctx.fillStyle = pal.axis;
  ctx.font = "500 10px 'Inter', -apple-system, sans-serif";
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  ctx.fillText(leftLbl, padL + 4, padT + plotH + 9);
  ctx.textAlign = "right";
  ctx.fillText(rightLbl, w - padR, padT + plotH + 9);
}

function initEquityCurveInteractions() {
  const canvas = $("equity-curve-canvas");
  const tooltip = $("equity-tooltip");
  if (!canvas || !tooltip) return;

  // Clickable Execution Attribution rows -> jump to History Ledger with smart filter
  document.querySelectorAll("[data-jump-exec]").forEach((row) => {
    row.addEventListener("click", () => {
      const mode = row.getAttribute("data-jump-exec");
      const targetFilter = ["maker", "taker", "flip"].includes(mode) ? mode : "traded";
      historyFilterMode = targetFilter;
      document.querySelectorAll("#history-filter-group .filter-btn").forEach((b) => {
        b.classList.toggle("active", b.getAttribute("data-filter") === targetFilter);
      });
      window.location.hash = "#history";
      if (typeof renderHistoryTable === "function" && cachedHistory) {
        renderHistoryTable(cachedHistory);
      }
    });
  });

  const findNearestIdx = (clientX) => {
    if (!equityChartGeom || !equityChartGeom.pts?.length) return null;
    const rect = canvas.getBoundingClientRect();
    const mx = clientX - rect.left;
    let bestIdx = 0;
    let bestDist = Infinity;
    equityChartGeom.pts.forEach((p, idx) => {
      const d = Math.abs(p.x - mx);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = idx;
      }
    });
    return { bestIdx, rect };
  };

  const renderTooltipAt = (bestIdx) => {
    equityHoverIdx = bestIdx;
    drawEquityCurve(cachedHistory);

    const pt = equityChartGeom.pts[bestIdx];
    canvas.style.cursor = "crosshair";
    if (bestIdx === 0) {
      tooltip.innerHTML = `
        <span class="chb-idx">起点 #0</span>
        <span class="chb-sep">·</span>
        <span>本金 <b>${fmtUsd(cachedHistory?.initial_capital || 100)}</b></span>
      `;
    } else {
      const winInfo = slugToWindowText(pt.slug, pt.ts);
      const delta = Number(pt.round_pnl || 0);
      const cum = Number(pt.cum_pnl || 0);
      tooltip.innerHTML = `
        <span class="chb-idx">#${pt.idx} ${escapeHtml(winInfo.range)}</span>
        <span class="chb-sep">·</span>
        <span>单场 <b class="${delta > 0 ? "up" : delta < 0 ? "down" : ""}">${fmtUsd(delta, true)}</b></span>
        <span class="chb-sep">·</span>
        <span>累计 <b class="${cum > 0 ? "up" : cum < 0 ? "down" : ""}">${fmtUsd(cum, true)}</b></span>
      `;
    }
    tooltip.hidden = false;
  };

  canvas.addEventListener("mousemove", (e) => {
    const hit = findNearestIdx(e.clientX);
    if (!hit) return;
    renderTooltipAt(hit.bestIdx);
  });

  canvas.addEventListener("mouseleave", () => {
    equityHoverIdx = null;
    canvas.style.cursor = "default";
    tooltip.hidden = true;
    if (cachedHistory) drawEquityCurve(cachedHistory);
  });

  canvas.addEventListener("touchstart", (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    const hit = findNearestIdx(t.clientX);
    if (hit) renderTooltipAt(hit.bestIdx);
  }, { passive: true });

  canvas.addEventListener("touchmove", (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    const hit = findNearestIdx(t.clientX);
    if (hit) renderTooltipAt(hit.bestIdx);
  }, { passive: true });
}

// ============================================================================
// TABLE ROW BUILDER FOR SETTLED ROUNDS (OVERVIEW & HISTORY)
// ============================================================================
function buildRoundTableRow(rawRec, includeInsuranceCol = false) {
  const r = rawRec.up_shares !== undefined ? rawRec : normalizeRecord(rawRec);
  const winInfo = slugToWindowText(r.slug, r.start_ts, r.end_ts);
  const winner = String(r.winner || "PENDING").toUpperCase();
  const isProvisional = String(r.winner_source || "").toLowerCase() === "spot_fallback";
  let winnerBadge = `<span class="winner-pill pending">平局 / 未定</span>`;
  if (winner === "UP") {
    winnerBadge = `<span class="winner-pill up">${SVG_ICONS.up}UP 胜出</span>`;
  } else if (winner === "DOWN") {
    winnerBadge = `<span class="winner-pill down">${SVG_ICONS.down}DOWN 胜出</span>`;
  }
  const provNote = isProvisional
    ? `<small style="display:block;font-size:10.5px;color:var(--caption);margin-top:2px" title="Polymarket 官方 60s TWAP 结算价尚未公布，当前为同链暂估价，确认后将自动刷新">暂估 · 待官方核验</small>`
    : "";

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

  const regimeShortMap = {
    range_bound: "震荡",
    normal_oscillation: "常规",
    skewed_drift: "单边",
    trending_up: "涨势",
    trending_down: "跌势",
    toxic: "毒流",
    wide_spread: "宽差",
    transitional: "过渡",
    tail_freeze: "尾盘",
  };
  const modeMetaMap = {
    flip: ["调仓", "tag-mode-b"],
    mode_b: ["模式B", "tag-mode-b"],
    taker: ["Taker", "tag-taker"],
    maker: ["Maker", "tag-maker"],
  };
  const regLabel = regimeShortMap[r.regime] || "";
  const modeMeta = r.traded && r.entry_mode ? modeMetaMap[r.entry_mode] : null;
  const miniTags = [
    regLabel ? `<span class="row-mini-tag tag-regime">${regLabel}</span>` : "",
    modeMeta ? `<span class="row-mini-tag ${modeMeta[1]}">${modeMeta[0]}</span>` : "",
  ].filter(Boolean).join("");

  return `
    <tr class="history-row-clickable" data-slug="${escapeHtml(r.slug)}" title="点击调阅 ${escapeHtml(winInfo.fullLabel)} 的 5 分钟行情与交易复盘">
      <td>
        <span class="time-cell-main">${escapeHtml(winInfo.range)}</span>
        <div class="time-cell-sub">
          <small>${escapeHtml(winInfo.dateStr)}</small>
          ${miniTags ? `<span class="row-tags">${miniTags}</span>` : ""}
        </div>
      </td>
      <td class="center">${winnerBadge}${provNote}</td>
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
let _historyRenderCap = 200;

function renderHistoryTable(hist) {
  const tbody = $("history-tbody");
  const countEl = $("history-count");
  if (!tbody) return;

  const rawRecords = Array.isArray(hist?.records) ? hist.records : [];
  const totalServerRounds = Number(hist?.records_total || hist?.total_rounds || rawRecords.length);
  const allRecords = rawRecords.map(normalizeRecord).filter(Boolean);
  const todayDateStr = String(hist?.today_date || "");
  const fmtBjtYmd = (ts) => {
    if (!ts) return "";
    const d = new Date((Number(ts) + 8 * 3600) * 1000);
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
  };
  const filtered = allRecords.filter((r) => {
    if (historyTodayOnly && todayDateStr) {
      const rDay = fmtBjtYmd(r.end_ts || r.start_ts);
      if (rDay !== todayDateStr) return false;
    }
    if (historyFilterMode === "traded") return r.traded;
    if (historyFilterMode === "win") return r.pnl > 1e-6;
    if (historyFilterMode === "loss") return r.pnl < -1e-6;
    if (historyFilterMode === "flip") return r.traded && (r.entry_mode === "flip" || (r.up_shares > 1e-6 && r.down_shares > 1e-6));
    if (historyFilterMode === "taker") return r.traded && (r.entry_mode === "taker" || r.entry_mode === "mode_b");
    if (historyFilterMode === "maker") return r.traded && r.entry_mode === "maker";
    return true;
  });

  const visible = filtered.slice(0, _historyRenderCap);
  if (countEl) {
    const isNarrowed = filtered.length !== allRecords.length;
    const statHtml = isNarrowed
      ? `${filtered.length} / ${totalServerRounds} 场`
      : `${totalServerRounds} 场`;
    setHtmlIfChanged(countEl, statHtml);
  }

  if (visible.length === 0) {
    setHtmlIfChanged(tbody, `<tr><td colspan="9" class="action-empty">当前筛选条件下暂无场次记录</td></tr>`);
    return;
  }
  let rowsHtml = visible.map((r) => buildRoundTableRow(r, false)).join("");
  if (filtered.length > visible.length) {
    rowsHtml += `<tr><td colspan="9" class="action-empty"><button type="button" class="filter-btn" id="history-load-more-btn">显示更多（剩余 ${filtered.length - visible.length} 场）</button></td></tr>`;
  } else if (hist?.records_truncated && totalServerRounds > allRecords.length) {
    rowsHtml += `<tr><td colspan="9" class="action-empty"><button type="button" class="filter-btn" id="history-fetch-older-btn">从服务端加载更早记录（已载入 ${allRecords.length} / ${totalServerRounds} 场）</button></td></tr>`;
  }
  if (setHtmlIfChanged(tbody, rowsHtml)) {
    const moreBtn = $("history-load-more-btn");
    if (moreBtn) {
      moreBtn.addEventListener("click", () => {
        _historyRenderCap += 200;
        renderHistoryTable(cachedHistory || hist);
      });
    }
    const olderBtn = $("history-fetch-older-btn");
    if (olderBtn) {
      olderBtn.addEventListener("click", async () => {
        olderBtn.disabled = true;
        olderBtn.textContent = "正在加载更早场次...";
        const nextLim = Math.min(5000, Math.max(allRecords.length + 500, 1000));
        try {
          const r = await fetchWithTimeout(`/api/history?limit=${nextLim}`, { cache: "no-store" }, 10000);
          if (r.ok) {
            cachedHistory = await r.json();
            _historyRenderCap = nextLim;
            renderHistoryTable(cachedHistory);
          }
        } catch (_) {
          olderBtn.disabled = false;
          olderBtn.textContent = "加载失败，点击重试";
        }
      });
    }
  }
}

// ============================================================================
// VIEW 2: LIVE MARKET WORKSPACE (实时工作区)
// ============================================================================
let _prevPosUpShares = 0;
let _prevPosDnShares = 0;
let _prevPosSlug = "";
let liveFocusSide = null; // "UP" | "DOWN" | null (card <-> chart highlight sync)

function triggerCardShimmer(cardEl) {
  if (!cardEl) return;
  cardEl.classList.remove("is-shimmer");
  void cardEl.offsetWidth;
  cardEl.classList.add("is-shimmer");
  setTimeout(() => cardEl.classList.remove("is-shimmer"), 900);
}

function updateMarket(live) {
  if (!live || live.status === "idle") return;

  if (live.slug && live.slug !== liveSelectedActionSlug) {
    liveSelectedActionIdx = null;
    liveSelectedActionSlug = live.slug;
  }

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
  const apiGuard = live.api_schema_guard || {};
  const nowSec = live.server_time ? Number(live.server_time) : (Date.now() / 1000);
  const hbAge = live.now ? Math.max(0, nowSec - Number(live.now)) : 0;
  const riskPill = $("market-risk-pill");
  const riskText = $("market-risk");
  if (riskPill && riskText) {
    const regKey = live.regime || live.micro_regime || "normal_oscillation";
    const plainRegimeMap = {
      normal_oscillation: "平稳震荡",
      mean_reverting: "来回震荡",
      transitional_drift: "温和单边",
      strong_bull: "强势上涨",
      strong_bear: "强势下跌",
      strong_trend: "强势单边",
      wide_spread: "价差偏宽",
      toxic: "剧烈波动",
      toxic_shock: "剧烈波动",
      late_window: "尾盘阶段",
    };
    const regLabel = plainRegimeMap[regKey] || (REGIME_META[regKey]?.label?.split(" ")[0] || "平稳行情");
    if (hbAge > 60) {
      riskPill.className = "status-pill status-critical";
      riskText.textContent = "引擎离线";
    } else if (apiGuard.paused) {
      riskPill.className = "status-pill status-critical";
      riskText.textContent = "接口异常暂停";
    } else if (cb.active || cb.tripped) {
      riskPill.className = "status-pill status-critical";
      riskText.textContent = translateCircuitBreakerReason(cb.reason);
    } else if (hbAge > 15) {
      riskPill.className = "status-pill status-warn";
      riskText.textContent = "心跳延迟";
    } else if (live.spot_stale || live.poly_spot_stale) {
      riskPill.className = "status-pill status-warn";
      riskText.textContent = "行情滞后";
    } else if (regKey === "toxic" || regKey === "toxic_shock") {
      riskPill.className = "status-pill status-warn";
      riskText.textContent = "剧烈波动避险";
    } else {
      riskPill.className = "status-pill status-ready";
      riskText.textContent = regLabel;
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
  if ($("card-up")) $("card-up").classList.toggle("is-holding", upShares > 1e-6);

  if ($("down-pos-shares")) $("down-pos-shares").textContent = `${fmtShares(dnShares)} 股`;
  if ($("down-pos-avg")) $("down-pos-avg").textContent = dnShares > 0 ? fmtPriceCents(posDn.avg_px) : "--";
  if ($("down-pos-cost")) $("down-pos-cost").textContent = fmtUsd(dnCost);
  if ($("card-down")) $("card-down").classList.toggle("is-holding", dnShares > 1e-6);

  // Trigger subtle shimmer when new fill increases position on UP or DOWN card
  const curSlugPos = live.slug || "";
  if (curSlugPos !== _prevPosSlug) {
    _prevPosSlug = curSlugPos;
    _prevPosUpShares = upShares;
    _prevPosDnShares = dnShares;
  } else {
    if (upShares > _prevPosUpShares + 1e-6) triggerCardShimmer($("card-up"));
    if (dnShares > _prevPosDnShares + 1e-6) triggerCardShimmer($("card-down"));
    _prevPosUpShares = upShares;
    _prevPosDnShares = dnShares;
  }

  const unwindPnl = Number(live.realized_unwind_pnl || 0);
  const upWinNet = upShares * 1.0 - totalCost + unwindPnl;
  const dnWinNet = dnShares * 1.0 - totalCost + unwindPnl;

  // Live Battle Standing Sync: Highlight leading side & show live standing pill in top bar
  const upLeadingNow = openPx > 0 ? (polySpot >= openPx) : true;
  if ($("card-up")) $("card-up").classList.toggle("is-leading", openPx > 0 && upLeadingNow);
  if ($("card-down")) $("card-down").classList.toggle("is-leading", openPx > 0 && !upLeadingNow);
  if ($("up-lead-tag")) $("up-lead-tag").hidden = !(openPx > 0 && upLeadingNow);
  if ($("down-lead-tag")) $("down-lead-tag").hidden = !(openPx > 0 && !upLeadingNow);

  const renderSideForecast = (elId, sideName, netVal) => {
    const el = $(elId);
    if (!el) return;
    if (totalCost > 0 || Math.abs(unwindPnl) > 1e-6) {
      setHtmlIfChanged(
        el,
        `<strong class="bhv-pnl-num ${netVal >= 0 ? "up" : "down"}">${escapeHtml(fmtUsd(netVal, true))}</strong>`
      );
    } else {
      setHtmlIfChanged(el, `<strong class="bhv-pnl-num flat">$0.00</strong>`);
    }
  };
  renderSideForecast("pnl-forecast-up", "UP", upWinNet);
  renderSideForecast("pnl-forecast-down", "DOWN", dnWinNet);

  // Always keep probability track visible; auto-expand holding bottom-line section beneath it when holding or unwound
  const hasAnyHolding = upShares > 1e-6 || dnShares > 1e-6 || totalCost > 1e-6 || Math.abs(unwindPnl) > 1e-6;
  const holdView = $("battle-holding-view");
  const battleCard = $("copilot-battle-card");
  if (holdView) holdView.hidden = !hasAnyHolding;
  if (battleCard) battleCard.classList.toggle("is-holding-mode", hasAnyHolding);

  // Update active stage label to reflect v3.3 paced phases (早盘轻仓试探 vs 中盘控仓博弈)
  const activeStageEl = $("stage-step-active");
  const freezeStageEl = $("stage-step-freeze");
  const tauNowStage = Number(live.tau_s ?? 0);
  if (activeStageEl) {
    const hasWorking = Array.isArray(live.working_orders) && live.working_orders.length > 0;
    const isEarlyProbe = tauNowStage > 240;
    if (hasAnyHolding) {
      activeStageEl.textContent = isEarlyProbe ? "早盘轻仓" : "中盘控仓";
    } else if (hasWorking) {
      activeStageEl.textContent = "排队接货";
    } else {
      activeStageEl.textContent = isEarlyProbe ? "早盘试探" : "中盘机会";
    }
  }
  if (freezeStageEl) {
    freezeStageEl.textContent = tauNowStage > 0 && tauNowStage <= 15 ? "尾盘锁定" : "尾盘收官";
  }

  if (hasAnyHolding) {
    const headlineEl = $("battle-pos-headline");
    const subEl = $("battle-pos-sub");
    if (headlineEl && subEl) {
      if (upShares > 1e-6 && dnShares > 1e-6) {
        headlineEl.innerHTML = `<span class="bhv-pos-pill up">看涨 ${fmtShares(upShares)}股</span><span class="bhv-pos-pill down">看跌 ${fmtShares(dnShares)}股</span>`;
        subEl.innerHTML = `<span class="bhv-sub-k">总投入</span><strong class="bhv-sub-val">${escapeHtml(fmtUsd(totalCost))}</strong>`;
      } else if (upShares > 1e-6) {
        headlineEl.innerHTML = `<span class="bhv-pos-pill up">已买看涨</span><span class="bhv-pos-qty">${fmtShares(upShares)} 股</span>`;
        subEl.innerHTML = `<span class="bhv-sub-k">均价</span><strong class="bhv-sub-val">${escapeHtml(fmtPriceCents(posUp.avg_px))}</strong><span class="bhv-sub-sep">·</span><span class="bhv-sub-k">投入</span><strong class="bhv-sub-val">${escapeHtml(fmtUsd(upCost))}</strong>`;
      } else if (dnShares > 1e-6) {
        headlineEl.innerHTML = `<span class="bhv-pos-pill down">已买看跌</span><span class="bhv-pos-qty">${fmtShares(dnShares)} 股</span>`;
        subEl.innerHTML = `<span class="bhv-sub-k">均价</span><strong class="bhv-sub-val">${escapeHtml(fmtPriceCents(posDn.avg_px))}</strong><span class="bhv-sub-sep">·</span><span class="bhv-sub-k">投入</span><strong class="bhv-sub-val">${escapeHtml(fmtUsd(dnCost))}</strong>`;
      } else {
        const uwCls = unwindPnl >= 0 ? "up" : "down";
        headlineEl.innerHTML = `<span class="bhv-pos-pill ${uwCls}">已落袋清仓</span><span class="bhv-pos-qty">${escapeHtml(fmtUsd(unwindPnl, true))}</span>`;
        subEl.innerHTML = `<span class="bhv-sub-k">本轮已主动平仓锁定盈亏，等待结算</span>`;
      }
    }
  }

  const upBid = bookUp.best_bid ?? bookUp.bid ?? null;
  const upAsk = bookUp.best_ask ?? bookUp.ask ?? null;
  const upBidSz = bookUp.best_bid_size ?? null;
  const upAskSz = bookUp.best_ask_size ?? null;
  const upBidD5 = bookUp.bid_depth_5 ?? null;
  const upAskD5 = bookUp.ask_depth_5 ?? null;

  const dnBid = bookDn.best_bid ?? bookDn.bid ?? null;
  const dnAsk = bookDn.best_ask ?? bookDn.ask ?? null;
  const dnBidSz = bookDn.best_bid_size ?? null;
  const dnAskSz = bookDn.best_ask_size ?? null;
  const dnBidD5 = bookDn.bid_depth_5 ?? null;
  const dnAskD5 = bookDn.ask_depth_5 ?? null;

  const fmtCompactShares = (n) => {
    const val = Number(n);
    if (!Number.isFinite(val)) return "--";
    if (val >= 1000) return `${(val / 1000).toFixed(1)}k`;
    return fmtShares(val);
  };

  const upMid =
    upBid && upAsk ? (Number(upBid) + Number(upAsk)) / 2 : Number(live.poly_up ?? upBid ?? upAsk ?? 0.5);
  const dnMid =
    dnBid && dnAsk ? (Number(dnBid) + Number(dnAsk)) / 2 : Number(live.poly_down ?? dnBid ?? dnAsk ?? (1 - upMid));

  const renderBookMeta = (prefix, bid, ask, mid, bidD5, askD5) => {
    const depthEl = $(`${prefix}-meta-depth`);
    if (depthEl) {
      depthEl.textContent =
        bidD5 !== null && askD5 !== null
          ? `${fmtCompactShares(bidD5)} / ${fmtCompactShares(askD5)}`
          : "--";
    }
    const impEl = $(`${prefix}-meta-implied`);
    if (impEl) {
      const hasMid = Number.isFinite(mid) && mid >= 0 && mid <= 1;
      impEl.textContent = hasMid ? `${(mid * 100).toFixed(1)}%` : "--%";
    }
    const spEl = $(`${prefix}-meta-spread`);
    if (spEl) {
      spEl.textContent = bid && ask ? `$${Math.max(0, Number(ask) - Number(bid)).toFixed(2)}` : "--";
    }
  };
  renderBookMeta("up", upBid, upAsk, upMid, upBidD5, upAskD5);
  renderBookMeta("down", dnBid, dnAsk, dnMid, dnBidD5, dnAskD5);

  // Sync probability readouts for both Scan Balance bar & Holding Outcome boxes
  const upPctNum = Number.isFinite(upMid) ? Math.min(98, Math.max(2, upMid * 100)) : 50;
  const balUpEl = $("battle-balance-up");
  if (balUpEl) balUpEl.style.width = `${upPctNum.toFixed(1)}%`;
  if ($("up-prob-hold")) $("up-prob-hold").textContent = Number.isFinite(upMid) ? `${(upMid * 100).toFixed(1)}%` : "--%";
  if ($("down-prob-hold")) $("down-prob-hold").textContent = Number.isFinite(dnMid) ? `${(dnMid * 100).toFixed(1)}%` : "--%";

  // Update Scan Sniper Radar text (shows distance to trigger so flat mode feels alive & reassuring)
  const radarEl = $("battle-radar-text");
  if (radarEl && !hasAnyHolding) {
    const rawR = String(live.decision_reason || "");
    const wOrdersNow = Array.isArray(live.working_orders) ? live.working_orders : [];
    const tauNow = Number(live.tau_s ?? 0);
    if (wOrdersNow.length > 0) {
      const wo = wOrdersNow[0];
      const wSideCn = String(wo.side || "").toUpperCase() === "UP" ? "看涨" : "看跌";
      const wPx = Number(wo.price ?? wo.px ?? 0);
      const qAhead = Number(wo.queue_ahead ?? 0);
      radarEl.innerHTML = `已在 <b>$${wPx.toFixed(2)}</b> 挂单排队买 <b class="${wSideCn === "看涨" ? "up" : "down"}">${wSideCn}</b> · ${qAhead <= 1 ? "已排在队首，随时可能成交" : `前方还有 ${qAhead.toFixed(1)} 股排队`}`;
    } else if (tauNow > 280 || /Observation period/i.test(rawR)) {
      const waitLeft = Math.max(1, Math.round(tauNow - 280));
      radarEl.textContent = `开盘前 20 秒校准基准价与波动节奏 · 约 ${waitLeft} 秒后开启早盘轻仓试探`;
    } else if (tauNow > 0 && tauNow <= 15) {
      radarEl.textContent = "已进入最后 15 秒尾盘锁定阶段 · 冻结下单，准备迎接下一轮";
    } else if (tauNow > 15 && tauNow <= 30) {
      radarEl.textContent = "已进入最后 30 秒尾盘收官阶段 · 停止新开仓，静待本轮交割";
    } else if (/Anti-toxicity|Toxic|shock|极端巨震|风暴避险/i.test(rawR)) {
      radarEl.textContent = "检测到 BTC 突发剧烈波动 · 风控已自动挡在门外，等行情平稳后再评估";
    } else {
      const pm = rawR.match(/P\*\(?Up\)?=([\d.]+)/i);
      const um = rawR.match(/Up=([\d.-]+)\/([\d.-]+)/i);
      const dm = rawR.match(/Dn=([\d.-]+)\/([\d.-]+)/i);
      if (pm && (um || dm)) {
        const pUp = parseFloat(pm[1]);
        const favorUp = pUp >= 0.5;
        const sideTxt = favorUp ? "看涨" : "看跌";
        const sideCls = favorUp ? "up" : "down";
        const fairCents = Math.round((favorUp ? pUp : 1 - pUp) * 100);
        const askNum = parseFloat(favorUp ? (um ? um[2] : "") : (dm ? dm[2] : ""));
        if (Number.isFinite(askNum) && askNum > 0) {
          const askCents = Math.round(askNum * 100);
          const diffCents = fairCents - askCents;
          const gapHint = diffCents < 0
            ? `市场偏贵 <b>${Math.abs(diffCents)}%</b>，耐心等更划算价格`
            : diffCents < 5
              ? `折价仅 <b>${diffCents}%</b>，再降一点就出手`
              : `已出现 <b>${diffCents}%</b> 折价，准备择机出手`;
          radarEl.innerHTML = `当前关注 <b class="${sideCls}">${sideTxt}</b>（模型胜率 <b>${fairCents}%</b> vs 市场价 <b>${askCents}%</b>）· ${gapHint}`;
        } else {
          radarEl.textContent = translateDecisionReason(rawR);
        }
      } else {
        radarEl.textContent = translateDecisionReason(rawR);
      }
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

  const deltaPill = $("chart-delta-pill");
  if (deltaPill) {
    const diffUsd = openPx > 0 ? (polySpot - openPx) : 0;
    const diffPct = openPx > 0 ? (diffUsd / openPx) * 100 : 0;
    const isUp = diffUsd > 0.005;
    const isDn = diffUsd < -0.005;
    const arrow = isUp ? "▲" : isDn ? "▼" : "•";
    const sign = isUp ? "+" : isDn ? "-" : "";
    const pctSign = isUp ? "+" : "";
    deltaPill.textContent = `${arrow} ${sign}$${Math.abs(diffUsd).toFixed(2)} (${pctSign}${diffPct.toFixed(3)}%)`;
    deltaPill.className = `chart-delta-pill ${isUp ? "up" : isDn ? "down" : "flat"}`;
  }

  const lastPt = hist.length > 0 ? hist[hist.length - 1] : null;
  const probUpMarket = lastPt?.poly_up ?? live.poly_up ?? upMid ?? null;

  if ($("chart-stat-prob-up")) {
    const pVal = probUpMarket !== null ? Number(probUpMarket) * 100 : null;
    $("chart-stat-prob-up").textContent = pVal !== null ? `${pVal.toFixed(1)}%` : "--%";
    $("chart-stat-prob-up").className = pVal !== null ? (pVal >= 50 ? "up" : "down") : "";
  }
}

function renderWorkingOrders(orders) {
  const pill = $("working-order-pill");
  const box = $("live-working-orders-box");
  if (!pill || !box) return;

  if (!Array.isArray(orders) || orders.length === 0) {
    pill.hidden = true;
    pill.textContent = "";
    pill.className = "order-pill";
    box.hidden = true;
    setHtmlIfChanged(box, "");
    return;
  }

  pill.hidden = false;
  pill.textContent = `${orders.length} 笔排队中`;
  pill.className = "order-pill active";
  box.hidden = false;

  setHtmlIfChanged(box, orders
    .map((o) => {
      const side = String(o.side || "Up").toUpperCase();
      const sideCn = side === "UP" ? "看涨" : "看跌";
      const px = Number(o.price ?? o.px ?? 0);
      const rem = Number(o.remaining_shares ?? o.size ?? o.shares ?? 0);
      const qAhead = Number(o.queue_ahead ?? 0);
      const waitS = Number(o.wait_s ?? 0);
      const queueHint = qAhead <= 1 ? "已排到最前，随时可能成交" : `前方还有 ${qAhead.toFixed(1)} 股排队`;

      return `
        <div class="queue-live-card ${side === "UP" ? "up" : "down"}">
          <div class="qlc-top">
            <span class="qlc-badge"><span class="qlc-pulse"></span>挂单排队中</span>
            <span class="qlc-wait">已排 ${waitS.toFixed(0)}s</span>
          </div>
          <div class="qlc-main">
            <span class="qlc-side ${side === "UP" ? "up" : "down"}">${sideCn}</span>
            <strong class="qlc-spec">$${px.toFixed(2)} × ${rem.toFixed(1)} 股</strong>
          </div>
          <div class="qlc-queue">${queueHint}</div>
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
    const sec = Math.min(300, Math.max(0, Math.round(t - startTs)));
    const m = String(Math.floor(sec / 60)).padStart(2, "0");
    const s = String(sec % 60).padStart(2, "0");
    timeStr = `${m}:${s}`;
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
  const px = Number(a.price ?? a.px ?? 0);
  const sz = Number(a.size ?? a.shares ?? 0);
  const rawReason = String(a.reason || a.msg || "").trim();
  const isRealFill = (act === "fill" || act === "taker_partial") && px > 0 && sz > 0 && !/错失|拒单|拦截|reject/i.test(rawReason);

  const purpLower = String(a.purpose || "entry").toLowerCase();
  const isUnwindProfit = purpLower === "unwind" && /落袋|止盈/i.test(rawReason);
  let typeLabel = purpLower === "unwind" ? (isUnwindProfit ? "浮盈落袋" : "卖出平仓") : "撮合成交";
  let typeClass = "fill";
  if (act === "quote") {
    typeLabel = "限价挂单";
    typeClass = "quote";
  } else if (act === "cancel") {
    typeLabel = "已撤单";
    typeClass = "cancel";
  } else if (act === "taker_reject" || ((act === "fill" || act === "taker_partial") && !isRealFill)) {
    typeLabel = "暂缓拦截";
    typeClass = "reject";
  } else if (act === "taker_partial") {
    typeLabel = purpLower === "unwind" ? "部分落袋" : "部分成交";
    typeClass = "fill";
  }

  const side = String(a.side || "--").toUpperCase();
  const sideLabel = side === "UP" ? "看涨" : side === "DOWN" ? "看跌" : side;
  const sideClass = side === "UP" ? "up" : side === "DOWN" ? "down" : "";
  const specStr = px > 0 && sz > 0 ? `$${px.toFixed(2)} × ${sz.toFixed(1)}股` : "--";

  let reasonStr = "";
  if (rawReason) {
    reasonStr = translateDecisionReason(rawReason);
  } else if (isRealFill) {
    const purp = purpLower;
    if (purp === "unwind") {
      reasonStr = "主动卖出平仓成交（浮盈落袋）";
    } else {
      const kindTag = String(a.kind || "").toLowerCase() === "taker" ? "主动买入" : "挂单排队";
      const purpTag = purp === "hedge" ? "对冲锁利" : "建仓";
      reasonStr = `${kindTag}成交（${purpTag}）`;
    }
  } else if (act === "quote") {
    const ahead = Number(a.queue_ahead ?? 0);
    reasonStr = ahead <= 1 ? "已挂单排在最前，等待对手成交" : `已挂限价单，前方还有 ${ahead.toFixed(1)} 股排队`;
  } else if (act === "cancel") {
    reasonStr = "行情变化或超时，已主动撤回未成交挂单";
  } else {
    reasonStr = "交易指令记录";
  }

  return { timeStr, typeLabel, typeClass, sideLabel, sideClass, specStr, reasonStr };
}

function syncChartPinButton() {
  const pinBtn = $("chart-clear-pin-btn");
  if (pinBtn) {
    pinBtn.hidden = liveSelectedActionIdx === null;
  }
}

let _lastHudSlug = "";
let _lastHudActionCount = -1;
let _chartHudTimer = null;

function triggerChartLiveHud(item) {
  const hud = $("chart-live-hud");
  if (!hud || !item) return;
  const specTxt = item.specStr && item.specStr !== "--" ? ` ${item.specStr}` : "";
  const titleMap = {
    fill: "刚刚成交",
    quote: "已挂限价单",
    cancel: "已撤回挂单",
  };
  const label = titleMap[item.typeClass] || item.typeLabel;
  hud.className = `chart-live-hud is-${item.typeClass} is-show`;
  hud.hidden = false;
  hud.innerHTML = `
    <span class="hud-dot ${item.typeClass}"></span>
    <span class="hud-label">${escapeHtml(label)}</span>
    <span class="hud-side ${item.sideClass}">${escapeHtml(item.sideLabel)}</span>
    <strong class="hud-spec">${escapeHtml(specTxt)}</strong>
  `;
  if (_chartHudTimer) clearTimeout(_chartHudTimer);
  _chartHudTimer = setTimeout(() => {
    hud.classList.remove("is-show");
    hud.classList.add("is-hiding");
    setTimeout(() => {
      if (hud.classList.contains("is-hiding")) {
        hud.hidden = true;
        hud.classList.remove("is-hiding");
      }
    }, 320);
  }, 4200);
}

function renderActionFeed(actions, startTs = 0) {
  const list = $("live-action-feed");
  const countEl = $("live-action-count");
  if (!list) return;

  const arr = Array.isArray(actions) ? actions : [];
  if (countEl) countEl.textContent = `${arr.length} 笔`;
  syncChartPinButton();

  // Detect brand-new live action in current round and pop brief Dynamic-Island HUD on chart
  const curSlug = cachedLive?.slug || "";
  if (curSlug !== _lastHudSlug) {
    _lastHudSlug = curSlug;
    _lastHudActionCount = arr.length;
  } else if (arr.length > _lastHudActionCount && _lastHudActionCount >= 0) {
    for (let i = arr.length - 1; i >= _lastHudActionCount; i--) {
      const it = formatActionItem(arr[i], startTs);
      if (it.typeClass === "fill" || it.typeClass === "quote" || it.typeClass === "cancel") {
        triggerChartLiveHud(it);
        break;
      }
    }
    _lastHudActionCount = arr.length;
  }

  if (arr.length === 0) {
    setHtmlIfChanged(list, `
      <li class="action-empty console-empty">
        <span class="ce-title">本轮暂未出手</span>
        <span class="ce-sub">策略正在持续比对胜率与盘口报价，发现划算价格会自动下单</span>
      </li>`);
    return;
  }

  const indexed = arr.map((raw, idx) => ({ raw, origIdx: idx })).reverse();
  const filtered = indexed.filter(({ raw }) => {
    if (liveActionFilterMode === "all") return true;
    const act = String(raw.action || raw.kind || "fill").toLowerCase();
    const px = Number(raw.price ?? raw.px ?? 0);
    const sz = Number(raw.size ?? raw.shares ?? 0);
    const isRealFill = (act === "fill" || act === "taker_partial") && px > 0 && sz > 0 && !/错失|拒单|拦截|reject/i.test(String(raw.reason || ""));
    if (liveActionFilterMode === "fill") return isRealFill;
    if (liveActionFilterMode === "key") return isRealFill || act === "quote" || act === "cancel";
    return !isRealFill;
  });

  if (filtered.length === 0) {
    const emptyTitle = liveActionFilterMode === "key" ? "本轮暂无成交或挂撤" : "当前筛选下暂无记录";
    setHtmlIfChanged(list, `
      <li class="action-empty console-empty">
        <span class="ce-title">${emptyTitle}</span>
        <span class="ce-sub">策略正在持续比对胜率与盘口报价，发现划算价格会自动下单</span>
      </li>`);
    return;
  }

  setHtmlIfChanged(list, filtered
    .slice(0, 40)
    .map(({ raw, origIdx }) => {
      const item = formatActionItem(raw, startTs);
      const isSel = liveSelectedActionIdx === origIdx;
      const isFill = item.typeClass === "fill";
      const isSecondary = !isFill;
      const px = Number(raw.price ?? raw.px ?? 0);
      const sz = Number(raw.size ?? raw.shares ?? 0);
      const notional = px > 0 && sz > 0 ? px * sz : 0;
      const rawAct = String(raw.action || raw.kind || "").toLowerCase();
      const tagCls = isFill
        ? "fill"
        : item.typeClass === "cancel"
          ? "cancel"
          : (rawAct === "wait" || rawAct === "skip" || rawAct === "block" || /暂缓|拦截|观望/.test(item.typeLabel || ""))
            ? "wait"
            : "quote";

      const hasSpec = px > 0 && sz > 0;
      const itemCls = isFill ? "is-fill-hero" : `is-secondary is-${tagCls}`;
      const sideArrow = item.sideClass === "up" ? "▲ " : item.sideClass === "down" ? "▼ " : "";
      const pxLabel = isFill ? "均价" : "报价";
      const metricsHtml = hasSpec
        ? `
          <div class="ex-kpi-strip ${isFill ? "is-fill" : ""}">
            <div class="ex-kpi">
              <span class="ex-kpi-k">${pxLabel}</span>
              <strong class="ex-kpi-v">$${px.toFixed(2)}</strong>
            </div>
            <span class="ex-kpi-sep" aria-hidden="true"></span>
            <div class="ex-kpi">
              <span class="ex-kpi-k">份数</span>
              <strong class="ex-kpi-v">${sz.toFixed(1)} 股</strong>
            </div>
            ${isFill && notional > 0 ? `
            <span class="ex-kpi-sep" aria-hidden="true"></span>
            <div class="ex-kpi is-cost">
              <span class="ex-kpi-k">投入</span>
              <strong class="ex-kpi-v">${fmtUsd(notional)}</strong>
            </div>` : ""}
          </div>
        `
        : "";
      return `
        <li class="action-item tl-item clickable ${itemCls} ${isSel ? "selected" : ""}" data-action-idx="${origIdx}" title="点击在左图定位该节点">
          <div class="ex-card-top">
            <div class="ex-badges">
              ${item.sideLabel && item.sideLabel !== "--" ? `<span class="ex-side ${item.sideClass}">${sideArrow}${escapeHtml(item.sideLabel)}</span>` : ""}
              <span class="ex-tag ${tagCls}">${escapeHtml(item.typeLabel)}</span>
            </div>
            <span class="ex-time-stamp">${escapeHtml(item.timeStr)}</span>
          </div>
          ${metricsHtml}
          <div class="ex-reason">${escapeHtml(item.reasonStr)}</div>
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
  return { elapsed, spot, poly_spot: polySpot, up_mid: upMid, dn_mid: dnMid };
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
  const w = Math.max(280, rect.width || 800);
  const isMobile = w < 520;
  const minH = isMobile ? (chartViewMode === "dual" ? 368 : 230) : 360;
  const h = Math.max(minH, rect.height || (isMobile ? 368 : 500));

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
    ctx.fillText("正在采集本轮秒级行情与盘口轨迹...", w / 2, h / 2);
    return;
  }

  // Mobile reclaims ~46px horizontal plot width by showing delta-USD on left and hiding redundant 6-digit price axis
  const padL = isMobile ? 48 : 68;
  const padR = isMobile ? 14 : 56;
  const padT = 24;
  const padB = isMobile ? 22 : 26;
  const plotW = w - padL - padR;
  const totalPlotH = h - padT - padB;

  let priceTop = padT;
  let priceH = totalPlotH;
  let probTop = padT;
  let probH = 0;

  if (chartViewMode === "dual") {
    const gapBetween = isMobile ? 38 : 40;
    priceH = Math.round(totalPlotH * (isMobile ? 0.50 : 0.52));
    probTop = padT + priceH + gapBetween;
    probH = totalPlotH - priceH - gapBetween;
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
  ctx.font = "500 12px 'JetBrains Mono', monospace";
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
    ctx.fillText(`0${mins}:00`, x, h - padB + 9);
  }

  // -------------------- UPPER PANE: BASELINE PRICE TRAJECTORY --------------------
  if (priceH > 0) {
    ctx.font = isMobile ? "500 10.5px 'JetBrains Mono', monospace" : "500 11.5px 'JetBrains Mono', monospace";
    const pSteps = 4;
    const spanPx = Math.abs(maxPx - minPx);
    for (let i = 0; i <= pSteps; i++) {
      const val = minPx + ((maxPx - minPx) * i) / pSteps;
      const y = yOfPrice(val);
      ctx.strokeStyle = pal.grid;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();

      const diff = openPx > 0 ? (val - openPx) : 0;
      const absD = Math.abs(diff);
      const sign = diff > 0.05 ? "+" : diff < -0.05 ? "-" : "";
      const fmtDiff = spanPx < 25 ? absD.toFixed(1) : Math.round(absD).toString();
      const diffLabel = sign ? `${sign}$${fmtDiff}` : "$0";
      const diffColor = diff > 0.05 ? pal.up : diff < -0.05 ? pal.down : pal.axis;

      ctx.textBaseline = "middle";
      if (isMobile) {
        // Mobile: clean delta-USD on left, maximizing chart width
        ctx.fillStyle = diffColor;
        ctx.textAlign = "right";
        ctx.fillText(diffLabel, padL - 6, y);
      } else {
        ctx.fillStyle = pal.axis;
        ctx.textAlign = "right";
        ctx.fillText(`$${val.toFixed(1)}`, padL - 9, y);

        if (openPx > 0) {
          ctx.fillStyle = diffColor;
          ctx.textAlign = "left";
          ctx.fillText(diffLabel, w - padR + 8, y);
        }
      }
    }

    const openY = Math.max(priceTop, Math.min(priceTop + priceH, yOfPrice(openPx)));
    if (openPx >= minPx && openPx <= maxPx) {
      ctx.save();
      ctx.strokeStyle = pal.isDark ? "rgba(148, 163, 184, 0.45)" : "rgba(100, 116, 139, 0.40)";
      ctx.lineWidth = 1.15;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, openY);
      ctx.lineTo(w - padR, openY);
      ctx.stroke();
      ctx.restore();
    }

    // Smooth Quadratic Bezier Baseline Chart (eliminates jagged stair-steps)
    const tracePricePath = () => {
      ctx.beginPath();
      if (pts.length === 1) {
        ctx.moveTo(xOfSec(pts[0].elapsed), yOfPrice(pts[0].poly_spot));
        return;
      }
      ctx.moveTo(xOfSec(pts[0].elapsed), yOfPrice(pts[0].poly_spot));
      for (let i = 1; i < pts.length - 1; i++) {
        const x1 = xOfSec(pts[i].elapsed);
        const y1 = yOfPrice(pts[i].poly_spot);
        const x2 = xOfSec(pts[i + 1].elapsed);
        const y2 = yOfPrice(pts[i + 1].poly_spot);
        ctx.quadraticCurveTo(x1, y1, (x1 + x2) * 0.5, (y1 + y2) * 0.5);
      }
      const lastP = pts[pts.length - 1];
      ctx.lineTo(xOfSec(lastP.elapsed), yOfPrice(lastP.poly_spot));
    };
    const firstX = xOfSec(pts[0].elapsed);
    const lastPtPx = pts[pts.length - 1];
    const lastX = xOfSec(lastPtPx.elapsed);
    const lastY = yOfPrice(lastPtPx.poly_spot);

    // Unified clean finance-blue BTC price trajectory (eliminates red/green visual fatigue)
    const priceLineColor = pal.isDark ? "#60a5fa" : "#2563eb";
    const priceAreaGrad = ctx.createLinearGradient(0, priceTop, 0, priceTop + priceH);
    priceAreaGrad.addColorStop(0, pal.isDark ? "rgba(96, 165, 250, 0.20)" : "rgba(37, 99, 235, 0.12)");
    priceAreaGrad.addColorStop(1, "rgba(37, 99, 235, 0.00)");
    ctx.save();
    tracePricePath();
    ctx.lineTo(lastX, priceTop + priceH);
    ctx.lineTo(firstX, priceTop + priceH);
    ctx.closePath();
    ctx.fillStyle = priceAreaGrad;
    ctx.fill();
    tracePricePath();
    ctx.strokeStyle = priceLineColor;
    ctx.lineWidth = 2.0;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.restore();

    // TradingView-style subtle horizontal guide to right axis + clean axis tag (no bulky pill on curve tip)
    const curDiff = openPx > 0 ? (lastPtPx.poly_spot - openPx) : 0;
    const tipColor = priceLineColor;
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(96, 165, 250, 0.36)" : "rgba(37, 99, 235, 0.28)";
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(lastX + 4, lastY);
    ctx.lineTo(w - padR, lastY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Crisp minimalist terminal dot
    ctx.beginPath();
    ctx.arc(lastX, lastY, 5.5, 0, Math.PI * 2);
    ctx.fillStyle = pal.isDark ? "rgba(96, 165, 250, 0.22)" : "rgba(37, 99, 235, 0.16)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(lastX, lastY, 2.8, 0, Math.PI * 2);
    ctx.fillStyle = tipColor;
    ctx.fill();

    // Right-axis price tag on desktop; clean unobtrusive inline text on mobile
    const tipSign = curDiff > 0.005 ? "+" : curDiff < -0.005 ? "-" : "";
    const tipTxt = `${tipSign}$${Math.abs(curDiff).toFixed(1)}`;
    ctx.font = isMobile ? "700 10px 'JetBrains Mono', monospace" : "700 10.5px 'JetBrains Mono', monospace";
    if (!isMobile) {
      const tw = ctx.measureText(tipTxt).width;
      const bw = Math.min(padR - 4, tw + 10);
      const bh = 18;
      const bx = w - padR + 3;
      const by = Math.max(priceTop, Math.min(priceTop + priceH - bh, lastY - bh / 2));
      ctx.fillStyle = tipColor;
      ctx.beginPath();
      ctx.roundRect(bx, by, bw, bh, 4);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(tipTxt, bx + bw / 2, by + bh / 2 + 0.5);
    } else {
      ctx.fillStyle = tipColor;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      const lblX = Math.min(w - padR - 44, lastX + 8);
      ctx.fillText(tipTxt, lblX, lastY);
    }
    ctx.restore();

    // Subtle pane label
    ctx.fillStyle = pal.axis;
    ctx.font = isMobile ? "600 10.5px Inter, sans-serif" : "600 11px Inter, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText("BTC 相对开盘涨跌 (美元)", padL + 4, priceTop - 7);
  }

  // -------------------- LOWER PANE: IMPLIED PROBABILITY --------------------
  if (probH > 0) {
    if (chartViewMode === "dual") {
      ctx.strokeStyle = pal.divider;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, probTop - 21);
      ctx.lineTo(w - padR, probTop - 21);
      ctx.stroke();
    }

    // Sub-pane header label
    ctx.fillStyle = pal.axis;
    ctx.font = "600 11px Inter, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText("UP 市场隐含概率 (%)", padL + 4, probTop - 5);
    ctx.font = "500 12px 'JetBrains Mono', monospace";

    [0.0, 0.25, 0.5, 0.75, 1.0].forEach((prob) => {
      const y = yOfProb(prob);
      ctx.save();
      if (prob === 0.5) {
        ctx.strokeStyle = pal.isDark ? "rgba(148, 163, 184, 0.45)" : "rgba(100, 116, 139, 0.35)";
        ctx.lineWidth = 1.2;
        ctx.setLineDash([4, 4]);
      } else {
        ctx.strokeStyle = pal.gridSoft;
        ctx.lineWidth = 1;
      }
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();
      ctx.restore();

      const lbl = `${Math.round(prob * 100)}%`;
      ctx.fillStyle = prob === 0.5 ? pal.label : pal.axis;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(lbl, padL - (isMobile ? 6 : 9), y);

      if (!isMobile) {
        ctx.textAlign = "left";
        ctx.fillText(lbl, w - padR + 8, y);
      }
    });

    const probPts = pts.filter((p) => p.up_mid !== null);
    if (probPts.length > 0) {
      const midY = yOfProb(0.5);
      // Smooth quadratic curve for probability pane as well
      const traceProbPath = () => {
        ctx.beginPath();
        if (probPts.length === 1) {
          ctx.moveTo(xOfSec(probPts[0].elapsed), yOfProb(probPts[0].up_mid));
          return;
        }
        ctx.moveTo(xOfSec(probPts[0].elapsed), yOfProb(probPts[0].up_mid));
        for (let i = 1; i < probPts.length - 1; i++) {
          const x1 = xOfSec(probPts[i].elapsed);
          const y1 = yOfProb(probPts[i].up_mid);
          const x2 = xOfSec(probPts[i + 1].elapsed);
          const y2 = yOfProb(probPts[i + 1].up_mid);
          ctx.quadraticCurveTo(x1, y1, (x1 + x2) * 0.5, (y1 + y2) * 0.5);
        }
        const lp = probPts[probPts.length - 1];
        ctx.lineTo(xOfSec(lp.elapsed), yOfProb(lp.up_mid));
      };
      const pFirstX = xOfSec(probPts[0].elapsed);
      const lastP = probPts[probPts.length - 1];
      const lx = xOfSec(lastP.elapsed);
      const ly = yOfProb(lastP.up_mid);

      // Unified calm teal curve for implied probability (distinct from price blue & PnL red/green)
      const probLineColor = pal.isDark ? "#2dd4bf" : "#0d9488";
      const probAreaGrad = ctx.createLinearGradient(0, probTop, 0, probTop + probH);
      probAreaGrad.addColorStop(0, pal.isDark ? "rgba(45, 212, 191, 0.18)" : "rgba(13, 148, 136, 0.13)");
      probAreaGrad.addColorStop(1, "rgba(13, 148, 136, 0.00)");
      ctx.save();
      traceProbPath();
      ctx.lineTo(lx, probTop + probH);
      ctx.lineTo(pFirstX, probTop + probH);
      ctx.closePath();
      ctx.fillStyle = probAreaGrad;
      ctx.fill();
      traceProbPath();
      ctx.strokeStyle = probLineColor;
      ctx.lineWidth = 2.0;
      ctx.lineJoin = "round";
      ctx.stroke();
      ctx.restore();

      // Overlay Live Holding Cost Lines & Working Order Queue Lines on Probability Pane
      const drawProbLevelLine = (probPos, label, color, isDashed, emphasized) => {
        if (!Number.isFinite(probPos) || probPos <= 0.01 || probPos >= 0.99) return;
        const lyLine = yOfProb(probPos);
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = emphasized ? 2.0 : 1.25;
        ctx.globalAlpha = emphasized ? 0.95 : 0.72;
        if (isDashed) ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(padL, lyLine);
        ctx.lineTo(w - padR, lyLine);
        ctx.stroke();
        ctx.setLineDash([]);

        // Clean translucent glass badge anchored on the left side of the line (never obscures curve)
        ctx.font = isMobile ? "600 9.5px 'JetBrains Mono', monospace" : "600 10px 'JetBrains Mono', monospace";
        const tw = ctx.measureText(label).width;
        const bw = tw + 10;
        const bh = 16;
        const bx = padL + 6;
        const by = Math.max(probTop + 2, Math.min(probTop + probH - bh - 2, lyLine - bh - 3));
        ctx.globalAlpha = emphasized ? 0.96 : 0.88;
        ctx.fillStyle = pal.isDark ? "rgba(15, 23, 42, 0.82)" : "rgba(255, 255, 255, 0.88)";
        ctx.beginPath();
        ctx.roundRect(bx, by, bw, bh, 4);
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(label, bx + 5, by + bh / 2 + 0.5);
        ctx.restore();
      };

      const pUpPos = live.pos_up || {};
      const pDnPos = live.pos_down || {};
      if (Number(pUpPos.shares || 0) > 1e-6 && Number(pUpPos.avg_px || 0) > 0) {
        const avgU = Number(pUpPos.avg_px);
        drawProbLevelLine(avgU, `成本 $${avgU.toFixed(2)}`, pal.up, true, liveFocusSide === "UP");
      }
      if (Number(pDnPos.shares || 0) > 1e-6 && Number(pDnPos.avg_px || 0) > 0) {
        const avgD = Number(pDnPos.avg_px);
        drawProbLevelLine(1 - avgD, `成本 $${avgD.toFixed(2)}`, pal.down, true, liveFocusSide === "DOWN");
      }
      const wOrders = Array.isArray(live.working_orders) ? live.working_orders : [];
      wOrders.forEach((wo) => {
        const wSide = String(wo.side || "").toUpperCase();
        const wPx = Number(wo.price ?? wo.px ?? 0);
        if (wPx <= 0 || wPx >= 1) return;
        const yProbPos = wSide === "UP" ? wPx : (1 - wPx);
        const wLbl = `挂单 $${wPx.toFixed(2)}`;
        drawProbLevelLine(yProbPos, wLbl, "#2563eb", true, liveFocusSide === wSide);
      });

      // Endpoint dot + horizontal guide to right axis (matches calm teal probability curve)
      const probColor = pal.isDark ? "#2dd4bf" : "#0d9488";
      ctx.save();
      ctx.strokeStyle = pal.isDark ? "rgba(45, 212, 191, 0.30)" : "rgba(13, 148, 136, 0.25)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(lx + 4, ly);
      ctx.lineTo(w - padR, ly);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.beginPath();
      ctx.arc(lx, ly, 2.8, 0, Math.PI * 2);
      ctx.fillStyle = probColor;
      ctx.fill();

      const tagText = `${(lastP.up_mid * 100).toFixed(1)}%`;
      ctx.font = isMobile ? "700 10px 'JetBrains Mono', monospace" : "700 10.5px 'JetBrains Mono', monospace";
      if (!isMobile) {
        const tw = ctx.measureText(tagText).width;
        const bw = Math.min(padR - 4, tw + 10);
        const bh = 18;
        const bx = w - padR + 3;
        const by = Math.max(probTop, Math.min(probTop + probH - bh, ly - bh / 2));
        ctx.fillStyle = probColor;
        ctx.beginPath();
        ctx.roundRect(bx, by, bw, bh, 4);
        ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(tagText, bx + bw / 2, by + bh / 2 + 0.5);
      } else {
        ctx.fillStyle = probColor;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        const labelX = Math.min(w - padR - 40, lx + 7);
        const labelY = Math.max(probTop + 8, Math.min(probTop + probH - 8, ly));
        ctx.fillText(tagText, labelX, labelY);
      }
      ctx.restore();
    }
  }

  // -------------------- FILL MARKERS & CLICKED ACTION CROSSHAIR --------------------
  const actions = Array.isArray(live.actions) ? live.actions : [];
  actions.forEach((a, idx) => {
    const actType = String(a.action || "").toLowerCase();
    const px = Number(a.price ?? a.px ?? 0);
    const sz = Number(a.size ?? a.shares ?? 0);
    const isRealFill = (actType === "fill" || actType === "taker_partial") && px > 0 && sz > 0 && !/错失|拒单|拦截|reject/i.test(String(a.reason || ""));
    const isSel = liveSelectedActionIdx === idx;
    if (!isRealFill && !isSel) return;

    const actT = Number(a.t ?? a.ts ?? a.timestamp ?? startTs);
    const elSec = Math.min(300, Math.max(0, startTs > 0 ? actT - startTs : actT));
    const ax = xOfSec(elSec);
    let nearest = pts[0];
    let minDt = Infinity;
    pts.forEach((pt) => {
      const dt = Math.abs(pt.elapsed - elSec);
      if (dt < minDt) {
        minDt = dt;
        nearest = pt;
      }
    });

    const sideUpper = String(a.side || "").toUpperCase();
    const sideCn = sideUpper === "UP" ? "看涨" : sideUpper === "DOWN" ? "看跌" : sideUpper;
    const actColor =
      actType === "cancel"
        ? "#d97706"
        : actType === "quote"
        ? "#2563eb"
        : sideUpper === "DOWN"
        ? pal.down
        : sideUpper === "UP"
        ? pal.up
        : pal.spot;

    if (!isSel) {
      if (probH > 0 && nearest && nearest.up_mid !== null) {
        const py = yOfProb(nearest.up_mid);
        ctx.beginPath();
        ctx.arc(ax, py, 3.8, 0, Math.PI * 2);
        ctx.fillStyle = actColor;
        ctx.fill();
        ctx.strokeStyle = pal.cardBg;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      return;
    }

    // Selected action (Fill / Quote / Cancel): full vertical + horizontal extension lines across both panes
    ctx.save();
    ctx.strokeStyle = actColor;
    ctx.lineWidth = 1.4;
    ctx.setLineDash([5, 4]);

    // Vertical extension line spanning the entire canvas height
    ctx.beginPath();
    ctx.moveTo(ax, padT);
    ctx.lineTo(ax, h - padB);
    ctx.stroke();

    // Upper price pane intersection dot
    if (priceH > 0 && nearest && nearest.poly_spot > 0) {
      const sy = yOfPrice(nearest.poly_spot);
      ctx.save();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(ax, sy, 4.2, 0, Math.PI * 2);
      ctx.fillStyle = actColor;
      ctx.fill();
      ctx.strokeStyle = pal.cardBg;
      ctx.lineWidth = 1.6;
      ctx.stroke();
      ctx.restore();
    }

    // Horizontal extension line across probability pane (or price pane when in price-only mode)
    const hasProbTarget = probH > 0 && nearest && nearest.up_mid !== null;
    if (hasProbTarget) {
      const py = yOfProb(nearest.up_mid);
      ctx.beginPath();
      ctx.moveTo(padL, py);
      ctx.lineTo(w - padR, py);
      ctx.stroke();

      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(ax, py, 7.5, 0, Math.PI * 2);
      ctx.fillStyle = actType === "cancel"
        ? "rgba(245, 158, 11, 0.20)"
        : actType === "quote"
        ? "rgba(37, 99, 235, 0.18)"
        : sideUpper === "DOWN" ? pal.downSoft : pal.upSoft;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(ax, py, 4.4, 0, Math.PI * 2);
      ctx.fillStyle = actColor;
      ctx.fill();
      ctx.strokeStyle = pal.cardBg;
      ctx.lineWidth = 1.8;
      ctx.stroke();

      // Callout badge beside the probability intersection
      const pxVal = Number(a.price ?? a.px ?? 0);
      const szVal = Number(a.size ?? a.shares ?? 0);
      const actLabel = isRealFill ? "成交" : actType === "cancel" ? "撤单" : actType === "quote" ? "挂单" : "记录";
      const badgeTxt =
        pxVal > 0
          ? `${actLabel} ${sideCn} $${pxVal.toFixed(2)}${szVal > 0 ? " × " + szVal.toFixed(0) : ""} · ${(nearest.up_mid * 100).toFixed(1)}%`
          : `${actLabel} ${sideCn} · ${(nearest.up_mid * 100).toFixed(1)}%`;
      ctx.font = "700 11px 'JetBrains Mono', monospace";
      const bw = ctx.measureText(badgeTxt).width + 14;
      const bx = ax + 10 + bw > w - padR ? Math.max(padL + 4, ax - bw - 10) : ax + 10;
      const by = py - 24 < probTop + 2 ? Math.min(probTop + probH - 22, py + 8) : py - 24;
      ctx.fillStyle = pal.cardBg;
      ctx.strokeStyle = actColor;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.roundRect(bx, by, bw, 20, 5);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = actColor;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(badgeTxt, bx + 7, by + 10);
    }

    // Also mirror spot intersection on upper price pane (and horizontal line when in price-only mode)
    if (priceH > 0 && nearest) {
      const hyPrice = yOfPrice(nearest.poly_spot);
      if (probH === 0) {
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(padL, hyPrice);
        ctx.lineTo(w - padR, hyPrice);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(ax, hyPrice, 4.2, 0, Math.PI * 2);
      ctx.fillStyle = actColor;
      ctx.fill();
      ctx.strokeStyle = pal.cardBg;
      ctx.lineWidth = 1.8;
      ctx.stroke();
    }

    // Highlight exact elapsed time on X-axis
    const mins = Math.floor(elSec / 60);
    const secs = Math.floor(elSec % 60);
    const tLabel = `0${mins}:${String(secs).padStart(2, "0")}`;
    ctx.font = "700 11px 'JetBrains Mono', monospace";
    const tw = ctx.measureText(tLabel).width + 10;
    const tx = Math.max(padL, Math.min(w - padR - tw, ax - tw / 2));
    ctx.fillStyle = actColor;
    ctx.beginPath();
    ctx.roundRect(tx, h - padB + 4, tw, 18, 4);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(tLabel, tx + tw / 2, h - padB + 13);
    ctx.restore();
  });

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
    if (probH > 0 && hp.up_mid !== null) {
      const hpy = yOfProb(hp.up_mid);
      ctx.beginPath();
      ctx.arc(hx, hpy, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = pal.up;
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

  const syncScrubPreview = (pt) => {
    const upCard = $("card-up");
    const dnCard = $("card-down");
    if (!pt) {
      if (upCard) upCard.classList.remove("is-scrubbing");
      if (dnCard) dnCard.classList.remove("is-scrubbing");
      if (cachedLive) updateMarket(cachedLive);
      return;
    }
    const openPx = liveChartGeom?.openPx || pt.poly_spot;
    const diffUsd = openPx > 0 ? (pt.poly_spot - openPx) : 0;
    const diffPct = openPx > 0 ? (diffUsd / openPx) * 100 : 0;
    const isUp = diffUsd > 0.005;
    const isDn = diffUsd < -0.005;
    if ($("chart-stat-poly-spot")) $("chart-stat-poly-spot").textContent = fmtUsd(pt.poly_spot);
    const deltaPill = $("chart-delta-pill");
    if (deltaPill) {
      const arrow = isUp ? "▲" : isDn ? "▼" : "•";
      const sign = isUp ? "+" : isDn ? "-" : "";
      const pctSign = isUp ? "+" : "";
      deltaPill.textContent = `${arrow} ${sign}$${Math.abs(diffUsd).toFixed(2)} (${pctSign}${diffPct.toFixed(3)}%)`;
      deltaPill.className = `chart-delta-pill ${isUp ? "up" : isDn ? "down" : "flat"}`;
    }
    if (pt.up_mid !== null && Number.isFinite(pt.up_mid)) {
      const uPct = `${(pt.up_mid * 100).toFixed(1)}%`;
      const dPct = `${((1 - pt.up_mid) * 100).toFixed(1)}%`;
      if ($("up-meta-implied")) $("up-meta-implied").textContent = uPct;
      if ($("down-meta-implied")) $("down-meta-implied").textContent = dPct;
      if ($("up-prob-hold")) $("up-prob-hold").textContent = uPct;
      if ($("down-prob-hold")) $("down-prob-hold").textContent = dPct;
      const balEl = $("battle-balance-up");
      if (balEl) balEl.style.width = `${Math.min(98, Math.max(2, pt.up_mid * 100)).toFixed(1)}%`;
      if ($("chart-stat-prob-up")) {
        $("chart-stat-prob-up").textContent = uPct;
        $("chart-stat-prob-up").className = pt.up_mid >= 0.5 ? "up" : "down";
      }
    }
    if (upCard) upCard.classList.add("is-scrubbing");
    if (dnCard) dnCard.classList.add("is-scrubbing");
  };

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

    if (bestDist > 24) {
      if (liveChartHoverIdx !== null) {
        liveChartHoverIdx = null;
        drawLivePriceChart(cachedLive);
        syncScrubPreview(null);
      }
      tooltip.hidden = true;
      return;
    }

    liveChartHoverIdx = bestIdx;
    drawLivePriceChart(cachedLive);

    const pt = liveChartGeom.pts[bestIdx];
    syncScrubPreview(pt);

    const openPx = liveChartGeom.openPx || pt.poly_spot;
    const diffUsd = openPx > 0 ? (pt.poly_spot - openPx) : 0;
    const sign = diffUsd > 0.005 ? "+" : diffUsd < -0.005 ? "-" : "";
    const mm = String(Math.floor(pt.elapsed / 60)).padStart(2, "0");
    const ss = String(Math.round(pt.elapsed % 60)).padStart(2, "0");

    tooltip.innerHTML = `
      <div class="tt-time" style="display:flex;align-items:center;justify-content:space-between;gap:10px">
        <span>${mm}:${ss} 切片</span>
        <span class="${diffUsd >= 0 ? "up" : "down"}">${sign}$${Math.abs(diffUsd).toFixed(2)}</span>
      </div>
      <div class="tt-row"><span>BTC 实时价格</span><b>${fmtUsd(pt.poly_spot)}</b></div>
      <div class="tt-row"><span>看涨 / 看跌概率</span><b><span class="up">${pt.up_mid !== null ? (pt.up_mid * 100).toFixed(1) + "%" : "--"}</span> / <span class="down">${pt.up_mid !== null ? ((1 - pt.up_mid) * 100).toFixed(1) + "%" : "--"}</span></b></div>
    `;
    tooltip.style.left = "0px";
    tooltip.hidden = false;

    const hx = liveChartGeom.xOfSec(pt.elapsed);
    const ttW = Math.max(210, tooltip.offsetWidth || 245);
    const rightLimit = rect.width - (liveChartGeom.padR || 52) - 8;
    const rawLeft = hx + 16 + ttW > rightLimit ? hx - ttW - 16 : hx + 16;
    const leftPos = Math.min(rect.width - ttW - 12, Math.max(12, rawLeft));
    tooltip.style.left = `${leftPos}px`;
    tooltip.style.top = `16px`;
  });

  canvas.addEventListener("mouseleave", () => {
    liveChartHoverIdx = null;
    tooltip.hidden = true;
    syncScrubPreview(null);
    if (cachedLive) drawLivePriceChart(cachedLive);
  });

  canvas.addEventListener("touchstart", (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: t.clientX, clientY: t.clientY }));
  }, { passive: true });

  canvas.addEventListener("touchmove", (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: t.clientX, clientY: t.clientY }));
  }, { passive: true });

  canvas.addEventListener("touchend", () => {
    liveChartHoverIdx = null;
    tooltip.hidden = true;
    syncScrubPreview(null);
    if (cachedLive) drawLivePriceChart(cachedLive);
  }, { passive: true });

  // Bidirectional Card -> Chart Focus Sync (Hover or Tap UP/DOWN card highlights its cost/order lines)
  document.querySelectorAll("[data-focus-side]").forEach((card) => {
    const side = card.getAttribute("data-focus-side");
    card.addEventListener("mouseenter", () => {
      liveFocusSide = side;
      card.classList.add("is-focused");
      if (cachedLive) drawLivePriceChart(cachedLive);
    });
    card.addEventListener("mouseleave", () => {
      if (liveFocusSide === side) liveFocusSide = null;
      card.classList.remove("is-focused");
      if (cachedLive) drawLivePriceChart(cachedLive);
    });
    card.addEventListener("click", () => {
      const next = liveFocusSide === side ? null : side;
      liveFocusSide = next;
      document.querySelectorAll("[data-focus-side]").forEach((c) => {
        c.classList.toggle("is-focused", c.getAttribute("data-focus-side") === next);
      });
      if (cachedLive) drawLivePriceChart(cachedLive);
    });
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

  const clearPinSelection = () => {
    if (liveSelectedActionIdx === null) return;
    liveSelectedActionIdx = null;
    syncChartPinButton();
    renderActionFeed(cachedLive?.actions || [], cachedLive?.start || 0);
    if (cachedLive) drawLivePriceChart(cachedLive);
  };

  const clearPinBtn = $("chart-clear-pin-btn");
  if (clearPinBtn) {
    clearPinBtn.addEventListener("click", clearPinSelection);
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && currentPage === "market" && liveSelectedActionIdx !== null) {
      clearPinSelection();
    }
  });

  const actionFilter = $("action-feed-filter");
  if (actionFilter) {
    actionFilter.addEventListener("click", (e) => {
      const btn = e.target.closest(".af-btn[data-af]");
      if (!btn) return;
      liveActionFilterMode = btn.dataset.af || "key";
      actionFilter.querySelectorAll(".af-btn").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      const isThoughts = liveActionFilterMode === "thoughts";
      const feedEl = $("live-action-feed");
      const thoughtsEl = $("inline-thoughts-feed");
      const hintEl = $("console-feed-hint");
      if (feedEl) feedEl.hidden = isThoughts;
      if (thoughtsEl) thoughtsEl.hidden = !isThoughts;
      if (hintEl) hintEl.textContent = isThoughts ? "本轮实时思路" : "点击定位左图";
      if (!isThoughts) {
        renderActionFeed(cachedLive?.actions || [], cachedLive?.start || 0);
      } else if (cachedLive) {
        renderDecisionBulletin(cachedLive);
      }
    });
  }

  const actionFeed = $("live-action-feed");
  if (actionFeed) {
    actionFeed.addEventListener("click", (e) => {
      const row = e.target.closest("li.action-item[data-action-idx]");
      if (!row) return;
      const idx = Number(row.dataset.actionIdx);
      if (!Number.isFinite(idx)) return;
      liveSelectedActionIdx = liveSelectedActionIdx === idx ? null : idx;
      liveSelectedActionSlug = cachedLive?.slug || null;
      renderActionFeed(cachedLive?.actions || [], cachedLive?.start || 0);
      if (cachedLive) drawLivePriceChart(cachedLive);
    });
  }

  const canvasBox = canvas.parentElement;
  if (canvasBox && typeof ResizeObserver !== "undefined") {
    let resizeRaf = null;
    const ro = new ResizeObserver(() => {
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(() => {
        if (cachedLive && currentPage === "market") drawLivePriceChart(cachedLive);
      });
    });
    ro.observe(canvasBox);
  }
}

// ============================================================================
// 5-MINUTE ROUND REVIEW MODAL (CLICK ANY SETTLED ROUND OR EQUITY POINT)
// ============================================================================
const REGIME_LABELS_ZH = {
  range_bound: "窄幅震荡",
  normal_oscillation: "常态波动",
  normal: "常态波动",
  calm: "低波静默",
  skewed_drift: "温和偏态漂移",
  strong_trend: "单边强趋势",
  trending_up: "单边强势上行",
  trending_down: "单边强势下行",
  volatile: "高波剧震",
  wide_spread: "宽价差低流动性",
  toxic: "毒流冲击",
  toxic_shock: "毒流冲击",
  transitional: "过渡观察区间",
  tail_freeze: "尾盘冻结",
};

function updateModalHeaderBadges(slug, rec, winData) {
  const sourceEl = $("modal-round-source");
  const regimeEl = $("modal-round-regime");

  const srcRaw = String(winData?.winner_source || rec?.winner_source || "").toLowerCase();
  if (sourceEl) {
    if (srcRaw === "gamma") {
      sourceEl.textContent = "✓ Gamma 官方交割";
      sourceEl.className = "modal-meta-badge gamma";
      sourceEl.hidden = false;
    } else if (srcRaw === "twap") {
      sourceEl.textContent = "Chainlink 60s TWAP";
      sourceEl.className = "modal-meta-badge";
      sourceEl.hidden = false;
    } else if (srcRaw === "spot_fallback") {
      sourceEl.textContent = "暂估 · 待官方核验";
      sourceEl.className = "modal-meta-badge";
      sourceEl.hidden = false;
    } else if (srcRaw) {
      sourceEl.textContent = "现货基准结算";
      sourceEl.className = "modal-meta-badge";
      sourceEl.hidden = false;
    } else {
      sourceEl.hidden = true;
    }
  }

  const regRaw = String(winData?.regime || rec?.regime || "").toLowerCase();
  if (regimeEl) {
    if (regRaw && REGIME_LABELS_ZH[regRaw]) {
      regimeEl.textContent = REGIME_LABELS_ZH[regRaw];
      regimeEl.hidden = false;
    } else {
      regimeEl.hidden = true;
    }
  }
}

async function openRoundModal(slug) {
  if (!slug) return;
  const modal = $("round-detail-modal");
  const body = $("modal-round-body");
  const winnerEl = $("modal-round-winner");
  const titleEl = $("modal-round-title");

  if (!modal || !body) return;
  modal.hidden = false;
  modalCurrentSlug = slug;
  modalHoverIdx = null;
  modalSelectedActionIdx = null;

  const rawRec = (cachedHistory?.records || []).find((r) => r.slug === slug) || null;
  const rec = normalizeRecord(rawRec);
  cachedModalRecord = rec;
  const winInfo = slugToWindowText(slug, rec?.start_ts, rec?.end_ts);

  if (titleEl) titleEl.textContent = `${winInfo.fullLabel} 轮次复盘`;
  if (winnerEl) {
    const w = String(rec?.winner || "SETTLED").toUpperCase();
    winnerEl.className = `winner-pill ${w === "UP" ? "up" : w === "DOWN" ? "down" : "pending"}`;
    winnerEl.textContent = w === "UP" ? "▲ UP 胜出" : w === "DOWN" ? "▼ DOWN 胜出" : "平局";
  }
  updateModalHeaderBadges(slug, rec, null);

  body.innerHTML = `<div class="action-empty">正在加载 ${escapeHtml(slug)} 的 5 分钟秒级复盘数据...</div>`;

  try {
    const res = await fetchWithTimeout(`/api/window?slug=${encodeURIComponent(slug)}`, { cache: "no-store" }, 8000);
    if (!res.ok) {
      if (res.status === 404 && rec) {
        const pnlCls = rec.pnl > 1e-6 ? "up" : rec.pnl < -1e-6 ? "down" : "";
        body.innerHTML = `
          <div class="settings-section" style="margin: 6px 0;">
            <div class="settings-section-head">
              <h3>历史结算收益汇总（详情回放已自动清理）</h3>
              <span class="settings-sub">${escapeHtml(winInfo.fullLabel)}</span>
            </div>
            <div class="settings-storage-grid">
              <div class="storage-item"><div class="st-name"><span>单场净收益</span></div><div class="st-val ${pnlCls}">${fmtUsd(rec.pnl, true)}</div><div class="st-note">结算方向: ${escapeHtml(rec.winner || "--")} · ${fmtUsd(rec.open_px)} → ${fmtUsd(rec.close_px)}</div></div>
              <div class="storage-item"><div class="st-name"><span>持仓与成本</span></div><div class="st-val">UP ${fmtShares(rec.up_shares)} / DN ${fmtShares(rec.down_shares)}</div><div class="st-note">总成本 $${Number(rec.cost || 0).toFixed(2)} → 兑现 $${Number(rec.payout || 0).toFixed(2)}</div></div>
            </div>
            <div class="settings-sub" style="margin-top: 10px;">该场次的逐秒详情回放包已按保留策略自动释放，但永久收益账本（summary.jsonl）已完整保留。</div>
          </div>
        `;
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    }
    const winData = await res.json();
    cachedModalWindow = winData;
    if (winnerEl && winData?.winner) {
      const wLatest = String(winData.winner).toUpperCase();
      winnerEl.className = `winner-pill ${wLatest === "UP" ? "up" : wLatest === "DOWN" ? "down" : "pending"}`;
      winnerEl.textContent = wLatest === "UP" ? "▲ UP 胜出" : wLatest === "DOWN" ? "▼ DOWN 胜出" : "平局";
    }
    updateModalHeaderBadges(slug, rec, winData);
    if (winData?.archive_pruned) {
      const rFallback = normalizeRecord(winData) || rec;
      const pnlVal = Number(rFallback?.pnl ?? 0);
      const pnlCls = pnlVal > 1e-6 ? "up" : pnlVal < -1e-6 ? "down" : "";
      body.innerHTML = `
        <div class="settings-section" style="margin: 6px 0;">
          <div class="settings-section-head">
            <h3>历史结算收益汇总（详情回放已自动清理）</h3>
            <span class="settings-sub">${escapeHtml(winInfo.fullLabel)}</span>
          </div>
          <div class="settings-storage-grid">
            <div class="storage-item"><div class="st-name"><span>单场净收益</span></div><div class="st-val ${pnlCls}">${fmtUsd(pnlVal, true)}</div><div class="st-note">结算方向: ${escapeHtml(rFallback?.winner || "--")} · ${fmtUsd(rFallback?.open_px)} → ${fmtUsd(rFallback?.close_px)}</div></div>
            <div class="storage-item"><div class="st-name"><span>持仓与成本</span></div><div class="st-val">UP ${fmtShares(rFallback?.up_shares)} / DN ${fmtShares(rFallback?.down_shares)}</div><div class="st-note">总成本 $${Number(rFallback?.cost || 0).toFixed(2)} → 兑现 $${Number(rFallback?.payout || 0).toFixed(2)}</div></div>
          </div>
          <div class="settings-sub" style="margin-top: 10px;">该场次的逐秒详情回放包已按保留策略自动释放，但永久收益账本（summary.jsonl）已完整保留。</div>
        </div>
      `;
      return;
    }
    renderModalDetailContent(winData, rec);
  } catch (err) {
    body.innerHTML = `<div class="action-empty">无法加载该轮次详细轨迹数据 (${escapeHtml(err.message)})</div>`;
  }
}

function closeRoundModal() {
  const modal = $("round-detail-modal");
  if (modal) modal.hidden = true;
  modalCurrentSlug = null;
  cachedModalWindow = null;
  cachedModalRecord = null;
  modalHoverIdx = null;
  modalSelectedActionIdx = null;
}

window.openRoundModal = openRoundModal;
window.closeRoundModal = closeRoundModal;

function classifyModalActions(actions, fills) {
  // Assign sequential fill numbers (#1, #2, #3...) and strategy stage labels
  let fillSeq = 0;
  let entryLegsCount = 0;
  let firstEntrySide = null;
  let lastFillT = -999;
  let lastFillSide = null;
  let lastFillPx = -1;

  return actions.map((a, idx) => {
    const act = String(a.action || a.kind || "fill").toLowerCase();
    const side = String(a.side || "").toUpperCase();
    const px = Number(a.price ?? a.px ?? 0);
    const sz = Number(a.size ?? a.shares ?? 0);
    const rawReason = String(a.reason || "");
    const isFill = (act === "fill" || act === "taker_partial") && px > 0 && sz > 0 && !/错失|拒单|拦截|reject/i.test(rawReason);
    const t = Number(a.t ?? a.ts ?? a.timestamp ?? 0);
    const purp = String(a.purpose || "entry").toLowerCase();

    let seqNum = null;
    let stageLabel = "";
    let stageCls = "";
    let isPrimaryChartCallout = false;

    if (isFill) {
      // Merge split fills occurring within 0.4s at identical side & price into the same visual callout #
      if (Math.abs(t - lastFillT) <= 0.4 && side === lastFillSide && Math.abs(px - lastFillPx) < 1e-4) {
        seqNum = fillSeq;
        isPrimaryChartCallout = false;
      } else {
        fillSeq += 1;
        seqNum = fillSeq;
        isPrimaryChartCallout = true;
        lastFillT = t;
        lastFillSide = side;
        lastFillPx = px;
      }

      if (purp === "hedge" || px <= 0.08) {
        stageLabel = "极深黑天鹅锁利";
        stageCls = "hedge";
      } else if (purp === "unwind") {
        stageLabel = /落袋|止盈/i.test(rawReason) ? "浮盈落袋" : "尾盘防守回收";
        stageCls = "hedge";
      } else {
        if (isPrimaryChartCallout) entryLegsCount += 1;
        if (!firstEntrySide) firstEntrySide = side;
        if (entryLegsCount <= 1) {
          stageLabel = "首单建仓";
          stageCls = "entry1";
        } else if (side === firstEntrySide) {
          stageLabel = "顺势确认加仓";
          stageCls = "pyramid";
        } else {
          stageLabel = "坍塌独立反手";
          stageCls = "pyramid";
        }
      }
    } else if (act === "quote") {
      stageLabel = "FIFO 队列挂单";
    } else if (act === "cancel") {
      stageLabel = "主动风控撤单";
    } else if (act === "taker_reject" || /错失|防滑点|拦截|reject/i.test(rawReason)) {
      stageLabel = "防滑点/接飞刀拦截";
    }

    return {
      raw: a,
      idx,
      isFill,
      seqNum,
      stageLabel,
      stageCls,
      isPrimaryChartCallout,
    };
  });
}

function renderModalDetailContent(winData, rec) {
  const body = $("modal-round-body");
  if (!body) return;

  const normWin = normalizeRecord(winData);
  const openPx = Number((normWin?.open_px > 0 ? normWin.open_px : rec?.open_px) || 0);
  const closePx = Number((normWin?.close_px > 0 ? normWin.close_px : rec?.close_px) || 0);
  const upShares = Number(normWin?.up_shares ?? rec?.up_shares ?? 0);
  const dnShares = Number(normWin?.down_shares ?? rec?.down_shares ?? 0);
  const cost = Number(normWin?.cost ?? rec?.cost ?? 0);
  const payout = Number(normWin?.payout ?? rec?.payout ?? 0);
  const pnl = Number(normWin?.pnl ?? rec?.pnl ?? 0);
  const winner = String(
    (normWin?.winner && normWin.winner !== "PENDING" ? normWin.winner : rec?.winner) || "UP"
  ).toUpperCase();
  const winnerSource = String(normWin?.winner_source || rec?.winner_source || "").toLowerCase();
  const isProvisionalPrice = winnerSource === "spot_fallback";
  const startTs = Number(winData?.start || rec?.start_ts || 0);
  const actions = Array.isArray(winData?.actions) ? winData.actions : [];
  const fills = Array.isArray(winData?.fills) ? winData.fills : [];
  const hist = Array.isArray(winData?.price_history) ? winData.price_history : [];

  const diffPx = closePx - openPx;
  const diffPct = openPx > 0 ? (diffPx / openPx) * 100 : 0;
  const diffBps = openPx > 0 ? (diffPx / openPx) * 10000 : 0;
  const diffSign = diffPx > 0 ? "+" : "";
  const diffCls = diffPx > 0 ? "up" : diffPx < 0 ? "down" : "";
  const openShort = openPx.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const closeShort = closePx.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // Compute TWAP range in bps
  const twapVals = hist.map((p) => Number(p.poly_price ?? 0)).filter((v) => v > 0);
  if (openPx > 0) twapVals.push(openPx);
  const rangeBps =
    twapVals.length >= 2 && openPx > 0
      ? (((Math.max(...twapVals) - Math.min(...twapVals)) / openPx) * 10000).toFixed(1)
      : "0.0";

  // Compute exact per-leg entry VWAP & cost from fills (exclude defensive unwinds)
  let upFillSh = 0;
  let upFillCost = 0;
  let dnFillSh = 0;
  let dnFillCost = 0;
  let makerFills = 0;
  let takerFills = 0;
  let hasUnwindFill = false;
  fills.forEach((f) => {
    const s = String(f.side || "").toUpperCase();
    const sh = Number(f.size ?? f.shares ?? 0);
    const px = Number(f.price ?? 0);
    const k = String(f.kind || "maker").toLowerCase();
    const purp = String(f.purpose || "entry").toLowerCase();
    if (k === "taker") takerFills += 1;
    else makerFills += 1;
    if (purp === "unwind") {
      hasUnwindFill = true;
      return;
    }
    if (s === "UP") {
      upFillSh += sh;
      upFillCost += sh * px;
    } else if (s === "DOWN") {
      dnFillSh += sh;
      dnFillCost += sh * px;
    }
  });
  const upAvg = upFillSh > 0 ? upFillCost / upFillSh : 0;
  const dnAvg = dnFillSh > 0 ? dnFillCost / dnFillSh : 0;

  // Strategy Pattern Banner
  const pairedShares = Math.min(upShares, dnShares);
  let stratBannerHtml = "";
  if (pairedShares >= 0.5) {
    const pairUnit = (upAvg > 0 && dnAvg > 0) ? (upAvg + dnAvg) : (cost / Math.max(1, pairedShares));
    stratBannerHtml = `<span>★ 极深双边锁利 (${pairedShares.toFixed(1)} 对)</span><span class="up">配对 $${pairUnit.toFixed(3)} → 兑现 $1.00</span>`;
  } else if (Math.max(upShares, dnShares) >= 9.5) {
    stratBannerHtml = `<span>▲ 顺势确认双倍满仓 (2 手 · ${Math.max(upShares, dnShares).toFixed(1)} 股)</span><span>持有至交割</span>`;
  } else if (Math.max(upShares, dnShares) > 0.1) {
    stratBannerHtml = `<span>● 标准单手方向持仓 (1 手 · ${Math.max(upShares, dnShares).toFixed(1)} 股)</span><span>持有至交割</span>`;
  } else {
    stratBannerHtml = `<span>○ 本轮未触发建仓</span><span>全程观望零风险</span>`;
  }

  // Classify actions and count tabs
  const enrichedActions = classifyModalActions(actions, fills);
  const fillActionCount = enrichedActions.filter((x) => x.isFill).length;
  const orderActionCount = enrichedActions.length - fillActionCount;
  const quoteCount = enrichedActions.filter((x) => String(x.raw.action || "").toLowerCase() === "quote").length;
  const cancelCount = enrichedActions.filter((x) => !x.isFill && String(x.raw.action || "").toLowerCase() !== "quote").length;
  const grossFillCost = upFillCost + dnFillCost;
  const feeDiff = Math.max(0, cost - grossFillCost);
  const feeText = feeDiff >= 0.005 ? `含手续费 $${feeDiff.toFixed(2)}` : "Maker 零手续费";

  const roiPct = cost > 0.01 ? ((pnl / cost) * 100).toFixed(1) : "0.0";
  const roiSign = pnl > 0 ? "+" : "";
  const maxHeldSh = Math.max(upShares, dnShares);
  const perSharePnl = maxHeldSh > 0 ? pnl / maxHeldSh : 0;

  const visibleActions = enrichedActions.filter((item) => {
    if (modalActionFilterMode === "fill") return item.isFill;
    if (modalActionFilterMode === "order") return !item.isFill;
    return true;
  });

  const pnlCls = pnl > 0.001 ? "up" : pnl < -0.001 ? "down" : "";
  const focalCls = pnl > 0.001 ? "is-win" : pnl < -0.001 ? "is-loss" : "";

  body.innerHTML = `
    <div class="review-banner">
      <!-- Primary Visual Focal Point: Net PnL & Cash Outcome -->
      <div class="review-pnl-focal ${focalCls}">
        <span class="review-focal-label">本轮已实现净损益</span>
        <div class="review-focal-row">
          <span class="review-focal-val ${pnlCls}">${fmtUsd(pnl, true)}</span>
          ${cost > 0.01 ? `<span class="review-focal-roi ${pnlCls}">ROI ${roiSign}${roiPct}%</span>` : ""}
        </div>
        <span class="review-focal-sub">总成本 ${fmtUsd(cost)}${!hasUnwindFill && feeDiff >= 0.005 ? ` (${feeText})` : ""} → 期末兑现 ${fmtUsd(payout)}</span>
      </div>

      <!-- Supporting Metric 1: UP Position -->
      <div class="review-kpi-col">
        <span class="review-kpi-label">UP 期末持仓</span>
        <span class="review-kpi-val ${upShares > 0 ? "up" : "muted"}">${fmtShares(upShares)} 股</span>
        <span class="review-kpi-sub">${
          upShares > 0
            ? `均价 $${upAvg.toFixed(2)} · 成本 $${upFillCost.toFixed(2)}`
            : upFillSh > 0
              ? `已提前平仓 · 曾买 ${fmtShares(upFillSh)} 股 ($${upAvg.toFixed(2)})`
              : "本轮未持仓"
        }</span>
      </div>

      <!-- Supporting Metric 2: DN Position -->
      <div class="review-kpi-col">
        <span class="review-kpi-label">DN 期末持仓</span>
        <span class="review-kpi-val ${dnShares > 0 ? "down" : "muted"}">${fmtShares(dnShares)} 股</span>
        <span class="review-kpi-sub">${
          dnShares > 0
            ? `均价 $${dnAvg.toFixed(2)} · 成本 $${dnFillCost.toFixed(2)}`
            : dnFillSh > 0
              ? `已提前平仓 · 曾买 ${fmtShares(dnFillSh)} 股 ($${dnAvg.toFixed(2)})`
              : "本轮未持仓"
        }</span>
      </div>

      <!-- Supporting Metric 3: Settlement Price -->
      <div class="review-kpi-col">
        <span class="review-kpi-label">${isProvisionalPrice ? "开盘基准 → 暂估收盘（待官方核验）" : "开盘基准 → 交割收盘"}</span>
        <span class="review-kpi-val">$${openShort} → $${closeShort}</span>
        <span class="review-kpi-sub ${diffCls}">${fmtUsd(diffPx, true)} (${diffSign}${diffPct.toFixed(3)}%)</span>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-card-head">
        <div class="detail-head-left">
          <span class="detail-card-title">5 分钟结算价格与盘口隐含概率</span>
          <div class="curve-hover-bar modal-readout-bar" id="modal-chart-tooltip" hidden aria-live="polite"></div>
        </div>
        <div class="detail-card-legend">
          <span><i class="legend-dot twap"></i>Chainlink TWAP</span>
          <span><i class="legend-dot open-ref"></i>开盘基准</span>
          <span><i class="legend-dot up-prob"></i>UP 隐含概率</span>
        </div>
      </div>
      <div class="history-canvas-box">
        <canvas id="modal-hist-canvas" height="265"></canvas>
      </div>
    </div>

    <div class="detail-section table-section">
      <div class="detail-card-head">
        <span class="detail-card-title"><span>撮合与策略决策流水</span><span class="detail-count-pill">${actions.length} 条</span></span>
        ${
          actions.length > 0
            ? `<div class="modal-table-filters" id="modal-table-filter-group">
                <button type="button" class="modal-tf-btn ${modalActionFilterMode === "all" ? "active" : ""}" data-mtf="all">全部 ${enrichedActions.length}</button>
                <button type="button" class="modal-tf-btn ${modalActionFilterMode === "fill" ? "active" : ""}" data-mtf="fill">仅看成交 ${fillActionCount}</button>
                <button type="button" class="modal-tf-btn ${modalActionFilterMode === "order" ? "active" : ""}" data-mtf="order">挂单与撤单 ${orderActionCount}</button>
              </div>`
            : ""
        }
      </div>
      ${
        actions.length === 0
          ? `<div class="modal-empty-state">本轮全程观望，未触发开仓或挂单动作</div>`
          : `<div class="table-wrap">
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
                    visibleActions.length === 0
                      ? `<tr><td colspan="7" class="action-empty">当前筛选条件下无记录</td></tr>`
                      : visibleActions
                          .map(({ raw: a, idx, isFill }) => {
                            const item = formatActionItem(a, startTs);
                            const px = Number(a.price ?? a.px ?? 0);
                            const sz = Number(a.size ?? a.shares ?? 0);
                            const notional = px * sz;
                            const actT = Number(a.t ?? a.ts ?? a.timestamp ?? startTs);
                            const elapsed = Math.min(300, Math.max(0, startTs > 0 ? actT - startTs : actT));
                            const rawAct = String(a.action || "").toLowerCase();
                            const evtCls = item.typeClass === "fill"
                              ? "fill"
                              : item.typeClass === "cancel"
                                ? "cancel"
                                : (rawAct === "wait" || rawAct === "skip" || rawAct === "block" || /暂缓|拦截|观望/.test(item.typeLabel || ""))
                                  ? "wait"
                                  : "quote";

                            return `
                              <tr class="modal-action-row ${isFill ? "is-fill-row" : ""} ${px > 0 ? "has-nums" : "no-nums"} ${modalSelectedActionIdx === idx ? "is-selected" : ""}" data-action-idx="${idx}" data-elapsed="${elapsed.toFixed(2)}">
                                <td class="num td-time">${escapeHtml(item.timeStr)}</td>
                                <td class="td-evt"><span class="evt-tag ${evtCls}">${escapeHtml(item.typeLabel)}</span></td>
                                <td class="td-side"><span class="side-txt ${item.sideClass || ""}">${escapeHtml(item.sideLabel)}</span></td>
                                <td class="num td-px" data-lbl="价格">${px > 0 ? "$" + px.toFixed(2) : "--"}</td>
                                <td class="num td-sz" data-lbl="股数">${sz > 0 ? sz.toFixed(1) : "--"}</td>
                                <td class="num td-amt" data-lbl="金额">${notional > 0 ? fmtUsd(notional) : "--"}</td>
                                <td class="reason-cell">${escapeHtml(item.reasonStr || "--")}</td>
                              </tr>
                            `;
                          })
                          .join("")
                  }
                </tbody>
              </table>
            </div>`
      }
    </div>
  `;

  requestAnimationFrame(() => {
    drawModalHistoryChart(winData, rec);
    bindModalChartHover();
    bindModalActionRowClicks(winData, startTs);
    const filterGroup = $("modal-table-filter-group");
    if (filterGroup) {
      filterGroup.addEventListener("click", (e) => {
        const btn = e.target.closest(".modal-tf-btn[data-mtf]");
        if (!btn) return;
        modalActionFilterMode = btn.dataset.mtf || "all";
        renderModalDetailContent(winData, rec);
      });
    }
  });
}

function drawModalHistoryChart(winData, rec) {
  const canvas = $("modal-hist-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(260, rect.width || 760);
  const h = Math.max(200, rect.height || 265);

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
  // Settlement strike: always anchor on Polymarket / Chainlink open price, matching workspace chart
  const firstValidTwap = hist.find((p) => Number(p.poly_price ?? p.poly_spot ?? 0) > 0);
  const openPx = Number(
    winData.open_px || rec?.open_px || firstValidTwap?.poly_price || hist[0].price || hist[0].spot || 0
  );
  const firstValidSpot = hist.find((p) => Number(p.spot_price ?? p.price ?? p.spot ?? 0) > 0);
  const binOpenRef = Number(
    winData.binance_open_px || firstValidSpot?.spot_price || firstValidSpot?.price || firstValidSpot?.spot || openPx
  );
  const basis0 = binOpenRef > 0 && openPx > 0 ? binOpenRef - openPx : 0;

  const padL = 66;
  const padR = 52;
  const padT = 16;
  const padB = 22;
  const plotW = w - padL - padR;
  const totalH = h - padT - padB;
  const priceH = Math.round(totalH * 0.54);
  const probTop = padT + priceH + 26;
  const probH = totalH - priceH - 26;

  // Forward-fill stale/missing Chainlink TWAP ticks identically to drawLivePriceChart
  let lastTwap = openPx;
  const pts = hist.map((p, i) => {
    const np = normalizePricePoint(p, startTs, openPx, i, hist.length);
    const twap = Number(p.poly_price ?? 0);
    if (twap > 0) lastTwap = twap;
    np.poly_spot = lastTwap;
    np.spot_aligned = np.spot > 0 ? np.spot - basis0 : lastTwap;
    return np;
  });

  const vals = [openPx];
  pts.forEach((p) => {
    if (p.poly_spot > 0) vals.push(p.poly_spot);
    if (modalShowDeBasisSpot && p.spot_aligned > 0) vals.push(p.spot_aligned);
  });
  let minPx = Math.min(...vals);
  let maxPx = Math.max(...vals);
  if (Math.abs(maxPx - minPx) < 4) {
    maxPx += 4;
    minPx -= 4;
  } else {
    const span = maxPx - minPx;
    maxPx += span * 0.14;
    minPx -= span * 0.14;
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
    basis0,
    pts,
    xOfSec,
    yOfPrice,
    yOfProb,
  };

  ctx.font = "500 10.5px Consolas, 'JetBrains Mono', monospace";
  for (let s = 0; s <= 300; s += 60) {
    const x = xOfSec(s);
    ctx.strokeStyle = pal.grid;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + priceH);
    ctx.moveTo(x, probTop);
    ctx.lineTo(x, probTop + probH);
    ctx.stroke();

    ctx.fillStyle = pal.axis;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText(`0${Math.floor(s / 60)}:00`, x, h - padB + 5);
  }

  // Price grid + dual axis ($ price left, relative bps right)
  ctx.font = "500 10.5px Consolas, 'JetBrains Mono', monospace";
  for (let i = 0; i <= 4; i++) {
    const val = minPx + ((maxPx - minPx) * i) / 4;
    const y = yOfPrice(val);
    ctx.strokeStyle = pal.grid;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();

    ctx.fillStyle = pal.axis;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const pxText = val.toLocaleString("en-US", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    });
    ctx.fillText(`$${pxText}`, padL - 8, y);

    if (openPx > 0) {
      const bps = ((val - openPx) / openPx) * 10000;
      const bpsText = Math.abs(bps) < 0.05 ? "0.0bp" : `${bps > 0 ? "+" : ""}${bps.toFixed(1)}bp`;
      ctx.textAlign = "left";
      ctx.fillText(bpsText, w - padR + 8, y);
    }
  }

  ctx.fillStyle = pal.axis;
  ctx.font = "600 10.5px Inter, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  ctx.fillText("BTC 价格 (Chainlink 60s TWAP)", padL + 4, padT - 3);

  // Open benchmark
  if (openPx >= minPx && openPx <= maxPx) {
    const oy = yOfPrice(openPx);
    ctx.save();
    ctx.strokeStyle = pal.amber;
    ctx.lineWidth = 1.2;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, oy);
    ctx.lineTo(w - padR, oy);
    ctx.stroke();
    ctx.restore();
  }

  // Primary settlement line: Chainlink 60s TWAP with area fill (identical to workspace)
  const twapGrad = ctx.createLinearGradient(0, padT, 0, padT + priceH);
  twapGrad.addColorStop(0, pal.spotFillTop);
  twapGrad.addColorStop(1, pal.spotFillBot);

  ctx.beginPath();
  pts.forEach((p, idx) => {
    const x = xOfSec(p.elapsed);
    const y = yOfPrice(p.poly_spot);
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineTo(xOfSec(pts[pts.length - 1].elapsed), padT + priceH);
  ctx.lineTo(xOfSec(pts[0].elapsed), padT + priceH);
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

  // Optional de-basis Binance spot overlay aligned to the exact same starting baseline
  if (modalShowDeBasisSpot) {
    ctx.save();
    ctx.beginPath();
    pts.forEach((p, idx) => {
      const x = xOfSec(p.elapsed);
      const y = yOfPrice(p.spot_aligned);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = pal.spot;
    ctx.lineWidth = 1.4;
    ctx.globalAlpha = 0.82;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.restore();
  }

  // Divider + Prob pane (aligned with workspace 0% / 25% / 50% / 75% / 100%)
  ctx.strokeStyle = pal.divider;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, probTop - 18);
  ctx.lineTo(w - padR, probTop - 18);
  ctx.stroke();

  ctx.fillStyle = pal.axis;
  ctx.font = "600 10.5px Inter, sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  ctx.fillText("UP 市场隐含概率 (%)", padL + 4, probTop - 4);
  ctx.font = "500 10.5px 'JetBrains Mono', monospace";

  [0.0, 0.25, 0.5, 0.75, 1.0].forEach((pr) => {
    const y = yOfProb(pr);
    ctx.save();
    if (pr === 0.5) {
      ctx.strokeStyle = pal.isDark ? "rgba(148, 163, 184, 0.45)" : "rgba(100, 116, 139, 0.35)";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([4, 4]);
    } else {
      ctx.strokeStyle = pal.gridSoft;
      ctx.lineWidth = 1;
    }
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.restore();

    const lbl = `${Math.round(pr * 100)}%`;
    ctx.fillStyle = pr === 0.5 ? pal.label : pal.axis;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(lbl, padL - 8, y);

    ctx.textAlign = "left";
    ctx.fillText(lbl, w - padR + 8, y);
  });

  const probPts = pts.filter((p) => p.up_mid !== null);
  if (probPts.length > 0) {
    const probGrad = ctx.createLinearGradient(0, probTop, 0, probTop + probH);
    probGrad.addColorStop(0, pal.isDark ? "rgba(16, 185, 129, 0.16)" : "rgba(5, 150, 105, 0.12)");
    probGrad.addColorStop(1, "rgba(16, 185, 129, 0.00)");

    ctx.beginPath();
    probPts.forEach((p, idx) => {
      const x = xOfSec(p.elapsed);
      const y = yOfProb(p.up_mid);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOfSec(probPts[probPts.length - 1].elapsed), probTop + probH);
    ctx.lineTo(xOfSec(probPts[0].elapsed), probTop + probH);
    ctx.closePath();
    ctx.fillStyle = probGrad;
    ctx.fill();

    ctx.beginPath();
    probPts.forEach((p, idx) => {
      const x = xOfSec(p.elapsed);
      const y = yOfProb(p.up_mid);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = pal.up;
    ctx.lineWidth = 2.0;
    ctx.lineJoin = "round";
    ctx.stroke();

    const lastP = probPts[probPts.length - 1];
    const lx = xOfSec(lastP.elapsed);
    const ly = yOfProb(lastP.up_mid);
    ctx.font = "600 10.5px Consolas, 'JetBrains Mono', monospace";
    ctx.fillStyle = pal.up;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(`${(lastP.up_mid * 100).toFixed(1)}%`, Math.min(w - padR - 42, lx + 6), Math.max(probTop + 8, Math.min(probTop + probH - 8, ly)));
  }

  // Action event markers along timeline: ONLY plot executed fills (no dots/lines for non-fills)
  const actions = Array.isArray(winData?.actions) ? winData.actions : [];
  const actionMetaList = classifyModalActions(actions, winData?.fills || []);

  actions.forEach((a, idx) => {
    const meta = actionMetaList[idx] || {};
    if (!meta.isFill) return;

    const actT = Number(a.t ?? a.ts ?? a.timestamp ?? startTs);
    const elSec = Math.min(300, Math.max(0, startTs > 0 ? actT - startTs : actT));
    const ax = xOfSec(elSec);
    const isSel = modalSelectedActionIdx === idx;
    const sideUpper = String(a.side || "").toUpperCase();
    const isDownSide = sideUpper === "DOWN" || sideUpper === "DN";
    const evtColor = isDownSide ? pal.down : pal.up;
    const haloColor = isDownSide ? "rgba(244, 63, 94, 0.20)" : "rgba(16, 185, 129, 0.20)";

    ctx.save();
    let nearest = pts[0];
    let minDt = Infinity;
    pts.forEach((pt) => {
      const dt = Math.abs(pt.elapsed - elSec);
      if (dt < minDt) {
        minDt = dt;
        nearest = pt;
      }
    });

    if (isSel) {
      ctx.strokeStyle = evtColor;
      ctx.lineWidth = 1.6;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(ax, padT);
      ctx.lineTo(ax, padT + priceH);
      ctx.moveTo(ax, probTop);
      ctx.lineTo(ax, probTop + probH);
      ctx.stroke();

      if (nearest) {
        const ny = yOfPrice(nearest.poly_spot);
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(ax, ny, 8.0, 0, Math.PI * 2);
        ctx.fillStyle = haloColor;
        ctx.fill();

        ctx.beginPath();
        ctx.arc(ax, ny, 4.6, 0, Math.PI * 2);
        ctx.fillStyle = evtColor;
        ctx.fill();
        ctx.strokeStyle = pal.cardBg;
        ctx.lineWidth = 1.8;
        ctx.stroke();

        if (nearest.up_mid !== null) {
          const npy = yOfProb(nearest.up_mid);
          ctx.setLineDash([4, 3]);
          ctx.strokeStyle = evtColor;
          ctx.lineWidth = 1.3;
          ctx.beginPath();
          ctx.moveTo(padL, npy);
          ctx.lineTo(w - padR, npy);
          ctx.stroke();

          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.arc(ax, npy, 7.0, 0, Math.PI * 2);
          ctx.fillStyle = haloColor;
          ctx.fill();

          ctx.beginPath();
          ctx.arc(ax, npy, 4.2, 0, Math.PI * 2);
          ctx.fillStyle = evtColor;
          ctx.fill();
          ctx.strokeStyle = pal.cardBg;
          ctx.lineWidth = 1.6;
          ctx.stroke();
        }
      }
    } else {
      ctx.strokeStyle = isDownSide
        ? pal.isDark
          ? "rgba(244, 63, 94, 0.22)"
          : "rgba(225, 29, 72, 0.15)"
        : pal.isDark
          ? "rgba(16, 185, 129, 0.22)"
          : "rgba(5, 150, 105, 0.15)";
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(ax, padT);
      ctx.lineTo(ax, padT + priceH);
      ctx.moveTo(ax, probTop);
      ctx.lineTo(ax, probTop + probH);
      ctx.stroke();

      if (nearest) {
        const ny = yOfPrice(nearest.poly_spot);
        ctx.beginPath();
        ctx.arc(ax, ny, 6.5, 0, Math.PI * 2);
        ctx.fillStyle = haloColor;
        ctx.fill();

        ctx.beginPath();
        ctx.arc(ax, ny, 4.2, 0, Math.PI * 2);
        ctx.fillStyle = evtColor;
        ctx.fill();
        ctx.strokeStyle = pal.cardBg;
        ctx.lineWidth = 1.5;
        ctx.stroke();

        if (nearest.up_mid !== null) {
          const npy = yOfProb(nearest.up_mid);
          ctx.beginPath();
          ctx.arc(ax, npy, 6.0, 0, Math.PI * 2);
          ctx.fillStyle = haloColor;
          ctx.fill();

          ctx.beginPath();
          ctx.arc(ax, npy, 3.8, 0, Math.PI * 2);
          ctx.fillStyle = evtColor;
          ctx.fill();
          ctx.strokeStyle = pal.cardBg;
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      }
    }
    ctx.restore();
  });

  // Hover crosshair in modal
  if (modalHoverIdx !== null && pts[modalHoverIdx]) {
    const hp = pts[modalHoverIdx];
    const hx = xOfSec(hp.elapsed);
    const hy = yOfPrice(hp.poly_spot);
    ctx.save();
    ctx.strokeStyle = pal.isDark ? "rgba(248, 250, 252, 0.35)" : "rgba(15, 23, 42, 0.25)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hx, padT);
    ctx.lineTo(hx, padT + priceH);
    ctx.moveTo(hx, probTop);
    ctx.lineTo(hx, probTop + probH);
    ctx.stroke();
    ctx.restore();

    ctx.beginPath();
    ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = pal.twap;
    ctx.fill();
    ctx.strokeStyle = pal.cardBg;
    ctx.lineWidth = 2;
    ctx.stroke();

    if (hp.up_mid !== null) {
      const hpy = yOfProb(hp.up_mid);
      ctx.beginPath();
      ctx.arc(hx, hpy, 4.0, 0, Math.PI * 2);
      ctx.fillStyle = pal.up;
      ctx.fill();
      ctx.strokeStyle = pal.cardBg;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    }
  }
}

function showModalTooltipForElapsed(elapsedSec, actionMeta = null) {
  const tooltip = $("modal-chart-tooltip");
  if (!tooltip || !modalChartGeom || !modalChartGeom.pts?.length) return;

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
  const openPx = modalChartGeom.openPx || pt.poly_spot;
  const bps = openPx > 0 ? ((pt.poly_spot - openPx) / openPx) * 10000 : 0;
  const sign = bps > 0 ? "+" : "";
  const mm = String(Math.floor(Math.max(0, elapsedSec) / 60)).padStart(2, "0");
  const ss = String(Math.floor(Math.max(0, elapsedSec) % 60)).padStart(2, "0");
  const actTxt = actionMeta
    ? `<span class="chb-sep">·</span><b class="${actionMeta.sideClass || ""}">${escapeHtml(actionMeta.typeLabel)} ${escapeHtml(actionMeta.sideLabel)}</b>`
    : "";

  tooltip.innerHTML = `
    <span class="chb-idx">+${mm}:${ss}</span>
    <span class="chb-sep">·</span>
    <span>TWAP <b>${fmtUsd(pt.poly_spot)}</b> <b class="${bps >= 0 ? "up" : "down"}">(${sign}${bps.toFixed(1)}bp)</b></span>
    <span class="chb-sep">·</span>
    <span>UP <b class="up">${pt.up_mid !== null ? (pt.up_mid * 100).toFixed(1) + "%" : "--"}</b></span>
    ${actTxt}
  `;
  tooltip.hidden = false;
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

    if (bestDist > 24) {
      if (modalHoverIdx !== null) {
        modalHoverIdx = null;
        drawModalHistoryChart(cachedModalWindow, cachedModalRecord);
      }
      tooltip.hidden = true;
      return;
    }

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

  canvas.addEventListener("touchstart", (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: t.clientX, clientY: t.clientY }));
  }, { passive: true });

  canvas.addEventListener("touchmove", (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: t.clientX, clientY: t.clientY }));
  }, { passive: true });
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
  const modalCountEl = $("modal-bulletin-count");
  const previewEl = $("bulletin-latest-preview");
  const statusPillEl = $("bulletin-status-badge");
  const statusTextEl = $("bulletin-status-text");
  if (!listEl) return;

  ingestLiveDecisions(live);

  const timeline = decisionAuditLog;
  if (!timeline.length) {
    if (modalCountEl) modalCountEl.textContent = "0 条记录";
    if (statusPillEl) statusPillEl.className = "css-pill is-wait";
    if (statusTextEl) statusTextEl.textContent = "开盘观察";
    if (previewEl) previewEl.textContent = "先看行情走势，寻找划算机会";
    setHtmlIfChanged(listEl, `
      <div class="bulletin-empty">
        <div class="bulletin-empty-title">暂无策略思路记录</div>
        <div class="bulletin-empty-sub">每次策略判断出新的买入、挂单或风控保护动作时，都会用大白话记录在这里</div>
      </div>`);
    return;
  }

  if (modalCountEl) modalCountEl.textContent = `${timeline.length} 条记录`;
  const tagMap = {
    taker_buy: ["出手买入", "is-buy"],
    taker_hedge: ["锁定利润", "is-buy"],
    taker_unwind: ["风控止损", "is-warn"],
    quote: ["排队买入", "is-quote"],
    wait: ["暂不追高", "is-wait"],
  };

  const latest = timeline[timeline.length - 1];
  if (latest) {
    let [lTag, lCls] = tagMap[latest.action] || ["观察中", "is-wait"];
    const uSh = Number(live?.pos_up?.shares || 0);
    const dSh = Number(live?.pos_down?.shares || 0);
    const hasPosNow = uSh > 1e-6 || dSh > 1e-6;
    const wOrders = Array.isArray(live?.working_orders) ? live.working_orders : [];
    const tauS = Number(live?.tau_s ?? 0);
    const spotNow = Number(live?.poly_spot || live?.spot || 0);
    const openNow = Number(live?.open_px || 0);
    const diffUsdNow = openNow > 0 && spotNow > 0 ? (spotNow - openNow) : 0;
    const curRawReason = String(live?.decision_reason || latest.reason || "");

    // Ultra-crisp, human-first 1-sentence verdict (<= 18 Chinese chars, zero jargon, zero repetition)
    let shortMsg = translateDecisionReason(curRawReason);
    if (wOrders.length > 0) {
      const wo = wOrders[0];
      const wSide = String(wo.side || "").toUpperCase() === "UP" ? "看涨" : "看跌";
      const wPx = Number(wo.price ?? wo.px ?? 0);
      const qAhead = Number(wo.queue_ahead ?? 0);
      lTag = "排队买入";
      lCls = "is-quote";
      shortMsg = qAhead <= 1
        ? `已挂 $${wPx.toFixed(2)} 买${wSide}，排在队首随时成交`
        : `已挂 $${wPx.toFixed(2)} 买${wSide}，前方还有 ${qAhead.toFixed(0)} 股`;
    } else if (hasPosNow) {
      const heldUp = uSh >= dSh;
      const padUsd = heldUp ? diffUsdNow : -diffUsdNow;
      if (tauS > 0 && tauS <= 15) {
        lTag = "尾盘开奖";
        lCls = "is-warn";
        shortMsg = padUsd >= 0
          ? `最后 ${Math.round(tauS)} 秒，目前领先 +$${Math.abs(padUsd).toFixed(1)}，等开奖`
          : `最后 ${Math.round(tauS)} 秒，落后 -$${Math.abs(padUsd).toFixed(1)}，看能否翻盘`;
      } else if (Math.abs(padUsd) < 12) {
        lTag = "焦灼拉锯";
        lCls = "is-warn";
        shortMsg = padUsd >= 0
          ? `仅领先 +$${Math.abs(padUsd).toFixed(1)}，随时可能翻盘，紧盯中`
          : `仅落后 -$${Math.abs(padUsd).toFixed(1)}，随时可能反超，紧盯中`;
      } else if (padUsd >= 12) {
        lTag = "目前稳赢";
        lCls = "is-buy";
        shortMsg = `已拉开 +$${padUsd.toFixed(1)} 优势，安心等交割`;
      } else {
        lTag = "暂时落后";
        lCls = "is-down";
        shortMsg = `目前落后 -$${Math.abs(padUsd).toFixed(1)}，剩 ${Math.round(tauS)} 秒看能否反超`;
      }
    } else if (Boolean(live?.window_unwound) || /本窗已完成极端浮盈落袋/i.test(curRawReason)) {
      const uPnlLive = Number(live?.realized_unwind_pnl || 0);
      lTag = "已落袋";
      lCls = "is-buy";
      shortMsg = `本轮已高位止盈落袋（${fmtUsd(uPnlLive, true)}），安心等下一轮`;
    } else if (Boolean(live?.basis_anomaly_active) || /基差瞬变脱锚/i.test(curRawReason)) {
      lTag = "脱锚暂停";
      lCls = "is-warn";
      shortMsg = "现货与基准价突发脱锚，暂停开仓等待收敛";
    } else if (/持续极端分歧/i.test(curRawReason)) {
      lTag = "分歧避险";
      lCls = "is-warn";
      shortMsg = "模型与盘口分歧持续异常，暂不追单";
    } else if (/Anti-toxicity|Toxic|shock|极端巨震|风暴避险/i.test(curRawReason)) {
      lTag = "急震暂停";
      lCls = "is-warn";
      shortMsg = "BTC 刚剧烈异动，先避险不乱冲";
    } else if (tauS > 280 || /Observation period/i.test(curRawReason)) {
      lTag = "开盘校准";
      lCls = "is-wait";
      const leftObs = Math.max(1, Math.round(tauS - 280));
      shortMsg = `开盘校准基准价，${leftObs} 秒后轻仓试探`;
    } else if (tauS > 0 && tauS <= 15) {
      lTag = "尾盘锁定";
      lCls = "is-wait";
      shortMsg = "最后 15 秒冻结下单，准备下一轮";
    } else if (tauS > 15 && tauS <= 30) {
      lTag = "尾盘收官";
      lCls = "is-wait";
      shortMsg = "最后 30 秒停止新开仓，静待交割";
    } else {
      const pm = curRawReason.match(/P\*\(?Up\)?=([\d.]+)/i);
      const um = curRawReason.match(/Up=([\d.-]+)\/([\d.-]+)/i);
      const dm = curRawReason.match(/Dn=([\d.-]+)\/([\d.-]+)/i);
      if (pm && (um || dm)) {
        const pUp = parseFloat(pm[1]);
        const favorUp = pUp >= 0.5;
        const sideTxt = favorUp ? "看涨" : "看跌";
        const fairCents = Math.round((favorUp ? pUp : 1 - pUp) * 100);
        const askNum = parseFloat(favorUp ? (um ? um[2] : "") : (dm ? dm[2] : ""));
        if (Number.isFinite(askNum) && askNum > 0) {
          const askCents = Math.round(askNum * 100);
          const diffCents = fairCents - askCents;
          if (askCents >= 92) {
            lTag = "胜负已分";
            lCls = "is-wait";
            shortMsg = `${sideTxt}已涨到 ${askCents}%，太贵不追，等下一轮`;
          } else if (diffCents < 0) {
            lTag = "暂不追高";
            lCls = "is-wait";
            shortMsg = `${sideTxt}胜算更大，但目前价格偏贵 ${Math.abs(diffCents)}%，等回落`;
          } else if (diffCents < 5) {
            lTag = "紧盯出手";
            lCls = "is-quote";
            shortMsg = `${sideTxt}已有 ${diffCents}% 折价，再降一点就出手`;
          } else {
            lTag = "机会出现";
            lCls = "is-buy";
            shortMsg = `${sideTxt}出现 ${diffCents}% 折价，准备择机买入`;
          }
        }
      }
    }

    if (statusPillEl) statusPillEl.className = `css-pill ${lCls}`;
    if (statusTextEl) statusTextEl.textContent = lTag;
    if (previewEl) previewEl.textContent = shortMsg;
  }

  const curSlugT = live?.slug || "";
  const liveStartTs = Number(live?.start || 0);
  const formatThoughtTime = (item) => {
    const tNum = Number(item?.t || 0);
    const sameRound = !item?.slug || !curSlugT || item.slug === curSlugT;
    if (sameRound && liveStartTs > 0 && tNum > 1e9) {
      const sec = Math.min(300, Math.max(0, Math.round(tNum - liveStartTs)));
      const m = String(Math.floor(sec / 60)).padStart(2, "0");
      const s = String(sec % 60).padStart(2, "0");
      return `${m}:${s}`;
    }
    if (item?.tau_s != null && Number.isFinite(Number(item.tau_s))) {
      const sec = Math.min(300, Math.max(0, Math.round(300 - Number(item.tau_s))));
      const m = String(Math.floor(sec / 60)).padStart(2, "0");
      const s = String(sec % 60).padStart(2, "0");
      return `${m}:${s}`;
    }
    return tNum > 1e9 ? new Date(tNum * 1000).toLocaleTimeString("zh-CN", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai"
    }) : "--:--";
  };

  const resolveThoughtTag = (item, text) => {
    const raw = `${item.reason || ""} ${text || ""}`;
    if (item.action === "taker_unwind") {
      return /落袋|止盈/i.test(raw) ? ["浮盈落袋", "is-buy"] : ["风控止损", "is-warn"];
    }
    if (item.action && item.action !== "wait" && tagMap[item.action]) {
      return tagMap[item.action];
    }
    if (/本窗已完成极端浮盈落袋|已高位止盈落袋/i.test(raw)) return ["已落袋", "is-buy"];
    if (/基差瞬变脱锚|持续极端分歧|高波动暂停被动 Maker/i.test(raw)) return ["防灾避险", "is-warn"];
    if (/开盘前|校准基准|Observation/i.test(raw)) return ["开盘校准", "is-calm"];
    if (/剧烈波动|急跌|急涨|防接飞刀|避险|Anti-toxicity|Toxic|shock|风暴|暂停下单/i.test(raw)) return ["风控避险", "is-warn"];
    if (/延迟|同步|超时|网络|断流/i.test(raw)) return ["延迟暂缓", "is-warn"];
    if (/持有.*结算|稳赢|锁仓|等交割/i.test(raw)) return ["持仓观望", "is-calm"];
    if (/尾盘|停止新开|冻结下单|最后\s*\d+\s*秒/i.test(raw)) return ["尾盘收官", "is-calm"];
    if (/偏贵|不够划算|折价|等待|胜率/i.test(raw)) return ["等待好价", "is-wait"];
    return tagMap[item.action] || ["观察中", "is-wait"];
  };

  const rows = [...timeline].reverse().map((item, idx) => {
    const ts = formatThoughtTime(item);
    const text = translateDecisionReason(item.reason);
    const [tagLabel, tagCls] = resolveThoughtTag(item, text);
    const latestCls = idx === 0 ? " is-latest" : "";
    return `
      <div class="bulletin-card${latestCls}">
        <div class="bulletin-card-top">
          <span class="bulletin-status-pill ${tagCls}">${escapeHtml(tagLabel)}</span>
          <span class="bulletin-card-time">${ts}</span>
        </div>
        <div class="bulletin-card-text">${escapeHtml(text)}</div>
      </div>`;
  }).join("");
  setHtmlIfChanged(listEl, rows);

  // Inline Strategy Thought Timeline in Right Rail (Zero Modal Blocking)
  const inlineThoughtsEl = $("inline-thoughts-feed");
  if (inlineThoughtsEl) {
    const curRoundItems = curSlugT ? timeline.filter((it) => !it.slug || it.slug === curSlugT) : timeline;
    const displayItems = (curRoundItems.length ? curRoundItems : timeline).slice(-30).reverse();
    if (!displayItems.length) {
      setHtmlIfChanged(inlineThoughtsEl, `
        <div class="console-empty">
          <span class="ce-title">暂无思路记录</span>
          <span class="ce-sub">策略每次判断变化都会实时记录在这里</span>
        </div>`);
    } else {
      const inlineHtml = displayItems.map((item, idx) => {
        const ts = formatThoughtTime(item);
        const text = translateDecisionReason(item.reason);
        const [tagLabel, tagCls] = resolveThoughtTag(item, text);
        return `
          <div class="ith-item ${idx === 0 ? "is-latest" : ""}">
            <div class="ith-top">
              <span class="ith-tag ${tagCls}">${escapeHtml(tagLabel)}</span>
              <span class="ith-time-stamp">${ts}</span>
            </div>
            <div class="ith-text">${escapeHtml(text)}</div>
          </div>`;
      }).join("");
      setHtmlIfChanged(inlineThoughtsEl, inlineHtml);
    }
  }
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

  const tauMm = String(Math.floor(tau / 60)).padStart(2, "0");
  const tauSs = String(tau % 60).padStart(2, "0");
  if ($("market-tau")) $("market-tau").textContent = `${tauMm}:${tauSs}`;
  if ($("chart-elapsed-badge")) $("chart-elapsed-badge").textContent = `已运行 ${elapsed}s / 300s`;
  if ($("overview-live-tau")) {
    setHtmlIfChanged($("overview-live-tau"), `${tau}<span class="val-unit">s</span>`);
  }
  const elapsedPct = ((elapsed / 300) * 100).toFixed(1);
  const bar = $("overview-tau-bar");
  if (bar) bar.style.width = `${elapsedPct}%`;
  const mBar = $("market-tau-bar");
  if (mBar) mBar.style.width = `${elapsedPct}%`;
  const roundBar = $("market-round-bar");
  if (roundBar) {
    roundBar.classList.toggle("is-crunch", tau <= 60 && tau > 15);
    roundBar.classList.toggle("is-freeze", tau <= 15 && tau > 0);
  }

  // Update 4-Act Stage Stepper (v3.3): observe (0..20s) -> active (20..255s) -> freeze/late (255..300s) -> settle (0s)
  const stageOrder = ["observe", "active", "freeze", "settle"];
  const curStageIdx = tau === 0 ? 3 : tau <= 45 ? 2 : elapsed < 20 ? 0 : 1;
  document.querySelectorAll("#round-stage-stepper .stage-step").forEach((el) => {
    const idx = stageOrder.indexOf(el.getAttribute("data-stage"));
    el.classList.toggle("is-active", idx === curStageIdx);
    el.classList.toggle("is-done", idx >= 0 && idx < curStageIdx);
  });
  document.querySelectorAll("#round-stage-stepper .stage-line").forEach((ln) => {
    const lIdx = Number(ln.getAttribute("data-line"));
    ln.classList.toggle("is-done", Number.isFinite(lIdx) && lIdx < curStageIdx);
  });
}

// ============================================================================
// DATA POLLING & EVENT BINDINGS (Fast 300ms Live Channel + Non-blocking History Channel)
// ============================================================================
let _fetchInFlight = false;
let _historyInFlight = false;
let _lastHistoryFetchMs = 0;
let _lastHistorySlug = null;
let _lastHistoryFillsCount = -1;
let _pollTimerId = null;
const LIVE_POLL_ACTIVE_MS = 300;
const LIVE_POLL_HIDDEN_MS = 3000;

async function refreshHistoryInBackground(isManual = false, curSlug = null, curFills = 0) {
  if (!isManual && _historyInFlight) {
    return;
  }
  _historyInFlight = true;
  try {
    const histRes = await fetchWithTimeout("/api/history?limit=300", { cache: "no-store" }, 8000);
    if (!histRes.ok) {
      throw new Error(`HTTP history=${histRes.status}`);
    }
    const hist = await histRes.json();
    // 换轮结算即时 Toast 反馈（非首次加载时，最新已结算场次变化即刻播报）
    const topSettled = Array.isArray(hist?.records) && hist.records.length > 0 ? hist.records[0] : null;
    if (topSettled && topSettled.slug) {
      if (_lastNotifiedSettledSlug && _lastNotifiedSettledSlug !== topSettled.slug) {
        const nr = normalizeRecord(topSettled);
        const wInfo = slugToWindowText(nr.slug, nr.start_ts, nr.end_ts);
        const winLbl = String(nr.winner || "").toUpperCase() === "UP" ? "UP 胜出" : String(nr.winner || "").toUpperCase() === "DOWN" ? "DOWN 胜出" : "已结算";
        if (nr.traded) {
          const tone = nr.pnl > 1e-6 ? "up" : nr.pnl < -1e-6 ? "down" : "info";
          showToast(`上轮 ${wInfo.range} 结算：${winLbl} · 净收益 ${fmtUsd(nr.pnl, true)}`, tone);
        } else if (currentPage === "market") {
          showToast(`上轮 ${wInfo.range} 结算：${winLbl} · 本轮空仓观望`, "info");
        }
      }
      _lastNotifiedSettledSlug = topSettled.slug;
    }
    cachedHistory = hist;
    _lastHistoryFetchMs = Date.now();
    if (curSlug !== null) _lastHistorySlug = curSlug;
    _lastHistoryFillsCount = curFills;

    const versionEl = $("page-version");
    const activeVer = (hist && (hist.current_batch || hist.batch_id)) || (cachedLive && cachedLive.batch_id);
    if (versionEl && activeVer) {
      versionEl.textContent = activeVer;
    }
    if (cachedLive) {
      updateOverview(cachedLive, hist);
    }
    renderHistoryTable(hist);
  } catch (_) {
    // Non-fatal in background channel; next tick will retry
  } finally {
    _historyInFlight = false;
  }
}

async function fetchAll(isManual = false) {
  if (!isManual && _fetchInFlight) {
    return;
  }
  _fetchInFlight = true;

  const connPill = $("conn-pill");
  const connText = $("conn-text");
  const refreshBtn = $("refresh-btn");

  if (isManual && refreshBtn) {
    refreshBtn.classList.add("spinning");
  }

  try {
    const liveRes = await fetchWithTimeout("/api/live", { cache: "no-store" }, 4000);
    if (!liveRes.ok) {
      throw new Error(`HTTP live=${liveRes.status}`);
    }
    const live = await liveRes.json();
    cachedLive = live;
    if (live && live.server_time) {
      syncServerClockOffset(live.server_time);
    }

    const versionEl = $("page-version");
    const activeVer =
      (live && live.batch_id) ||
      (cachedHistory && (cachedHistory.current_batch || cachedHistory.batch_id));
    if (versionEl && activeVer) {
      versionEl.textContent = activeVer;
    }

    // 1. Render live market data immediately (0ms wait — never blocked by /api/history!)
    updateNetworkTelemetry(live);
    updateOverview(live, cachedHistory);
    updateMarket(live);
    updateSmoothCountdowns();

    // 2. Check if history ledger needs refresh, and trigger it asynchronously
    const nowMs = Date.now();
    const nowSec = live && live.server_time ? Number(live.server_time) : (nowMs / 1000);
    const curSlug = (live && live.slug) || null;
    const curFills = Number((live && live.fills_count) || 0);
    const recentRecs = Array.isArray(cachedHistory?.records) ? cachedHistory.records.slice(0, 3) : [];
    const hasFreshProvisional = recentRecs.some((r) => {
      if (String(r?.winner_source || "").toLowerCase() !== "spot_fallback") return false;
      const endTs = Number(r?.end || (Number(r?.start || 0) ? Number(r.start) + 300 : 0));
      return endTs <= 0 || (nowSec - endTs) <= 300;
    });
    const histIntervalMs = document.hidden ? 60000 : (hasFreshProvisional ? 4000 : 20000);
    const needHistory =
      isManual ||
      !cachedHistory ||
      nowMs - _lastHistoryFetchMs >= histIntervalMs ||
      curSlug !== _lastHistorySlug ||
      curFills !== _lastHistoryFillsCount;

    if (needHistory) {
      if (isManual || !cachedHistory) {
        await refreshHistoryInBackground(isManual, curSlug, curFills);
      } else {
        refreshHistoryInBackground(false, curSlug, curFills);
      }
    }

    if (isManual) {
      showToast("实时行情与历史结算账本已同步", "up");
    }
  } catch (err) {
    if (connPill) connPill.className = "status-pill status-critical";
    if (connText) connText.textContent = "连接中断";
    if ($("modal-conn-pill")) $("modal-conn-pill").className = "status-pill status-critical";
    if ($("modal-conn-text")) $("modal-conn-text").textContent = "连接中断";
    if (isManual) {
      showToast("数据同步失败，请检查本地服务状态", "down");
    }
  } finally {
    _fetchInFlight = false;
    if (isManual && refreshBtn) {
      setTimeout(() => refreshBtn.classList.remove("spinning"), 420);
    }
  }
}

function scheduleNextPoll(lastElapsedMs = 0) {
  if (_pollTimerId !== null) {
    clearTimeout(_pollTimerId);
    _pollTimerId = null;
  }
  const targetMs = document.hidden ? LIVE_POLL_HIDDEN_MS : LIVE_POLL_ACTIVE_MS;
  const delayMs = document.hidden
    ? targetMs
    : Math.max(120, targetMs - Math.max(0, Number(lastElapsedMs) || 0));
  _pollTimerId = setTimeout(async () => {
    const t0 = performance.now();
    await fetchAll(false);
    const elapsed = performance.now() - t0;
    scheduleNextPoll(elapsed);
  }, delayMs);
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

  const todayToggleBtn = $("history-today-toggle");
  if (todayToggleBtn) {
    todayToggleBtn.addEventListener("click", () => {
      historyTodayOnly = !historyTodayOnly;
      todayToggleBtn.classList.toggle("active", historyTodayOnly);
      todayToggleBtn.setAttribute("aria-pressed", String(historyTodayOnly));
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
  const modalConnHelpBtn = $("modal-conn-help-btn");
  const connGuideModal = $("conn-guide-modal");
  const connGuideClose = $("conn-guide-close-btn");
  const openConnGuide = (e) => {
    if (e) e.stopPropagation();
    if (connGuideModal) connGuideModal.hidden = false;
  };
  const closeConnGuide = () => {
    if (connGuideModal) connGuideModal.hidden = true;
  };
  if (connHelpBtn && connGuideModal) {
    connHelpBtn.addEventListener("click", openConnGuide);
  }
  if (modalConnHelpBtn && connGuideModal) {
    modalConnHelpBtn.addEventListener("click", openConnGuide);
  }
  const connPillBtn = $("conn-pill");
  const connStatusModal = $("conn-status-modal");
  const connStatusClose = $("conn-status-close-btn");
  const openConnStatusModal = () => {
    if (connStatusModal) connStatusModal.hidden = false;
  };
  const closeConnStatusModal = () => {
    if (connStatusModal) connStatusModal.hidden = true;
  };
  if (connPillBtn && connStatusModal) {
    const maybeOpenConnModal = () => {
      if (window.matchMedia && window.matchMedia("(max-width: 960px)").matches) {
        openConnStatusModal();
      }
    };
    connPillBtn.addEventListener("click", maybeOpenConnModal);
    connPillBtn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        maybeOpenConnModal();
      }
    });
  }
  if (connStatusClose) {
    connStatusClose.addEventListener("click", closeConnStatusModal);
  }
  if (connStatusModal) {
    connStatusModal.addEventListener("click", (e) => {
      if (e.target === connStatusModal) closeConnStatusModal();
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
  const timelineModal = $("decision-timeline-modal");
  const timelineClose = $("decision-timeline-close-btn");
  const closeTimelineModal = () => {
    if (timelineModal) timelineModal.hidden = true;
  };
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

  // System & Storage Settings Modal
  const settingsBtn = $("settings-btn");
  const settingsModal = $("settings-modal");
  const settingsClose = $("settings-close-btn");
  const settingsRefreshBtn = $("settings-refresh-btn");
  const settingsCleanupBtn = $("settings-cleanup-btn");
  const closeSettingsModal = () => {
    if (settingsModal) settingsModal.hidden = true;
  };
  const setFillBar = (el, pct) => {
    if (!el) return;
    const p = Math.max(0, Math.min(100, Number(pct || 0)));
    el.style.width = `${p}%`;
    el.className = "settings-fill" + (p >= 85 ? " bad" : (p >= 70 ? " warn" : ""));
  };
  const renderSystemSettings = (sys) => {
    if (!sys) return;
    const st = sys.storage || {};
    const ret = sys.retention || {};
    const gd = sys.disk_guard || {};

    const badge = $("settings-guard-badge");
    if (badge) {
      if (gd.paused) {
        badge.textContent = "已暂停原始录制";
        badge.style.color = "var(--down)";
      } else if (gd.pressure_triggered || Number(sys.disk_used_pct || 0) >= Number(ret.disk_soft_limit_pct || 75)) {
        badge.textContent = "容量偏紧 · 自动防护中";
        badge.style.color = "#d97706";
      } else {
        badge.textContent = "容量健康";
        badge.style.color = "var(--up)";
      }
    }

    const hostMeta = $("settings-host-meta");
    if (hostMeta) {
      const cpuStr = `CPU ${sys.cpu_count || 1}核` + (sys.load_1m !== null && sys.load_1m !== undefined ? ` · 负载 ${sys.load_1m}` : "");
      const pidStr = sys.pipeline_running ? `交易进程正常 (PID ${sys.pipeline_pid || "--"})` : "交易进程未运行";
      hostMeta.textContent = `${pidStr} · ${cpuStr}`;
    }

    if ($("set-disk-val") && sys.disk_total_gb) {
      $("set-disk-val").textContent = `已用 ${sys.disk_used_gb} / ${sys.disk_total_gb} GB (剩 ${sys.disk_free_gb} GB · ${sys.disk_used_pct}%)`;
      setFillBar($("set-disk-bar"), sys.disk_used_pct);
    }

    const quotaMb = Number(ret.max_data_dir_mb || 4096);
    const totalMb = Number(st.total_mb || 0);
    const quotaPct = quotaMb > 0 ? Math.min(100, (totalMb / quotaMb) * 100) : 0;
    if ($("set-quota-val")) {
      $("set-quota-val").textContent = `${totalMb.toFixed(2)} MB / ${(quotaMb / 1024).toFixed(1)} GB (${quotaPct.toFixed(1)}%)`;
      setFillBar($("set-quota-bar"), quotaPct);
    }

    if ($("set-ram-val") && sys.mem_total_mb) {
      const swapTxt = sys.swap_total_mb > 0 ? ` · Swap ${(sys.swap_used_mb / 1024).toFixed(2)}/${(sys.swap_total_mb / 1024).toFixed(2)}G` : "";
      $("set-ram-val").textContent = `${(sys.mem_used_mb / 1024).toFixed(2)} / ${(sys.mem_total_mb / 1024).toFixed(2)} GB (${sys.mem_used_pct}%)${swapTxt}`;
      setFillBar($("set-ram-bar"), sys.mem_used_pct);
    }

    const alertEl = $("settings-guard-alert");
    if (alertEl) {
      if (gd.paused && gd.reason) {
        alertEl.hidden = false;
        alertEl.textContent = gd.reason;
      } else {
        alertEl.hidden = true;
      }
    }

    if ($("settings-total-files")) {
      $("settings-total-files").textContent = `总计 ${totalMb.toFixed(2)} MB · ${st.total_files || 0} 个文件`;
    }

    const grid = $("settings-storage-grid");
    if (grid) {
      const items = [
        { name: "原始行情录制 (1_market)", val: `${Number(st.market_mb || 0).toFixed(2)} MB`, note: `${st.market_files || 0} 文件 · 保留 ${ret.market_days || 3} 天` },
        { name: "近期详情回放 (windows/*.gz)", val: `${Number(st.replays_mb || 0).toFixed(2)} MB`, note: `${st.replay_files || 0} 场 · 保留 ${ret.replay_days || 7} 天` },
        { name: "永久收益账本 (summary.jsonl)", val: `${Number(st.summary_mb || 0).toFixed(3)} MB`, note: `${st.summary_files || 0} 个版本 · 永久不删` },
        { name: "报告与运行日志", val: `${(Number(st.reports_mb || 0) + Number(st.runtime_mb || 0)).toFixed(2)} MB`, note: `报告 ${Number(st.reports_mb || 0).toFixed(1)}M / 日志 ${Number(st.runtime_mb || 0).toFixed(1)}M` },
      ];
      grid.innerHTML = items.map((it) => `
        <div class="storage-item">
          <div class="st-name"><span>${it.name}</span></div>
          <div class="st-val">${it.val}</div>
          <div class="st-note">${it.note}</div>
        </div>
      `).join("");
    }

    const pol = $("settings-policy-list");
    if (pol) {
      const intMin = Math.round(Number(ret.cleanup_interval_s || 1800) / 60);
      pol.innerHTML = `
        <div>原始行情保留：<strong>${ret.market_days || 3} 天</strong> (${gd.raw_recording_allowed ? "录制中" : "已暂停"})</div>
        <div>近期回放保留：<strong>${ret.replay_days || 7} 天</strong> (紧张时保底 ${ret.min_keep_replays || 30} 场)</div>
        <div>自动巡检周期：<strong>每 ${intMin} 分钟</strong></div>
        <div>目录配额上限：<strong>${(quotaMb / 1024).toFixed(1)} GB</strong></div>
        <div>磁盘警戒瘦身：<strong>≥ ${ret.disk_hard_limit_pct || 85}% 或剩 &lt; ${ret.min_free_disk_gb || 3} GB</strong></div>
        <div>极限熔断保护：<strong>≥ ${ret.disk_critical_pause_pct || 93}% 暂停录行情</strong></div>
      `;
    }
  };

  const setSettingsFeedback = (text, isError = false) => {
    const el = $("settings-cleanup-msg");
    if (!el) return;
    el.textContent = text;
    el.className = `settings-feedback${isError ? " is-error" : ""}`;
  };

  const loadSystemSettings = async (forceRefresh = false) => {
    try {
      const resp = await fetchWithTimeout(`/api/system${forceRefresh ? "?refresh=1" : ""}`, { cache: "no-store" }, 8000);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      renderSystemSettings(data);
    } catch (err) {
      setSettingsFeedback(`加载系统占用失败: ${err.message}`, true);
    }
  };

  const openSettings = () => {
    if (!settingsModal) return;
    settingsModal.hidden = false;
    setSettingsFeedback("");
    loadSystemSettings(true);
  };
  if (settingsBtn && settingsModal) {
    settingsBtn.addEventListener("click", openSettings);
  }
  if (settingsClose) settingsClose.addEventListener("click", closeSettingsModal);
  if (settingsModal) {
    settingsModal.addEventListener("click", (e) => {
      if (e.target === settingsModal) closeSettingsModal();
    });
  }
  if (settingsRefreshBtn) {
    settingsRefreshBtn.addEventListener("click", () => loadSystemSettings(true));
  }
  if (settingsCleanupBtn) {
    settingsCleanupBtn.addEventListener("click", async () => {
      settingsCleanupBtn.disabled = true;
      setSettingsFeedback("正在扫描并清理过期数据...");
      try {
        const resp = await fetchWithTimeout("/api/cleanup", { method: "POST" }, 15000);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const res = await resp.json();
        if (res.system) {
          renderSystemSettings(res.system);
        }
        const cl = res.cleanup || {};
        setSettingsFeedback(
          cl.removed_files > 0 || cl.removed_dirs > 0
            ? `清理完成：删除 ${cl.removed_dirs} 个目录、${cl.removed_files} 个文件，释放 ${Number(cl.freed_mb || 0).toFixed(2)} MB（summary.jsonl 完整保留）`
            : "当前没有过期数据需要清理，空间状态良好。"
        );
      } catch (err) {
        setSettingsFeedback(`清理失败: ${err.message}`, true);
      } finally {
        settingsCleanupBtn.disabled = false;
      }
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (connGuideModal && !connGuideModal.hidden) {
      closeConnGuide();
      return;
    }
    if (connStatusModal && !connStatusModal.hidden) {
      closeConnStatusModal();
      return;
    }
    if (modalBackdrop && !modalBackdrop.hidden) {
      closeRoundModal();
      return;
    }
    if (settingsModal && !settingsModal.hidden) {
      closeSettingsModal();
      return;
    }
    if (regimeGuideModal && !regimeGuideModal.hidden) {
      closeRegimeGuide();
      return;
    }
    if (timelineModal && !timelineModal.hidden) {
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
  setInterval(updateSmoothCountdowns, 250);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      fetchAll(false).finally(() => scheduleNextPoll());
    } else {
      scheduleNextPoll();
    }
  });

  window.addEventListener("focus", () => {
    if (!document.hidden) {
      fetchAll(false).finally(() => scheduleNextPoll());
    }
  });

  fetchAll(false).finally(() => scheduleNextPoll());
});
