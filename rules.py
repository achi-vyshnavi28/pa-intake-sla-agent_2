"""
Deterministic data-validation rule engine for PA intake.

Implements SOP-001 (Intake Validation) as executable code -- the SOP in
docs/sops/ describes exactly these rules in prose, so the two stay in
lockstep by construction.

Every rule returns a 5-status verdict, never a bare pass/fail:
  VALID       passed every applicable check
  INVALID     structurally broken; must not enter the core warehouse
  ANOMALOUS   structurally fine, but statistically/behaviorally unusual;
              loaded into core AND flagged for the SLA escalation agent
  INCOMPLETE  missing a required field
  UNCERTAIN   can't be conclusively judged yet (e.g. still pending)

Nothing is ever silently dropped: every row that isn't VALID is logged
with a rule_code and a human-readable reason (see etl/staging_to_core.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from stats_utils import robust_zscore

VALID, INVALID, ANOMALOUS, INCOMPLETE, UNCERTAIN = (
    "VALID", "INVALID", "ANOMALOUS", "INCOMPLETE", "UNCERTAIN",
)

UNIT_OUTLIER_ZSCORE_THRESHOLD = 6.0
DUPLICATE_WINDOW_DAYS = 3


@dataclass(frozen=True)
class Violation:
    source_table: str
    source_pk: str
    rule_code: str
    status: str
    reason: str


def _is_valid_iso_date(value) -> bool:
    try:
        date.fromisoformat(str(value))
        return True
    except (ValueError, TypeError):
        return False


def rule_valid_provider_ref(pa: pd.DataFrame, providers: pd.DataFrame) -> list[Violation]:
    """R-001: provider_id must reference a known provider."""
    known = set(providers["provider_id"])
    bad = pa[~pa["provider_id"].isin(known)]
    return [Violation("pa_requests", r.pa_id, "R-001", INVALID,
                       f"provider_id '{r.provider_id}' not found in providers dimension")
            for r in bad.itertuples()]


def rule_required_fields_present(pa: pd.DataFrame) -> list[Violation]:
    """R-002: member_id, provider_id, service_category must be non-null/non-empty."""
    out = []
    for col in ["member_id", "provider_id", "service_category"]:
        missing = pa[pa[col].isna() | (pa[col].astype(str).str.strip() == "")]
        out += [Violation("pa_requests", r.pa_id, "R-002", INCOMPLETE,
                           f"required field '{col}' is missing") for r in missing.itertuples()]
    return out


def rule_valid_date_format(pa: pd.DataFrame) -> list[Violation]:
    """R-003: request_date must parse as an ISO-8601 date."""
    bad = pa[~pa["request_date"].apply(_is_valid_iso_date)]
    return [Violation("pa_requests", r.pa_id, "R-003", INVALID,
                       f"request_date '{r.request_date}' is not a valid ISO date")
            for r in bad.itertuples()]


def rule_positive_units(pa: pd.DataFrame) -> list[Violation]:
    """R-004: requested_units must be a positive integer."""
    bad = pa[pd.to_numeric(pa["requested_units"], errors="coerce").fillna(-1) <= 0]
    return [Violation("pa_requests", r.pa_id, "R-004", INVALID,
                       f"requested_units={r.requested_units} is not a positive integer")
            for r in bad.itertuples()]


def rule_duplicate_submission(pa: pd.DataFrame) -> list[Violation]:
    """R-005: same member+provider+service_category submitted again within
    DUPLICATE_WINDOW_DAYS days -> ANOMALOUS (not rejected -- both
    submissions are structurally valid; this is a behavioral/process flag
    for the escalation agent to investigate)."""
    out = []
    df = pa.copy()
    df["_date"] = pd.to_datetime(df["request_date"], errors="coerce")
    df = df.dropna(subset=["_date"])
    for _, group in df.groupby(["member_id", "provider_id", "service_category"]):
        if len(group) < 2:
            continue
        group = group.sort_values("_date")
        dates, pa_ids = group["_date"].tolist(), group["pa_id"].tolist()
        for i in range(1, len(dates)):
            gap = (dates[i] - dates[i - 1]).days
            if 0 <= gap <= DUPLICATE_WINDOW_DAYS:
                out.append(Violation("pa_requests", pa_ids[i], "R-005", ANOMALOUS,
                                      f"possible duplicate submission of {pa_ids[i-1]} "
                                      f"({gap}d apart, same member/provider/category)"))
    return out


def rule_unit_outlier(pa: pd.DataFrame) -> list[Violation]:
    """R-006: robust (median/MAD) z-score of requested_units within each
    provider x service_category group flags wildly-wrong unit entries
    without being thrown off by the outliers themselves."""
    out = []
    df = pa.copy()
    df["requested_units"] = pd.to_numeric(df["requested_units"], errors="coerce")
    for _, group in df.groupby(["provider_id", "service_category"]):
        if len(group) < 5:
            continue
        z = robust_zscore(group["requested_units"].to_numpy())
        flagged = group.assign(zscore=z)
        flagged = flagged[np.abs(flagged["zscore"]) > UNIT_OUTLIER_ZSCORE_THRESHOLD]
        out += [Violation("pa_requests", r.pa_id, "R-006", ANOMALOUS,
                           f"requested_units={r.requested_units} is a robust-z-score outlier "
                           f"(|z|={abs(r.zscore):.1f}) vs. this provider/category's typical volume")
                for r in flagged.itertuples()]
    return out


def rule_pending_beyond_sla(pa: pd.DataFrame, determinations: pd.DataFrame, as_of: date) -> list[Violation]:
    """R-007: a request with no determination yet, older than its own SLA
    target, is UNCERTAIN -- not a confirmed breach, but needs human eyes."""
    decided = set(determinations["pa_id"])
    df = pa.copy()
    df["reqdate"] = pd.to_datetime(df["request_date"], errors="coerce")
    out = []
    for r in df.itertuples():
        if r.pa_id in decided or pd.isna(r.reqdate):
            continue
        age_days = (as_of - r.reqdate.date()).days
        if age_days > r.sla_target_days:
            out.append(Violation("pa_requests", r.pa_id, "R-007", UNCERTAIN,
                                  f"still pending {age_days}d after request; SLA target was "
                                  f"{r.sla_target_days}d -- needs human review"))
    return out


def rule_decision_after_request(pa: pd.DataFrame, determinations: pd.DataFrame) -> list[Violation]:
    """R-008: decision_date must not precede request_date."""
    merged = determinations.merge(pa[["pa_id", "request_date"]], on="pa_id", how="left")
    merged["_req"] = pd.to_datetime(merged["request_date"], errors="coerce")
    merged["_dec"] = pd.to_datetime(merged["decision_date"], errors="coerce")
    bad = merged[merged["_dec"] < merged["_req"]]
    return [Violation("pa_determinations", r.pa_id, "R-008", INVALID,
                       f"decision_date {r.decision_date} precedes request_date {r.request_date}")
            for r in bad.itertuples()]


RULE_CODES_DOC = {
    "R-001": "valid provider reference",
    "R-002": "required fields present",
    "R-003": "valid ISO date format",
    "R-004": "positive requested units",
    "R-005": "duplicate submission detection",
    "R-006": "unit outlier (robust z-score)",
    "R-007": "pending beyond SLA target",
    "R-008": "decision date after request date",
}
