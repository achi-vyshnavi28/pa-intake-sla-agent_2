# SOP-001: Prior Authorization Intake Validation

**Scope:** Every prior authorization (PA) request received from a provider before it enters the core review warehouse.
**Owner:** Data Operations
**System of record:** `validation/rules.py` implements every rule below in executable code; this document and the code are kept in lockstep -- a rule change updates both in the same commit.

## 1. Purpose

Define the deterministic checks a PA intake record must pass before it is trusted for downstream SLA monitoring, provider scorecards, or investigation workflows. Nothing is silently dropped: every record that is not fully `VALID` is logged with a rule code and a human-readable reason in `etl_reject_log` (see `sql/schema.sql`).

## 2. Status taxonomy

Every record receives one of five statuses -- never a bare pass/fail:

| Status | Meaning | Loaded to core? |
|---|---|---|
| `VALID` | Passed every applicable check | Yes |
| `INVALID` | Structurally broken (bad reference, malformed date, non-positive units) | **No** -- rejected, logged |
| `INCOMPLETE` | A required field is missing | **No** -- rejected, logged |
| `ANOMALOUS` | Structurally fine but statistically/behaviorally unusual | Yes -- and flagged for investigation |
| `UNCERTAIN` | Cannot yet be conclusively judged (e.g. still pending) | Yes -- and flagged for human review |

## 3. Rules

| Rule | Check | Status on failure |
|---|---|---|
| R-001 | `provider_id` must reference a known provider | INVALID |
| R-002 | `member_id`, `provider_id`, `service_category` must be present | INCOMPLETE |
| R-003 | `request_date` must be a valid ISO-8601 date | INVALID |
| R-004 | `requested_units` must be a positive integer | INVALID |
| R-005 | Same member + provider + service category resubmitted within 3 days of a prior request | ANOMALOUS (possible duplicate submission) |
| R-006 | `requested_units` is a robust (median/MAD) z-score outlier (\|z\| > 6) within its provider + service-category peer group | ANOMALOUS (possible coding/data-entry error) |
| R-007 | Request has no determination on file and is already older than its own SLA target | UNCERTAIN (SLA-at-risk while pending) |
| R-008 | Determination `decision_date` precedes the request's `request_date` | INVALID (on the determination record) |

## 4. Attention-to-detail requirement

Analysts reviewing the reject log must confirm, for every `INVALID`/`INCOMPLETE` row, whether the source system upstream needs a data-entry fix (most common cause) or whether the rule itself needs revisiting. A rule change requires a corresponding update to `docs/sops/SOP-001-intake-validation.md` and `validation/rules.py` in the same change set -- this SOP is not a description written after the fact, it is the spec the code implements.

## 5. Escalation

`ANOMALOUS` and `UNCERTAIN` records are never escalated to a provider or stakeholder directly from the validation layer. They are handed to the SLA escalation agent (SOP-002), which applies the Verification & Trust Layer (confidence scoring, cross-checking) before anything reaches a human.

## 6. Disclaimer

This SOP describes a demo pipeline built against 100% synthetic data for a fictional health plan (Amaranth Health Partners). It illustrates industry-standard prior-authorization intake practices and is not a reproduction of any real payer's proprietary procedure.
