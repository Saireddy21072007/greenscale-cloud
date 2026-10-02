/* Front-end glue for GreenScale Cloud 2.0.
 *
 * No framework on purpose. Every page is server-rendered by Flask; this file only
 * adds the charts and the few fetch() calls that hit the JSON API. Keeping it
 * plain meant we could explain all of it during the review.
 */
const GS = (() => {
  const PALETTE = ["#46c98b", "#62a8e0", "#e0b243", "#e0645d", "#a78bfa",
                   "#4fd1c5", "#f28b82", "#9ccc65", "#ff9e6d"];

  const fmt = (n, d = 3) => (n === null || n === undefined ? "—" : Number(n).toFixed(d));
  const el = (html) => { const d = document.createElement("div"); d.innerHTML = html; return d.firstElementChild; };

  async function api(path, options) {
    const res = await fetch(path, options);
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || `request failed (${res.status})`);
    return body;
  }

  const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { labels: { color: "#8aa598", boxWidth: 12, font: { size: 11 } } } },
    scales: {
      x: { ticks: { color: "#8aa598", font: { size: 10 } }, grid: { color: "#27392f" } },
      y: { ticks: { color: "#8aa598", font: { size: 10 } }, grid: { color: "#27392f" } },
    },
  };

  // ---------------------------------------------------------------- charts
  async function drawCarbonCurves(canvasId) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const curves = await api("/api/carbon/curves");
    const labels = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, "0"));
    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: Object.entries(curves).map(([rid, values], i) => ({
          label: rid,
          data: values,
          borderColor: PALETTE[i % PALETTE.length],
          backgroundColor: PALETTE[i % PALETTE.length],
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.35,
        })),
      },
      options: {
        ...chartDefaults,
        scales: {
          ...chartDefaults.scales,
          y: { ...chartDefaults.scales.y, title: { display: true, text: "gCO₂ / kWh", color: "#8aa598" } },
        },
      },
    });
  }

  function drawRegionMix(canvasId, byRegion) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const entries = Object.entries(byRegion || {});
    if (!entries.length) {
      canvas.replaceWith(el('<p class="muted">No jobs scheduled yet.</p>'));
      return;
    }
    new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: entries.map(([rid]) => rid),
        datasets: [{
          data: entries.map(([, s]) => s.tasks),
          backgroundColor: entries.map((_, i) => PALETTE[i % PALETTE.length]),
          borderColor: "#16211d",
          borderWidth: 2,
        }],
      },
      options: { responsive: true, maintainAspectRatio: false,
                 plugins: { legend: { position: "right", labels: { color: "#8aa598", boxWidth: 12, font: { size: 11 } } } } },
    });
  }

  // ------------------------------------------------------------- dashboard
  function wireSeedButton(btnId, statusId) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    const status = document.getElementById(statusId);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      status.textContent = " mining blocks…";
      try {
        await api("/api/simulate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ n: 100, seed: 42, policy: "greenscale" }),
        });
        location.reload();
      } catch (err) {
        status.textContent = ` ${err.message}`;
        btn.disabled = false;
      }
    });
  }

  // ------------------------------------------------------------- scheduler
  function collectWeights() {
    const weights = {};
    document.querySelectorAll("input.weight").forEach((slider) => {
      weights[slider.dataset.name] = parseFloat(slider.value);
    });
    return weights;
  }

  function wireSchedulerForm() {
    const form = document.getElementById("job-form");
    if (!form) return;

    document.querySelectorAll("input.weight").forEach((slider) => {
      slider.addEventListener("input", () => {
        document.getElementById(`w-${slider.dataset.name}-val`).textContent = slider.value;
      });
    });

    const policySelect = document.getElementById("policy");
    policySelect.addEventListener("change", () => {
      document.getElementById("policy-note").textContent = GS.POLICY_NOTES[policySelect.value] || "";
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const btn = document.getElementById("submit-btn");
      const out = document.getElementById("result");
      btn.disabled = true;
      out.innerHTML = '<p class="muted" style="margin-top:20px">Scoring candidates and mining a block…</p>';

      const body = Object.fromEntries(new FormData(form).entries());
      body.weights = collectWeights();

      try {
        const d = await api("/api/schedule", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        out.innerHTML = renderDecision(d);
      } catch (err) {
        out.innerHTML = `<div class="banner no" style="margin-top:20px"><strong>Cannot schedule.</strong> ${err.message}</div>`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderDecision(d) {
    const c = d.chosen, b = d.baseline;
    const saved = d.carbon_saved_pct;
    const rows = d.candidates.slice(0, 14).map((k) => `
      <tr class="${k.region_id === c.region_id && k.start_hour === c.start_hour ? "highlight" : ""}">
        <td>${k.region_label}</td>
        <td class="num">${String(k.start_hour).padStart(2, "0")}:00</td>
        <td class="num">${k.hours_deferred}</td>
        <td class="num">${fmt(k.carbon_intensity, 0)}</td>
        <td class="num">${fmt(k.energy_kwh, 4)}</td>
        <td class="num">${fmt(k.carbon_kg, 4)}</td>
        <td class="num">$${fmt(k.cost_usd, 3)}</td>
        <td class="num">${fmt(k.latency_ms, 0)}</td>
        <td class="num"><strong>${fmt(k.score, 4)}</strong></td>
      </tr>`).join("");

    return `
    <h2>Decision</h2>
    <div class="grid cols-4">
      <div class="panel kpi"><div class="label">Placed in</div>
        <div class="value" style="font-size:19px">${c.region_id}</div>
        <div class="foot">${c.provider} · start ${String(c.start_hour).padStart(2, "0")}:00 UTC
          ${c.hours_deferred ? `· deferred ${c.hours_deferred} h` : "· immediate"}</div></div>
      <div class="panel kpi"><div class="label">CO₂ avoided</div>
        <div class="value ${saved > 0 ? "good" : ""}">${fmt(saved, 1)}<span class="unit">%</span></div>
        <div class="foot">${fmt(d.carbon_saved_kg, 4)} kg vs ${b ? b.region_id : "baseline"}</div></div>
      <div class="panel kpi"><div class="label">Cost impact</div>
        <div class="value ${d.cost_delta_usd <= 0 ? "good" : "warn"}">${d.cost_delta_usd <= 0 ? "−" : "+"}$${fmt(Math.abs(d.cost_delta_usd), 3)}</div>
        <div class="foot">job costs $${fmt(c.cost_usd, 3)}</div></div>
      <div class="panel kpi"><div class="label">SLA</div>
        <div class="value ${d.sla_met ? "good" : "bad"}" style="font-size:19px">${d.sla_met ? "met" : "violated"}</div>
        <div class="foot">${d.sla_notes.length ? d.sla_notes.join("; ") : `${fmt(c.latency_ms, 0)} ms within budget`}</div></div>
    </div>

    <div class="panel" style="margin-top:16px">
      <h3>Written to the chain</h3>
      <p style="margin:0">
        Block <strong>#${d.ledger.block_index}</strong> · nonce ${d.ledger.nonce} ·
        ${d.ledger.transactions.map((t) => `<span class="pill info">${t.type}</span>`).join(" ")}
      </p>
      <p style="margin:8px 0 0"><code class="hash">${d.ledger.block_hash}</code></p>
      <p class="note">
        Predicted runtime ${fmt(d.prediction.runtime_hours, 2)} h at
        ${fmt(d.prediction.utilisation * 100, 0)}% CPU, from the
        <strong>${d.prediction.source}</strong> predictor.
      </p>
    </div>

    <h2>Candidates the scheduler scored</h2>
    <div class="panel table-scroll">
      <table>
        <thead><tr>
          <th>Region</th><th class="num">Start</th><th class="num">Defer</th>
          <th class="num">gCO₂/kWh</th><th class="num">kWh</th><th class="num">kg CO₂</th>
          <th class="num">Cost</th><th class="num">Latency</th><th class="num">Score</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="note">
        Top ${Math.min(14, d.candidates.length)} of ${d.candidates.length} candidates, best score first.
        Lower score wins. Weights in use: ${Object.entries(d.weights).map(([k, v]) => `${k} ${fmt(v, 2)}`).join(" · ")}.
      </p>
    </div>`;
  }

  // ------------------------------------------------------------- migration
  function wireMigration() {
    const btn = document.getElementById("mig-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const out = document.getElementById("mig-result");
      btn.disabled = true;
      out.innerHTML = '<p class="muted">Evaluating…</p>';
      try {
        const r = await api("/api/migration/evaluate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            current_region: document.getElementById("mig-region").value,
            utc_hour: parseInt(document.getElementById("mig-hour").value, 10),
            vcpus: parseInt(document.getElementById("vcpus").value, 10),
            memory_gb: parseFloat(document.getElementById("memory_gb").value),
            task_type: document.getElementById("task_type").value,
            latency_budget_ms: parseFloat(document.getElementById("latency_budget_ms").value),
            residency_zone: document.getElementById("residency_zone").value,
          }),
        });
        out.innerHTML = `
          <div class="banner ${r.should_migrate ? "ok" : "no"}" style="margin:14px 0 0">
            <strong>${r.should_migrate ? `Migrate to ${r.to_region}` : "Stay put"}</strong><br>
            <span class="muted">${r.reason}</span>
          </div>
          <table style="margin-top:10px">
            <tr><th>Stay</th><td class="num">${fmt(r.carbon_if_stay_kg, 4)} kg</td></tr>
            <tr><th>Move</th><td class="num">${fmt(r.carbon_if_move_kg, 4)} kg</td></tr>
            <tr><th>Migration cost</th><td class="num">${fmt(r.migration_cost_kg, 4)} kg</td></tr>
            <tr><th>Net saving</th><td class="num ${r.net_saving_kg > 0 ? "good" : ""}">${fmt(r.net_saving_kg, 4)} kg</td></tr>
            <tr><th>Downtime</th><td class="num">${fmt(r.downtime_seconds, 2)} s</td></tr>
          </table>`;
      } catch (err) {
        out.innerHTML = `<div class="banner no" style="margin:14px 0 0">${err.message}</div>`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  // --------------------------------------------------------------- compare
  function wireCompare() {
    const btn = document.getElementById("run-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const status = document.getElementById("status");
      const out = document.getElementById("out");
      btn.disabled = true;
      status.textContent = "running…";
      try {
        const n = document.getElementById("n").value;
        const seed = document.getElementById("seed").value;
        const data = await api(`/api/compare?n=${n}&seed=${seed}`);
        out.innerHTML = renderCompare(data);
        drawCompareChart(data);
        status.textContent = "";
      } catch (err) {
        out.innerHTML = `<div class="banner no" style="margin-top:16px">${err.message}</div>`;
        status.textContent = "";
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderCompare(data) {
    const rows = data.results.map((r) => `
      <tr class="${r.policy === "greenscale" ? "highlight" : ""}">
        <td><strong>${r.policy}</strong><br><span class="muted" style="white-space:normal">${r.note || ""}</span></td>
        <td class="num">${fmt(r.carbon_kg, 2)}</td>
        <td class="num ${r.carbon_saving_pct > 0 ? "good" : (r.carbon_saving_pct < 0 ? "bad" : "")}">${fmt(r.carbon_saving_pct, 1)}%</td>
        <td class="num">$${fmt(r.cost_usd, 2)}</td>
        <td class="num ${r.cost_change_pct <= 0 ? "good" : "warn"}">${fmt(r.cost_change_pct, 1)}%</td>
        <td class="num">${fmt(r.avg_deferral_hours, 2)}</td>
        <td class="num">${fmt(r.sla_compliance_pct, 1)}%</td>
        <td class="num">${r.unplaceable}</td>
      </tr>`).join("");

    return `
    <h2>Results over ${data.trace_size} jobs (seed ${data.seed})</h2>
    <div class="panel table-scroll">
      <table>
        <thead><tr>
          <th style="min-width:260px">Policy</th><th class="num">kg CO₂</th><th class="num">CO₂ saved</th>
          <th class="num">Cost</th><th class="num">Cost change</th>
          <th class="num">Avg delay (h)</th><th class="num">SLA</th><th class="num">Unplaceable</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="note">Savings and cost changes are relative to <strong>${data.baseline_policy}</strong>.</p>
    </div>
    <div class="panel" style="margin-top:16px">
      <h3>Carbon against average delay</h3>
      <div class="chart-box"><canvas id="cmpChart"></canvas></div>
      <p class="note">
        The interesting comparison is greenscale against carbon_only: chasing the last
        few percent of CO₂ costs several hours of average delay per job, which is what
        makes a pure carbon policy unshippable for a real customer.
      </p>
    </div>`;
  }

  function drawCompareChart(data) {
    const canvas = document.getElementById("cmpChart");
    if (!canvas) return;
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: data.results.map((r) => r.policy),
        datasets: [
          { label: "kg CO₂", data: data.results.map((r) => r.carbon_kg),
            backgroundColor: "#46c98b", yAxisID: "y" },
          { label: "avg delay (h)", data: data.results.map((r) => r.avg_deferral_hours || 0),
            backgroundColor: "#e0b243", yAxisID: "y1", type: "line",
            borderColor: "#e0b243", pointRadius: 4 },
        ],
      },
      options: {
        ...chartDefaults,
        scales: {
          x: chartDefaults.scales.x,
          y: { ...chartDefaults.scales.y, position: "left", title: { display: true, text: "kg CO₂", color: "#8aa598" } },
          y1: { ...chartDefaults.scales.y, position: "right", grid: { drawOnChartArea: false },
                title: { display: true, text: "avg delay (h)", color: "#8aa598" } },
        },
      },
    });
  }

  // ---------------------------------------------------------------- ledger
  function wireTamper() {
    const btn = document.getElementById("tamper-btn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const out = document.getElementById("tamper-out");
      btn.disabled = true;
      out.innerHTML = '<p class="muted">Forging a record…</p>';
      try {
        const r = await api("/api/ledger/tamper-test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ field: "carbon_kg", value: 0.0 }),
        });
        out.innerHTML = `
          <table style="margin-top:12px">
            <tr><th>Edited</th><td>block #${r.edited.block_index}, ${r.edited.tx_id}</td></tr>
            <tr><th>Field</th><td><code>${r.edited.field}</code>: ${r.edited.original_value} → ${r.edited.forged_value}</td></tr>
            <tr><th>Before edit</th><td><span class="pill ok">valid</span> ${r.before.checked_blocks} blocks checked</td></tr>
            <tr><th>After edit</th><td><span class="pill no">invalid</span> ${r.after.problems.length} problem(s) detected</td></tr>
          </table>
          <ul class="note">${r.after.problems.map((p) => `<li>${p}</li>`).join("")}</ul>
          <p class="note">${r.note}</p>`;
      } catch (err) {
        out.innerHTML = `<div class="banner no" style="margin-top:12px">${err.message}</div>`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  return { drawCarbonCurves, drawRegionMix, wireSeedButton, wireSchedulerForm,
           wireMigration, wireCompare, wireTamper, POLICY_NOTES: {} };
})();
