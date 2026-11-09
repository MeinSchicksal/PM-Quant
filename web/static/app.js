const roundsElement = document.getElementById("rounds");
const openDetails = new Set();
let requestPending = false;

const RESULT_LABELS = {
  win: "本轮赢",
  loss: "本轮亏",
  flat: "持平",
  no_trade: "未成交",
  pending: "进行中",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function money(value, signed = false) {
  const amount = Number(value) || 0;
  const sign = amount < 0 ? "-" : signed && amount > 0 ? "+" : "";
  return `${sign}$${Math.abs(amount).toFixed(2)}`;
}

function price(value) {
  return value === null || value === undefined ? "—" : `$${Number(value).toFixed(2)}`;
}

function shares(value) {
  const amount = Number(value) || 0;
  return `${Number.isInteger(amount) ? amount : amount.toFixed(2)} 股`;
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function dateParts(timestamp) {
  const date = new Date((Number(timestamp) || 0) * 1000);
  return {
    date: `${pad(date.getMonth() + 1)}月${pad(date.getDate())}日`,
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`,
    minute: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  };
}

function sideLabel(side) {
  if (side !== "Up" && side !== "Down") return "";
  return `<span class="side-label ${side.toLowerCase()}">${side}</span>`;
}

function actionText(action) {
  const hedge = action.purpose === "hedge";
  if (action.action === "quote") {
    return hedge ? "挂单对冲" : "挂单买入";
  }
  if (action.purpose === "unwind") return "卖出平仓";
  return hedge ? "对冲成交" : "买入成交";
}

function renderActions(actions) {
  return actions.map(action => {
    const hedge = action.purpose === "hedge" ? "is-hedge" : "";
    const filled = action.action === "fill" ? "is-fill" : "";
    return `
      <div class="action ${filled} ${hedge}">
        <time class="action-time">${dateParts(action.t).time}</time>
        <span class="action-dot" aria-hidden="true"></span>
        <div class="action-content">
          <span class="action-title">${actionText(action)}</span>
          ${sideLabel(action.side)}
          <span class="action-detail">${shares(action.size)} @ ${price(action.price)}</span>
        </div>
      </div>`;
  }).join("");
}

function renderTimeline(actions, key, expanded = false) {
  if (!actions.length) {
    return '<p class="no-trade-note">暂无挂单或成交记录</p>';
  }
  const limit = expanded ? actions.length : 7;
  const visible = actions.slice(0, limit);
  const remaining = actions.slice(limit);
  let html = `<div class="timeline">${renderActions(visible)}</div>`;
  if (remaining.length) {
    html += `
      <details class="more-actions" data-key="${escapeHtml(key)}-more" ${openDetails.has(`${key}-more`) ? "open" : ""}>
        <summary>展开其余 ${remaining.length} 条动作</summary>
        <div class="timeline">${renderActions(remaining)}</div>
      </details>`;
  }
  return html;
}

function renderSummary(round) {
  const resultClass = round.result === "win" ? "profit" : round.result === "loss" ? "loss" : "";
  const marketResult = round.winner ? `<small>${escapeHtml(round.winner)} 获胜</small>` : "";
  const finalText = round.result === "pending" ? "等待结算" :
    round.result === "no_trade" ? "无成交" :
    round.result === "win" ? "赢" : round.result === "loss" ? "亏" : "持平";
  return `
    <div class="round-summary">
      <div>
        <p class="summary-label">Up 实际成交均价</p>
        <p class="summary-value">${price(round.average_prices?.Up)}</p>
      </div>
      <div>
        <p class="summary-label">Down 实际成交均价</p>
        <p class="summary-value">${price(round.average_prices?.Down)}</p>
      </div>
      <div>
        <p class="summary-label">最终结果</p>
        <p class="summary-value result-text ${resultClass}">${finalText}${marketResult}</p>
      </div>
      <div>
        <p class="summary-label">单轮落袋</p>
        <p class="summary-value result-text ${resultClass}">${round.net_pnl === null ? "—" : money(round.net_pnl, true)}</p>
      </div>
    </div>`;
}

function renderRound(round, index, totalRounds) {
  const start = dateParts(round.start);
  const end = dateParts(round.end);
  const hasTime = Number(round.start) > 0 && Number(round.end) > 0;
  const resultClass = round.result === "win" ? "profit" : round.result === "loss" ? "loss" : "neutral";
  const pnlText = round.result === "pending" ? "—" : round.result === "no_trade" ? "$0.00" : money(round.net_pnl, true);
  const sequence = round.result === "pending" ? "LIVE" : `#${String(Math.max(1, totalRounds - index)).padStart(2, "0")}`;
  const key = round.slug || `round-${index}`;

  let body;
  if (!round.traded && round.result !== "pending") {
    body = `
      <div class="no-trade-body">
        <p class="no-trade-note">本轮没有成交，没有产生盈亏。</p>
        ${round.actions.length ? `
          <details data-key="${escapeHtml(key)}-empty" ${openDetails.has(`${key}-empty`) ? "open" : ""}>
            <summary>查看 ${round.actions.length} 次挂单</summary>
            ${renderTimeline(round.actions, `${key}-nested`, true)}
          </details>` : ""}
      </div>`;
  } else {
    body = `<div class="round-body">${renderTimeline(round.actions, key)}${renderSummary(round)}</div>`;
  }

  return `
    <article class="round-card ${round.result === "pending" ? "is-active" : ""} ${!round.traded ? "is-empty" : ""}">
      <header class="round-head">
        <div class="round-time-wrap">
          <span class="round-index">${sequence}</span>
          <p class="round-time">
            ${hasTime ? `${start.minute}—${end.minute}<span class="round-date">${start.date}</span>` : "本轮时间待同步"}
          </p>
        </div>
        <span class="result-badge ${round.result}">${RESULT_LABELS[round.result] || "已结算"}</span>
        <p class="round-pnl ${resultClass}">${pnlText}</p>
      </header>
      ${body}
    </article>`;
}

function renderMetrics(data) {
  const pnl = Number(data.total_pnl) || 0;
  const average = Number(data.avg_pnl) || 0;
  const pnlElement = document.getElementById("kpi-pnl");
  const averageElement = document.getElementById("kpi-average");
  pnlElement.textContent = money(pnl, true);
  pnlElement.className = `metric-value ${pnl > 0 ? "profit" : pnl < 0 ? "loss" : ""}`;
  averageElement.textContent = money(average, true);
  averageElement.className = `metric-value ${average > 0 ? "profit" : average < 0 ? "loss" : ""}`;
  document.getElementById("kpi-winrate").textContent = `${(Number(data.win_rate) || 0).toFixed(1)}%`;
  document.getElementById("kpi-winloss").textContent = `${data.wins || 0} 赢 · ${data.losses || 0} 亏`;
  document.getElementById("kpi-pnl-foot").textContent = `${data.traded_rounds || 0} 轮有成交的累计结果`;
  document.getElementById("settled-note").textContent = `已结算 ${data.total_rounds || 0} 轮`;
}

function renderError() {
  roundsElement.innerHTML = `
    <div class="error-state">
      <h3>暂时无法读取收益数据</h3>
      <p>请确认看板服务正在运行，页面会自动重试。</p>
      <button type="button" onclick="updateDashboard()">立即重试</button>
    </div>`;
}

async function updateDashboard() {
  if (requestPending) return;
  requestPending = true;
  try {
    const response = await fetch("/api/history", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderMetrics(data);

    const rounds = Array.isArray(data.rounds) ? data.rounds : [];
    if (!rounds.length) {
      roundsElement.innerHTML = "";
      roundsElement.appendChild(document.getElementById("empty-template").content.cloneNode(true));
    } else {
      const activeOffset = rounds[0]?.result === "pending" ? 1 : 0;
      roundsElement.innerHTML = rounds.map((round, index) =>
        renderRound(round, index - activeOffset, data.total_rounds || rounds.length)
      ).join("");
    }

    const now = new Date();
    document.getElementById("updated-at").textContent = `更新于 ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
    document.getElementById("connection").className = "connection is-online";
    document.getElementById("connection-text").textContent = "自动更新中";
  } catch (error) {
    console.error("Dashboard update failed:", error);
    document.getElementById("connection").className = "connection is-offline";
    document.getElementById("connection-text").textContent = "连接中断";
    if (!roundsElement.querySelector(".round-card")) renderError();
  } finally {
    requestPending = false;
  }
}

roundsElement.addEventListener("toggle", event => {
  const details = event.target.closest("details[data-key]");
  if (!details) return;
  if (details.open) openDetails.add(details.dataset.key);
  else openDetails.delete(details.dataset.key);
}, true);

updateDashboard();
setInterval(updateDashboard, 2500);
