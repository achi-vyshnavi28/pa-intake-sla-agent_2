-- Amaranth Health Partners -- PA Intake & SLA Core Warehouse Schema
-- Portable ANSI SQL (no SQLite-only pragmas/syntax) so this DDL reads
-- against Postgres/Snowflake with only minor type substitutions.
--
-- VALIDATED / CORE layer, populated by etl/staging_to_core.py from the
-- raw staging tables. Rows that fail validation never reach this layer --
-- they are written to etl_reject_log instead (see SOP-001).

CREATE TABLE dim_providers (
    provider_id     TEXT PRIMARY KEY,
    provider_name   TEXT NOT NULL,
    specialty       TEXT NOT NULL
);

CREATE TABLE dim_review_queues (
    queue_id        TEXT PRIMARY KEY,
    queue_code      TEXT NOT NULL UNIQUE,
    queue_name      TEXT NOT NULL
);

CREATE TABLE fact_pa_requests (
    pa_id               TEXT PRIMARY KEY,
    member_id           TEXT NOT NULL,        -- tokenized only: MBR-######## ; never a name/DOB/SSN
    provider_id         TEXT NOT NULL REFERENCES dim_providers(provider_id),
    service_category    TEXT NOT NULL,
    urgency              TEXT NOT NULL CHECK (urgency IN ('standard', 'urgent')),
    sla_target_days      INTEGER NOT NULL,    -- 14 standard / 3 urgent: illustrative industry-typical convention, not a quoted regulation
    review_queue_code    TEXT NOT NULL REFERENCES dim_review_queues(queue_code),
    requested_units       INTEGER NOT NULL,
    request_date           DATE NOT NULL,
    request_week            INTEGER NOT NULL   -- 0-indexed week of quarter, precomputed for trend queries
);
CREATE INDEX idx_pa_requests_provider ON fact_pa_requests(provider_id);
CREATE INDEX idx_pa_requests_queue ON fact_pa_requests(review_queue_code);
CREATE INDEX idx_pa_requests_week ON fact_pa_requests(request_week);

CREATE TABLE fact_pa_determinations (
    pa_id               TEXT PRIMARY KEY REFERENCES fact_pa_requests(pa_id),
    decision             TEXT NOT NULL CHECK (decision IN ('approved', 'partial', 'denied')),
    approved_units         INTEGER NOT NULL,
    decision_date            DATE NOT NULL,
    turnaround_days           INTEGER NOT NULL,
    sla_breached                INTEGER NOT NULL CHECK (sla_breached IN (0, 1))
);

-- Full reject-row audit trail: every row that failed a validation rule
-- during staging-to-core ETL, with its reason code and original payload.
-- Nothing is ever silently dropped (see SOP-001).
CREATE TABLE etl_reject_log (
    reject_id            INTEGER PRIMARY KEY,
    source_table          TEXT NOT NULL,
    source_pk               TEXT,
    rule_code                 TEXT NOT NULL,
    status                     TEXT NOT NULL CHECK (status IN ('INVALID', 'ANOMALOUS', 'INCOMPLETE', 'UNCERTAIN')),
    reason                       TEXT NOT NULL,
    raw_payload_json            TEXT NOT NULL,
    logged_at                     TEXT NOT NULL
);
CREATE INDEX idx_reject_log_table ON etl_reject_log(source_table);
CREATE INDEX idx_reject_log_status ON etl_reject_log(status);
