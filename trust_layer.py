"""
Verification & Trust Layer -- pa-intake-sla-agent.

This module is what turns "an LLM that writes escalation notes" into
something a stakeholder can actually rely on. It is used identically by
the agent for every finding it produces:

1. Cross-check layer: every key metric (here: SLA breach rate) is computed
   TWO independent ways -- once via a SQL query, once via an independent
   Pandas recomputation -- and must agree within a tolerance before being
   trusted. Disagreement is never silently resolved; it downgrades
   confidence and is logged.
2. Confidence layer: every finding carries an explicit confidence level --
   HIGH / MEDIUM / LOW / INSUFFICIENT_EVIDENCE -- derived from (a) sample
   size vs. a documented minimum, (b) cross-check agreement, (c) evidence
   completeness. Never a bare pass/fail.
3. Evidence-grounding verifier: every number in an LLM-drafted sentence is
   checked against the evidence bundle before release. Unverified claims
   are rejected and replaced with a deterministic, evidence-tied template.
4. Escalation gate: LOW / INSUFFICIENT_EVIDENCE findings never auto-
   escalate to a stakeholder -- they route to a "needs human review"
   bucket instead of forcing a confident-sounding conclusion.
5. Pluggable LLM: get_llm() returns a real API-backed LLM only if an API
   key env var is set; otherwise a deterministic, evidence-grounded
   template renderer. The whole pipeline runs reproducibly with zero
   external dependency/cost by default.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from stats_utils import MIN_SAMPLE_SIZE

CONFIDENCE_LEVELS = ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_EVIDENCE")
CROSS_CHECK_TOLERANCE = 1e-6  # relative tolerance for two independently-computed metrics to "agree"


# --------------------------------------------------------------------------- #
# 1. Cross-check layer
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CrossCheckResult:
    metric_name: str
    sql_value: float
    pandas_value: float
    agrees: bool
    delta: float


def cross_check(metric_name: str, sql_value: float, pandas_value: float,
                 tolerance: float = CROSS_CHECK_TOLERANCE) -> CrossCheckResult:
    """Compare a metric computed via SQL against the same metric computed
    independently via Pandas. Disagreement is never silently averaged away
    -- it is returned as-is and the caller must factor it into confidence."""
    delta = abs(sql_value - pandas_value)
    scale = max(abs(sql_value), abs(pandas_value), 1e-9)
    agrees = (delta / scale) <= tolerance
    return CrossCheckResult(metric_name, sql_value, pandas_value, agrees, delta)


# --------------------------------------------------------------------------- #
# 2. Confidence layer
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ConfidenceAssessment:
    level: str
    sample_size: int
    cross_check_agrees: bool
    evidence_complete: bool
    rationale: str


def assess_confidence(sample_size: int, cross_check_agrees: bool, evidence_complete: bool,
                       min_sample_size: int = MIN_SAMPLE_SIZE) -> ConfidenceAssessment:
    """Derive an explicit confidence level from three documented inputs.
    Rules (in priority order, most-restrictive wins):
      - any missing evidence, or a cross-check disagreement -> INSUFFICIENT_EVIDENCE
      - sample_size < min_sample_size -> LOW
      - sample_size < 2 * min_sample_size -> MEDIUM
      - otherwise -> HIGH
    """
    if not evidence_complete:
        return ConfidenceAssessment("INSUFFICIENT_EVIDENCE", sample_size, cross_check_agrees,
                                     evidence_complete, "evidence bundle is incomplete")
    if not cross_check_agrees:
        return ConfidenceAssessment("INSUFFICIENT_EVIDENCE", sample_size, cross_check_agrees,
                                     evidence_complete, "SQL and Pandas cross-check disagree")
    if sample_size < min_sample_size:
        return ConfidenceAssessment("LOW", sample_size, cross_check_agrees, evidence_complete,
                                     f"sample_size={sample_size} < minimum {min_sample_size}")
    if sample_size < 2 * min_sample_size:
        return ConfidenceAssessment("MEDIUM", sample_size, cross_check_agrees, evidence_complete,
                                     f"sample_size={sample_size} meets minimum but is not ample")
    return ConfidenceAssessment("HIGH", sample_size, cross_check_agrees, evidence_complete,
                                 f"sample_size={sample_size}, cross-check agrees, evidence complete")


# --------------------------------------------------------------------------- #
# 3. Evidence-grounding verifier for LLM drafts
# --------------------------------------------------------------------------- #

NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])-?\d+(?:\.\d+)?%?")


def _flatten_evidence_numbers(evidence: dict) -> set[str]:
    """Extract every numeric token that appears anywhere in the evidence
    dict (as a plain number and, for floats, rounded to 1 decimal) so the
    verifier can compare against both '42' and '42.0'-style renderings."""
    numbers: set[str] = set()

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            numbers.add(str(v))
            if isinstance(v, float):
                numbers.add(f"{v:.1f}")
                numbers.add(f"{v:.0f}")
                numbers.add(str(round(v)))
            numbers.add(f"{v}%")
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            for m in NUMBER_PATTERN.findall(v):
                numbers.add(m)

    walk(evidence)
    return numbers


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    unverified_numbers: list[str]
    checked_numbers: list[str]


_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")


def verify_grounding(draft_text: str, evidence: dict, tolerance: float = 0.02) -> VerificationResult:
    """Regex-extract every number in `draft_text` and confirm each one
    appears (within `tolerance` relative tolerance for plain numbers) among
    the numbers present anywhere in `evidence`. Any number that cannot be
    grounded fails verification -- this is what catches an LLM inventing a
    plausible-but-false figure.

    Thousands-separator commas (e.g. "$24,000.00") are stripped before
    extraction -- otherwise a legitimately evidence-grounded number like
    24000.0 rendered as "24,000.00" would regex-split into "24" and
    "000.00" and spuriously fail verification. This normalization only
    strips a comma directly between digits followed by exactly 3 more
    digits (a thousands group), so it does not merge a genuine
    comma-separated list of numbers."""
    evidence_numbers_raw = _flatten_evidence_numbers(evidence)
    evidence_floats = []
    for n in evidence_numbers_raw:
        try:
            evidence_floats.append(float(n.rstrip("%")))
        except ValueError:
            pass

    normalized_text = _THOUSANDS_COMMA.sub("", draft_text)
    draft_numbers = NUMBER_PATTERN.findall(normalized_text)
    unverified = []
    for tok in draft_numbers:
        bare = tok.rstrip("%")
        if tok in evidence_numbers_raw or bare in evidence_numbers_raw:
            continue
        try:
            val = float(bare)
        except ValueError:
            unverified.append(tok)
            continue
        grounded = any(abs(val - ev) <= max(tolerance * max(abs(ev), 1.0), 0.05) for ev in evidence_floats)
        if not grounded:
            unverified.append(tok)

    return VerificationResult(verified=(len(unverified) == 0), unverified_numbers=unverified,
                               checked_numbers=draft_numbers)


def render_verified_or_fallback(draft_text: str, evidence: dict, fallback_template: str) -> tuple[str, bool]:
    """Verify a draft against evidence; if it fails, discard it entirely
    and render the deterministic fallback template instead (which is
    built directly from evidence, so it is grounded by construction).
    Returns (final_text, was_llm_text_used)."""
    result = verify_grounding(draft_text, evidence)
    if result.verified:
        return draft_text, True
    return fallback_template, False


# --------------------------------------------------------------------------- #
# 4. Escalation / inclusion gate
# --------------------------------------------------------------------------- #

def gate_for_escalation(confidence_level: str) -> bool:
    """LOW or INSUFFICIENT_EVIDENCE findings never auto-escalate to a
    stakeholder -- they route to a 'needs human review' bucket instead."""
    return confidence_level in ("HIGH", "MEDIUM")


# --------------------------------------------------------------------------- #
# 5. Pluggable LLM, deterministic by default
# --------------------------------------------------------------------------- #

class LLM(Protocol):
    def draft(self, prompt: str, evidence: dict) -> str: ...


@dataclass
class DeterministicTemplateLLM:
    """Zero-dependency, zero-cost 'LLM': renders a fixed, evidence-tied
    template. Deterministic and fully reproducible -- the default so the
    whole pipeline runs with no external API key required."""

    def draft(self, prompt: str, evidence: dict) -> str:
        return prompt.format(**evidence)


@dataclass
class HallucinatingLLM:
    """Adversarial test double: ALWAYS invents a plausible-but-false
    number that does not appear anywhere in the evidence bundle. Used only
    by tests/test_trust_layer.py to prove verify_grounding() actually
    rejects it -- never used in the real pipeline."""
    fake_value: str = "173.4%"

    def draft(self, prompt: str, evidence: dict) -> str:
        base = prompt.format(**evidence)
        return base + f" (independently confirmed at {self.fake_value} elevated risk)"


def get_llm() -> LLM:
    """Returns a real API-backed LLM only if ANTHROPIC_API_KEY (or
    OPENAI_API_KEY) is set in the environment; otherwise returns the
    deterministic template renderer. This lets the whole pipeline run
    reproducibly with zero external dependency/cost by default, while
    proving the guardrail architecture works independent of whether a
    live LLM is attached."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        try:
            return _RealLLMAdapter()
        except Exception:
            return DeterministicTemplateLLM()
    return DeterministicTemplateLLM()


class _RealLLMAdapter:
    """Thin adapter kept separate so importing this module never requires
    an LLM SDK to be installed. Only instantiated when an API key is
    present. Not exercised by the default (offline, deterministic) test
    and pipeline runs."""

    def draft(self, prompt: str, evidence: dict) -> str:  # pragma: no cover
        raise NotImplementedError(
            "Real LLM backend not wired up in this portfolio project -- "
            "set no API key to use the deterministic renderer instead."
        )
