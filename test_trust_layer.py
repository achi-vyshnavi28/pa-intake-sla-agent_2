"""Trust-layer / guardrail tests, including the adversarial hallucinating-
LLM test double that proves the evidence-grounding verifier actually
rejects an invented number rather than trusting it."""
import os

import pytest

from agent.trust_layer import (
    DeterministicTemplateLLM, HallucinatingLLM, assess_confidence, cross_check,
    gate_for_escalation, get_llm, render_verified_or_fallback, verify_grounding,
)


# --------------------------- cross-check --------------------------- #

def test_cross_check_agrees_within_tolerance():
    result = cross_check("breach_rate", 0.25, 0.25)
    assert result.agrees is True


def test_cross_check_disagrees_beyond_tolerance():
    result = cross_check("breach_rate", 0.25, 0.40)
    assert result.agrees is False
    assert result.delta == pytest.approx(0.15)


# --------------------------- confidence --------------------------- #

def test_confidence_high_with_ample_sample_and_agreement():
    c = assess_confidence(sample_size=50, cross_check_agrees=True, evidence_complete=True)
    assert c.level == "HIGH"


def test_confidence_low_with_small_sample():
    c = assess_confidence(sample_size=2, cross_check_agrees=True, evidence_complete=True)
    assert c.level == "LOW"


def test_confidence_insufficient_evidence_on_cross_check_disagreement():
    c = assess_confidence(sample_size=50, cross_check_agrees=False, evidence_complete=True)
    assert c.level == "INSUFFICIENT_EVIDENCE"


def test_confidence_insufficient_evidence_on_incomplete_evidence():
    c = assess_confidence(sample_size=50, cross_check_agrees=True, evidence_complete=False)
    assert c.level == "INSUFFICIENT_EVIDENCE"


# --------------------------- escalation gate --------------------------- #

@pytest.mark.parametrize("level,expected", [
    ("HIGH", True), ("MEDIUM", True), ("LOW", False), ("INSUFFICIENT_EVIDENCE", False),
])
def test_gate_for_escalation(level, expected):
    assert gate_for_escalation(level) is expected


# --------------------------- evidence-grounding verifier --------------------------- #

def test_verify_grounding_passes_when_numbers_match_evidence():
    evidence = {"breach_rate_pct": 42.0, "n_requests": 100}
    draft = "Provider X breached SLA on 42.0% of 100 requests this quarter."
    result = verify_grounding(draft, evidence)
    assert result.verified is True
    assert result.unverified_numbers == []


def test_verify_grounding_fails_on_invented_number():
    evidence = {"breach_rate_pct": 42.0, "n_requests": 137}
    draft = "Provider X breached SLA on 91.5% of requests -- a critical failure."
    result = verify_grounding(draft, evidence)
    assert result.verified is False
    assert "91.5%" in result.unverified_numbers


def test_hallucinating_llm_invented_number_never_reaches_output():
    """Adversarial test: HallucinatingLLM ALWAYS appends a plausible-but-
    false number (173.4%) that does not appear anywhere in the evidence
    bundle. This test proves the trust layer catches it -- the fake number
    must never survive into the final rendered text."""
    llm = HallucinatingLLM()
    evidence = {"breach_rate_pct": 30.0, "n_requests": 50, "provider_id": "PRV-006"}
    prompt = "Provider {provider_id} breached SLA on {breach_rate_pct}% of {n_requests} requests."
    fallback = prompt.format(**evidence)

    draft = llm.draft(prompt, evidence)
    assert "173.4" in draft  # sanity check: the test double actually hallucinated

    final_text, was_llm_used = render_verified_or_fallback(draft, evidence, fallback)

    assert "173.4" not in final_text, "hallucinated number leaked into the final output"
    assert was_llm_used is False
    assert final_text == fallback


def test_deterministic_llm_always_passes_grounding():
    """The default, zero-cost LLM renders straight from evidence, so it
    must always pass its own verification."""
    llm = DeterministicTemplateLLM()
    evidence = {"breach_rate_pct": 12.5, "n_requests": 80, "provider_id": "PRV-001"}
    prompt = "Provider {provider_id}: {breach_rate_pct}% breach rate across {n_requests} requests."
    draft = llm.draft(prompt, evidence)
    result = verify_grounding(draft, evidence)
    assert result.verified is True


# --------------------------- pluggable LLM selection --------------------------- #

def test_get_llm_returns_deterministic_by_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = get_llm()
    assert isinstance(llm, DeterministicTemplateLLM)
