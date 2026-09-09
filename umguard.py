"""
PA Intake Validation & SLA Escalation Agent.

Step sequence (identical shape to the other agents in this portfolio, even
though the code is independent):
  1. tool call: SQL evidence pull (weekly provider + queue breach rates,
     using RANK / LAG / moving-average window functions)
  2. tool call: corroborating context (independent Pandas recomputation of
     the same metrics, from the same core tables)
  3. deterministic root-cause classification (provider-side trend/chronic
     pattern vs. a documented review-queue capacity incident)
  4. cross-check (trust_layer.cross_check: do SQL and Pandas agree?)
  5. confidence score (trust_layer.assess_confidence)
  6. narrow LLM-assisted drafting of an escalation note
  7. evidence-verification gate (trust_layer.verify_grounding) before the
     note is allowed out; unverified drafts fall back to a deterministic,
     evidence-tied template
  8. audit log: every step's input/output written to JSON, so any
     escalation is traceable back to the exact source rows.

Run: python3 agent/umguard.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.trust_layer import assess_confidence, cross_check, gate_for_escalation, get_llm, verify_grounding
from stats_utils import MIN_SAMPLE_SIZE, ols_trend_slope

HERE = Path(__file__).resolve().parent.parent
CORE_DB = HERE / "data" / "amaranth_pa_core.db"
OUTPUT_DIR = HERE / "output"

# Thresholds -- documented, not tuned to the data after the fact. Chosen
# before looking at detection results: a provider needs a breach rate
# meaningfully above the ~5-10% "clean" baseline observed across pilot
# archetypes to warrant a stakeholder-facing escalation.
QUEUE_INCIDENT_THRESHOLD = 0.30      # queue-wide breach rate (excl. subject provider) this high => queue-side signal
PROVIDER_CHRONIC_THRESHOLD = 0.35    # median weekly breach rate this high, most weeks => provider-side chronic
TREND_SLOPE_THRESHOLD = 0.02         # OLS slope (breach-rate change per week) this large => material trend
TREND_R2_THRESHOLD = 0.30            # trend must also explain a reasonable share of week-to-week variance

ESCALATION_NOTE_TEMPLATE = (
    "Provider {provider_name} ({provider_id}) shows an elevated SLA breach rate of "
    "{breach_rate_pct}% across {n_requests} reviewed PA requests this quarter "
    "(confidence: {confidence_level}). Root cause: {root_cause_label}. {root_cause_detail}"
)


def _sql(conn: sqlite3.Connection, query: str) -> pd.DataFrame:
    return pd.read_sql(query, conn)


def pull_weekly_provider_breach_sql(conn: sqlite3.Connection) -> pd.DataFrame:
    """Step 1 tool call: weekly SLA breach rate per provider via SQL,
    using RANK (relative standing among providers that week), LAG
    (week-over-week comparison), and a 3-week moving average -- exactly
    the kind of window-function query a UM analytics team runs."""
    query = """
        WITH weekly AS (
            SELECT r.provider_id, r.request_week AS week_idx,
                   COUNT(*) AS n_requests,
                   SUM(d.sla_breached) AS n_breached,
                   CAST(SUM(d.sla_breached) AS REAL) / COUNT(*) AS breach_rate
            FROM fact_pa_requests r
            JOIN fact_pa_determinations d ON d.pa_id = r.pa_id
            GROUP BY r.provider_id, r.request_week
        )
        SELECT *,
               RANK() OVER (PARTITION BY week_idx ORDER BY breach_rate DESC) AS week_rank,
               LAG(breach_rate) OVER (PARTITION BY provider_id ORDER BY week_idx) AS prev_week_breach_rate,
               AVG(breach_rate) OVER (PARTITION BY provider_id ORDER BY week_idx
                                       ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS moving_avg_3wk
        FROM weekly
        ORDER BY provider_id, week_idx;
    """
    return _sql(conn, query)


def pull_weekly_queue_breach_sql(conn: sqlite3.Connection) -> pd.DataFrame:
    """Step 2 tool call (corroborating context): weekly breach rate per
    review queue, used to test whether a provider's bad week lines up with
    a queue-wide spike (everyone routed there that week also breached) --
    the signature of a capacity incident rather than a provider problem."""
    query = """
        SELECT r.review_queue_code AS queue_code, r.provider_id, r.request_week AS week_idx,
               COUNT(*) AS n_requests, SUM(d.sla_breached) AS n_breached
        FROM fact_pa_requests r
        JOIN fact_pa_determinations d ON d.pa_id = r.pa_id
        GROUP BY r.review_queue_code, r.provider_id, r.request_week
        ORDER BY queue_code, week_idx;
    """
    return _sql(conn, query)


def recompute_weekly_provider_breach_pandas(conn: sqlite3.Connection) -> pd.DataFrame:
    """Independent Pandas recomputation of the same weekly provider breach
    rate, from the same core tables but without reusing the SQL query
    text -- this is the second, independent leg of the cross-check."""
    pa = _sql(conn, "SELECT pa_id, provider_id, request_week FROM fact_pa_requests")
    det = _sql(conn, "SELECT pa_id, sla_breached FROM fact_pa_determinations")
    merged = pa.merge(det, on="pa_id", how="inner")
    grouped = merged.groupby(["provider_id", "request_week"]).agg(
        n_requests=("pa_id", "count"), n_breached=("sla_breached", "sum")
    ).reset_index()
    grouped["breach_rate"] = grouped["n_breached"] / grouped["n_requests"]
    grouped = grouped.rename(columns={"request_week": "week_idx"})
    return grouped


def classify_root_cause(provider_id: str, weekly_sql: pd.DataFrame, queue_weekly: pd.DataFrame) -> dict:
    """Deterministic root-cause attribution: distinguishes a provider-side
    pattern (chronic high breach rate, or a significant trend) from a
    review-queue capacity incident (a queue-wide spike in one specific
    week that hit every provider routed there, not just this one)."""
    prov_weeks = weekly_sql[weekly_sql["provider_id"] == provider_id].sort_values("week_idx")
    if prov_weeks.empty:
        return dict(label="no_material_pattern", detail="No determinations on file for this provider.",
                     flagged_weeks=[])

    incident_weeks = []
    for _, row in prov_weeks.iterrows():
        week_idx, breach_rate = row["week_idx"], row["breach_rate"]
        if breach_rate < 0.30:
            continue
        # queue-wide breach rate that week, EXCLUDING this provider
        others = queue_weekly[(queue_weekly["week_idx"] == week_idx) & (queue_weekly["provider_id"] != provider_id)]
        # restrict to the same queue(s) this provider used that week
        same_week_queues = queue_weekly[(queue_weekly["provider_id"] == provider_id)
                                         & (queue_weekly["week_idx"] == week_idx)]["queue_code"].unique()
        others = others[others["queue_code"].isin(same_week_queues)]
        if others.empty or others["n_requests"].sum() == 0:
            continue
        others_breach_rate = others["n_breached"].sum() / others["n_requests"].sum()
        if others_breach_rate >= QUEUE_INCIDENT_THRESHOLD:
            incident_weeks.append(dict(week_idx=int(week_idx), provider_breach_rate=round(float(breach_rate), 3),
                                        other_providers_breach_rate=round(float(others_breach_rate), 3)))

    trend = ols_trend_slope(prov_weeks["week_idx"].to_numpy(), prov_weeks["breach_rate"].to_numpy())
    median_breach = float(prov_weeks["breach_rate"].median())

    non_incident_weeks = prov_weeks[~prov_weeks["week_idx"].isin([w["week_idx"] for w in incident_weeks])]
    non_incident_median = float(non_incident_weeks["breach_rate"].median()) if not non_incident_weeks.empty else 0.0

    has_material_trend = (abs(trend.slope) >= TREND_SLOPE_THRESHOLD and trend.r_squared >= TREND_R2_THRESHOLD
                           and trend.n_points >= 3)

    # A significant multi-week trend is checked FIRST and takes priority
    # over any single flagged week: a provider whose breach rate is
    # systematically rising/falling across the whole quarter has a
    # provider-side pattern even if one of those weeks also happens to
    # land on a documented queue incident. A queue incident is only the
    # PRIMARY root cause when it is not just one data point inside a
    # larger provider-side trend.
    if has_material_trend:
        direction = "degrading" if trend.slope > 0 else "improving"
        detail = (f"Breach rate is {direction} over the quarter (OLS slope {trend.slope:+.3f}/week, "
                   f"R²={trend.r_squared:.2f}) -- a provider-side pattern, not a one-week anomaly.")
        if incident_weeks:
            wk = incident_weeks[0]
            detail += (f" Note: week {wk['week_idx']} also coincided with a queue-wide breach spike "
                       f"({wk['other_providers_breach_rate']*100:.0f}% among other providers on the same queue) "
                       f"-- a contributing factor, but the quarter-long trend is present independent of that week.")
        return dict(
            label=f"provider_side_trend_{direction}", detail=detail,
            flagged_weeks=incident_weeks, trend_slope=trend.slope, trend_r2=trend.r_squared,
            median_breach_rate=median_breach,
        )
    if median_breach >= PROVIDER_CHRONIC_THRESHOLD:
        return dict(
            label="provider_side_chronic",
            detail=(f"Median weekly breach rate of {median_breach*100:.0f}% is persistently elevated across "
                     f"the quarter, not concentrated in any single incident week -- a provider-side pattern."),
            flagged_weeks=incident_weeks, trend_slope=trend.slope, trend_r2=trend.r_squared,
            median_breach_rate=median_breach,
        )
    # No material provider-side trend or chronic pattern -- if there IS a
    # flagged week, and it coincides with a queue-wide spike, THAT week
    # alone is the root cause: a documented review-queue capacity
    # incident, not a provider problem.
    if incident_weeks:
        wk = incident_weeks[0]
        return dict(
            label="review_queue_capacity_incident",
            detail=(f"Week {wk['week_idx']}: breach rate {wk['provider_breach_rate']*100:.0f}% coincides with a "
                     f"{wk['other_providers_breach_rate']*100:.0f}% breach rate among other providers on the same "
                     f"queue that week -- consistent with a documented queue-capacity incident, not a "
                     f"provider-side problem. Excluding that week, this provider's typical breach rate is "
                     f"{non_incident_median*100:.0f}%."),
            flagged_weeks=incident_weeks, trend_slope=trend.slope, trend_r2=trend.r_squared,
            median_breach_rate=median_breach,
        )
    return dict(label="no_material_pattern",
                detail="Breach rate is within the expected clean-operations range for the quarter.",
                flagged_weeks=incident_weeks, trend_slope=trend.slope, trend_r2=trend.r_squared,
                median_breach_rate=median_breach)


def run() -> dict:
    conn = sqlite3.connect(CORE_DB)
    providers = _sql(conn, "SELECT * FROM dim_providers")

    weekly_sql = pull_weekly_provider_breach_sql(conn)
    queue_weekly = pull_weekly_queue_breach_sql(conn)
    weekly_pandas = recompute_weekly_provider_breach_pandas(conn)

    llm = get_llm()
    escalations, needs_review, cleared = [], [], []
    audit_log = []

    for _, prov in providers.iterrows():
        provider_id, provider_name = prov["provider_id"], prov["provider_name"]
        prov_sql = weekly_sql[weekly_sql["provider_id"] == provider_id]
        prov_pandas = weekly_pandas[weekly_pandas["provider_id"] == provider_id]

        n_requests = int(prov_sql["n_requests"].sum())
        n_breached = int(prov_sql["n_breached"].sum())
        overall_breach_sql = n_breached / n_requests if n_requests else 0.0
        overall_breach_pandas = (float(prov_pandas["n_breached"].sum()) / float(prov_pandas["n_requests"].sum())
                                  if prov_pandas["n_requests"].sum() else 0.0)

        cc = cross_check(f"{provider_id}_overall_breach_rate", overall_breach_sql, overall_breach_pandas)
        evidence_complete = n_requests > 0 and not prov_sql.isna().all().all()
        confidence = assess_confidence(sample_size=n_requests, cross_check_agrees=cc.agrees,
                                        evidence_complete=evidence_complete)

        root_cause = classify_root_cause(provider_id, weekly_sql, queue_weekly)

        evidence = dict(
            provider_id=provider_id, provider_name=provider_name,
            breach_rate_pct=round(overall_breach_sql * 100, 1), n_requests=n_requests, n_breached=n_breached,
            confidence_level=confidence.level, root_cause_label=root_cause["label"].replace("_", " "),
            root_cause_detail=root_cause["detail"],
        )

        draft = llm.draft(ESCALATION_NOTE_TEMPLATE, evidence)
        verification = verify_grounding(draft, evidence)
        deterministic_fallback = ESCALATION_NOTE_TEMPLATE.format(**evidence)
        final_note = draft if verification.verified else deterministic_fallback

        material_finding = root_cause["label"] != "no_material_pattern"
        escalate = material_finding and gate_for_escalation(confidence.level)
        route_to_review = material_finding and not gate_for_escalation(confidence.level)

        record = dict(
            provider_id=provider_id, provider_name=provider_name, specialty=prov["specialty"],
            overall_breach_rate=round(overall_breach_sql, 4), n_requests=n_requests, n_breached=n_breached,
            confidence=confidence.level, confidence_rationale=confidence.rationale,
            root_cause=root_cause["label"], root_cause_detail=root_cause["detail"],
            flagged_weeks=root_cause["flagged_weeks"], trend_slope=root_cause.get("trend_slope"),
            trend_r2=root_cause.get("trend_r2"), note=final_note, note_llm_verified=verification.verified,
        )

        audit_log.append(dict(
            provider_id=provider_id,
            step1_sql_query="pull_weekly_provider_breach_sql (RANK/LAG/moving-avg window functions)",
            step1_sql_overall_breach_rate=overall_breach_sql,
            step2_pandas_overall_breach_rate=overall_breach_pandas,
            step3_root_cause=root_cause,
            step4_cross_check=dict(metric=cc.metric_name, sql_value=cc.sql_value, pandas_value=cc.pandas_value,
                                    agrees=cc.agrees, delta=cc.delta),
            step5_confidence=dict(level=confidence.level, rationale=confidence.rationale,
                                   sample_size=confidence.sample_size),
            step6_llm_draft=draft,
            step7_verification=dict(verified=verification.verified,
                                     unverified_numbers=verification.unverified_numbers,
                                     checked_numbers=verification.checked_numbers),
            step7_final_note=final_note,
            decision="escalate" if escalate else ("needs_human_review" if route_to_review else "cleared"),
        ))

        if escalate:
            escalations.append(record)
        elif route_to_review:
            needs_review.append(record)
        else:
            cleared.append(record)

    conn.close()

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "escalations.json").write_text(json.dumps(escalations, indent=2, default=str))
    (OUTPUT_DIR / "needs_human_review.json").write_text(json.dumps(needs_review, indent=2, default=str))
    (OUTPUT_DIR / "cleared.json").write_text(json.dumps(cleared, indent=2, default=str))
    (OUTPUT_DIR / "audit_log.json").write_text(json.dumps(audit_log, indent=2, default=str))

    summary = dict(
        providers_evaluated=len(providers), escalated=len(escalations),
        needs_human_review=len(needs_review), cleared=len(cleared),
        min_sample_size=MIN_SAMPLE_SIZE,
    )
    (OUTPUT_DIR / "run_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
