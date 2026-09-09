# PA Intake Validation & SLA Escalation Agent

An agentic data-operations pipeline for prior-authorization (PA) intake: deterministic data validation, SQL/Pandas SLA analytics, and an investigation agent that escalates only the providers and review queues that actually need attention -- with root-cause attribution (a provider-side pattern vs. a documented review-queue capacity incident) and a verification layer that keeps every escalation traceable to source data instead of just "sounding right."

Built against 100% synthetic data for a fictional health plan, **Amaranth Health Partners**. No real member, provider, or claims data is used anywhere -- member identifiers are tokenized by design (`MBR-########`), never a name, DOB, or SSN. This is one of three independent projects in a broader healthcare data-operations portfolio (the other two cover payment-integrity reconciliation and data-science handoff); each is a separate repo with its own data, schema, and code, deliberately not sharing a database.

**🚀 Live dashboard:** [pa-intake-sla-agent-2.vercel.app](https://pa-intake-sla-agent-2.vercel.app/) *(deployed on Vercel from `output/dashboard.html` -- charts rendered with a vendored, offline-safe copy of Chart.js, zero external calls. See [Deployment](#deployment) below. Replace this link with your own project's URL after deploying -- Vercel may append a suffix if the name is taken.)*


## What this project demonstrates

**The differentiator: agentic AI with an actual verification layer, not "ask an LLM and hope."** Most "AI agent" demos wire an LLM up to some data and trust whatever it says. This project is built the other way around: every metric is computed deterministically (and cross-checked two independent ways), every finding carries an explicit, derived confidence level, and the LLM's *only* job is drafting the wording of a note -- which is then checked number-by-number against the evidence before it's allowed to reach anyone. That architecture (`agent/trust_layer.py`) is proven with an adversarial test, not just described in prose.

**Everything else that went into building it, end to end:**

- **Synthetic data engineering** -- a fixed-seed generator (`data/generate_data.py`) producing a full quarter of realistic PA intake traffic across 8 providers and 4 review queues, with deliberately planted behavioral archetypes (chronic SLA breach, improving/degrading trends, duplicate submissions, coding errors) and a documented queue-capacity incident, plus a ground-truth fixture so detection performance can be *measured*, not asserted.
- **A portable SQL warehouse** (`sql/schema.sql`) -- a validated core layer with foreign keys, indexes, and a full reject-row audit log, written in ANSI SQL rather than SQLite-only syntax.
- **A staging-to-core ETL layer** (`etl/staging_to_core.py`) that runs every validation rule and guarantees nothing is silently dropped -- every non-valid row is logged with a rule code and reason.
- **A deterministic validation rule engine** (`validation/rules.py`) -- 8 rules, a 5-status taxonomy (`VALID` / `INVALID` / `ANOMALOUS` / `INCOMPLETE` / `UNCERTAIN`) instead of a bare pass/fail, including a robust median/MAD z-score outlier check and a duplicate-submission window check.
- **SQL window-function analytics** (`RANK`, `LAG`, 3-week moving average) for weekly per-provider SLA breach-rate tracking, cross-checked against an independently written Pandas recomputation.
- **Explicit, documented statistics** (`stats_utils.py`) -- a closed-form OLS trend slope (not a library black box) for degrading/improving trend detection, and the robust z-score used for outlier detection. Every score in this project is explainable in one sentence; nothing is an opaque trained model.
- **An escalation agent** (`agent/umguard.py`) that runs the full pipeline -- SQL pull, Pandas cross-check, root-cause classification, confidence scoring, LLM-assisted note drafting, evidence verification, audit logging -- for every provider, and only escalates the ones that need it.
- **An Excel scorecard** (`excel/build_workbook.py`) -- a real Excel Table, a provider-by-week SLA breach-rate cross-tab driven entirely by live `SUMIFS`/`IFERROR`/`AVERAGE` formulas (never pasted values), conditional formatting, and a ranked scorecard sheet, verified correct via headless LibreOffice recalculation.
- **An editorial analytics dashboard** (`dashboard/build_dashboard.py`) -- not a KPI-grid admin template: a warm, minimal, narrative page (hero finding -> what changed -> why -> where -> what's unusual -> evidence -> methodology) with an annotated real trend chart, a driver-ranking chart, swipeable provider cards, per-provider drill-down panels (SQL-vs-Pandas cross-check, confidence rationale, evidence-grounding result), a sortable evidence table, and a progressive-disclosure methodology accordion. Every number is generated fresh from `output/*.json` and a live SQL pull -- nothing on the page is hand-typed.
- **Written SOPs** (`docs/sops/`) that the code *implements*, not documentation written after the fact -- a rule change updates the SOP and the code in the same commit.
- **A 47-test pytest suite** -- rule-level unit tests, hand-checked math tests, adversarial trust-layer tests (including a fake hallucinating LLM), and detection-performance tests that measure real precision/recall against the planted ground truth.
- **One-command reproducibility** (`run_pipeline.py`) -- data generation through the full test suite, in one call, with a fixed random seed.
- **CI** (`.github/workflows/ci.yml`) that runs the entire pipeline, including the Excel recalculation, on every push.

## Skills this project exercises

SQL (window functions, CTEs, portable ANSI DDL) · Python / Pandas (ETL, independent cross-check recomputation, merge-based logic) · data validation & QA (5-status taxonomy, full audit trail, documented rule thresholds) · statistics (robust z-score, closed-form OLS trend fitting, explicit weighted scoring) · Excel (live formulas, real Tables, conditional formatting, pivot-style cross-tabs, automated recalculation verification) · SOP authorship kept in lockstep with executable code · proactive risk communication (the entire purpose of the escalation agent) · Python data structures used deliberately (`dataclasses` for every structured result -- `Violation`, `TrendResult`, `ConfidenceAssessment`, `CrossCheckResult` -- instead of bare tuples) · privacy-by-design identifier tokenization · agentic AI system design with a verification and confidence layer, evidence-grounded LLM drafting, and an adversarially-tested guardrail · data-storytelling and dashboard/analytics UX design (progressive disclosure, drill-down, cross-filtering, annotated charts, accessible interaction states -- no framework, vanilla JS against the same evidence JSON the rest of the pipeline produces).

## Architecture

```
data/generate_data.py      synthetic PA intake data (fixed seed, planted anomalies + ground truth)
sql/schema.sql               validated core warehouse (portable ANSI SQL, FKs, indexes)
etl/staging_to_core.py       staging -> core ETL; runs every validation rule; full reject audit trail
validation/rules.py          8 deterministic rules, 5-status taxonomy (never bare pass/fail)
stats_utils.py                robust z-score, OLS trend slope, weighted scoring -- explicit, documented math
agent/trust_layer.py          Verification & Trust Layer (see below)
agent/umguard.py              the escalation agent itself
excel/build_workbook.py       Excel scorecard, verified via headless LibreOffice recalculation
dashboard/build_dashboard.py  static HTML ops dashboard
docs/sops/                     SOP-001 (intake validation), SOP-002 (SLA monitoring)
tests/                          47 tests: rule-level, math, trust-layer/adversarial, detection-performance
run_pipeline.py                 one command: data -> ETL -> agent -> Excel -> dashboard -> tests
```

### The Verification & Trust Layer (`agent/trust_layer.py`) -- the differentiator, in detail

This is the part of the project built specifically to answer "how do you keep an agentic pipeline from confidently telling a stakeholder something wrong":

1. **Cross-check layer** -- every key metric (SLA breach rate) is computed twice, independently: once via a SQL window-function query, once via a from-scratch Pandas recomputation. Disagreement is never averaged away; it downgrades confidence and is logged.
2. **Confidence layer** -- every finding carries an explicit `HIGH` / `MEDIUM` / `LOW` / `INSUFFICIENT_EVIDENCE` level, derived from documented inputs (sample size vs. a minimum threshold, cross-check agreement, evidence completeness) -- never a bare pass/fail.
3. **Evidence-grounding verifier** -- every number in an LLM-drafted escalation note is regex-extracted and checked against the evidence bundle before release. A note with any unverifiable number is discarded whole and replaced with a deterministic, evidence-tied template. Proven with an adversarial test: `HallucinatingLLM`, a test double that always invents a plausible-but-false number, and `test_hallucinating_llm_invented_number_never_reaches_output` asserts it never survives into the output.
4. **Escalation gate** -- `LOW` / `INSUFFICIENT_EVIDENCE` findings never auto-escalate; they route to a `needs_human_review` bucket instead of forcing a confident-sounding conclusion.
5. **Pluggable LLM, deterministic by default** -- `get_llm()` returns a real API-backed LLM only if an API key env var is set; otherwise a deterministic, evidence-grounded template renderer. The whole pipeline runs reproducibly with zero external dependency or cost by default, and the guardrails are provable independent of whether a live LLM is attached.

## How to run it

```bash
pip install -r requirements.txt
python3 run_pipeline.py
```

This regenerates the synthetic dataset, runs ETL + validation, runs the escalation agent, builds the Excel scorecard (with a headless-LibreOffice recalculation check), builds the HTML dashboard, and runs the full test suite -- one command, fully reproducible (fixed random seed).

Generated evidence is committed in `output/`: `escalations.json`, `needs_human_review.json`, `cleared.json`, `audit_log.json` (full per-provider derivation trace), `pa_sla_scorecard.xlsx`, and `dashboard.html`.

## Deployment

`dashboard/build_dashboard.py` produces `output/dashboard.html` as a fully self-contained static file -- inline CSS, two live charts (Chart.js, vendored into `dashboard/vendor/chart.umd.min.js` and inlined at build time so the page needs zero external network calls, even offline), zero backend. `vercel.json` at the repo root is already configured for it (`outputDirectory: output`, plus a rewrite so `/` serves `dashboard.html` directly instead of a file listing).

**Deploy it on Vercel (free, ~2 minutes):**

1. Push this repo to GitHub (see above if you haven't yet).
2. Go to [vercel.com](https://vercel.com) and sign in with your GitHub account.
3. **Add New...** -> **Project** -> **Import** this repo.
4. Framework Preset: leave as **Other** -- `vercel.json` already tells Vercel where the static files live and how to route `/`. Don't add a build command; there isn't one.
5. Click **Deploy**. Vercel assigns a live URL (something like `https://pa-intake-sla-agent.vercel.app`) within seconds.
6. From then on, every push to `main` (including a re-run of `python3 run_pipeline.py` that regenerates `output/dashboard.html` with fresh numbers) auto-redeploys -- no manual step, no CI required.

GitHub Pages works too if you'd rather not use Vercel: **Settings** -> **Pages** -> Source = `Deploy from a branch`, Branch = `main`, folder = `/ (root)` -> **Save**; the page then lives at `https://<your-username>.github.io/pa-intake-sla-agent/output/dashboard.html`.

The Excel scorecard and audit-trail JSON aren't web pages by nature, so they're demonstrated with a screenshot (above) and are downloadable straight from `output/` in this repo instead.

## Measured results (real numbers, tied to tests)

All numbers below come directly from `output/` and the pytest run in this repo -- nothing here is hand-typed.

- **47/47 tests passing** (`pytest tests/ -q`), covering rule-level validation, hand-checked math, trust-layer/adversarial guardrails, and detection performance graded against planted ground truth.
- **Duplicate-submission detection:** 8 planted duplicate PA submissions, 8 flagged, 0 false positives -- **100% precision, 100% recall** this run.
- **Unit-outlier / coding-error detection:** 5 planted wildly-wrong unit entries, 5 flagged via robust (median/MAD) z-score, 0 false positives -- **100% precision, 100% recall** this run.
- **Root-cause attribution:** across 8 synthetic providers (3 archetypes with a real provider-side SLA pattern, 4 archetypes with no material SLA pattern, plus 1 provider whose only bad week coincides with a documented queue-capacity incident), the agent correctly separates provider-side patterns from the queue incident in every case this run.
- **Escalation gate integrity:** 0 of 4 escalated findings carry `LOW` or `INSUFFICIENT_EVIDENCE` confidence (enforced by a test, not just asserted).
- **This run:** 8 providers evaluated, 4 escalated, 4 cleared, 0 routed to human review.
- **ETL reject-audit trail:** 3/3 deliberately planted dirty rows (bad provider reference, negative units, malformed date) correctly rejected with a logged reason -- nothing silently dropped.
- **Excel workbook:** 157 live formulas, 0 formula errors after headless LibreOffice recalculation, and the Scorecard sheet's live-formula breach rates match the agent's independently-computed Python numbers exactly.

## Interview-defensibility Q&A

**Why deterministic rules + a narrow LLM instead of "just ask an LLM"?**
Because a stakeholder-facing escalation needs to be *trustworthy*, not just plausible-sounding. An LLM asked "which providers should I escalate" can produce a fluent, confident answer that's wrong in a way that's hard to catch. This pipeline instead computes every metric deterministically (SQL + independent Pandas cross-check), classifies root cause with documented thresholds, and uses the LLM for exactly one narrow task -- drafting the wording of a note -- with every number in that note verified against the evidence bundle before it's allowed out. The LLM never decides *what* the finding is, only how to phrase it, and even that phrasing is checked.

**Why this particular SLA/trust design?**
Two failure modes matter most in an operational escalation system: (1) escalating too much, so stakeholders learn to ignore the alerts, and (2) blaming the wrong party, which burns trust and wastes time. The confidence gate addresses (1) -- low-evidence findings go to human review, not a stakeholder inbox. The root-cause classifier's ordering (check for a material multi-week trend *before* attributing a single week to an incident) addresses (2) -- it's specifically designed so a real provider-side trend isn't masked by one coincidentally-overlapping queue event, and conversely so a clean provider caught in a real queue incident isn't blamed for it.

**What breaks at scale?**
The root-cause thresholds (`QUEUE_INCIDENT_THRESHOLD`, `PROVIDER_CHRONIC_THRESHOLD`, `TREND_SLOPE_THRESHOLD`) are fixed constants tuned by domain judgment on this dataset's scale (~1,000 PA requests/quarter, 8 providers, 4 queues). At real scale (thousands of providers, dozens of queues) these would need to become data-driven per-specialty baselines rather than global constants, and the per-provider Python root-cause loop would need to move into set-based SQL. The duplicate-submission window and unit-outlier z-threshold are also demo-tuned and would need real historical calibration.

**What's honestly still missing?**
No real system integration (by design -- this is a synthetic-data portfolio piece). No multi-tenant configuration. The LLM adapter (`_RealLLMAdapter`) is a stub -- wiring up an actual API-backed LLM was out of scope for a project that needs to run with zero external dependencies by default, but the evidence-verification gate is written so a real LLM could be dropped in without changing anything else. No authentication/access-control layer, since there's no real PHI to protect here in the first place.

## Disclaimer

100% synthetic data, generated by `data/generate_data.py` with a fixed random seed (42). Amaranth Health Partners is a fictional health plan invented for this project. This is an independent portfolio project, not affiliated with, endorsed by, or built using any real company's proprietary system, data, or trademark. It uses standard, publicly known US healthcare administration terminology (prior authorization, utilization management, SLA turnaround, HIPAA/PHI) to demonstrate domain fluency, not to represent or reproduce any real company's product. No compliance certification (HIPAA or otherwise) is claimed.
