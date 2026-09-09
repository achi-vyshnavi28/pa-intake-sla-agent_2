"""
Synthetic data generator -- Amaranth Health Partners (FICTIONAL health plan).

Generates one quarter of prior-authorization (PA) intake traffic for 8
synthetic providers routed across 4 review queues. Fixed random seed for
full reproducibility. 100% synthetic: member identifiers are tokenized by
design (format MBR-######## -- never a name, DOB, or SSN is generated,
this is privacy-by-design, not after-the-fact redaction).

This repo is intentionally self-contained: it does not read or write
anything belonging to the payment-integrity or data-science-handoff repos.

Writes:
  - data/amaranth_pa_raw.db      raw/staging SQLite (includes a few
                                  deliberately dirty rows for the ETL
                                  reject-audit trail to catch)
  - data/ground_truth.json       TEST-ONLY fixture recording planted
                                  anomalies, never read by the agent or
                                  validation engine -- used solely so the
                                  pytest suite can measure real precision/
                                  recall instead of hand-typed numbers.

Run: python3 data/generate_data.py
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SEED = 42
QUARTER_START = date(2026, 1, 5)  # Monday, start of Q1 2026
N_WEEKS = 13
PEAK_WEEK = 11            # seasonal peak: chronic/degrading breach rates worsen here
INCIDENT_WEEK = 6         # documented review-queue capacity-backlog week
INCIDENT_QUEUE_CODE = "Q3"
INCIDENT_EXTRA_DAYS = 4

HERE = Path(__file__).resolve().parent
RAW_DB_PATH = HERE / "amaranth_pa_raw.db"
GROUND_TRUTH_PATH = HERE / "ground_truth.json"

rng = np.random.default_rng(SEED)
pyrand = random.Random(SEED)


@dataclass
class Provider:
    provider_id: str
    name: str
    specialty: str
    archetype: str


@dataclass
class ReviewQueue:
    queue_id: str
    queue_code: str
    name: str


PROVIDERS: list[Provider] = [
    Provider("PRV-001", "Ridgeline Orthopedic Associates", "Orthopedics", "clean"),
    Provider("PRV-002", "Amaranth Diagnostic Imaging Center", "Imaging", "clean"),
    Provider("PRV-003", "Harborview Behavioral Health Group", "Behavioral Health", "clean"),
    Provider("PRV-004", "Meridian Durable Medical Equipment", "DME", "duplicate_submission"),
    Provider("PRV-005", "Crestwood Surgical Partners", "Surgery", "coding_error"),
    Provider("PRV-006", "Amaranth Specialty Pharmacy Network", "Specialty Rx", "chronic_sla_breach"),
    Provider("PRV-007", "Lakeside Cardiology Associates", "Cardiology", "improving_trend"),
    Provider("PRV-008", "Sable Diagnostics & Labs", "Diagnostics", "degrading_trend"),
]

REVIEW_QUEUES: list[ReviewQueue] = [
    ReviewQueue("RQ-1", "Q1", "Standard Utilization Review Queue"),
    ReviewQueue("RQ-2", "Q2", "Urgent / Expedited Review Queue"),
    ReviewQueue("RQ-3", "Q3", "Specialty & DME Review Queue"),
    ReviewQueue("RQ-4", "Q4", "Surgical & Behavioral Health Review Queue"),
]

PRIMARY_QUEUE = {
    "PRV-001": "Q1", "PRV-002": "Q3", "PRV-003": "Q4", "PRV-004": "Q3",
    "PRV-005": "Q4", "PRV-006": "Q1", "PRV-007": "Q1", "PRV-008": "Q3",
}


def week_start(idx: int) -> date:
    return QUARTER_START + timedelta(days=7 * idx)


def gen_member_id() -> str:
    return f"MBR-{pyrand.randint(0, 99_999_999):08d}"


def breach_probability(archetype: str, week_idx: int) -> float:
    t = week_idx / max(N_WEEKS - 1, 1)
    if archetype == "clean":
        return 0.05
    if archetype == "duplicate_submission":
        return 0.06
    if archetype == "coding_error":
        return 0.06
    if archetype == "chronic_sla_breach":
        base = 0.50
        if week_idx == PEAK_WEEK:
            base += 0.20
        return min(base, 0.85)
    if archetype == "improving_trend":
        return 0.55 - 0.45 * t
    if archetype == "degrading_trend":
        return 0.08 + 0.47 * t
    return 0.05


@dataclass
class GroundTruth:
    duplicate_pairs: list[dict] = field(default_factory=list)
    coding_error_pa_ids: list[str] = field(default_factory=list)
    queue_incident: dict = field(default_factory=dict)
    provider_archetypes: dict = field(default_factory=dict)
    peak_week: int = PEAK_WEEK


def generate() -> None:
    gt = GroundTruth(
        queue_incident={
            "queue_code": INCIDENT_QUEUE_CODE, "week_index": INCIDENT_WEEK,
            "week_start": week_start(INCIDENT_WEEK).isoformat(), "extra_days_added": INCIDENT_EXTRA_DAYS,
            "description": ("Documented capacity backlog on the Specialty & DME Review Queue "
                             "during week 6 added ~4 days to every determination routed through "
                             "it that week, independent of provider behavior."),
        },
        provider_archetypes={p.provider_id: p.archetype for p in PROVIDERS},
    )

    pa_rows, det_rows = [], []
    pa_counter = 1

    for provider in PROVIDERS:
        archetype = provider.archetype
        for week_idx in range(N_WEEKS):
            n_requests = max(1, int(rng.normal(11, 2.5)))
            for _ in range(n_requests):
                pa_id = f"PA-{pa_counter:06d}"
                pa_counter += 1
                member_id = gen_member_id()
                urgency = "urgent" if pyrand.random() < 0.20 else "standard"
                sla_target = 3 if urgency == "urgent" else 14
                req_date = week_start(week_idx) + timedelta(days=pyrand.randint(0, 6))

                queue_code = PRIMARY_QUEUE[provider.provider_id] if pyrand.random() < 0.85 \
                    else pyrand.choice([q.queue_code for q in REVIEW_QUEUES])

                if archetype == "coding_error" and pyrand.random() < 0.05:
                    requested_units = pyrand.randint(300, 999)
                    gt.coding_error_pa_ids.append(pa_id)
                else:
                    # Tightly clipped so natural variation never approaches
                    # the robust-z-score outlier threshold on its own --
                    # only a planted coding-error value (300-999) should
                    # ever trip R-006. A heavy-tailed distribution here
                    # would contaminate the detection-performance signal
                    # with false positives that have nothing to do with
                    # the archetype being demonstrated.
                    requested_units = int(np.clip(rng.lognormal(mean=1.1, sigma=0.35), 1, 15))

                bp = breach_probability(archetype, week_idx)
                will_breach = pyrand.random() < bp
                turnaround = (sla_target + pyrand.randint(1, max(2, int(sla_target * 0.8) + 2))
                              if will_breach else pyrand.randint(1, sla_target))

                if queue_code == INCIDENT_QUEUE_CODE and week_idx == INCIDENT_WEEK:
                    turnaround += INCIDENT_EXTRA_DAYS

                breached = turnaround > sla_target
                is_pending = pyrand.random() < 0.02

                pa_rows.append(dict(
                    pa_id=pa_id, member_id=member_id, provider_id=provider.provider_id,
                    service_category=provider.specialty, urgency=urgency, sla_target_days=sla_target,
                    review_queue_code=queue_code, requested_units=requested_units,
                    request_date=req_date.isoformat(), request_week=week_idx,
                ))

                if not is_pending:
                    decision_date = req_date + timedelta(days=turnaround)
                    roll = pyrand.random()
                    if roll < 0.80:
                        decision, approved_units = "approved", requested_units
                    elif roll < 0.92:
                        decision = "partial"
                        approved_units = max(1, round(requested_units * pyrand.uniform(0.4, 0.9)))
                    else:
                        decision, approved_units = "denied", 0
                    det_rows.append(dict(
                        pa_id=pa_id, decision=decision, approved_units=approved_units,
                        decision_date=decision_date.isoformat(), turnaround_days=turnaround,
                        sla_breached=int(breached),
                    ))

                if archetype == "duplicate_submission" and pyrand.random() < 0.03:
                    dup_id = f"PA-{pa_counter:06d}"
                    pa_counter += 1
                    dup_date = req_date + timedelta(days=pyrand.randint(0, 2))
                    dup_units = max(1, requested_units + pyrand.choice([-1, 0, 0, 1]))
                    pa_rows.append(dict(
                        pa_id=dup_id, member_id=member_id, provider_id=provider.provider_id,
                        service_category=provider.specialty, urgency=urgency, sla_target_days=sla_target,
                        review_queue_code=queue_code, requested_units=dup_units,
                        request_date=dup_date.isoformat(), request_week=week_idx,
                    ))
                    gt.duplicate_pairs.append(dict(original_pa_id=pa_id, duplicate_pa_id=dup_id,
                                                    provider_id=provider.provider_id))

    _write_sqlite(pa_rows, det_rows)
    _write_ground_truth(gt)
    print(f"Generated: {len(pa_rows)} pa_requests, {len(det_rows)} determinations")
    print(f"Raw DB: {RAW_DB_PATH}\nGround truth: {GROUND_TRUTH_PATH}")


def _write_sqlite(pa_rows, det_rows) -> None:
    RAW_DB_PATH.unlink(missing_ok=True)
    conn = sqlite3.connect(RAW_DB_PATH)
    cur = conn.cursor()

    cur.execute("CREATE TABLE providers (provider_id TEXT PRIMARY KEY, name TEXT, specialty TEXT, archetype_note TEXT)")
    cur.executemany("INSERT INTO providers VALUES (?,?,?,?)",
                     [(p.provider_id, p.name, p.specialty, p.archetype) for p in PROVIDERS])

    cur.execute("CREATE TABLE review_queues (queue_id TEXT PRIMARY KEY, queue_code TEXT, name TEXT)")
    cur.executemany("INSERT INTO review_queues VALUES (?,?,?)",
                     [(q.queue_id, q.queue_code, q.name) for q in REVIEW_QUEUES])

    cur.execute("""CREATE TABLE pa_requests_raw (
        pa_id TEXT, member_id TEXT, provider_id TEXT, service_category TEXT, urgency TEXT,
        sla_target_days INTEGER, review_queue_code TEXT, requested_units INTEGER,
        request_date TEXT, request_week INTEGER)""")
    cur.executemany(
        "INSERT INTO pa_requests_raw VALUES (:pa_id,:member_id,:provider_id,:service_category,:urgency,"
        ":sla_target_days,:review_queue_code,:requested_units,:request_date,:request_week)", pa_rows)

    cur.execute("""CREATE TABLE pa_determinations_raw (
        pa_id TEXT, decision TEXT, approved_units INTEGER, decision_date TEXT,
        turnaround_days INTEGER, sla_breached INTEGER)""")
    cur.executemany(
        "INSERT INTO pa_determinations_raw VALUES (:pa_id,:decision,:approved_units,:decision_date,"
        ":turnaround_days,:sla_breached)", det_rows)

    # Deliberately dirty rows so the ETL reject-audit trail has real work to
    # do: bad provider FK, negative units, malformed date.
    cur.execute("INSERT INTO pa_requests_raw VALUES "
                "('PA-999901','MBR-00000001','PRV-999','Orthopedics','standard',14,'Q1',3,'2026-01-10',0)")
    cur.execute("INSERT INTO pa_requests_raw VALUES "
                "('PA-999902','MBR-00000002','PRV-001','Orthopedics','standard',14,'Q1',-4,'2026-01-12',0)")
    cur.execute("INSERT INTO pa_requests_raw VALUES "
                "('PA-999903','MBR-00000003','PRV-002','Imaging','standard',14,'Q3',2,'not-a-date',0)")
    conn.commit()
    conn.close()


def _write_ground_truth(gt: GroundTruth) -> None:
    payload = dict(
        seed=SEED, quarter_start=QUARTER_START.isoformat(), n_weeks=N_WEEKS, peak_week=gt.peak_week,
        provider_archetypes=gt.provider_archetypes, queue_incident=gt.queue_incident,
        duplicate_pairs=gt.duplicate_pairs, coding_error_pa_ids=sorted(set(gt.coding_error_pa_ids)),
        injected_dirty_rows=["PA-999901 (bad provider FK)", "PA-999902 (negative units)",
                              "PA-999903 (malformed date)"],
        note=("TEST FIXTURE ONLY. Never read by the agent or validation engine. "
              "100% synthetic data; no real member, provider, or claims data was used."),
    )
    GROUND_TRUTH_PATH.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    generate()
