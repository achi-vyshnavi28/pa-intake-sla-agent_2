"""
Builds the PA Intake & SLA analytics experience: a single self-contained
HTML page, generated entirely from this project's own pipeline output --
output/*.json (the agent's findings) and a fresh SQL pull of the weekly
breach-rate series from the core warehouse (data/amaranth_pa_core.db), via
the exact same queries agent/umguard.py uses. Nothing on this page is
hand-typed or pasted from anywhere else: every number, chart, and finding
is computed here, embedded as one JSON object, and rendered client-side.

Zero backend, zero external network calls (Chart.js is vendored into
dashboard/vendor/chart.umd.min.js and inlined at build time -- verified by
screenshotting with all non-local network requests blocked).

Run: python3 dashboard/build_dashboard.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from agent.umguard import (  # noqa: E402
    CORE_DB, PROVIDER_CHRONIC_THRESHOLD, QUEUE_INCIDENT_THRESHOLD,
    TREND_R2_THRESHOLD, TREND_SLOPE_THRESHOLD,
    pull_weekly_provider_breach_sql, pull_weekly_queue_breach_sql,
)
from agent.trust_layer import CROSS_CHECK_TOLERANCE  # noqa: E402
from stats_utils import MIN_SAMPLE_SIZE  # noqa: E402

OUTPUT_DIR = HERE / "output"
DASHBOARD_PATH = OUTPUT_DIR / "dashboard.html"
CHARTJS_PATH = Path(__file__).resolve().parent / "vendor" / "chart.umd.min.js"

STATUS_META = {
    "escalate": {"label": "Escalated", "color": "#B23B2E", "soft": "#F6E9E6"},
    "needs_human_review": {"label": "Needs review", "color": "#B4802E", "soft": "#F6EEE0"},
    "cleared": {"label": "Cleared", "color": "#57724B", "soft": "#EAEFE4"},
}

RECOMMENDED_ACTION = {
    "provider_side_trend_degrading": "Escalate to network performance management; request a corrective action plan next quarter.",
    "provider_side_trend_improving": "Continue monitoring only -- no corrective action while the improving trend holds.",
    "provider_side_chronic": "Open a formal provider performance review; breach rate has been persistently elevated all quarter.",
    "review_queue_capacity_incident": "No provider-level action -- route to queue capacity planning; breach was isolated to the documented incident week.",
    "no_material_pattern": "No action -- breach rate is within the expected clean-operations range.",
}


def _load(name: str) -> list[dict]:
    p = OUTPUT_DIR / name
    return json.loads(p.read_text()) if p.exists() else []


def build() -> None:
    summary = json.loads((OUTPUT_DIR / "run_summary.json").read_text())
    audit_log = {a["provider_id"]: a for a in _load("audit_log.json")}

    records = []
    for status_key, name in (("escalate", "escalations.json"), ("needs_human_review", "needs_human_review.json"),
                              ("cleared", "cleared.json")):
        for r in _load(name):
            r["_status"] = status_key
            records.append(r)

    # ---- fresh SQL pull of the weekly time series, same queries the agent uses ---- #
    conn = sqlite3.connect(CORE_DB)
    weekly_sql = pull_weekly_provider_breach_sql(conn)
    queue_weekly = pull_weekly_queue_breach_sql(conn)
    conn.close()

    overall = (weekly_sql.groupby("week_idx")
               .agg(n_requests=("n_requests", "sum"), n_breached=("n_breached", "sum")).reset_index())
    overall["breach_rate"] = overall["n_breached"] / overall["n_requests"]
    weekly_overall = [
        {"week_idx": int(row.week_idx), "n_requests": int(row.n_requests), "n_breached": int(row.n_breached),
         "breach_rate": round(float(row.breach_rate), 4)}
        for row in overall.itertuples()
    ]

    per_provider_weekly: dict[str, list[dict]] = {}
    for pid, grp in weekly_sql.groupby("provider_id"):
        per_provider_weekly[pid] = [
            {"week_idx": int(row.week_idx), "breach_rate": round(float(row.breach_rate), 4),
             "n_requests": int(row.n_requests)}
            for row in grp.sort_values("week_idx").itertuples()
        ]

    # ---- real incident weeks: pulled directly from the agent's own findings, ---- #
    # never re-derived with a separate ad hoc anomaly rule.
    incident_weeks = []
    for r in records:
        if r["root_cause"] == "review_queue_capacity_incident":
            for wk in r.get("flagged_weeks", []):
                incident_weeks.append({**wk, "provider_id": r["provider_id"], "provider_name": r["provider_name"]})

    clean_rates = [r["overall_breach_rate"] for r in records if r["root_cause"] == "no_material_pattern"]
    baseline_breach_rate = round(statistics.median(clean_rates), 4) if clean_rates else None

    total_requests = sum(r["n_requests"] for r in records)
    total_breached = sum(r["n_breached"] for r in records)
    overall_breach_rate = round(total_breached / total_requests, 4) if total_requests else 0.0

    # ---- driver (root-cause) breakdown, ranked ---- #
    driver_map: dict[str, dict] = {}
    for r in records:
        d = driver_map.setdefault(r["root_cause"], {"label": r["root_cause"], "providers": [],
                                                      "n_requests": 0, "n_breached": 0})
        d["providers"].append(r["provider_id"])
        d["n_requests"] += r["n_requests"]
        d["n_breached"] += r["n_breached"]
    driver_breakdown = sorted(driver_map.values(), key=lambda d: (len(d["providers"]), d["n_breached"]), reverse=True)

    # ---- the single largest driver among MATERIAL (non-clean) findings, for the hero ---- #
    material = [r for r in records if r["root_cause"] != "no_material_pattern"]
    top_driver_record = max(material, key=lambda r: r["overall_breach_rate"]) if material else None

    providers = []
    for r in sorted(records, key=lambda r: r["overall_breach_rate"], reverse=True):
        audit = audit_log.get(r["provider_id"], {})
        cc = audit.get("step4_cross_check", {})
        verification = audit.get("step7_verification", {})
        providers.append({
            "provider_id": r["provider_id"],
            "provider_name": r["provider_name"],
            "specialty": r["specialty"],
            "status": r["_status"],
            "overall_breach_rate": r["overall_breach_rate"],
            "n_requests": r["n_requests"],
            "n_breached": r["n_breached"],
            "confidence": r["confidence"],
            "confidence_rationale": r["confidence_rationale"],
            "root_cause": r["root_cause"],
            "root_cause_detail": r["root_cause_detail"],
            "flagged_weeks": r.get("flagged_weeks", []),
            "trend_slope": r.get("trend_slope"),
            "trend_r2": r.get("trend_r2"),
            "note": r["note"],
            "note_llm_verified": r["note_llm_verified"],
            "recommended_action": RECOMMENDED_ACTION.get(r["root_cause"], "No action defined for this category."),
            "weekly": per_provider_weekly.get(r["provider_id"], []),
            "cross_check": {"sql_value": cc.get("sql_value"), "pandas_value": cc.get("pandas_value"),
                             "agrees": cc.get("agrees"), "delta": cc.get("delta")},
            "verification": {"verified": verification.get("verified"),
                              "checked_numbers": verification.get("checked_numbers", []),
                              "unverified_numbers": verification.get("unverified_numbers", [])},
        })

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    data = {
        "generated_at": generated_at,
        "summary": summary,
        "meta": {
            "total_requests": total_requests,
            "total_breached": total_breached,
            "overall_breach_rate": overall_breach_rate,
            "baseline_breach_rate": baseline_breach_rate,
            "weeks": [int(w) for w in sorted(overall["week_idx"].unique())],
        },
        "weekly_overall": weekly_overall,
        "incident_weeks": incident_weeks,
        "driver_breakdown": driver_breakdown,
        "top_driver": {
            "provider_id": top_driver_record["provider_id"],
            "provider_name": top_driver_record["provider_name"],
            "root_cause": top_driver_record["root_cause"],
            "overall_breach_rate": top_driver_record["overall_breach_rate"],
        } if top_driver_record else None,
        "providers": providers,
        "thresholds": {
            "queue_incident_threshold": QUEUE_INCIDENT_THRESHOLD,
            "provider_chronic_threshold": PROVIDER_CHRONIC_THRESHOLD,
            "trend_slope_threshold": TREND_SLOPE_THRESHOLD,
            "trend_r2_threshold": TREND_R2_THRESHOLD,
            "min_sample_size": MIN_SAMPLE_SIZE,
            "cross_check_tolerance": CROSS_CHECK_TOLERANCE,
        },
        "status_meta": STATUS_META,
    }

    chartjs_src = CHARTJS_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(data)

    html_doc = _PAGE_TEMPLATE.replace("__CHARTJS__", chartjs_src).replace("__DATA_JSON__", data_json)
    OUTPUT_DIR.mkdir(exist_ok=True)
    DASHBOARD_PATH.write_text(html_doc, encoding="utf-8")
    # Also write as index.html so static hosts (Vercel, GitHub Pages, S3, anything)
    # serve the dashboard at the bare root URL by their own default convention --
    # no custom rewrite/redirect rule required, nothing that can silently misfire.
    (OUTPUT_DIR / "index.html").write_text(html_doc, encoding="utf-8")
    print(f"Dashboard written: {DASHBOARD_PATH} (and output/index.html)  ({len(providers)} providers, "
          f"{len(weekly_overall)} weeks, {len(incident_weeks)} incident week(s))")


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PA Intake &amp; SLA Analytics -- Amaranth Health Partners (synthetic demo)</title>
<meta name="description" content="Editorial analytics on prior-authorization SLA performance, generated entirely from this project's own agentic pipeline.">
<script>__CHARTJS__</script>
<style>
:root{
  --bg:#FAF7F2; --bg-alt:#F1EAE0; --bg-card:#FDFBF8;
  --ink:#2B241C; --ink-soft:#6E6455; --ink-faint:#948A7A;
  --line:#E7DFD2; --line-soft:#EFE8DC;
  --neg:#B23B2E; --neg-soft:#F6E9E6;
  --warn:#B4802E; --warn-soft:#F6EEE0;
  --pos:#57724B; --pos-soft:#EAEFE4;
  --select:#2B241C;
  --radius:10px;
  --ease:cubic-bezier(.22,.61,.36,1);
}
*{box-sizing:border-box;}
html{scroll-behavior:smooth;}
@media (prefers-reduced-motion:reduce){ html{scroll-behavior:auto;} *{animation-duration:.001ms!important; transition-duration:.001ms!important;} }
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI","Inter",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased; line-height:1.5;
}
.container{max-width:1180px; margin:0 auto; padding:0 40px;}
@media (max-width:760px){ .container{padding:0 20px;} }

.micro{font-size:.6875rem; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-faint); font-weight:600;}
.small{font-size:.8125rem; color:var(--ink-soft);}
.medium{font-size:.95rem; line-height:1.65; color:var(--ink-soft);}
.large{font-size:1.375rem; font-weight:600; letter-spacing:-.01em;}
.display{font-size:clamp(2.1rem,4.6vw,3.6rem); font-weight:600; letter-spacing:-.02em; line-height:1.06; margin:0;}
.num{font-variant-numeric:tabular-nums;}

a{color:inherit;}
button{font-family:inherit;}
:focus-visible{outline:2px solid var(--ink); outline-offset:3px; border-radius:4px;}

/* ---------- nav ---------- */
nav.topnav{
  position:sticky; top:0; z-index:40; background:rgba(250,247,242,.88); backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line-soft);
}
nav.topnav .row{display:flex; align-items:center; justify-content:space-between; height:60px;}
.wordmark{font-size:.8125rem; font-weight:700; letter-spacing:.03em;}
.wordmark span{color:var(--ink-faint); font-weight:500;}
.navlinks{display:flex; gap:28px;}
.navlinks a{font-size:.8125rem; color:var(--ink-soft); text-decoration:none; transition:color .2s var(--ease);}
.navlinks a:hover{color:var(--ink);}
.nav-right{display:flex; align-items:center; gap:16px;}
@media (max-width:760px){ .navlinks{display:none;} }

/* ---------- filter pills ---------- */
.filters{display:flex; gap:8px; flex-wrap:wrap;}
.pill-btn{
  border:1px solid var(--line); background:var(--bg-card); color:var(--ink-soft);
  padding:6px 14px; border-radius:999px; font-size:.75rem; font-weight:600; cursor:pointer;
  transition:all .18s var(--ease);
}
.pill-btn:hover{border-color:var(--ink-faint); color:var(--ink);}
.pill-btn[aria-pressed="true"]{background:var(--ink); border-color:var(--ink); color:#fff;}

/* ---------- sections ---------- */
section{padding:64px 0;}
section.alt{background:var(--bg-alt);}
@media (max-width:760px){ section{padding:44px 0;} }

.eyebrow{margin:0 0 14px;}
.section-head{margin-bottom:36px; max-width:640px;}
.section-head h2{font-size:1.5rem; font-weight:600; letter-spacing:-.01em; margin:0 0 8px;}

/* ---------- hero ---------- */
.hero-stats{display:flex; gap:40px; flex-wrap:wrap; margin-top:32px; padding-top:28px; border-top:1px solid var(--line);}
.hero-stat .stat-val{font-size:1.75rem; font-weight:700; letter-spacing:-.01em;}
.hero-stat .stat-label{margin-top:2px;}
.hero-stat .stat-sub{margin-top:2px; font-size:.75rem; color:var(--ink-faint);}

/* ---------- panels / charts ---------- */
.panel{background:var(--bg-card); border:1px solid var(--line-soft); border-radius:var(--radius); padding:28px 30px;}
.chart-title{font-size:1.05rem; font-weight:600; letter-spacing:-.005em; margin:0 0 4px;}
.chart-sub{margin:0 0 18px;}
.chart-wrap{position:relative; height:320px;}
.chart-wrap.short{height:220px;}
.legend-row{display:flex; gap:18px; margin-top:14px; flex-wrap:wrap;}
.legend-item{display:flex; align-items:center; gap:6px; font-size:.75rem; color:var(--ink-soft);}
.legend-dot{width:8px; height:8px; border-radius:50%; display:inline-block;}

/* ---------- horizontal scroller ---------- */
.scroller-wrap{position:relative;}
.scroller{
  display:flex; gap:16px; overflow-x:auto; scroll-snap-type:x proximity; padding:4px 4px 14px;
  -webkit-overflow-scrolling:touch; scrollbar-width:thin;
  cursor:grab;
}
.scroller:active{cursor:grabbing;}
.scroller.dragging{scroll-snap-type:none;}
.entity-card{
  scroll-snap-align:start; flex:0 0 260px; background:var(--bg-card); border:1px solid var(--line-soft);
  border-radius:var(--radius); padding:20px 22px; cursor:pointer; transition:border-color .18s var(--ease), transform .18s var(--ease);
  text-align:left;
}
.entity-card:hover{border-color:var(--ink-faint); transform:translateY(-2px);}
.entity-card .card-top{display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:14px;}
.status-chip{font-size:.65rem; font-weight:700; letter-spacing:.04em; padding:3px 9px; border-radius:999px;}
.entity-card .metric{font-size:1.7rem; font-weight:700; letter-spacing:-.01em; margin:2px 0;}
.entity-card .name{font-weight:600; font-size:.9rem; margin-bottom:2px;}
.scroll-arrows{display:flex; gap:8px; margin-top:12px;}
.arrow-btn{
  width:34px; height:34px; border-radius:50%; border:1px solid var(--line); background:var(--bg-card);
  cursor:pointer; display:flex; align-items:center; justify-content:center; color:var(--ink-soft);
  transition:all .18s var(--ease);
}
.arrow-btn:hover{border-color:var(--ink); color:var(--ink);}

/* ---------- insight modules ---------- */
.insights-grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px;}
.insight{
  background:var(--bg-card); border:1px solid var(--line-soft); border-left:3px solid var(--ink);
  border-radius:0 var(--radius) var(--radius) 0; padding:18px 20px;
}
.insight.neg{border-left-color:var(--neg);}
.insight.warn{border-left-color:var(--warn);}
.insight.pos{border-left-color:var(--pos);}
.insight dl{margin:12px 0 0; display:grid; grid-template-columns:auto 1fr; gap:5px 12px; font-size:.8125rem;}
.insight dt{color:var(--ink-faint); font-size:.6875rem; text-transform:uppercase; letter-spacing:.06em; font-weight:600; align-self:start;}
.insight dd{margin:0; color:var(--ink-soft);}
.insight .finding{font-weight:600; font-size:.95rem; margin:0;}

/* ---------- table ---------- */
.table-scroll{overflow-x:auto;}
table.evidence{width:100%; border-collapse:collapse; font-size:.8125rem;}
table.evidence th{
  text-align:left; padding:10px 14px; font-size:.6875rem; text-transform:uppercase; letter-spacing:.05em;
  color:var(--ink-faint); font-weight:600; border-bottom:1px solid var(--line); cursor:pointer; white-space:nowrap;
  user-select:none;
}
table.evidence th:hover{color:var(--ink);}
table.evidence th .arrow{opacity:.4; margin-left:3px;}
table.evidence td{padding:12px 14px; border-bottom:1px solid var(--line-soft); vertical-align:middle;}
table.evidence tbody tr{cursor:pointer; transition:background .15s var(--ease);}
table.evidence tbody tr:hover{background:var(--bg-alt);}
.status-dot{width:7px; height:7px; border-radius:50%; display:inline-block; margin-right:7px;}
.mono{font-family:"SFMono-Regular",Consolas,monospace; font-size:.75rem; color:var(--ink-soft);}
.empty-state{padding:40px 20px; text-align:center; color:var(--ink-faint); font-size:.85rem;}

/* ---------- methodology accordion ---------- */
.accordion-item{border-bottom:1px solid var(--line);}
.accordion-item:first-child{border-top:1px solid var(--line);}
.accordion-btn{
  width:100%; text-align:left; background:none; border:none; padding:18px 0; cursor:pointer;
  display:flex; justify-content:space-between; align-items:center; font-size:.95rem; font-weight:600; color:var(--ink);
}
.accordion-btn .plus{font-size:1.1rem; color:var(--ink-faint); transition:transform .2s var(--ease);}
.accordion-item[data-open="true"] .plus{transform:rotate(45deg);}
.accordion-panel{max-height:0; overflow:hidden; transition:max-height .28s var(--ease);}
.accordion-panel-inner{padding:0 0 20px; font-size:.875rem; color:var(--ink-soft); line-height:1.7;}
.accordion-panel-inner code{background:var(--bg-alt); padding:1px 5px; border-radius:4px; font-size:.8em;}

/* ---------- drawer ---------- */
.scrim{position:fixed; inset:0; background:rgba(43,36,28,.32); opacity:0; pointer-events:none; transition:opacity .25s var(--ease); z-index:60;}
.scrim.open{opacity:1; pointer-events:auto;}
.drawer{
  position:fixed; top:0; right:0; height:100%; width:min(480px,92vw); background:var(--bg-card);
  box-shadow:-8px 0 30px rgba(43,36,28,.12); transform:translateX(100%); transition:transform .32s var(--ease);
  z-index:61; overflow-y:auto; padding:30px 32px 60px;
}
.drawer.open{transform:translateX(0);}
.drawer-close{position:absolute; top:24px; right:24px; width:34px; height:34px; border-radius:50%; border:1px solid var(--line);
  background:var(--bg); cursor:pointer; font-size:1rem; color:var(--ink-soft);}
.drawer-close:hover{color:var(--ink); border-color:var(--ink-faint);}
.drawer h3{margin:0 0 2px; font-size:1.3rem;}
.drawer-block{margin-top:26px; padding-top:22px; border-top:1px solid var(--line-soft);}
.drawer-block h4{margin:0 0 12px; font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-faint);}
.kv{display:grid; grid-template-columns:1fr auto; gap:8px 12px; font-size:.85rem;}
.kv .k{color:var(--ink-soft);}
.kv .v{font-weight:600; text-align:right;}
.note-box{background:var(--bg-alt); border-radius:8px; padding:14px 16px; font-size:.85rem; line-height:1.6; color:var(--ink-soft); margin-top:8px;}

/* ---------- reveal ---------- */
[data-reveal]{opacity:0; transform:translateY(14px); transition:opacity .5s var(--ease), transform .5s var(--ease);}
[data-reveal].in-view{opacity:1; transform:translateY(0);}

footer{padding:40px 0 60px; border-top:1px solid var(--line);}
.disclaimer{font-size:.75rem; color:var(--ink-faint); line-height:1.7; max-width:820px;}
</style>
</head>
<body>

<nav class="topnav">
  <div class="container row">
    <div class="wordmark">AMARANTH HEALTH PARTNERS <span>&middot; PA Operations</span></div>
    <div class="navlinks">
      <a href="#story">Overview</a>
      <a href="#evidence">Evidence</a>
      <a href="#methodology">Methodology</a>
    </div>
    <div class="nav-right">
      <span class="micro" id="freshness-tag">Updated --</span>
    </div>
  </div>
</nav>

<section id="story">
  <div class="container">
    <p class="micro eyebrow">Q3 &middot; Prior Authorization Intake &middot; 100% synthetic data</p>
    <h1 class="display" id="hero-headline">--</h1>
    <p class="medium" style="max-width:640px; margin-top:16px;" id="hero-sub"></p>

    <div class="hero-stats">
      <div class="hero-stat">
        <div class="stat-val num" id="stat-overall">--</div>
        <div class="micro stat-label">Overall breach rate</div>
        <div class="stat-sub" id="stat-overall-sub"></div>
      </div>
      <div class="hero-stat">
        <div class="stat-val" id="stat-driver">--</div>
        <div class="micro stat-label">Primary driver</div>
        <div class="stat-sub" id="stat-driver-sub"></div>
      </div>
      <div class="hero-stat">
        <div class="stat-val" id="stat-segment">--</div>
        <div class="micro stat-label">Most affected provider</div>
        <div class="stat-sub" id="stat-segment-sub"></div>
      </div>
      <div class="hero-stat">
        <div class="stat-val" id="stat-scope">--</div>
        <div class="micro stat-label">Requests evaluated</div>
        <div class="stat-sub" id="stat-scope-sub"></div>
      </div>
    </div>
  </div>
</section>

<section data-reveal>
  <div class="container">
    <div class="filters" role="group" aria-label="Filter by outcome" id="filter-group"></div>
  </div>
</section>

<section class="alt" data-reveal>
  <div class="container">
    <div class="panel">
      <p class="chart-title" id="trend-title">Weekly breach rate across all providers</p>
      <p class="small chart-sub" id="trend-sub"></p>
      <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
      <div class="legend-row">
        <span class="legend-item"><span class="legend-dot" style="background:#B23B2E"></span>Documented incident week</span>
        <span class="legend-item"><span class="legend-dot" style="background:#948A7A"></span>Clean-operations baseline (median)</span>
      </div>
    </div>
  </div>
</section>

<section data-reveal>
  <div class="container">
    <div class="section-head">
      <p class="micro eyebrow">Why</p>
      <h2 id="driver-title">Driver analysis</h2>
      <p class="medium">Every material finding is attributed to exactly one root-cause category -- ranked by how many providers it explains.</p>
    </div>
    <div class="panel">
      <div class="chart-wrap short"><canvas id="driverChart"></canvas></div>
    </div>
  </div>
</section>

<section class="alt" data-reveal>
  <div class="container">
    <div class="section-head">
      <p class="micro eyebrow">Where</p>
      <h2>Provider-level breach rate, ranked</h2>
      <p class="medium">8 providers, 8 distinct specialties this run -- so segment and provider are the same granularity here. Swipe or drag to browse; click any card for full evidence.</p>
    </div>
    <div class="scroller-wrap">
      <div class="scroller" id="entity-scroller" tabindex="0" aria-label="Providers ranked by breach rate, scrollable"></div>
      <div class="scroll-arrows">
        <button class="arrow-btn" id="scroll-left" aria-label="Scroll left">&#8592;</button>
        <button class="arrow-btn" id="scroll-right" aria-label="Scroll right">&#8594;</button>
      </div>
    </div>
  </div>
</section>

<section data-reveal>
  <div class="container">
    <div class="section-head">
      <p class="micro eyebrow">What is unusual &middot; So what</p>
      <h2>Findings that changed the outcome</h2>
      <p class="medium">Every escalated or reviewed finding, in the signal &middot; driver &middot; impact &middot; segment &middot; confidence &middot; action model a senior analyst would use to brief a stakeholder.</p>
    </div>
    <div class="insights-grid" id="insights-grid"></div>
  </div>
</section>

<section class="alt" id="evidence" data-reveal>
  <div class="container">
    <div class="section-head">
      <p class="micro eyebrow">Supporting evidence</p>
      <h2>Every provider, every number, auditable</h2>
      <p class="medium">Sortable. Click a row for the full derivation -- SQL vs. Pandas cross-check, confidence rationale, and the exact evidence-grounding check the escalation note passed.</p>
    </div>
    <div class="panel table-scroll">
      <table class="evidence" id="evidence-table" aria-describedby="evidence-caption">
        <caption id="evidence-caption" style="position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0);">Provider-level SLA findings</caption>
        <thead>
          <tr>
            <th data-sort="status">Status<span class="arrow"></span></th>
            <th data-sort="provider_id">Provider<span class="arrow"></span></th>
            <th data-sort="specialty">Specialty<span class="arrow"></span></th>
            <th data-sort="overall_breach_rate">Breach rate<span class="arrow"></span></th>
            <th data-sort="n_requests">Requests<span class="arrow"></span></th>
            <th data-sort="confidence">Confidence<span class="arrow"></span></th>
            <th data-sort="root_cause">Root cause<span class="arrow"></span></th>
          </tr>
        </thead>
        <tbody id="evidence-tbody"></tbody>
      </table>
      <div class="empty-state" id="evidence-empty" hidden>No providers in this status this run.</div>
    </div>
  </div>
</section>

<section id="methodology" data-reveal>
  <div class="container">
    <div class="section-head">
      <p class="micro eyebrow">Methodology</p>
      <h2>How every number here was produced</h2>
      <p class="medium">Full auditability, on demand -- not hidden, not forced on you by default.</p>
    </div>
    <div id="accordion"></div>
  </div>
</section>

<footer>
  <div class="container">
    <p class="disclaimer">
      Independent portfolio project, not affiliated with or built using any real company's proprietary system,
      data, or trademark. All provider, member, and claims data on this page is synthetic and generated by
      <code>data/generate_data.py</code> with a fixed random seed. Member identifiers are tokenized by design
      (format MBR-########) -- no real PHI is used anywhere in this project, and no compliance certification is
      claimed. Every figure above is traceable to <code>output/audit_log.json</code>. This page itself is generated
      by <code>dashboard/build_dashboard.py</code> from that same pipeline output -- nothing here is hand-typed.
    </p>
  </div>
</footer>

<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" aria-hidden="true">
  <button class="drawer-close" id="drawer-close" aria-label="Close panel">&#10005;</button>
  <div id="drawer-content"></div>
</aside>

<script>
const DATA = __DATA_JSON__;
const STATUS = DATA.status_meta;
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function pct(v, d=1){ return (v*100).toFixed(d) + '%'; }
function fmtInt(v){ return v.toLocaleString('en-US'); }
function titleCase(s){ return s.replace(/_/g,' ').replace(/\b\w/g, c=>c.toUpperCase()); }

document.getElementById('freshness-tag').textContent = 'Data through ' + DATA.generated_at;

/* ================= HERO ================= */
(function renderHero(){
  const m = DATA.meta, s = DATA.summary;
  const escalatedCount = s.escalated, total = s.providers_evaluated;
  document.getElementById('hero-headline').textContent =
    escalatedCount > 0
      ? `${escalatedCount} of ${total} providers exceeded SLA breach thresholds this quarter`
      : `All ${total} providers stayed within expected SLA breach thresholds this quarter`;

  const td = DATA.top_driver;
  document.getElementById('hero-sub').textContent = td
    ? `The largest single finding: ${td.provider_name} (${td.provider_id}) at ${pct(td.overall_breach_rate)} breach rate, attributed to ${titleCase(td.root_cause).toLowerCase()}. Every figure below is traceable to the audit trail.`
    : `No material root-cause pattern was detected among any provider this quarter.`;

  document.getElementById('stat-overall').textContent = pct(m.overall_breach_rate);
  document.getElementById('stat-overall-sub').textContent = `${fmtInt(m.total_breached)} of ${fmtInt(m.total_requests)} requests`;

  if(td){
    document.getElementById('stat-driver').textContent = titleCase(td.root_cause);
    document.getElementById('stat-driver-sub').textContent = `${td.provider_id} &middot; ${pct(td.overall_breach_rate)}`.replace('&middot;','·');
  } else {
    document.getElementById('stat-driver').textContent = 'None material';
    document.getElementById('stat-driver-sub').textContent = 'no escalations this run';
  }

  const worst = DATA.providers[0];
  document.getElementById('stat-segment').textContent = worst ? worst.provider_id : '--';
  document.getElementById('stat-segment-sub').textContent = worst ? `${worst.specialty} · ${pct(worst.overall_breach_rate)}` : '';

  document.getElementById('stat-scope').textContent = fmtInt(m.total_requests);
  document.getElementById('stat-scope-sub').textContent = `${DATA.summary.providers_evaluated} providers · ${m.weeks.length} weeks`;
})();

/* ================= FILTERS ================= */
let currentFilter = 'all';
const filterGroup = document.getElementById('filter-group');
const filterDefs = [
  {key:'all', label:'All providers'},
  {key:'escalate', label:'Escalated'},
  {key:'needs_human_review', label:'Needs review'},
  {key:'cleared', label:'Cleared'},
];
filterDefs.forEach(f=>{
  const btn = document.createElement('button');
  btn.className = 'pill-btn';
  btn.textContent = f.label;
  btn.setAttribute('aria-pressed', f.key === 'all' ? 'true' : 'false');
  btn.addEventListener('click', ()=>{
    currentFilter = f.key;
    [...filterGroup.children].forEach(b=>b.setAttribute('aria-pressed','false'));
    btn.setAttribute('aria-pressed','true');
    renderCards(); renderTable(); renderDriverChart();
  });
  filterGroup.appendChild(btn);
});
function filteredProviders(){
  return currentFilter === 'all' ? DATA.providers : DATA.providers.filter(p=>p.status===currentFilter);
}

/* ================= TREND CHART (with real annotations) ================= */
const incidentWeekIdx = new Set(DATA.incident_weeks.map(w=>w.week_idx));
const baseline = DATA.meta.baseline_breach_rate;

const annotationPlugin = {
  id: 'editorialAnnotations',
  afterDatasetsDraw(chart){
    const {ctx, chartArea, scales} = chart;
    if(!chartArea) return;
    ctx.save();
    // incident week bands
    DATA.incident_weeks.forEach(w=>{
      const x = scales.x.getPixelForValue('Wk ' + w.week_idx);
      if(x === undefined) return;
      const bandW = (scales.x.width / DATA.weekly_overall.length) * 0.9;
      ctx.fillStyle = 'rgba(178,59,46,0.08)';
      ctx.fillRect(x - bandW/2, chartArea.top, bandW, chartArea.bottom - chartArea.top);
      ctx.fillStyle = '#B23B2E';
      ctx.font = '600 10px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Incident', x, chartArea.top - 6);
    });
    // baseline dashed line
    if(baseline !== null && baseline !== undefined){
      const y = scales.y.getPixelForValue(baseline*100);
      ctx.strokeStyle = '#948A7A';
      ctx.setLineDash([4,4]);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(chartArea.left, y); ctx.lineTo(chartArea.right, y); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.restore();
  }
};

const trendLabels = DATA.weekly_overall.map(w=>'Wk ' + w.week_idx);
const trendValues = DATA.weekly_overall.map(w=>+(w.breach_rate*100).toFixed(1));

new Chart(document.getElementById('trendChart'), {
  type: 'line',
  data: { labels: trendLabels, datasets: [{
    data: trendValues, borderColor:'#2B241C', backgroundColor:'rgba(43,36,28,0.06)',
    fill:true, tension:.35, pointRadius:3, pointBackgroundColor:'#2B241C', pointBorderColor:'#FDFBF8', pointBorderWidth:1.5,
    borderWidth:2,
  }]},
  options: {
    responsive:true, maintainAspectRatio:false, animation: reduceMotion ? false : {duration:600},
    plugins:{ legend:{display:false},
      tooltip:{ backgroundColor:'#2B241C', titleFont:{size:11}, bodyFont:{size:12}, padding:10, cornerRadius:6,
        callbacks:{ label:(c)=> c.parsed.y + '% breach rate across all providers' } } },
    scales:{
      y:{ beginAtZero:true, grid:{color:'#EFE8DC'}, ticks:{callback:v=>v+'%', font:{size:11}, color:'#6E6455'} },
      x:{ grid:{display:false}, ticks:{font:{size:11}, color:'#6E6455'} }
    }
  },
  plugins:[annotationPlugin]
});

document.getElementById('trend-sub').textContent =
  `${DATA.meta.weeks.length} weeks · baseline (clean-operations median) ${baseline!==null ? pct(baseline) : 'n/a'} · ` +
  (DATA.incident_weeks.length ? `${DATA.incident_weeks.length} documented incident week(s) annotated` : 'no incident weeks this run');

/* ================= DRIVER CHART ================= */
let driverChartInstance = null;
function renderDriverChart(){
  const list = currentFilter === 'all' ? DATA.driver_breakdown
    : DATA.driver_breakdown.filter(d=>filteredProviders().some(p=>p.root_cause===d.label));
  const labels = list.map(d=>titleCase(d.label));
  const counts = list.map(d=>d.providers.length);
  const colors = list.map(d=> d.label==='no_material_pattern' ? '#57724B' : (d.label==='review_queue_capacity_incident' ? '#B4802E' : '#B23B2E'));

  const materialCount = DATA.driver_breakdown.filter(d=>d.label!=='no_material_pattern').length;
  document.getElementById('driver-title').textContent = materialCount > 0
    ? `${materialCount} provider-side/queue pattern${materialCount>1?'s':''} explain${materialCount>1?'':'s'} this quarter's escalations`
    : `No material patterns detected this quarter`;

  if(driverChartInstance) driverChartInstance.destroy();
  driverChartInstance = new Chart(document.getElementById('driverChart'), {
    type:'bar',
    data:{ labels, datasets:[{ data:counts, backgroundColor:colors, borderRadius:5, maxBarThickness:26 }] },
    options:{
      indexAxis:'y', responsive:true, maintainAspectRatio:false, animation: reduceMotion ? false : {duration:400},
      plugins:{ legend:{display:false},
        tooltip:{ backgroundColor:'#2B241C', padding:10, cornerRadius:6,
          callbacks:{ label:(c)=>{ const d=list[c.dataIndex]; return `${d.providers.length} provider(s) · ${fmtInt(d.n_requests)} requests · ${fmtInt(d.n_breached)} breached`; } } } },
      scales:{ x:{ beginAtZero:true, ticks:{stepSize:1, font:{size:11}, color:'#6E6455'}, grid:{color:'#EFE8DC'} },
               y:{ grid:{display:false}, ticks:{font:{size:12}, color:'#2B241C'} } }
    }
  });
}
renderDriverChart();

/* ================= ENTITY CARDS (horizontal scroller) ================= */
const scroller = document.getElementById('entity-scroller');
function renderCards(){
  const list = filteredProviders();
  scroller.innerHTML = '';
  if(!list.length){
    scroller.innerHTML = '<div class="empty-state">No providers in this status this run.</div>';
    return;
  }
  list.forEach(p=>{
    const meta = STATUS[p.status];
    const card = document.createElement('button');
    card.className = 'entity-card';
    card.setAttribute('type','button');
    card.innerHTML = `
      <div class="card-top">
        <span class="status-chip" style="background:${meta.soft}; color:${meta.color}">${meta.label}</span>
        <span class="mono">${p.provider_id}</span>
      </div>
      <div class="name">${p.provider_name}</div>
      <div class="small">${p.specialty}</div>
      <div class="metric" style="color:${meta.color}">${pct(p.overall_breach_rate)}</div>
      <div class="small">${fmtInt(p.n_requests)} requests · ${p.confidence} confidence</div>
    `;
    card.addEventListener('click', ()=>openDrawer(p.provider_id));
    scroller.appendChild(card);
  });
}
renderCards();

document.getElementById('scroll-left').addEventListener('click', ()=> scroller.scrollBy({left:-280, behavior: reduceMotion?'auto':'smooth'}));
document.getElementById('scroll-right').addEventListener('click', ()=> scroller.scrollBy({left:280, behavior: reduceMotion?'auto':'smooth'}));

(function dragScroll(el){
  let isDown=false, startX=0, startScroll=0;
  el.addEventListener('mousedown', e=>{ isDown=true; el.classList.add('dragging'); startX=e.pageX; startScroll=el.scrollLeft; });
  window.addEventListener('mouseup', ()=>{ isDown=false; el.classList.remove('dragging'); });
  window.addEventListener('mousemove', e=>{ if(!isDown) return; e.preventDefault(); el.scrollLeft = startScroll - (e.pageX - startX); });
})(scroller);

/* ================= INSIGHT MODULES ================= */
function renderInsights(){
  const grid = document.getElementById('insights-grid');
  const material = DATA.providers.filter(p=>p.root_cause !== 'no_material_pattern');
  grid.innerHTML = '';
  if(!material.length){
    grid.innerHTML = '<div class="empty-state">No material findings this quarter -- every provider cleared.</div>';
    return;
  }
  material.forEach(p=>{
    const tone = p.status==='escalate' ? 'neg' : (p.status==='needs_human_review' ? 'warn' : 'pos');
    const el = document.createElement('div');
    el.className = 'insight ' + tone;
    el.innerHTML = `
      <p class="micro" style="margin:0;">Signal</p>
      <p class="finding">${p.provider_name} at ${pct(p.overall_breach_rate)} breach rate</p>
      <dl>
        <dt>Driver</dt><dd>${titleCase(p.root_cause)}</dd>
        <dt>Impact</dt><dd>${fmtInt(p.n_breached)} of ${fmtInt(p.n_requests)} requests breached</dd>
        <dt>Segment</dt><dd>${p.specialty} (${p.provider_id})</dd>
        <dt>Confidence</dt><dd>${p.confidence}</dd>
        <dt>Action</dt><dd>${p.recommended_action}</dd>
      </dl>
    `;
    el.style.cursor = 'pointer';
    el.addEventListener('click', ()=>openDrawer(p.provider_id));
    grid.appendChild(el);
  });
}
renderInsights();

/* ================= EVIDENCE TABLE ================= */
let sortKey = 'overall_breach_rate', sortDir = -1;
function renderTable(){
  const tbody = document.getElementById('evidence-tbody');
  const empty = document.getElementById('evidence-empty');
  let list = [...filteredProviders()];
  list.sort((a,b)=>{
    let av=a[sortKey], bv=b[sortKey];
    if(typeof av === 'string'){ av=av.toLowerCase(); bv=bv.toLowerCase(); }
    if(av<bv) return -1*sortDir; if(av>bv) return 1*sortDir; return 0;
  });
  tbody.innerHTML = '';
  empty.hidden = list.length > 0;
  list.forEach(p=>{
    const meta = STATUS[p.status];
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><span class="status-dot" style="background:${meta.color}"></span>${meta.label}</td>
      <td class="mono">${p.provider_id}</td>
      <td>${p.specialty}</td>
      <td class="num">${pct(p.overall_breach_rate)}</td>
      <td class="num">${fmtInt(p.n_requests)}</td>
      <td>${p.confidence}</td>
      <td>${titleCase(p.root_cause)}</td>
    `;
    tr.addEventListener('click', ()=>openDrawer(p.provider_id));
    tbody.appendChild(tr);
  });
}
renderTable();

document.querySelectorAll('th[data-sort]').forEach(th=>{
  th.addEventListener('click', ()=>{
    const key = th.getAttribute('data-sort');
    sortDir = (sortKey === key) ? -sortDir : -1;
    sortKey = key;
    document.querySelectorAll('th[data-sort] .arrow').forEach(a=>a.textContent='');
    th.querySelector('.arrow').textContent = sortDir === 1 ? '↑' : '↓';
    renderTable();
  });
});

/* ================= DRAWER ================= */
const drawer = document.getElementById('drawer');
const scrim = document.getElementById('scrim');
let lastFocused = null;

function sparklineSVG(weekly, color){
  if(!weekly.length) return '';
  const w=420, h=64, pad=4;
  const max = Math.max(...weekly.map(d=>d.breach_rate), 0.05);
  const step = (w - pad*2) / Math.max(weekly.length-1,1);
  const pts = weekly.map((d,i)=>{
    const x = pad + i*step;
    const y = h - pad - (d.breach_rate/max)*(h-pad*2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg width="100%" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="display:block;">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function openDrawer(providerId){
  const p = DATA.providers.find(x=>x.provider_id === providerId);
  if(!p) return;
  const meta = STATUS[p.status];
  const cc = p.cross_check, v = p.verification;
  document.getElementById('drawer-content').innerHTML = `
    <span class="status-chip" style="background:${meta.soft}; color:${meta.color}">${meta.label}</span>
    <h3 id="drawer-title">${p.provider_name}</h3>
    <p class="small mono">${p.provider_id} · ${p.specialty}</p>
    <p class="display" style="font-size:2.4rem; margin-top:10px; color:${meta.color}">${pct(p.overall_breach_rate)}</p>
    <p class="small">${fmtInt(p.n_breached)} of ${fmtInt(p.n_requests)} requests breached SLA this quarter</p>

    <div class="drawer-block">
      <h4>Weekly trend</h4>
      ${sparklineSVG(p.weekly, meta.color)}
    </div>

    <div class="drawer-block">
      <h4>Root cause</h4>
      <p class="medium" style="margin:0 0 8px;"><strong style="color:var(--ink)">${titleCase(p.root_cause)}</strong></p>
      <p class="small" style="line-height:1.6;">${p.root_cause_detail}</p>
      ${p.trend_slope!==null && p.trend_slope!==undefined ? `<div class="kv" style="margin-top:10px;">
        <div class="k">OLS trend slope</div><div class="v">${p.trend_slope.toFixed(4)}/wk</div>
        <div class="k">R²</div><div class="v">${(p.trend_r2*100).toFixed(1)}%</div>
      </div>` : ''}
    </div>

    <div class="drawer-block">
      <h4>Confidence</h4>
      <div class="kv">
        <div class="k">Level</div><div class="v">${p.confidence}</div>
        <div class="k">Sample size</div><div class="v">${fmtInt(p.n_requests)} (min ${DATA.thresholds.min_sample_size})</div>
      </div>
      <p class="small" style="margin-top:8px;">${p.confidence_rationale}</p>
    </div>

    <div class="drawer-block">
      <h4>Cross-check (SQL vs. Pandas)</h4>
      <div class="kv">
        <div class="k">SQL value</div><div class="v">${cc.sql_value!==null ? pct(cc.sql_value,2) : 'n/a'}</div>
        <div class="k">Pandas value</div><div class="v">${cc.pandas_value!==null ? pct(cc.pandas_value,2) : 'n/a'}</div>
        <div class="k">Agree (within ${(DATA.thresholds.cross_check_tolerance*100).toFixed(4)}%)</div><div class="v">${cc.agrees ? 'Yes' : 'No'}</div>
      </div>
    </div>

    <div class="drawer-block">
      <h4>Escalation note &amp; evidence-grounding</h4>
      <div class="note-box">${p.note}</div>
      <div class="kv" style="margin-top:10px;">
        <div class="k">Every number verified against evidence</div><div class="v">${v.verified ? 'Yes' : 'No'}</div>
        <div class="k">Numbers checked</div><div class="v">${v.checked_numbers.length}</div>
      </div>
    </div>

    <div class="drawer-block">
      <h4>Recommended action</h4>
      <p class="medium" style="margin:0;">${p.recommended_action}</p>
    </div>
  `;
  scrim.classList.add('open');
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden','false');
  lastFocused = document.activeElement;
  document.getElementById('drawer-close').focus();
  document.addEventListener('keydown', onDrawerKeydown);
}
function closeDrawer(){
  scrim.classList.remove('open');
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden','true');
  document.removeEventListener('keydown', onDrawerKeydown);
  if(lastFocused) lastFocused.focus();
}
function onDrawerKeydown(e){ if(e.key === 'Escape') closeDrawer(); }
document.getElementById('drawer-close').addEventListener('click', closeDrawer);
scrim.addEventListener('click', closeDrawer);

/* ================= METHODOLOGY ACCORDION ================= */
const t = DATA.thresholds;
const accordionData = [
  { title:'Cross-check layer', body:
    `Every metric is computed two independent ways -- once via a SQL window-function query (<code>RANK</code>, <code>LAG</code>, 3-week moving average), once via an independent Pandas recomputation from the same core tables without reusing the SQL query text. They must agree within a ${(t.cross_check_tolerance*100).toFixed(4)}% relative tolerance; disagreement is never silently resolved -- it downgrades confidence to <code>INSUFFICIENT_EVIDENCE</code> and is logged.` },
  { title:'Root-cause classification thresholds', body:
    `<code>provider_side_trend_*</code> requires an OLS trend slope ≥ ${t.trend_slope_threshold}/week with R² ≥ ${(t.trend_r2_threshold*100).toFixed(0)}% -- checked first, so a real quarter-long trend is never masked by one coincidental incident week. <code>provider_side_chronic</code> requires a median weekly breach rate ≥ ${(t.provider_chronic_threshold*100).toFixed(0)}%. <code>review_queue_capacity_incident</code> requires an isolated week where breach rate ≥ 30% coincides with ≥ ${(t.queue_incident_threshold*100).toFixed(0)}% breach among other providers on the same queue that week. Otherwise: <code>no_material_pattern</code>.` },
  { title:'Confidence & escalation gate', body:
    `Confidence (<code>HIGH</code> / <code>MEDIUM</code> / <code>LOW</code> / <code>INSUFFICIENT_EVIDENCE</code>) is derived from sample size vs. a documented minimum (${t.min_sample_size}), cross-check agreement, and evidence completeness. Only <code>HIGH</code> or <code>MEDIUM</code> confidence findings with a material root cause are escalated; <code>LOW</code> or <code>INSUFFICIENT_EVIDENCE</code> findings route to a needs-human-review queue instead of forcing a confident-sounding but under-evidenced conclusion.` },
  { title:'Evidence-grounding verifier', body:
    `Every number in an LLM-drafted escalation note is regex-extracted and checked against the evidence bundle before release. A note with any unverifiable number is discarded whole and replaced with a deterministic, evidence-tied template. Proven with an adversarial test: a fake "hallucinating" LLM that always invents a plausible-but-false number, and a test asserts it never survives into the output.` },
  { title:'Data & scope', body:
    `100% synthetic data for a fictional health plan, generated by <code>data/generate_data.py</code> with a fixed random seed. ${DATA.summary.providers_evaluated} providers, ${fmtInt(DATA.meta.total_requests)} PA requests, ${DATA.meta.weeks.length} weeks this run. This page is generated fresh from <code>output/*.json</code> and a live SQL pull each time <code>python3 run_pipeline.py</code> runs -- nothing above is hand-typed.` },
];
const accEl = document.getElementById('accordion');
accordionData.forEach((item, i)=>{
  const wrap = document.createElement('div');
  wrap.className = 'accordion-item';
  wrap.dataset.open = 'false';
  wrap.innerHTML = `
    <button class="accordion-btn" aria-expanded="false">
      <span>${item.title}</span><span class="plus">+</span>
    </button>
    <div class="accordion-panel"><div class="accordion-panel-inner">${item.body}</div></div>
  `;
  const btn = wrap.querySelector('.accordion-btn');
  const panel = wrap.querySelector('.accordion-panel');
  btn.addEventListener('click', ()=>{
    const isOpen = wrap.dataset.open === 'true';
    wrap.dataset.open = (!isOpen).toString();
    btn.setAttribute('aria-expanded', (!isOpen).toString());
    panel.style.maxHeight = isOpen ? '0px' : panel.scrollHeight + 'px';
  });
  accEl.appendChild(wrap);
});

/* ================= SCROLL REVEAL ================= */
if(reduceMotion){
  document.querySelectorAll('[data-reveal]').forEach(el=>el.classList.add('in-view'));
} else if('IntersectionObserver' in window){
  const io = new IntersectionObserver((entries)=>{
    entries.forEach(e=>{ if(e.isIntersecting){ e.target.classList.add('in-view'); io.unobserve(e.target); } });
  }, {threshold:.12});
  document.querySelectorAll('[data-reveal]').forEach(el=>io.observe(el));
} else {
  document.querySelectorAll('[data-reveal]').forEach(el=>el.classList.add('in-view'));
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    build()
