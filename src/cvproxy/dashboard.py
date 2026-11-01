"""HTML dashboard template for CVProxy stats."""

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>CVProxy — Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
  <link href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/1.11.3/font/bootstrap-icons.min.css" rel="stylesheet" integrity="sha512-dPXYcDub/aeb08c63jRq/k6GaKccl256JQy/AnOq7CAnEZ9FzSL9wSbcZkMp4R26vBsMLFYH4kQ67/bbV8XaCQ==" crossorigin="anonymous">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js" integrity="sha512-CQBWl4fJHWbryGE+Pc7UAxWMUMNMWzWxF4SQo9CgkJIN1kx6djDQZjh3Y8SZ1d+6I+1zze6Z7kHXO7q3UyZAWw==" crossorigin="anonymous"></script>
  <style>
    body { background: var(--bs-body-bg); }
    .metric { display:flex; align-items:center; gap:.75rem; }
    .metric .bi { font-size:1.6rem; opacity:.8; }
    .metric .value { font-size:1.9rem; font-weight:700; line-height:1; }
    .metric .label { color: var(--bs-secondary-color); font-size:.85rem; }
    .chart-card canvas { width:100% !important; height:320px !important; }
    .kpis .card { transition: transform .15s ease; }
    .kpis .card:hover { transform: translateY(-2px); }
    .table-sm td, .table-sm th { padding: .35rem .5rem; }
    .hit-rate { font-variant-numeric: tabular-nums; font-weight: 600; }
    .hit-rate.good { color: var(--bs-success); }
    .hit-rate.warn { color: var(--bs-warning); }
    .hit-rate.bad { color: var(--bs-danger); }
    .source-badge { font-size: .75rem; }
    .footer-note { color: var(--bs-secondary-color); }
    #clientsTable tbody tr:hover { background: rgba(var(--bs-primary-rgb), .08); }
    .small { font-size: .8rem; }
    .job-desc { max-width: 56rem; line-height: 1.3; }
    .job-log { max-height: 320px; overflow: auto; }
  </style>
</head>
<body>
  <nav class="navbar navbar-expand-lg bg-body-tertiary border-bottom">
    <div class="container">
      <a class="navbar-brand fw-semibold" href="/dashboard">
        <i class="bi bi-lightning-charge-fill me-2"></i>CVProxy
      </a>
      <div class="ms-auto d-flex align-items-center gap-2">
        <ul class="nav nav-pills nav-sm me-2" id="mainTabs">
          <li class="nav-item"><a class="nav-link active py-1 px-2" data-tab="stats" href="#">Stats</a></li>
          <li class="nav-item"><a class="nav-link py-1 px-2" data-tab="jobs" href="#">Jobs <span class="badge text-bg-secondary ms-1" id="jobsBadge" style="display:none"></span></a></li>
        </ul>
        <div class="btn-group btn-group-sm" role="group" aria-label="Time filter" id="timeFilterGroup">
          <button type="button" class="btn btn-outline-secondary time-btn" data-hours="1">1h</button>
          <button type="button" class="btn btn-outline-secondary time-btn active" data-hours="6">6h</button>
          <button type="button" class="btn btn-outline-secondary time-btn" data-hours="24">24h</button>
          <button type="button" class="btn btn-outline-secondary time-btn" data-hours="168">7d</button>
          <button type="button" class="btn btn-outline-secondary time-btn" data-hours="720">30d</button>
          <button type="button" class="btn btn-outline-secondary time-btn" data-hours="">All</button>
        </div>
        <button id="refreshBtn" class="btn btn-sm btn-outline-primary" title="Refresh now">
          <i class="bi bi-arrow-clockwise"></i>
        </button>
      </div>
    </div>
  </nav>

  <main class="container my-4">

    <!-- KPIs -->
    <div class="row g-3 kpis">
      <div class="col-6 col-md-3">
        <div class="card h-100"><div class="card-body">
          <div class="metric"><i class="bi bi-arrow-left-right text-primary"></i>
            <div><div class="value" id="totalRequests">0</div><div class="label">Total Requests</div></div>
          </div>
        </div></div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card h-100"><div class="card-body">
          <div class="metric"><i class="bi bi-database-check text-success"></i>
            <div><div class="value" id="cacheHits">0</div><div class="label">Cache Hits</div></div>
          </div>
        </div></div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card h-100"><div class="card-body">
          <div class="metric"><i class="bi bi-cloud-arrow-up text-warning"></i>
            <div><div class="value" id="upstreamCalls">0</div><div class="label">Upstream Calls</div></div>
          </div>
        </div></div>
      </div>
      <div class="col-6 col-md-3">
        <div class="card h-100"><div class="card-body">
          <div class="metric"><i class="bi bi-speedometer2 text-info"></i>
            <div><div class="value hit-rate" id="hitRate">0%</div><div class="label">Cache Hit Rate</div></div>
          </div>
        </div></div>
      </div>
    </div>

    <!-- CV API Quota -->
    <div class="row g-3 mt-1">
      <div class="col-12">
        <div class="card">
          <div class="card-header fw-semibold"><i class="bi bi-cloud-upload me-2"></i>CV API Hourly Quota <span class="badge text-bg-secondary ms-1" style="font-size:.75rem">rolling 60 min</span></div>
          <div class="card-body" id="quotaSection">
            <span class="text-secondary small">No upstream calls yet this hour.</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Charts row -->
    <div class="row g-3 mt-1">
      <div class="col-12 col-lg-5">
        <div class="card h-100 chart-card">
          <div class="card-header fw-semibold"><i class="bi bi-pie-chart me-2"></i>Request Sources</div>
          <div class="card-body d-flex align-items-center justify-content-center">
            <canvas id="sourceChart"></canvas>
          </div>
          <div class="card-footer small footer-note">Requests by client IP address.</div>
        </div>
      </div>
      <div class="col-12 col-lg-7">
        <div class="card h-100 chart-card">
          <div class="card-header fw-semibold"><i class="bi bi-bar-chart me-2"></i>Requests by Endpoint</div>
          <div class="card-body"><canvas id="endpointChart"></canvas></div>
          <div class="card-footer small footer-note">Cache hits vs upstream calls per endpoint.</div>
        </div>
      </div>
    </div>

    <!-- Clients table -->
    <div class="row g-3 mt-1">
      <div class="col-12">
        <div class="card">
          <div class="card-header fw-semibold"><i class="bi bi-people me-2"></i>Clients</div>
          <div class="card-body p-0">
            <div class="table-responsive">
              <table class="table table-sm table-hover mb-0" id="clientsTable">
                <thead class="table-dark">
                  <tr>
                    <th>Client IP</th>
                    <th>X-Forwarded-For</th>
                    <th class="text-end">Requests</th>
                    <th class="text-end">Cache</th>
                    <th class="text-end">Upstream</th>
                    <th class="text-end">Misses</th>
                    <th class="text-end">Hit Rate</th>
                    <th class="text-end">Avg Latency</th>
                    <th>Last Seen</th>
                  </tr>
                </thead>
                <tbody id="clientsBody">
                  <tr><td colspan="9" class="text-center text-secondary py-3">No data yet</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Recent requests -->
    <div class="row g-3 mt-1">
      <div class="col-12">
        <div class="card">
          <div class="card-header fw-semibold d-flex justify-content-between align-items-center">
            <span><i class="bi bi-clock-history me-2"></i>Recent Requests</span>
            <span class="d-flex align-items-center gap-2">
              <button id="toggleQueryBtn" class="btn btn-sm btn-outline-info" title="Show/hide query URLs">
                <i class="bi bi-code-slash"></i> Queries
              </button>
              <button id="exportCsvBtn" class="btn btn-sm btn-outline-success" title="Export recent requests as CSV">
                <i class="bi bi-download"></i> Export CSV
              </button>
              <span class="badge text-bg-secondary" id="recentCount">0</span>
            </span>
          </div>
          <div class="card-body p-0">
            <div class="table-responsive" style="max-height: 400px; overflow-y: auto;">
              <table class="table table-sm table-hover mb-0">
                <thead class="table-dark sticky-top">
                  <tr>
                    <th>Time</th>
                    <th>Client</th>
                    <th>Forwarded</th>
                    <th>Endpoint</th>
                    <th>Source</th>
                    <th class="text-end">Latency</th>
                    <th class="query-col" style="display:none">Query</th>
                  </tr>
                </thead>
                <tbody id="recentBody">
                  <tr><td colspan="7" class="text-center text-secondary py-3">No data yet</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>

  </main>

  <!-- =====================================================================
       JOBS TAB PANE
       ===================================================================== -->
  <main class="container my-4" id="jobsPane" style="display:none">
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
      <h5 class="mb-0"><i class="bi bi-list-task me-2"></i>Background Jobs</h5>
      <div class="d-flex align-items-center gap-3">
        <span class="small" id="jobsHealth"></span>
        <button class="btn btn-sm btn-outline-secondary" id="refreshJobsBtn">
          <i class="bi bi-arrow-clockwise"></i> Refresh
        </button>
      </div>
    </div>

    <!-- Backlog (latest Backlog snapshot run) -->
    <div class="card mb-3">
      <div class="card-header fw-semibold d-flex justify-content-between align-items-center">
        <span><i class="bi bi-hourglass-split me-2"></i>Backlog</span>
        <span class="small text-secondary fw-normal" id="backlogAsOf"></span>
      </div>
      <div class="card-body py-2" id="backlogBody">
        <span class="text-secondary small">No snapshot yet.</span>
      </div>
    </div>

    <!-- Job catalog -->
    <div class="card mb-3">
      <div class="card-header fw-semibold"><i class="bi bi-gear-wide-connected me-2"></i>Jobs</div>
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0 align-middle" id="catalogTable">
            <thead class="table-dark">
              <tr><th>Job</th><th>Schedule</th><th>Next run</th><th>Last run</th><th></th></tr>
            </thead>
            <tbody id="catalogBody">
              <tr><td colspan="5" class="text-center text-secondary py-3">Loading&hellip;</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Run history -->
    <div class="card">
      <div class="card-header fw-semibold d-flex justify-content-between align-items-center flex-wrap gap-2">
        <span><i class="bi bi-clock-history me-2"></i>Run history</span>
        <div class="d-flex gap-2">
          <select class="form-select form-select-sm" id="histJob"><option value="">All jobs</option></select>
          <select class="form-select form-select-sm" id="histStatus">
            <option value="">Any status</option>
            <option value="running">running</option><option value="done">done</option>
            <option value="partial">partial</option><option value="idle">idle</option>
            <option value="skipped">skipped</option><option value="error">error</option>
            <option value="cancelled">cancelled</option>
          </select>
          <select class="form-select form-select-sm" id="histTrigger">
            <option value="">Any trigger</option><option value="schedule">Scheduled</option>
            <option value="manual">Manual</option>
          </select>
        </div>
      </div>
      <div class="card-body p-0">
        <div class="table-responsive">
          <table class="table table-sm table-hover mb-0 align-middle">
            <thead class="table-dark">
              <tr><th style="width:1.5rem"></th><th>Job</th><th>Status</th><th>Started</th>
                  <th>Duration</th><th>Summary</th><th></th></tr>
            </thead>
            <tbody id="historyBody">
              <tr><td colspan="7" class="text-center text-secondary py-3">Loading&hellip;</td></tr>
            </tbody>
          </table>
        </div>
        <div class="text-center py-2">
          <button class="btn btn-sm btn-outline-secondary" id="histMore" style="display:none">Load more</button>
        </div>
      </div>
    </div>
  </main>

  <!-- =====================================================================
       RUN JOB MODAL
       ===================================================================== -->
  <div class="modal fade" id="runModal" tabindex="-1" aria-labelledby="runModalTitle" aria-hidden="true">
    <div class="modal-dialog modal-lg">
      <div class="modal-content">
        <div class="modal-header">
          <h5 class="modal-title" id="runModalTitle">Run job</h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p class="small text-secondary mb-2" id="runModalDesc"></p>
          <div class="mb-3" id="runModalMeta"></div>
          <form id="runModalForm" class="row g-2" onsubmit="return false"></form>
          <div id="runModalFeedback" class="mt-2"></div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Close</button>
          <button type="button" class="btn btn-primary btn-sm" id="runModalStart">
            <i class="bi bi-play-fill me-1"></i>Start
          </button>
        </div>
      </div>
    </div>
  </div>
  <footer class="container my-4 small footer-note">
    <div class="d-flex justify-content-between">
      <span>CVProxy Dashboard</span>
      <span>
        <a href="/health" class="link-secondary text-decoration-none me-3"><i class="bi bi-heart-pulse me-1"></i>Health</a>
        <a href="/api/search/?query=test" class="link-secondary text-decoration-none"><i class="bi bi-search me-1"></i>API</a>
      </span>
    </div>
  </footer>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" integrity="sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI" crossorigin="anonymous"></script>
  <script>
    // -----------------------------------------------------------------------
    // Chart defaults
    // -----------------------------------------------------------------------
    const baseOpts = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { usePointStyle: true, boxWidth: 8, color: '#adb5bd' } },
        tooltip: { mode: 'index', intersect: false }
      },
      scales: {
        x: { ticks: { color: '#adb5bd' }, grid: { color: 'rgba(255,255,255,.06)' } },
        y: { beginAtZero: true, ticks: { precision: 0, color: '#adb5bd' }, grid: { color: 'rgba(255,255,255,.06)' } }
      }
    };

    const charts = {};
    function upsertChart(id, cfg) {
      if (charts[id]) charts[id].destroy();
      const el = document.getElementById(id);
      if (!el) return null;
      return charts[id] = new Chart(el, cfg);
    }

    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    let currentHours = 6;
    let autoRefreshTimer = null;

    // -----------------------------------------------------------------------
    // Data fetch
    // -----------------------------------------------------------------------
    async function fetchStats() {
      const url = currentHours ? `/dashboard/data?hours=${currentHours}` : '/dashboard/data';
      const r = await fetch(url);
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }

    // -----------------------------------------------------------------------
    // Render
    // -----------------------------------------------------------------------
    function hitRateClass(rate) {
      if (rate >= 80) return 'good';
      if (rate >= 50) return 'warn';
      return 'bad';
    }

    function fmtTime(ts) {
      if (!ts) return '\\u2014';
      return new Date(ts * 1000).toLocaleString();
    }

    function fmtLatency(ms) {
      if (ms == null) return '\\u2014';
      return ms < 1 ? '<1 ms' : ms.toFixed(1) + ' ms';
    }

    function sourceBadge(src) {
      const cls = { cache: 'text-bg-success', upstream: 'text-bg-warning', miss: 'text-bg-danger' };
      return `<span class="badge source-badge ${cls[src] || 'text-bg-secondary'}">${src}</span>`;
    }

    function render(d) {
      const t = d.totals || {};

      // KPIs
      document.getElementById('totalRequests').textContent = (t.total_requests || 0).toLocaleString();
      document.getElementById('cacheHits').textContent = (t.cache_hits || 0).toLocaleString();
      document.getElementById('upstreamCalls').textContent = (t.upstream_calls || 0).toLocaleString();
      const rate = t.cache_hit_rate || 0;
      const rateEl = document.getElementById('hitRate');
      rateEl.textContent = rate + '%';
      rateEl.className = 'value hit-rate ' + hitRateClass(rate);

      // CV API quota progress bars
      const quota = d.cv_quota || {};
      const hardLimit = 200;
      const softLimit = d.cv_quota_limit || 180;
      const quotaSection = document.getElementById('quotaSection');
      const quotaEntries = Object.entries(quota).filter(([, v]) => v.used > 0);
      if (quotaEntries.length === 0) {
        quotaSection.innerHTML = '<span class="text-secondary small">No upstream calls yet this hour.</span>';
      } else {
        quotaSection.innerHTML = quotaEntries
          .sort((a, b) => b[1].used - a[1].used)
          .map(([ep, v]) => {
            const pct = Math.min(100, Math.round(v.used / hardLimit * 100));
            const cls = pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warning' : 'bg-success';
            const warn = v.used >= softLimit
              ? `<span class="badge text-bg-warning ms-2" title="Approaching CV hard limit">\\u26a0 near limit</span>`
              : '';
            return `
              <div class="mb-2">
                <div class="d-flex justify-content-between mb-1">
                  <span class="small fw-semibold">/${ep}</span>
                  <span class="small">${v.used} / ${hardLimit}${warn}</span>
                </div>
                <div class="progress" style="height:10px">
                  <div class="progress-bar ${cls}" role="progressbar" style="width:${pct}%" aria-valuenow="${v.used}" aria-valuemin="0" aria-valuemax="${hardLimit}"></div>
                </div>
              </div>`;
          }).join('');
      }

      // Source doughnut – by client IP
      const clients = d.by_client || [];
      const palette = ['#0d6efd','#6610f2','#6f42c1','#d63384','#fd7e14','#20c997','#0dcaf0','#198754','#ffc107','#dc3545'];
      upsertChart('sourceChart', {
        type: 'doughnut',
        data: {
          labels: clients.map(c => c.forwarded_for || c.client_ip),
          datasets: [{
            data: clients.map(c => c.total_requests),
            backgroundColor: clients.map((_, i) => palette[i % palette.length]),
            borderWidth: 0
          }]
        },
        options: { ...baseOpts, cutout: '65%', scales: {} }
      });

      // Endpoint bar chart
      const eps = d.by_endpoint || [];
      upsertChart('endpointChart', {
        type: 'bar',
        data: {
          labels: eps.map(e => e.endpoint),
          datasets: [
            { label: 'Cache', data: eps.map(e => e.cache_hits || 0), backgroundColor: '#198754' },
            { label: 'Upstream', data: eps.map(e => e.upstream_calls || 0), backgroundColor: '#ffc107' }
          ]
        },
        options: { ...baseOpts, plugins: { ...baseOpts.plugins }, scales: { ...baseOpts.scales, x: { ...baseOpts.scales.x, stacked: true }, y: { ...baseOpts.scales.y, stacked: true } } }
      });

      // Clients table
      const tbody = document.getElementById('clientsBody');
      if (clients.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center text-secondary py-3">No data yet</td></tr>';
      } else {
        tbody.innerHTML = clients.map(c => `
          <tr>
            <td><code>${c.client_ip}</code></td>
            <td>${c.forwarded_for ? '<code>' + c.forwarded_for + '</code>' : '<span class="text-secondary">\\u2014</span>'}</td>
            <td class="text-end">${c.total_requests}</td>
            <td class="text-end text-success">${c.cache_hits}</td>
            <td class="text-end text-warning">${c.upstream_calls}</td>
            <td class="text-end text-danger">${c.misses}</td>
            <td class="text-end"><span class="hit-rate ${hitRateClass(c.cache_hit_rate)}">${c.cache_hit_rate}%</span></td>
            <td class="text-end">${fmtLatency(c.avg_latency_ms)}</td>
            <td class="small">${fmtTime(c.last_seen)}</td>
          </tr>
        `).join('');
      }

      // Recent requests
      const recent = d.recent_requests || [];
      document.getElementById('recentCount').textContent = recent.length;
      window._recentData = recent;
      const rbody = document.getElementById('recentBody');
      const qVisible = document.querySelector('.query-col')?.style.display !== 'none';
      if (recent.length === 0) {
        rbody.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-3">No data yet</td></tr>';
      } else {
        rbody.innerHTML = recent.map(r => `
          <tr>
            <td class="small">${fmtTime(r.timestamp)}</td>
            <td><code>${r.client_ip}</code></td>
            <td>${r.forwarded_for ? '<code>' + r.forwarded_for + '</code>' : '<span class="text-secondary">\\u2014</span>'}</td>
            <td>${r.endpoint}</td>
            <td>${sourceBadge(r.source)}</td>
            <td class="text-end">${fmtLatency(r.latency_ms)}</td>
            <td class="query-col small" style="display:${qVisible ? '' : 'none'}"><code>${r.query_url || '\\u2014'}</code></td>
          </tr>
        `).join('');
      }
    }

    // -----------------------------------------------------------------------
    // Load + auto-refresh
    // -----------------------------------------------------------------------
    async function load() {
      try {
        const d = await fetchStats();
        render(d);
      } catch (e) {
        console.error('Dashboard load failed:', e);
      }
    }

    function startAutoRefresh() {
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshTimer = setInterval(load, 30000);
    }

    // -----------------------------------------------------------------------
    // Time filter buttons
    // -----------------------------------------------------------------------
    document.querySelectorAll('.time-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const h = btn.dataset.hours;
        currentHours = h ? parseFloat(h) : null;
        load();
      });
    });

    document.getElementById('refreshBtn').addEventListener('click', () => {
      const icon = document.querySelector('#refreshBtn .bi');
      icon.classList.add('spin');
      load().finally(() => setTimeout(() => icon.classList.remove('spin'), 400));
    });

    // Toggle query column visibility
    document.getElementById('toggleQueryBtn').addEventListener('click', () => {
      document.querySelectorAll('.query-col').forEach(el => {
        el.style.display = el.style.display === 'none' ? '' : 'none';
      });
    });

    // CSV export
    document.getElementById('exportCsvBtn').addEventListener('click', () => {
      const rows = window._recentData || [];
      if (!rows.length) return;
      const header = 'time,client_ip,forwarded_for,endpoint,source,latency_ms,query_url';
      const csv = [header, ...rows.map(r =>
        [fmtTime(r.timestamp), r.client_ip, r.forwarded_for||'', r.endpoint, r.source, r.latency_ms?.toFixed(1)||'', '"'+(r.query_url||'').replace(/"/g,'""')+'"'].join(',')
      )].join('\\n');
      const blob = new Blob([csv], {type:'text/csv'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'cvproxy-requests.csv';
      a.click();
      URL.revokeObjectURL(a.href);
    });

    // Spin animation + shared styles
    const style = document.createElement('style');
    style.textContent = `
      @keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}} .spin{animation:spin .5s linear}
      .job-detail { background: rgba(var(--bs-primary-rgb),.04); font-size:.82rem; }
      .job-detail pre { margin:0; white-space:pre-wrap; word-break:break-all; color: var(--bs-body-color); }
      .expand-btn { cursor:pointer; transition:transform .2s; }
      .expand-btn.open { transform: rotate(90deg); }
    `;
    document.head.appendChild(style);

    // -----------------------------------------------------------------------
    // Tab switching
    // -----------------------------------------------------------------------
    let currentTab = 'stats';
    const statsPane = document.querySelector('main.container:not(#jobsPane)');
    const jobsPane  = document.getElementById('jobsPane');
    const timeFilterGroup = document.getElementById('timeFilterGroup');

    document.querySelectorAll('[data-tab]').forEach(link => {
      link.addEventListener('click', e => {
        e.preventDefault();
        currentTab = link.dataset.tab;
        document.querySelectorAll('[data-tab]').forEach(l => l.classList.remove('active'));
        link.classList.add('active');
        statsPane.style.display = currentTab === 'stats' ? '' : 'none';
        jobsPane.style.display  = currentTab === 'jobs'  ? '' : 'none';
        timeFilterGroup.style.display = currentTab === 'stats' ? '' : 'none';
        if (currentTab === 'jobs') loadJobsTab();
      });
    });

    // -----------------------------------------------------------------------
    // Jobs tab
    // -----------------------------------------------------------------------
    const STATUS_STYLE = {
      running:   ['text-bg-primary',   'bi-arrow-repeat'],
      done:      ['text-bg-success',   'bi-check-circle'],
      partial:   ['text-bg-warning',   'bi-pause-circle'],
      idle:      ['text-bg-secondary', 'bi-moon'],
      skipped:   ['text-bg-secondary', 'bi-skip-forward'],
      error:     ['text-bg-danger',    'bi-x-circle'],
      cancelled: ['text-bg-dark',      'bi-slash-circle'],
    };
    const CATEGORIES = [['sync', 'Sync'], ['backfill', 'Backfill'], ['export', 'Export'],
                        ['maintenance', 'Maintenance'], ['repair', 'Repair']];
    const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
    function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ESC[c]); }

    function jobStatusBadge(status, title) {
      const [cls, icon] = STATUS_STYLE[status] || ['text-bg-secondary', 'bi-question-circle'];
      const tip = title ? ` title="${esc(title)}"` : '';
      return `<span class="badge ${cls}"${tip}><i class="bi ${icon} me-1"></i>${esc(status)}</span>`;
    }
    function fmtSecs(s) {
      if (s == null) return '-';
      if (s < 1) return Math.round(s * 1000) + ' ms';
      if (s < 60) return s.toFixed(1) + ' s';
      const m = Math.floor(s / 60);
      if (m < 60) return m + 'm ' + String(Math.floor(s % 60)).padStart(2, '0') + 's';
      return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
    }
    function relTime(iso) {
      if (!iso) return '-';
      const d = new Date(iso), diff = (d - Date.now()) / 1000, a = Math.abs(diff);
      const t = a < 60 ? Math.round(a) + 's' : a < 3600 ? Math.round(a / 60) + 'm'
              : a < 86400 ? (a / 3600).toFixed(1) + 'h' : (a / 86400).toFixed(1) + 'd';
      return `<span title="${esc(d.toLocaleString())}">${diff > 0 ? 'in ' + t : t + ' ago'}</span>`;
    }
    function progressBar(p) {
      if (!p) return '';
      const pct = p.total ? Math.min(100, Math.round(100 * p.done / p.total)) : 100;
      const label = (p.total ? `${p.done}/${p.total}` : `${p.done}`) + (p.note ? ' · ' + p.note : '');
      return `<div class="progress mt-1" style="height:5px"><div class="progress-bar progress-bar-striped progress-bar-animated" style="width:${pct}%"></div></div>
              <div class="small text-secondary">${esc(label)}</div>`;
    }

    let catalog = [];
    async function loadCatalog() {
      const r = await fetch('/admin/jobs/catalog');
      if (!r.ok) throw new Error(r.status);
      catalog = await r.json();
      renderCatalog();
      renderBacklog();
      fillJobFilter();
    }

    function lastRunCell(j) {
      if (j.running) {
        return `${jobStatusBadge('running')} <span class="small">started ${relTime(j.running.started_at)}</span>${progressBar(j.running.progress)}`;
      }
      if (!j.last) return '<span class="text-secondary small">never</span>';
      const l = j.last;
      let html = `${jobStatusBadge(l.status, l.stop_text)} <span class="small">${relTime(l.finished_at || l.started_at)}</span>`
               + `<div class="small text-secondary text-truncate" style="max-width:26rem" title="${esc(l.summary)}">${esc(l.summary)}</div>`;
      if (!['done', 'idle'].includes(l.status)) {
        html += `<div class="small text-secondary">last ok: ${j.last_ok ? relTime(j.last_ok.finished_at) : 'never'}</div>`;
      }
      return html;
    }

    function renderCatalog() {
      let html = '';
      for (const [cat, label] of CATEGORIES) {
        const jobs = catalog.filter(j => j.category === cat);
        if (!jobs.length) continue;
        html += `<tr class="table-active"><td colspan="5" class="small fw-semibold text-uppercase text-secondary">${label}</td></tr>`;
        for (const j of jobs) {
          const eps = j.endpoints.length
            ? `<span class="small text-secondary ms-2">${j.endpoints.map(e => '/' + esc(e)).join(' ')}</span>` : '';
          const off = j.disabled ? `<div class="small text-warning"><i class="bi bi-slash-circle me-1"></i>${esc(j.disabled)}</div>` : '';
          const btn = j.manual && !j.disabled
            ? `<button class="btn btn-sm btn-outline-primary run-btn text-nowrap" data-job="${esc(j.id)}" ${j.running ? 'disabled' : ''}><i class="bi bi-play-fill"></i> Run</button>` : '';
          html += `<tr class="${j.disabled ? 'opacity-50' : ''}">
              <td><span class="fw-semibold">${esc(j.name)}</span>${eps}
                  <div class="small text-secondary job-desc">${esc(j.description)}</div>${off}</td>
              <td class="small text-nowrap">${esc(j.schedule)}</td>
              <td class="small text-nowrap">${j.next_run ? relTime(j.next_run) : '-'}</td>
              <td style="min-width:15rem">${lastRunCell(j)}</td>
              <td class="text-end">${btn}</td></tr>`;
        }
      }
      const body = document.getElementById('catalogBody');
      body.innerHTML = html || '<tr><td colspan="5" class="text-center text-secondary py-3">No jobs</td></tr>';
      body.querySelectorAll('.run-btn').forEach(b => b.addEventListener('click', () => openRunModal(b.dataset.job)));

      const running = catalog.filter(j => j.running).length;
      const badge = document.getElementById('jobsBadge');
      if (running) { badge.textContent = running; badge.style.display = ''; } else badge.style.display = 'none';
      const failing = catalog.filter(j => j.last && j.last.status === 'error').map(j => j.name);
      document.getElementById('jobsHealth').innerHTML = failing.length
        ? `<span class="text-danger" title="${esc(failing.join(', '))}"><i class="bi bi-exclamation-octagon me-1"></i>${failing.length} job(s) failed on their last run</span>`
        : '<span class="text-success"><i class="bi bi-check-circle me-1"></i>No job failed on its last run</span>';
    }

    function renderBacklog() {
      const snap = catalog.find(j => j.id === 'queue_snapshot');
      const run = snap && snap.last_ok;
      const body = document.getElementById('backlogBody');
      if (!run || !run.result) {
        body.innerHTML = '<span class="text-secondary small">No snapshot yet. Run "Backlog snapshot" to take one (read-only, a few minutes).</span>';
        return;
      }
      const r = run.result;
      document.getElementById('backlogAsOf').innerHTML = 'snapshot ' + relTime(run.finished_at);
      const pending = Object.entries(r.pending_fetch || {}).map(([k, v]) => `${k} ${v}`).join(', ') || 'empty';
      const cache = Object.values(r.response_cache || {}).reduce((a, b) => a + b, 0);
      const win = r.backfill_min_year ? ` (${r.backfill_min_year}+ or undated)` : '';
      const inWin = (w, all) => w == null ? all : `${w.toLocaleString()} of ${(all ?? 0).toLocaleString()}`;
      const items = [
        [`Issues without details${win}`, inWin(r.issues_missing_details_in_window, r.issues_missing_details), 'Issue detail backfill'],
        [`Issues missing fields${win}`, inWin(r.issues_missing_fields_in_window, r.issues_missing_fields), 'Issue backfill'],
        ['Volumes missing fields', r.volumes_missing_fields, 'Volume backfill'],
        ['Publishers missing fields', r.publishers_missing_fields, 'Publisher backfill'],
        ['Newest issue / newest hashed', `${r.newest_issue_id} / ${r.newest_hashed_issue_id}`, 'Cover hash backfill'],
        ['Pending fetch queue', pending, 'Pending fetch'],
        ['Response cache rows', cache, 'Cache eviction'],
        ['Sync watermark', r.sync_watermark, 'Incremental issue sync'],
        ['Database / WAL', `${(r.db_bytes / 1073741824).toFixed(2)} GB / ${(r.wal_bytes / 1048576).toFixed(0)} MB`, 'WAL checkpoint'],
      ];
      body.innerHTML = '<div class="row g-2">' + items.map(([k, v, job]) =>
        `<div class="col-6 col-md-4 col-xl-3"><div class="small text-secondary" title="Worked off by: ${esc(job)}">${k}</div>`
        + `<div class="fw-semibold">${typeof v === 'number' ? v.toLocaleString() : esc(v ?? '-')}</div></div>`).join('') + '</div>';
    }

    function fillJobFilter() {
      const sel = document.getElementById('histJob');
      if (sel.options.length > 1) return;
      [...catalog].sort((a, b) => a.name.localeCompare(b.name)).forEach(j => sel.add(new Option(j.name, j.id)));
    }
    // Run history
    const HIST_PAGE = 50;
    let histRows = [], histOffset = 0;
    const openDetails = new Set();

    async function loadHistory(append = false) {
      if (!append) histOffset = 0;
      const q = new URLSearchParams({limit: HIST_PAGE, offset: histOffset});
      for (const [id, key] of [['histJob', 'job'], ['histStatus', 'status'], ['histTrigger', 'trigger']]) {
        const v = document.getElementById(id).value;
        if (v) q.set(key, v);
      }
      const r = await fetch('/admin/jobs?' + q);
      if (!r.ok) throw new Error(r.status);
      const rows = await r.json();
      histRows = append ? histRows.concat(rows) : rows;
      histOffset += rows.length;
      document.getElementById('histMore').style.display = rows.length >= HIST_PAGE ? '' : 'none';
      renderHistory();
    }

    function renderHistory() {
      const body = document.getElementById('historyBody');
      if (!histRows.length) {
        body.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-3">No runs recorded yet.</td></tr>';
        return;
      }
      body.innerHTML = histRows.map(j => {
        const params = Object.entries(j.params || {}).filter(([, v]) => v !== '' && v != null)
          .map(([k, v]) => `${k}=${String(v).length > 40 ? String(v).slice(0, 40) + '...' : v}`).join(' ');
        const trig = j.trigger === 'manual' ? '<span class="badge text-bg-info ms-1">manual</span>' : '';
        const cancel = j.status === 'running'
          ? `<button class="btn btn-sm btn-outline-danger cancel-btn" data-run="${j.run_id}" title="Cancel"><i class="bi bi-x-lg"></i></button>` : '';
        const open = openDetails.has(j.run_id);
        const err = j.status === 'error' && j.error && j.error !== j.summary ? `<div class="text-danger">${esc(j.error)}</div>` : '';
        return `<tr>
            <td><i class="bi bi-chevron-right expand-btn ${open ? 'open' : ''}" data-run="${j.run_id}" title="Details"></i></td>
            <td class="text-nowrap"><span class="fw-semibold">${esc(j.name)}</span>${trig}${params ? `<div class="small text-secondary">${esc(params)}</div>` : ''}</td>
            <td>${jobStatusBadge(j.status, j.stop_text)}</td>
            <td class="small text-nowrap">${relTime(j.started_at)}</td>
            <td class="small text-nowrap">${fmtSecs(j.duration_s)}</td>
            <td class="small">${esc(j.summary || '')}${j.status === 'running' ? progressBar(j.progress) : ''}${err}</td>
            <td class="text-end">${cancel}</td></tr>
          <tr class="job-detail" id="detail-${j.run_id}" style="${open ? '' : 'display:none'}">
            <td colspan="7" class="px-4 py-2"><div class="detail-body small text-secondary">Loading&hellip;</div></td></tr>`;
      }).join('');
      body.querySelectorAll('.expand-btn').forEach(btn => btn.addEventListener('click', () => {
        const id = btn.dataset.run, row = document.getElementById('detail-' + id);
        const show = row.style.display === 'none';
        row.style.display = show ? '' : 'none';
        btn.classList.toggle('open', show);
        if (show) { openDetails.add(id); loadDetail(id); } else openDetails.delete(id);
      }));
      body.querySelectorAll('.cancel-btn').forEach(btn => btn.addEventListener('click', async () => {
        btn.disabled = true;
        const r = await fetch('/admin/jobs/' + btn.dataset.run, {method: 'DELETE'});
        if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.detail || 'Cancel failed'); }
        loadJobsTab();
      }));
      for (const id of openDetails) loadDetail(id);
    }

    async function loadDetail(runId) {
      const row = document.getElementById('detail-' + runId);
      if (!row) return;
      const r = await fetch('/admin/jobs/' + runId);
      if (!r.ok) return;
      const j = await r.json();
      const meta = [
        `run ${j.run_id.slice(0, 8)}`, `trigger: ${j.trigger}`,
        j.started_at && `started ${new Date(j.started_at).toLocaleString()}`,
        j.finished_at && `finished ${new Date(j.finished_at).toLocaleString()}`,
        j.stop_text && `stopped: ${j.stop_text}`,
      ].filter(Boolean).join(' | ');
      const log = (j.log || []).join('\\n');
      const dropped = j.log_dropped ? ` (newest ${j.log.length} lines; ${j.log_dropped} earlier lines dropped)` : '';
      row.querySelector('.detail-body').innerHTML = `<div class="mb-1">${esc(meta)}</div>`
        + (j.error ? `<div class="text-danger mb-1">${esc(j.error)}</div>` : '')
        + (j.result ? `<div class="fw-semibold mt-2">Result</div><pre>${esc(JSON.stringify(j.result, null, 2))}</pre>` : '')
        + `<div class="fw-semibold mt-2">Log${dropped}</div><pre class="job-log">${esc(log) || 'no log lines'}</pre>`;
    }

    // Run modal
    let runModal = null, modalJob = null;
    function paramField(p) {
      const id = 'param-' + p.name, help = p.help ? `<div class="form-text">${esc(p.help)}</div>` : '';
      if (p.kind === 'bool') {
        return `<div class="col-12"><div class="form-check"><input class="form-check-input" type="checkbox" id="${id}" ${p.default ? 'checked' : ''}>
                <label class="form-check-label" for="${id}">${esc(p.label)}</label>${help}</div></div>`;
      }
      if (p.kind === 'text') {
        return `<div class="col-12"><label class="form-label small mb-1" for="${id}">${esc(p.label)}</label>
                <textarea class="form-control form-control-sm font-monospace" rows="3" id="${id}">${esc(p.default)}</textarea>${help}</div>`;
      }
      const num = p.kind === 'int' || p.kind === 'float';
      return `<div class="col-6 col-md-4"><label class="form-label small mb-1" for="${id}">${esc(p.label)}</label>
              <input class="form-control form-control-sm" type="${num ? 'number' : 'text'}" step="${p.kind === 'float' ? 'any' : '1'}" id="${id}"
                value="${esc(p.default)}" ${p.min != null ? `min="${p.min}"` : ''} ${p.max != null ? `max="${p.max}"` : ''}>${help}</div>`;
    }
    function openRunModal(id) {
      modalJob = catalog.find(j => j.id === id);
      if (!modalJob) return;
      document.getElementById('runModalTitle').textContent = 'Run: ' + modalJob.name;
      document.getElementById('runModalDesc').textContent = modalJob.description;
      const meta = [
        modalJob.schedule,
        modalJob.endpoints.length ? 'ComicVine: ' + modalJob.endpoints.map(e => '/' + e).join(', ') : 'No ComicVine API calls',
        modalJob.cancellable ? null : 'Cannot be cancelled once started',
      ].filter(Boolean);
      document.getElementById('runModalMeta').innerHTML = meta.map(t => `<span class="badge text-bg-secondary me-1">${esc(t)}</span>`).join('');
      document.getElementById('runModalForm').innerHTML = modalJob.params.map(paramField).join('')
        || '<div class="col-12 small text-secondary">No parameters.</div>';
      document.getElementById('runModalFeedback').innerHTML = modalJob.destructive
        ? '<div class="alert alert-warning py-1 small mb-0"><i class="bi bi-exclamation-triangle me-1"></i>This job can delete data. Check the options before starting.</div>' : '';
      document.getElementById('runModalStart').disabled = false;
      runModal = runModal || new bootstrap.Modal(document.getElementById('runModal'));
      runModal.show();
    }
    document.getElementById('runModalStart').addEventListener('click', async () => {
      const btn = document.getElementById('runModalStart');
      const params = {};
      for (const p of modalJob.params) {
        const el = document.getElementById('param-' + p.name);
        params[p.name] = p.kind === 'bool' ? el.checked : el.value;
      }
      const deletes = modalJob.destructive && (params.dry_run === false || params.purge === true);
      if (deletes && !confirm(`${modalJob.name} will delete data. Continue?`)) return;
      btn.disabled = true;
      try {
        const r = await fetch(`/admin/jobs/${modalJob.id}/run`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(params),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || r.status);
        runModal.hide();
        openDetails.add(d.run_id);
        await loadJobsTab();
      } catch (e) {
        document.getElementById('runModalFeedback').innerHTML =
          `<div class="alert alert-danger py-1 small mb-0"><i class="bi bi-x-circle me-1"></i>${esc(e.message)}</div>`;
        btn.disabled = false;
      }
    });

    async function loadJobsTab() {
      try { await Promise.all([loadCatalog(), loadHistory()]); }
      catch (e) { console.error('Jobs load failed:', e); }
    }
    ['histJob', 'histStatus', 'histTrigger'].forEach(id =>
      document.getElementById(id).addEventListener('change', () => loadHistory()));
    document.getElementById('histMore').addEventListener('click', () => loadHistory(true));
    document.getElementById('refreshJobsBtn').addEventListener('click', () => {
      const icon = document.querySelector('#refreshJobsBtn .bi');
      icon.classList.add('spin');
      loadJobsTab().finally(() => setTimeout(() => icon.classList.remove('spin'), 400));
    });

    // Live updates: the whole tab while it is open, otherwise just the running-jobs badge
    function refreshJobsBadge() {
      if (currentTab === 'jobs') {
        if (!document.querySelector('.modal.show')) loadJobsTab();
        return;
      }
      fetch('/admin/jobs?status=running&limit=50').then(r => r.json()).then(runs => {
        const badge = document.getElementById('jobsBadge');
        if (runs.length) { badge.textContent = runs.length; badge.style.display = ''; }
        else badge.style.display = 'none';
      }).catch(() => {});
    }
    // Boot
    load();
    startAutoRefresh();
    setInterval(refreshJobsBadge, 5000);
  </script>
</body>
</html>
"""
