"""Rule-level unit tests for validation/rules.py, built on small hand-
constructed DataFrames so each rule's behavior is checked in isolation
(independent of the generated synthetic dataset)."""
from datetime import date

import pandas as pd

from validation.rules import (
    ANOMALOUS, INCOMPLETE, INVALID, UNCERTAIN,
    rule_decision_after_request, rule_duplicate_submission, rule_pending_beyond_sla,
    rule_positive_units, rule_required_fields_present, rule_unit_outlier,
    rule_valid_date_format, rule_valid_provider_ref,
)

PROVIDERS = pd.DataFrame({"provider_id": ["PRV-001", "PRV-002"], "name": ["A", "B"], "specialty": ["X", "Y"]})


def _pa_row(**overrides):
    base = dict(pa_id="PA-1", member_id="MBR-00000001", provider_id="PRV-001",
                service_category="Orthopedics", urgency="standard", sla_target_days=14,
                review_queue_code="Q1", requested_units=3, request_date="2026-01-05", request_week=0)
    base.update(overrides)
    return base


def test_valid_provider_ref_flags_unknown_provider():
    pa = pd.DataFrame([_pa_row(provider_id="PRV-999")])
    v = rule_valid_provider_ref(pa, PROVIDERS)
    assert len(v) == 1 and v[0].status == INVALID and v[0].rule_code == "R-001"


def test_valid_provider_ref_passes_known_provider():
    pa = pd.DataFrame([_pa_row()])
    assert rule_valid_provider_ref(pa, PROVIDERS) == []


def test_required_fields_present_flags_missing_member_id():
    pa = pd.DataFrame([_pa_row(member_id=None)])
    v = rule_required_fields_present(pa)
    assert len(v) == 1 and v[0].status == INCOMPLETE


def test_valid_date_format_flags_malformed_date():
    pa = pd.DataFrame([_pa_row(request_date="not-a-date")])
    v = rule_valid_date_format(pa)
    assert len(v) == 1 and v[0].status == INVALID


def test_valid_date_format_passes_iso_date():
    pa = pd.DataFrame([_pa_row(request_date="2026-02-14")])
    assert rule_valid_date_format(pa) == []


def test_positive_units_flags_negative_and_zero():
    pa = pd.DataFrame([_pa_row(pa_id="PA-1", requested_units=-4), _pa_row(pa_id="PA-2", requested_units=0)])
    v = rule_positive_units(pa)
    assert {x.source_pk for x in v} == {"PA-1", "PA-2"}
    assert all(x.status == INVALID for x in v)


def test_duplicate_submission_flags_same_day_resubmit():
    pa = pd.DataFrame([
        _pa_row(pa_id="PA-1", request_date="2026-01-05"),
        _pa_row(pa_id="PA-2", request_date="2026-01-06"),  # 1 day later, same member/provider/category
    ])
    v = rule_duplicate_submission(pa)
    assert len(v) == 1 and v[0].source_pk == "PA-2" and v[0].status == ANOMALOUS


def test_duplicate_submission_does_not_flag_outside_window():
    pa = pd.DataFrame([
        _pa_row(pa_id="PA-1", request_date="2026-01-05"),
        _pa_row(pa_id="PA-2", request_date="2026-01-20"),  # 15 days later -- not a duplicate
    ])
    assert rule_duplicate_submission(pa) == []


def test_unit_outlier_flags_extreme_value_in_stable_group():
    rows = [_pa_row(pa_id=f"PA-{i}", requested_units=3 + (i % 2)) for i in range(8)]
    rows.append(_pa_row(pa_id="PA-OUTLIER", requested_units=900))
    pa = pd.DataFrame(rows)
    v = rule_unit_outlier(pa)
    flagged_ids = {x.source_pk for x in v}
    assert "PA-OUTLIER" in flagged_ids
    assert all(x.status == ANOMALOUS for x in v)


def test_unit_outlier_skips_small_groups():
    pa = pd.DataFrame([_pa_row(pa_id="PA-1", requested_units=3), _pa_row(pa_id="PA-2", requested_units=900)])
    # fewer than 5 rows in the peer group -> not enough data to establish a baseline
    assert rule_unit_outlier(pa) == []


def test_pending_beyond_sla_flags_stale_pending_request():
    pa = pd.DataFrame([_pa_row(pa_id="PA-1", request_date="2026-01-01", sla_target_days=14)])
    det = pd.DataFrame(columns=["pa_id"])
    v = rule_pending_beyond_sla(pa, det, as_of=date(2026, 2, 1))  # 31 days later, target was 14
    assert len(v) == 1 and v[0].status == UNCERTAIN


def test_pending_beyond_sla_does_not_flag_recent_pending():
    pa = pd.DataFrame([_pa_row(pa_id="PA-1", request_date="2026-01-01", sla_target_days=14)])
    det = pd.DataFrame(columns=["pa_id"])
    v = rule_pending_beyond_sla(pa, det, as_of=date(2026, 1, 5))  # only 4 days later
    assert v == []


def test_decision_after_request_flags_backwards_dates():
    pa = pd.DataFrame([_pa_row(pa_id="PA-1", request_date="2026-01-10")])
    det = pd.DataFrame([dict(pa_id="PA-1", decision="approved", approved_units=3,
                              decision_date="2026-01-05", turnaround_days=-5, sla_breached=0)])
    v = rule_decision_after_request(pa, det)
    assert len(v) == 1 and v[0].status == INVALID
