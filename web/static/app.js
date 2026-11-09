// PM-Quant Institutional Dashboard Logic & Real-time Canvas Rendering

let currentTab = "history";
let paramsLoaded = false;

function switchTab(tab) {
  currentTab = tab;
  document.getElementById("tab-btn-history").className = `tab-pill ${tab === "history" ? "active" : ""}`;
  document.getElementById("tab-btn-params").className = `tab-pill ${tab === "params" ? "active" : ""}`;
  document.getElementById("tab-history").style.display = tab === "history" ? "block" : "none";
  document.getElementById("tab-params").style.display = tab === "params" ? "block" : "none";

  if (tab === "params" && !paramsLoaded) {
    loadParams();
  }
}

async function loadParams() {
  try {
    const res = await fetch("/api/params");
    if (!res.ok) return;
    const data = await res.json();
    const container = document.getElementById("params-container");
    if (!data.params) return;

    let html = "";
    for (const [k, v] of Object.entries(data.params)) {
      html += `
        <div class="param-item">
          <span class="param-name">${k}</span>
          <span class="param-val">${v}</span>
        </div>
      `;
    }
    container.innerHTML = html;
    paramsLoaded = true;
  } catch (err) {
    console.error("Failed to load parameters:", err);
  }
}

async function updateLive() {
  const t0 = performance.now();
  try {
    const res = await fetch("/api/live");
    if (!res.ok) return;
    const data = await res.json();

    const ping = Math.round(performance.now() - t0);
    document.getElementById("poll-ping").textContent = `${ping}ms poll`;

    if (data.batch_id) {
      document.getElementById("batch-id").textContent = data.batch_id;
    }

    if (data.status === "idle") {
      document.getElementById("active-slug").textContent = "Engine Standby (Waiting for Window)";
      document.getElementById("live-spot").textContent = "--";
      document.getElementById("live-delta").textContent = "Open: --";
      document.getElementById("live-tau").textContent = "--";
      document.getElementById("live-pulse").style.background = "var(--text-tertiary)";
      document.getElementById("progress-fill").style.width = "0%";
      return;
    }

    document.getElementById("live-pulse").style.background = "var(--up)";
    document.getElementById("active-slug").textContent = data.slug || "--";

    // Progress bar & Phase badge
    const tau = data.tau_s !== undefined ? data.tau_s : 0;
    const elapsed = Math.max(0, 300 - tau);
    const pct = Math.max(0, Math.min(100, (tau / 300) * 100));
    const fillEl = document.getElementById("progress-fill");
    fillEl.style.width = `${pct}%`;
    document.getElementById("progress-text").textContent = `${Math.round(elapsed)}s elapsed (${Math.round(tau)}s left)`;

    const phaseEl = document.getElementById("phase-badge");
    if (tau <= 60) {
      phaseEl.textContent = `Phase: Tail Freeze (${Math.round(tau)}s)`;
      phaseEl.className = "badge badge-warning mono";
      fillEl.className = "progress-fill freeze";
    } else {
      phaseEl.textContent = `Phase: Active Quoting (${Math.round(tau)}s)`;
      phaseEl.className = "badge badge-accent mono";
      fillEl.className = "progress-fill";
    }

    // Spot & Delta vs Open
    const spot = data.spot || 0;
    const openPx = data.open_px || 0;
    const spotEl = document.getElementById("live-spot");
    spotEl.textContent = spot > 0 ? `$${spot.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : "--";

    const deltaEl = document.getElementById("live-delta");
    if (openPx > 0 && spot > 0) {
      const diff = spot - openPx;
      const bps = (diff / openPx) * 10000;
      const sign = diff >= 0 ? "+" : "";
      const arrow = diff >= 0 ? "▲" : "▼";
      const dirClass = diff >= 0 ? "text-up" : "text-down";
      deltaEl.innerHTML = `Open: $${openPx.toLocaleString("en-US", {minimumFractionDigits: 2})} <span class="${dirClass}">(${arrow} ${sign}$${diff.toFixed(2)} / ${sign}${bps.toFixed(1)} bps)</span>`;
    } else {
      deltaEl.textContent = openPx > 0 ? `Open: $${openPx.toLocaleString("en-US", {minimumFractionDigits: 2})}` : "Open: --";
    }

    document.getElementById("live-tau").textContent = `${tau.toFixed(1)}s`;

    // Order books
    const bUp = data.book_up || {};
    const bDn = data.book_down || {};
    const upBidStr = bUp.best_bid !== undefined && bUp.best_bid !== null ? `$${bUp.best_bid.toFixed(2)}` : "--";
    const upAskStr = bUp.best_ask !== undefined && bUp.best_ask !== null ? `$${bUp.best_ask.toFixed(2)}` : "--";
    const dnBidStr = bDn.best_bid !== undefined && bDn.best_bid !== null ? `$${bDn.best_bid.toFixed(2)}` : "--";
    const dnAskStr = bDn.best_ask !== undefined && bDn.best_ask !== null ? `$${bDn.best_ask.toFixed(2)}` : "--";

    document.getElementById("live-book-up").textContent = `Bid: ${upBidStr} | Ask: ${upAskStr}`;
    document.getElementById("live-book-down").textContent = `Bid: ${dnBidStr} | Ask: ${dnAskStr}`;

    // Inventory & Orders
    if (data.pos_up) {
      const upAvg = data.pos_up.avg_px ? `@ $${data.pos_up.avg_px.toFixed(2)}` : "";
      document.getElementById("live-up").textContent = `${data.pos_up.shares} shares ($${data.pos_up.cash.toFixed(2)}) ${upAvg}`;
    }
    if (data.pos_down) {
      const dnAvg = data.pos_down.avg_px ? `@ $${data.pos_down.avg_px.toFixed(2)}` : "";
      document.getElementById("live-down").textContent = `${data.pos_down.shares} shares ($${data.pos_down.cash.toFixed(2)}) ${dnAvg}`;
    }

    // Working orders
    const workingEl = document.getElementById("live-working");
    if (data.working_orders && data.working_orders.length > 0) {
      workingEl.textContent = data.working_orders.map(o => `BUY ${o.side} ${o.size} @ $${o.price.toFixed(2)}`).join(", ");
    } else {
      workingEl.textContent = "None";
    }

    // Decision badge
    const badgeEl = document.getElementById("live-action-badge");
    const reasonEl = document.getElementById("live-reason");
    if (data.last_decision) {
      const dec = data.last_decision;
      const act = (dec.action || "HOLD").toUpperCase();
      const side = dec.side ? ` ${dec.side}` : "";
      const px = dec.price ? ` @ $${dec.price.toFixed(2)}` : "";
      badgeEl.textContent = `${act}${side}${px}`;

      if (act === "QUOTE") {
        badgeEl.className = "badge badge-accent mono";
      } else if (act.includes("HEDGE") || act.includes("UNWIND")) {
        badgeEl.className = "badge badge-warning mono";
      } else {
        badgeEl.className = "badge badge-neutral mono";
      }
      reasonEl.textContent = dec.reason || "";
    } else {
      badgeEl.textContent = "OBSERVING";
      badgeEl.className = "badge badge-neutral mono";
      reasonEl.textContent = "Monitoring market condition";
    }

  } catch (err) {
    console.error("Live update error:", err);
  }
}

async function updateHistory() {
  try {
    const res = await fetch("/api/history");
    if (!res.ok) return;
    const data = await res.json();

    // Summary KPIs
    const pnlEl = document.getElementById("kpi-pnl");
    pnlEl.textContent = `${data.total_pnl >= 0 ? "+" : ""}$${data.total_pnl.toFixed(2)}`;
    pnlEl.className = `card-value mono ${data.total_pnl > 0 ? "text-up" : data.total_pnl < 0 ? "text-down" : ""}`;

    document.getElementById("kpi-avg-pnl").textContent = `Avg: ${data.avg_pnl >= 0 ? "+" : ""}$${data.avg_pnl.toFixed(3)} / traded session`;
    document.getElementById("kpi-winrate").textContent = `${data.win_rate.toFixed(1)}%`;
    document.getElementById("kpi-winloss").textContent = `${data.wins} Wins ${data.losses} Losses`;
    document.getElementById("kpi-windows").textContent = `${data.traded_windows || 0} (${data.total_windows || 0})`;

    let totalInsurance = 0;
    if (data.windows && data.windows.length > 0) {
      totalInsurance = data.windows.reduce((acc, w) => acc + (w.insurance_paid || 0), 0);
    }
    document.getElementById("kpi-insurance").textContent = `$${totalInsurance.toFixed(2)}`;

    // Draw Cumulative Equity Curve SVG
    renderEquityCurve(data.windows || []);

    // Table update
    const tbody = document.getElementById("history-tbody");
    if (!data.windows || data.windows.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="8" style="text-align: center; color: var(--text-tertiary); padding: 32px 0;">
            No settled windows recorded yet in this revision.
          </td>
        </tr>
      `;
      return;
    }

    let rowsHtml = "";
    for (const w of data.windows) {
      const pnl = w.net_pnl || 0.0;
      const isWin = pnl > 0;
      const pnlClass = isWin ? "text-up" : pnl < 0 ? "text-down" : "";
      const winnerBadge = w.winner === "Up" 
        ? `<span class="badge badge-up">UP</span>` 
        : `<span class="badge badge-down">DOWN</span>`;

      rowsHtml += `
        <tr>
          <td class="mono">${w.slug}</td>
          <td>${winnerBadge}</td>
          <td class="mono">$${(w.open_px || 0).toLocaleString("en-US", {minimumFractionDigits: 2})}</td>
          <td class="mono">$${(w.close_px || 0).toLocaleString("en-US", {minimumFractionDigits: 2})}</td>
          <td class="mono">${w.pos_up || 0} / ${w.pos_down || 0}</td>
          <td class="mono">$${(w.total_cost || 0).toFixed(2)}</td>
          <td class="mono">$${(w.proceeds || 0).toFixed(2)}</td>
          <td class="mono ${pnlClass}"><strong>${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}</strong></td>
        </tr>
      `;
    }
    tbody.innerHTML = rowsHtml;

  } catch (err) {
    console.error("History fetch error:", err);
  }
}

function renderEquityCurve(windows) {
  if (!windows || windows.length < 2) {
    document.getElementById("equity-path").setAttribute("d", "");
    return;
  }

  // Windows are sorted in descending order (latest first), reverse for chronological equity
  const chronological = [...windows].reverse();
  const points = [0];
  let cum = 0;
  for (const w of chronological) {
    cum += (w.net_pnl || 0);
    points.push(cum);
  }

  const minVal = Math.min(0, ...points);
  const maxVal = Math.max(0.1, ...points);
  const range = (maxVal - minVal) || 1.0;

  const width = 1000;
  const height = 120;
  const padding = 15;

  let d = "";
  for (let i = 0; i < points.length; i++) {
    const x = (i / (points.length - 1)) * width;
    // Invert y since svg 0 is top
    const normalizedY = (points[i] - minVal) / range;
    const y = height - padding - (normalizedY * (height - 2 * padding));
    d += (i === 0 ? `M ${x.toFixed(1)} ${y.toFixed(1)}` : ` L ${x.toFixed(1)} ${y.toFixed(1)}`);
  }

  const pathEl = document.getElementById("equity-path");
  pathEl.setAttribute("d", d);
  const finalPnl = points[points.length - 1];
  pathEl.setAttribute("stroke", finalPnl >= 0 ? "var(--up)" : "var(--down)");
}

// Initial bootstrap and timers
updateLive();
updateHistory();
setInterval(updateLive, 1000);
setInterval(updateHistory, 3000);
