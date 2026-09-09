# SOP-002: PA Turnaround SLA Monitoring & Escalation

**Scope:** Turnaround-time compliance for all PA requests with a determination on file.
**Owner:** Utilization Management Operations
**System of record:** `agent/umguard.py` implements every step below; `agent/trust_layer.py` implements the verification gate that governs what may reach a stakeholder.

## 1. SLA targets

| Urgency | Target turnaround | Note |
|---|---|---|
| Standard | 14 calendar days | Illustrative, industry-typical convention used for this demo -- not a quoted regulation or a specific payer's actual contractual SLA. |
| Urgent | 3 calendar days | Same caveat. |

A request with no determination yet is **not** counted as breached or compliant -- SOP-001 rule R-007 flags it `UNCERTAIN` once it is already older than its own target, and it is routed for human review rather than silently excluded or silently marked pending-forever.

## 2. Weekly monitoring

Every week, breach rate is computed per provider and per review queue using two independent methods that must agree (trust-layer cross-check):

1. **SQL**: a window-function query (`RANK`, `LAG`, 3-week moving average) over the core warehouse.
2. **Pandas**: an independent recomputation from the same core tables, without reusing the SQL query text.

Disagreement between the two is never silently resolved -- it downgrades the finding's confidence to `INSUFFICIENT_EVIDENCE` and is logged.

## 3. Root-cause attribution

A provider's elevated breach rate is attributed to exactly one of:

- **`provider_side_trend_degrading` / `provider_side_trend_improving`** -- a statistically material (OLS slope threshold + R² threshold, both documented in `agent/umguard.py`) multi-week trend. Checked first, because a real quarter-long trend should not be masked by one week that happens to coincide with an unrelated event.
- **`provider_side_chronic`** -- persistently elevated breach rate across most weeks, no single dominant trend or incident.
- **`review_queue_capacity_incident`** -- an isolated elevated week that coincides with a documented queue-wide breach spike among *other* providers on the same queue that week (a capacity/backlog event, not a provider problem).
- **`no_material_pattern`** -- breach rate is within the expected clean-operations range; not escalated.

## 4. Confidence & escalation gate

Every finding carries an explicit confidence level (`HIGH` / `MEDIUM` / `LOW` / `INSUFFICIENT_EVIDENCE`) from the sample size, cross-check agreement, and evidence completeness. Only `HIGH` or `MEDIUM` confidence findings with a material root cause are escalated to a stakeholder. `LOW` or `INSUFFICIENT_EVIDENCE` findings are routed to a **needs-human-review** queue instead of forcing a confident-sounding but under-evidenced conclusion.

## 5. Escalation note drafting

Escalation notes may be drafted by an LLM (or, with no API key configured, a deterministic evidence-tied template -- both paths are exercised by the test suite). Every number in a drafted note is checked against the evidence bundle before release (`verify_grounding`); a note containing any unverifiable number is discarded in full and replaced with the deterministic template. This is proven with an adversarial test (`tests/test_trust_layer.py`) using a fake "hallucinating" LLM that always invents a plausible-but-false number.

## 6. Audit trail

Every provider's full derivation -- SQL result, Pandas result, cross-check outcome, root-cause reasoning, confidence assessment, LLM draft, verification result, and final decision -- is written to `output/audit_log.json`. Any escalation is traceable back to the exact source rows that produced it.

## 7. Disclaimer

100% synthetic data, fictional health plan. Independent project, not affiliated with or built using any real payer's proprietary SLA methodology or system.
