"""
Staging -> Core ETL for the PA Intake & SLA warehouse.

Reads the raw staging tables, runs every validation rule from
validation/rules.py, and produces the validated core warehouse
(sql/schema.sql). Rows with an INVALID or INCOMPLETE verdict never reach
core -- they are logged to etl_reject_log with a reason code and the full
original payload (nothing is silently dropped). Rows with only ANOMALOUS
or UNCERTAIN verdicts DO load into core (they are structurally legitimate
PA requests) but are also logged, so the escalation agent can pull them
back out as investigation leads.

Run: python3 etl/staging_to_core.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for `python3 etl/staging_to_core.py`

from validation.rules import (
    INCOMPLETE, INVALID,
    rule_decision_after_request, rule_duplicate_submission, rule_pending_beyond_sla,
    rule_positive_units, rule_required_fields_present, rule_unit_outlier,
    rule_valid_date_format, rule_valid_provider_ref,
)

HERE = Path(__file__).resolve().parent.parent
RAW_DB = HERE / "data" / "amaranth_pa_raw.db"
CORE_DB = HERE / "data" / "amaranth_pa_core.db"
SCHEMA_SQL = HERE / "sql" / "schema.sql"

# Dataset "as-of" date for pending-beyond-SLA evaluation: one day after the
# last request date in the generated quarter, so the demo dataset has a
# realistic, deterministic "today".
AS_OF_DATE = date(2026, 4, 6)


def run() -> dict:
    raw = sqlite3.connect(RAW_DB)
    providers = pd.read_sql("SELECT * FROM providers", raw)
    queues = pd.read_sql("SELECT * FROM review_queues", raw)
    pa = pd.read_sql("SELECT * FROM pa_requests_raw", raw)
    det = pd.read_sql("SELECT * FROM pa_determinations_raw", raw)
    raw.close()

    violations = []
    violations += rule_valid_provider_ref(pa, providers)
    violations += rule_required_fields_present(pa)
    violations += rule_valid_date_format(pa)
    violations += rule_positive_units(pa)
    violations += rule_duplicate_submission(pa)
    violations += rule_unit_outlier(pa)
    violations += rule_pending_beyond_sla(pa, det, AS_OF_DATE)
    det_violations = rule_decision_after_request(pa, det)
    violations += det_violations

    pa_by_id = pa.set_index("pa_id", drop=False).to_dict("index")
    det_by_id = det.set_index("pa_id", drop=False).to_dict("index")

    reject_pa_ids = {v.source_pk for v in violations
                      if v.source_table == "pa_requests" and v.status in (INVALID, INCOMPLETE)}
    reject_det_pa_ids = {v.source_pk for v in det_violations if v.status in (INVALID, INCOMPLETE)}

    core_pa = pa[~pa["pa_id"].isin(reject_pa_ids)].copy()
    core_det = det[~det["pa_id"].isin(reject_det_pa_ids) & det["pa_id"].isin(core_pa["pa_id"])].copy()

    CORE_DB.unlink(missing_ok=True)
    core = sqlite3.connect(CORE_DB)
    core.executescript(SCHEMA_SQL.read_text())

    providers[["provider_id", "name", "specialty"]] \
        .rename(columns={"name": "provider_name"}) \
        .to_sql("dim_providers", core, if_exists="append", index=False)
    queues.rename(columns={"name": "queue_name"}).to_sql("dim_review_queues", core, if_exists="append", index=False)
    core_pa.to_sql("fact_pa_requests", core, if_exists="append", index=False)
    core_det.to_sql("fact_pa_determinations", core, if_exists="append", index=False)

    reject_rows = []
    for v in violations:
        payload = pa_by_id.get(v.source_pk) if v.source_table == "pa_requests" else det_by_id.get(v.source_pk)
        reject_rows.append(dict(
            source_table=v.source_table, source_pk=v.source_pk, rule_code=v.rule_code,
            status=v.status, reason=v.reason,
            raw_payload_json=json.dumps(payload, default=str) if payload else "{}",
            logged_at=AS_OF_DATE.isoformat(),
        ))
    reject_df = pd.DataFrame(reject_rows, columns=[
        "source_table", "source_pk", "rule_code", "status", "reason", "raw_payload_json", "logged_at"])
    if not reject_df.empty:
        reject_df.to_sql("etl_reject_log", core, if_exists="append", index=False)
    core.commit()
    core.close()

    summary = dict(
        pa_requests_raw=len(pa), pa_requests_core=len(core_pa),
        pa_requests_rejected=len(reject_pa_ids),
        determinations_raw=len(det), determinations_core=len(core_det),
        determinations_rejected=len(reject_det_pa_ids),
        total_violations_logged=len(reject_df),
        violations_by_status=reject_df["status"].value_counts().to_dict() if not reject_df.empty else {},
        violations_by_rule=reject_df["rule_code"].value_counts().to_dict() if not reject_df.empty else {},
    )
    return summary


if __name__ == "__main__":
    s = run()
    print(json.dumps(s, indent=2, default=str))
