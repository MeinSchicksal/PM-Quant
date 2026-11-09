// PM-Quant Dashboard Polling and DOM Updates

async function updateLive() {
  try {
    const res = await fetch("/api/live");
    if (!res.ok) return;
    const data = await res.json();

    if (data.batch_id) {
      document.getElementById("batch-id").textContent = data.batch_id;
    }

    if (data.status === "idle") {
      document.getElementById("active-slug").textContent = "Idle (Waiting for Window)";
      document.getElementById("live-spot").textContent = "--";
      document.getElementById("live-open").textContent = "--";
      document.getElementById("live-tau").textContent = "--";
      document.getElementById("live-action").textContent = "--";
      document.getElementById("live-reason").textContent = "Standby";
      document.getElementById("live-pulse").style.background = "var(--text-tertiary)";
      return;
    }

    document.getElementById("live-pulse").style.background = "var(--up)";
    document.getElementById("active-slug").textContent = data.slug || "--";
    document.getElementById("live-spot").textContent = data.spot ? `$${data.spot.toFixed(2)}` : "--";
    document.getElementById("live-open").textContent = data.open_px ? `$${data.open_px.toFixed(2)}` : "--";
    document.getElementById("live-tau").textContent = data.tau_s !== undefined ? `${data.tau_s.toFixed(1)}s` : "--";

    if (data.pos_up) {
      document.getElementById("live-up").textContent = `${data.pos_up.shares} shares ($${data.pos_up.cash.toFixed(2)})`;
    }
    if (data.pos_down) {
      document.getElementById("live-down").textContent = `${data.pos_down.shares} shares ($${data.pos_down.cash.toFixed(2)})`;
    }
    document.getElementById("live-fills").textContent = data.fills_count || 0;

    if (data.last_decision) {
      const dec = data.last_decision;
      const actText = dec.action === "hold" ? "Hold" : `${dec.action.toUpperCase()} ${dec.side} @ $${dec.price?.toFixed(2) || ""}`;
      document.getElementById("live-action").textContent = actText;
      document.getElementById("live-reason").textContent = dec.reason || "";
    } else {
      document.getElementById("live-action").textContent = "Observing";
      document.getElementById("live-reason").textContent = "";
    }
  } catch (err) {
    console.error("Live fetch failed:", err);
  }
}

async function updateHistory() {
  try {
    const res = await fetch("/api/history");
    if (!res.ok) return;
    const data = await res.json();

    // Update KPI summary cards
    const pnlEl = document.getElementById("kpi-pnl");
    pnlEl.textContent = `${data.total_pnl >= 0 ? "+" : ""}$${data.total_pnl.toFixed(2)}`;
    pnlEl.className = `card-value mono ${data.total_pnl > 0 ? "text-up" : data.total_pnl < 0 ? "text-down" : ""}`;

    document.getElementById("kpi-avg-pnl").textContent = `Avg: ${data.avg_pnl >= 0 ? "+" : ""}$${data.avg_pnl.toFixed(3)} / window`;
    document.getElementById("kpi-winrate").textContent = `${data.win_rate.toFixed(1)}%`;
    document.getElementById("kpi-winloss").textContent = `${data.wins} Wins ${data.losses} Losses`;
    document.getElementById("kpi-windows").textContent = data.total_windows;

    let totalInsurance = 0;
    if (data.windows && data.windows.length > 0) {
      totalInsurance = data.windows.reduce((acc, w) => acc + (w.insurance_paid || 0), 0);
    }
    document.getElementById("kpi-insurance").textContent = `$${totalInsurance.toFixed(2)}`;

    // Update Table
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
          <td class="mono">$${(w.open_px || 0).toFixed(2)}</td>
          <td class="mono">$${(w.close_px || 0).toFixed(2)}</td>
          <td class="mono">${w.pos_up || 0} / ${w.pos_down || 0}</td>
          <td class="mono">$${(w.total_cost || 0).toFixed(2)}</td>
          <td class="mono">$${(w.proceeds || 0).toFixed(2)}</td>
          <td class="mono ${pnlClass}"><strong>${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}</strong></td>
        </tr>
      `;
    }
    tbody.innerHTML = rowsHtml;
  } catch (err) {
    console.error("History fetch failed:", err);
  }
}

// Initial fetch and loop
updateLive();
updateHistory();
setInterval(updateLive, 1000);
setInterval(updateHistory, 3000);
