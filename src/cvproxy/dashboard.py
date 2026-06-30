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
    .job-ref-card { border-left: 3px solid var(--bs-primary); }
    .job-ref-card .param-tag { font-size: .75rem; background: rgba(var(--bs-primary-rgb),.12); border-radius: .25rem; padding: .1rem .35rem; }
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
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h5 class="mb-0"><i class="bi bi-list-task me-2"></i>Background Jobs</h5>
      <button class="btn btn-sm btn-outline-secondary" id="refreshJobsBtn">
        <i class="bi bi-arrow-clockwise"></i> Refresh
      </button>
    </div>

    <!-- Run a job -->
    <div class="card mb-3">
      <div class="card-header fw-semibold d-flex justify-content-between align-items-center">
        <span><i class="bi bi-play-circle me-2"></i>Run a Job</span>
        <button class="btn btn-sm btn-outline-info" data-bs-toggle="modal" data-bs-target="#jobInfoModal" title="Job reference — what each job does and when to use it">
          <i class="bi bi-info-circle me-1"></i>Job Reference
        </button>
      </div>
      <div class="card-body">
        <div class="row g-2 align-items-end">
          <div class="col-12 col-md-4">
            <label class="form-label small mb-1">Job type</label>
            <select class="form-select form-select-sm" id="jobType">
              <option value="sync-issues">Issue sync &mdash; re-fetch recently updated issues</option>
              <option value="repair-publishers">Publisher repair &mdash; correct publisher IDs from CV</option>
              <option value="rebuild-fts">FTS rebuild &mdash; resync search index from database</option>
              <optgroup label="Maintenance">
                <option value="evict">Cache eviction &mdash; remove stale issues, volumes &amp; response cache</option>
                <option value="cleanup-images">Image cache cleanup &mdash; remove expired image files</option>
              </optgroup>
            </select>
          </div>
          <div class="col-6 col-md-2" id="daysFieldWrap">
            <label class="form-label small mb-1">Look-back (days)</label>
            <input type="number" class="form-control form-control-sm" id="syncDays"
                   value="14" min="1" max="365" step="1">
          </div>
          <div class="col-6 col-md-2" id="batchFieldWrap" style="display:none">
            <label class="form-label small mb-1">Batch size</label>
            <input type="number" class="form-control form-control-sm" id="repairBatch"
                   value="100" min="10" max="100" step="10">
          </div>
          <div class="col-auto align-self-end" id="calendarOnlyWrap" style="display:none">
            <div class="form-check mb-1">
              <input class="form-check-input" type="checkbox" id="calendarOnly" checked>
              <label class="form-check-label small" for="calendarOnly"
                     title="Only repair volumes that appear in new-release calendar queries (much faster)">
                Calendar volumes only
              </label>
            </div>
          </div>
          <div class="col-auto align-self-end" id="dryRunWrap" style="display:none">
            <div class="form-check mb-1">
              <input class="form-check-input" type="checkbox" id="evictDryRun" checked>
              <label class="form-check-label small text-warning fw-semibold" for="evictDryRun"
                     title="Preview what would be deleted without actually deleting anything">
                <i class="bi bi-eye me-1"></i>Dry run (preview only)
              </label>
            </div>
          </div>
          <div class="col-12" id="evictInfoWrap" style="display:none">
            <div class="alert alert-secondary py-1 px-2 mb-0 small" id="evictInfoText">
              Loading eviction settings&hellip;
            </div>
          </div>
          <div class="col-auto">
            <button class="btn btn-sm btn-primary" id="runJobBtn">
              <i class="bi bi-play-fill me-1"></i>Start
            </button>
          </div>
          <div class="col-12" id="runJobFeedback" style="display:none"></div>
        </div>
      </div>
    </div>

    <!-- Admin-triggered jobs table -->
    <div class="card mb-3">
      <div class="card-header fw-semibold small text-secondary">Triggered Jobs</div>
      <div class="card-body p-0">
        <table class="table table-sm table-hover mb-0" id="jobsTable">
          <thead class="table-dark">
            <tr>
              <th style="width:1.5rem"></th>
              <th>Job</th>
              <th>Status</th>
              <th>Started</th>
              <th>Finished</th>
              <th class="text-end">Actions</th>
            </tr>
          </thead>
          <tbody id="jobsBody">
            <tr><td colspan="6" class="text-center text-secondary py-3">No jobs yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Scheduler history table -->
    <div class="card">
      <div class="card-header fw-semibold d-flex justify-content-between align-items-center">
        <span><i class="bi bi-clock-history me-2"></i>Scheduler History</span>
        <span class="badge text-bg-secondary" id="schedulerHistoryCount" style="display:none"></span>
      </div>
      <div class="card-body p-0">
        <table class="table table-sm table-hover mb-0">
          <thead class="table-dark">
            <tr>
              <th>Job</th>
              <th>Status</th>
              <th>Started</th>
              <th>Duration</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody id="schedulerHistoryBody">
            <tr><td colspan="5" class="text-center text-secondary py-3">No scheduler runs recorded yet — runs appear here after the first scheduled job fires.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </main>

  <!-- =====================================================================
       JOB REFERENCE MODAL
       ===================================================================== -->
  <div class="modal fade" id="jobInfoModal" tabindex="-1" aria-labelledby="jobInfoModalLabel" aria-hidden="true">
    <div class="modal-dialog modal-lg modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header">
          <h5 class="modal-title" id="jobInfoModalLabel">
            <i class="bi bi-info-circle me-2"></i>Job Reference
          </h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">

          <!-- Issue Sync -->
          <div class="card job-ref-card mb-3">
            <div class="card-body">
              <h6 class="card-title mb-1">
                <i class="bi bi-arrow-repeat me-2 text-primary"></i>Issue Sync
              </h6>
              <p class="small text-secondary mb-2">
                Re-fetches issues from ComicVine whose <code>date_last_updated</code> falls within the
                look-back window. Use this to pick up corrections made on CV — store dates fixed,
                descriptions updated, credits added — that occurred more than 48 hours ago (older than the
                nightly scheduler's default window).
              </p>
              <div class="mb-1"><span class="param-tag">look-back days</span> <span class="small ms-1">How far back to search. The daily scheduler uses 48 h; set to 30+ days to catch older corrections. Wider windows consume more of the hourly CV API quota.</span></div>
              <div class="mt-2 small text-secondary"><i class="bi bi-lightbulb me-1"></i>Tip: run with 7–14 days after a bulk data import to ensure nothing was missed.</div>
            </div>
          </div>

          <!-- Publisher Repair -->
          <div class="card job-ref-card mb-3">
            <div class="card-body">
              <h6 class="card-title mb-1">
                <i class="bi bi-wrench me-2 text-warning"></i>Publisher Repair
              </h6>
              <p class="small text-secondary mb-2">
                Re-fetches <code>id</code> and <code>publisher</code> from CV for every cached volume,
                then writes the canonical CV publisher IDs back into the database. Needed when the DB was
                seeded by an external tool (e.g. an older <code>sqlite_cv_updater.py</code>) that assigned
                sequential auto-increment publisher IDs instead of CV's own IDs, causing publisher lookups
                to return wrong names or fail.
              </p>
              <div class="mb-1"><span class="param-tag">batch size</span> <span class="small ms-1">Volumes fetched per API call (10–100). Smaller batches use less hourly quota per request and are more resumable if interrupted.</span></div>
              <div class="mb-1"><span class="param-tag">calendar volumes only</span> <span class="small ms-1">Limits repair to volumes that have at least one issue with a <code>store_date</code> — the subset relevant to new-release calendar queries. Typically far less than 1 % of the full catalog. Uncheck only if publisher data for older volumes is also wrong.</span></div>
              <div class="mt-2 small text-secondary"><i class="bi bi-lightbulb me-1"></i>Tip: run once with calendar&nbsp;only checked first. If publishers still look wrong, re-run with it unchecked (slow — uses most of the hourly quota).</div>
            </div>
          </div>

          <!-- FTS Rebuild -->
          <div class="card job-ref-card mb-3">
            <div class="card-body">
              <h6 class="card-title mb-1">
                <i class="bi bi-search me-2 text-info"></i>FTS Rebuild
              </h6>
              <p class="small text-secondary mb-2">
                Drops and rebuilds the FTS5 full-text search shadow tables for volumes, issues, and
                publishers. Under normal operation, database triggers installed at startup keep the index
                in sync with every upsert automatically — this job is only needed if the index becomes
                inconsistent.
              </p>
              <div class="mb-1 small text-secondary">No configurable parameters.</div>
              <div class="mt-2 small text-secondary"><i class="bi bi-lightbulb me-1"></i>Tip: use this after importing data directly into SQLite (bypassing CVProxy's upsert layer) or if searches return stale or missing results.</div>
            </div>
          </div>

          <hr class="my-2">
          <p class="small text-secondary mb-2 fw-semibold">Maintenance</p>

          <!-- Cache Eviction -->
          <div class="card job-ref-card mb-3">
            <div class="card-body">
              <h6 class="card-title mb-1">
                <i class="bi bi-trash3 me-2 text-danger"></i>Cache Eviction
              </h6>
              <p class="small text-secondary mb-2">
                Permanently removes stale entities from the local database and expired entries from the
                response and search caches. Eviction behaviour is controlled entirely by environment
                variables set at startup — this job applies whatever is currently configured:
              </p>
              <ul class="small text-secondary mb-2">
                <li><code>EVICT_OLDER_THAN_YEARS</code> — issues whose <code>cover_date</code> predates the cutoff <em>and</em> haven't been accessed within <code>EVICT_UNACCESSED_DAYS</code> are deleted, then volumes with no remaining issues are cascade-removed.</li>
                <li><code>RESPONSE_CACHE_TTL_DAYS</code> / <code>SEARCH_CACHE_TTL_DAYS</code> — response and search cache rows older than the TTL are removed.</li>
              </ul>
              <div class="mb-1"><span class="param-tag">Dry run</span> <span class="small ms-1">When checked (default), returns the count of rows that <em>would</em> be removed without deleting anything. Uncheck only when you are ready to permanently delete data.</span></div>
              <div class="mt-2 small text-secondary"><i class="bi bi-lightbulb me-1"></i>Tip: always run with dry run first to review the candidate counts, then uncheck and run again to commit.</div>
            </div>
          </div>

          <!-- Image Cache Cleanup -->
          <div class="card job-ref-card mb-0">
            <div class="card-body">
              <h6 class="card-title mb-1">
                <i class="bi bi-images me-2 text-secondary"></i>Image Cache Cleanup
              </h6>
              <p class="small text-secondary mb-2">
                Scans the on-disk image cache and removes any entries whose <code>last_accessed</code>
                timestamp is older than the configured TTL (<code>IMAGE_CACHE_TTL_DAYS</code>, default 14 days).
                This is the same operation the daily scheduler runs automatically — trigger it manually to
                free up disk space immediately.
              </p>
              <div class="mb-1 small text-secondary">No configurable parameters.</div>
              <div class="mt-2 small text-secondary"><i class="bi bi-lightbulb me-1"></i>Tip: lower <code>IMAGE_CACHE_TTL_DAYS</code> in your environment and then run this job to reduce disk usage on space-constrained systems.</div>
            </div>
          </div>

        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Close</button>
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
        if (currentTab === 'jobs') { loadJobs(); loadSchedulerHistory(); }
      });
    });

    // -----------------------------------------------------------------------
    // Shared job status badge
    // -----------------------------------------------------------------------
    function jobStatusBadge(status) {
      const map  = { running: 'text-bg-warning', done: 'text-bg-success', error: 'text-bg-danger', cancelled: 'text-bg-secondary' };
      const icon = { running: 'bi-hourglass-split', done: 'bi-check-circle', error: 'bi-x-circle', cancelled: 'bi-slash-circle' };
      return `<span class="badge ${map[status]||'text-bg-secondary'}">
        <i class="bi ${icon[status]||'bi-question-circle'} me-1"></i>${status}
      </span>`;
    }

    // -----------------------------------------------------------------------
    // Triggered (admin) jobs
    // -----------------------------------------------------------------------
    async function fetchJobs() {
      const r = await fetch('/admin/jobs');
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }

    function renderJobs(jobs) {
      const badge = document.getElementById('jobsBadge');
      const running = jobs.filter(j => j.status === 'running').length;
      if (running > 0) { badge.textContent = running; badge.style.display = ''; }
      else badge.style.display = 'none';

      const tbody = document.getElementById('jobsBody');
      if (!jobs.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-secondary py-3">No jobs yet</td></tr>';
        return;
      }

      tbody.innerHTML = jobs.map(j => {
        const detailId = `detail-${j.job_id}`;
        const params = Object.entries(j.params || {}).map(([k,v]) => `${k}: ${v}`).join(', ') || '\\u2014';
        const resultJson = j.result ? JSON.stringify(j.result, null, 2) : null;
        const canCancel = j.status === 'running';

        return `
          <tr data-job-id="${j.job_id}">
            <td class="align-middle">
              <i class="bi bi-chevron-right expand-btn" data-target="${detailId}" title="Expand"></i>
            </td>
            <td class="align-middle">
              <span class="fw-semibold">${j.name || '\\u2014'}</span>
              <span class="text-secondary small ms-2">${params}</span>
            </td>
            <td class="align-middle">${jobStatusBadge(j.status)}</td>
            <td class="align-middle small">${j.started_at ? new Date(j.started_at).toLocaleString() : '\\u2014'}</td>
            <td class="align-middle small">${j.finished_at ? new Date(j.finished_at).toLocaleString() : '\\u2014'}</td>
            <td class="align-middle text-end">
              ${canCancel
                ? `<button class="btn btn-sm btn-outline-danger cancel-btn" data-job-id="${j.job_id}" title="Cancel job">
                     <i class="bi bi-x-lg"></i> Cancel
                   </button>`
                : ''}
            </td>
          </tr>
          <tr class="job-detail" id="${detailId}" style="display:none">
            <td colspan="6" class="px-4 py-2">
              <div class="mb-1 text-secondary small"><strong>Job ID:</strong> <code>${j.job_id}</code></div>
              ${resultJson
                ? `<div class="mb-1 text-secondary small"><strong>Result:</strong></div><pre>${resultJson}</pre>`
                : '<span class="text-secondary small">No result yet.</span>'}
            </td>
          </tr>`;
      }).join('');

      tbody.querySelectorAll('.expand-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const row = document.getElementById(btn.dataset.target);
          const hidden = row.style.display === 'none';
          row.style.display = hidden ? '' : 'none';
          btn.classList.toggle('open', hidden);
        });
      });

      tbody.querySelectorAll('.cancel-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          try {
            await fetch(`/admin/jobs/${btn.dataset.jobId}`, { method: 'DELETE' });
            await loadJobs();
          } catch(e) { console.error('Cancel failed:', e); btn.disabled = false; }
        });
      });
    }

    async function loadJobs() {
      try { renderJobs(await fetchJobs()); }
      catch(e) { console.error('Jobs load failed:', e); }
    }

    document.getElementById('refreshJobsBtn').addEventListener('click', () => {
      const icon = document.querySelector('#refreshJobsBtn .bi');
      icon.classList.add('spin');
      Promise.all([loadJobs(), loadSchedulerHistory()]).finally(() => setTimeout(() => icon.classList.remove('spin'), 400));
    });

    // -----------------------------------------------------------------------
    // Scheduler history
    // -----------------------------------------------------------------------
    async function loadSchedulerHistory() {
      try {
        const r = await fetch('/admin/scheduler/history');
        if (!r.ok) return;
        renderSchedulerHistory(await r.json());
      } catch(e) { console.error('Scheduler history load failed:', e); }
    }

    function fmtDuration(entry) {
      if (entry.status === 'running') return '<span class="text-warning">running\\u2026</span>';
      if (!entry.started_at || !entry.finished_at) return '\\u2014';
      const secs = (new Date(entry.finished_at) - new Date(entry.started_at)) / 1000;
      return secs >= 60 ? (secs / 60).toFixed(1) + ' m' : secs.toFixed(1) + ' s';
    }

    function fmtResult(result) {
      if (!result) return '<span class="text-secondary">\\u2014</span>';
      if (result.error) return `<span class="text-danger small">${result.error}</span>`;
      return Object.entries(result)
        .map(([k, v]) => `<span class="text-secondary small">${k.replace(/_/g,' ')}:</span> <strong>${v}</strong>`)
        .join(' &nbsp; ');
    }

    function renderSchedulerHistory(entries) {
      const badge = document.getElementById('schedulerHistoryCount');
      if (entries.length) { badge.textContent = entries.length; badge.style.display = ''; }
      else badge.style.display = 'none';

      const tbody = document.getElementById('schedulerHistoryBody');
      if (!entries.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-secondary py-3">No scheduler runs recorded yet — runs appear here after the first scheduled job fires.</td></tr>';
        return;
      }
      tbody.innerHTML = entries.map(e => `
        <tr>
          <td class="fw-semibold">${e.name}</td>
          <td>${jobStatusBadge(e.status)}</td>
          <td class="small">${e.started_at ? new Date(e.started_at).toLocaleString() : '\\u2014'}</td>
          <td class="small">${fmtDuration(e)}</td>
          <td class="small">${fmtResult(e.result)}</td>
        </tr>
      `).join('');
    }

    // -----------------------------------------------------------------------
    // Run job form
    // -----------------------------------------------------------------------
    const jobTypeEl        = document.getElementById('jobType');
    const daysWrap         = document.getElementById('daysFieldWrap');
    const batchWrap        = document.getElementById('batchFieldWrap');
    const calendarOnlyWrap = document.getElementById('calendarOnlyWrap');
    const dryRunWrap       = document.getElementById('dryRunWrap');
    const evictInfoWrap    = document.getElementById('evictInfoWrap');
    const runJobBtn        = document.getElementById('runJobBtn');
    const jobFeedback      = document.getElementById('runJobFeedback');

    let _evictSettings = null;

    function updateEvictInfo(settings) {
      _evictSettings = settings;
      const el = document.getElementById('evictInfoText');
      if (!settings) { el.textContent = 'Eviction settings unavailable.'; return; }
      const parts = [];
      if (settings.evict_older_than_years > 0)
        parts.push(`issues/volumes older than <strong>${settings.evict_older_than_years} years</strong> (unaccessed for ${settings.evict_unaccessed_days}d)`);
      if (settings.response_cache_ttl_days > 0)
        parts.push(`response cache entries older than <strong>${settings.response_cache_ttl_days} days</strong>`);
      if (settings.search_cache_ttl_days > 0)
        parts.push(`search cache entries older than <strong>${settings.search_cache_ttl_days} days</strong>`);
      if (settings.cache_cutoff_year > 0)
        parts.push(`ingest filter: skipping content before <strong>${settings.cache_cutoff_year}</strong> (already applied on write)`);
      el.innerHTML = parts.length
        ? '<i class="bi bi-gear me-1"></i>Will evict: ' + parts.join(' &bull; ')
        : '<i class="bi bi-exclamation-triangle me-1 text-warning"></i>No eviction configured — set <code>EVICT_OLDER_THAN_YEARS</code> or <code>RESPONSE_CACHE_TTL_DAYS</code> to enable.';
    }

    jobTypeEl.addEventListener('change', () => {
      const v = jobTypeEl.value;
      daysWrap.style.display         = v === 'sync-issues'       ? '' : 'none';
      batchWrap.style.display        = v === 'repair-publishers' ? '' : 'none';
      calendarOnlyWrap.style.display = v === 'repair-publishers' ? '' : 'none';
      dryRunWrap.style.display       = v === 'evict'             ? '' : 'none';
      evictInfoWrap.style.display    = v === 'evict'             ? '' : 'none';
      if (v === 'evict' && !_evictSettings) {
        fetch('/dashboard/data').then(r => r.json()).then(d => updateEvictInfo(d.eviction_settings || null)).catch(() => {});
      }
    });

    runJobBtn.addEventListener('click', async () => {
      runJobBtn.disabled = true;
      jobFeedback.style.display = 'none';
      try {
        let url, resp;
        if (jobTypeEl.value === 'sync-issues') {
          const days = parseFloat(document.getElementById('syncDays').value) || 14;
          url = `/admin/sync/issues?days=${days}`;
        } else if (jobTypeEl.value === 'repair-publishers') {
          const batch   = parseInt(document.getElementById('repairBatch').value) || 100;
          const calOnly = document.getElementById('calendarOnly').checked;
          url = `/admin/repair/publishers?batch_size=${batch}&calendar_only=${calOnly}`;
        } else if (jobTypeEl.value === 'evict') {
          const dryRun = document.getElementById('evictDryRun').checked;
          url = `/admin/evict?dry_run=${dryRun}`;
        } else if (jobTypeEl.value === 'cleanup-images') {
          url = '/admin/cleanup/images';
        } else {
          url = '/admin/rebuild/fts';
        }
        resp = await fetch(url, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.status);
        jobFeedback.innerHTML =
          `<div class="alert alert-success py-1 mb-0 small">
             <i class="bi bi-check-circle me-1"></i>
             Job started &mdash; ID: <code>${data.job_id}</code>
           </div>`;
        jobFeedback.style.display = '';
        await loadJobs();
      } catch(e) {
        jobFeedback.innerHTML =
          `<div class="alert alert-danger py-1 mb-0 small">
             <i class="bi bi-x-circle me-1"></i> ${e.message}
           </div>`;
        jobFeedback.style.display = '';
      } finally {
        runJobBtn.disabled = false;
      }
    });

    // Keep jobs badge updated even when on stats tab
    function refreshJobsBadge() {
      fetchJobs().then(jobs => {
        const badge = document.getElementById('jobsBadge');
        const running = jobs.filter(j => j.status === 'running').length;
        if (running > 0) { badge.textContent = running; badge.style.display = ''; }
        else badge.style.display = 'none';
        if (currentTab === 'jobs') renderJobs(jobs);
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
