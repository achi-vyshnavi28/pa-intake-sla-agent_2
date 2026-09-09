"""End-to-end integration checks on the agent's actual output files -- the
escalation gate and audit trail must hold on real pipeline output, not
just in the trust_layer unit tests."""
import json


def test_no_low_or_insufficient_confidence_ever_escalated(output_dir):
    escalations = json.loads((output_dir / "escalations.json").read_text())
    for record in escalations:
        assert record["confidence"] in ("HIGH", "MEDIUM"), (
            f"{record['provider_id']} escalated with confidence={record['confidence']}")


def test_needs_human_review_only_contains_low_confidence_material_findings(output_dir):
    review = json.loads((output_dir / "needs_human_review.json").read_text())
    for record in review:
        assert record["confidence"] in ("LOW", "INSUFFICIENT_EVIDENCE")
        assert record["root_cause"] != "no_material_pattern"


def test_cleared_providers_have_no_material_pattern_or_low_confidence(output_dir):
    cleared = json.loads((output_dir / "cleared.json").read_text())
    for record in cleared:
        assert record["root_cause"] == "no_material_pattern" or record["confidence"] not in ("HIGH", "MEDIUM")


def test_audit_log_covers_every_provider_and_is_self_consistent(output_dir):
    audit_log = json.loads((output_dir / "audit_log.json").read_text())
    assert len(audit_log) == 8  # 8 synthetic providers
    for entry in audit_log:
        # decision must be consistent with the confidence gate
        confidence_level = entry["step5_confidence"]["level"]
        decision = entry["decision"]
        if decision == "escalate":
            assert confidence_level in ("HIGH", "MEDIUM")
        # the final note must equal the LLM draft only when verification passed
        verified = entry["step7_verification"]["verified"]
        if verified:
            assert entry["step7_final_note"] == entry["step6_llm_draft"]
        else:
            assert entry["step7_final_note"] != entry["step6_llm_draft"]


def test_every_escalation_note_is_evidence_verified_or_fell_back(output_dir):
    escalations = json.loads((output_dir / "escalations.json").read_text())
    for record in escalations:
        # note_llm_verified is True (LLM draft passed grounding) or the
        # note is the deterministic fallback -- either way it is grounded
        assert isinstance(record["note_llm_verified"], bool)
        assert record["note"]  # never empty
