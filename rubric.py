"""Rubric compiler + discussion (Sonnet), per DESIGN.md §4.

Admission rule: a criterion is admissible iff the verifier could quote a
specific passage of the deliverable to justify failing it (falsifiability).
revise() TRANSLATES vague requests into checkable criteria instead of
refusing; hard refusal only when translation is impossible.

Every function retries the LLM call once; a second failure raises
LLMUnavailable (callers map it to 503). MOCK_LLM=1 short-circuits to
canned, deterministic outputs.
"""

from __future__ import annotations

import anthropic

import config
from models import CompileResult, FalsifiabilityResult, ReviseResult, RubricItem


class LLMUnavailable(Exception):
    pass


_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


FALSIFIABILITY_RULE = """\
Admission rule for every criterion you output: a verifier reading ONLY the
deliverable text must be able to quote a specific passage to justify a FAIL.
Subjective intent is fine when operationalized into a checkable test
("formal tone: no slang, no contractions" is admissible; "well-written" as
stated is not). Translate vague quality goals into concrete, quotable
proxies (grammar errors, sentence length limits, required structure,
citation counts, word-count ranges). Prefer ranges over exact numbers.
Each criterion needs a `checkable_test`: the literal instruction the
verifier will execute (e.g. "Count words of the body; pass iff 180<=n<=220")."""


async def _parse(system: str, user: str, schema):
    client = _get_client()
    last_error: Exception | None = None
    for _ in range(2):  # retry once
        try:
            response = await client.messages.parse(
                model=config.RUBRIC_MODEL,
                max_tokens=2000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
            if response.parsed_output is None:
                raise ValueError("no parsed output")
            return response.parsed_output
        except Exception as error:  # noqa: BLE001 — anything twice = unavailable
            last_error = error
    raise LLMUnavailable(str(last_error))


# ---- Mock branch (MOCK_LLM=1): deterministic, keyless ----

_MOCK_RUBRIC = [
    RubricItem(
        criterion="Word count 180-220",
        checkable_test="Count words of the deliverable body; pass iff 180 <= n <= 220",
    ),
    RubricItem(
        criterion="Cites at least 2 sources",
        checkable_test="Count distinct cited sources (URLs or named publications); pass iff >= 2",
    ),
    RubricItem(
        criterion="Contains a one-sentence summary as the first line",
        checkable_test="Check the first line is a single sentence summarizing the brief; pass iff present",
    ),
    RubricItem(
        criterion="No sentence exceeds 30 words",
        checkable_test="Measure every sentence length; pass iff all sentences <= 30 words",
    ),
]


def _mock_skills(spec: str) -> list[str]:
    lowered = spec.lower()
    skills = [s for s in config.ALLOWED_SKILLS if s.rstrip("ion") in lowered or s in lowered]
    return skills or ["writing"]


async def compile(spec: str) -> CompileResult:
    """One Sonnet call: spec -> required skills (enum) + 4-6 falsifiable criteria."""
    if config.MOCK_LLM:
        return CompileResult(required_skills=_mock_skills(spec), rubric=list(_MOCK_RUBRIC))
    system = f"""You are the rubric compiler for a verified-work marketplace.
Given a task spec, produce:
1. required_skills: the minimal set of skills needed, chosen ONLY from
   "research", "writing", "extraction".
2. rubric: 4-6 checkable acceptance criteria.

{FALSIFIABILITY_RULE}

The spec may be vague; do NOT reject it — produce the most checkable
best-effort rubric you can. The buyer refines it in discussion afterwards."""
    return await _parse(system, f"Task spec:\n{spec}", CompileResult)


async def revise(
    current_rubric: list[RubricItem],
    thread: list[dict[str, str]],
    message: str,
) -> ReviseResult:
    """One discussion round: returns the full revised rubric + what changed and why."""
    if config.MOCK_LLM:
        return ReviseResult(
            rubric=list(current_rubric),
            changes=f"(mock) Considered: {message!r}. Rubric unchanged.",
        )
    system = f"""You are the rubric guardian in a verified-work marketplace.
The buyer discusses the rubric with you before it freezes into escrow.
Apply their request and return the FULL revised rubric plus a `changes`
summary of what changed and why.

{FALSIFIABILITY_RULE}

If the buyer asks for a vague/unfalsifiable criterion ("make it
well-written"), TRANSLATE it into checkable proxies and explain the
translation in `changes` — do not just refuse. Only if translation is
impossible, return the rubric unchanged and explain why in `changes`."""
    thread_text = "\n".join(f"[{m['role']}] {m['content']}" for m in thread)
    rubric_text = "\n".join(
        f"- {r.criterion} | test: {r.checkable_test}" for r in current_rubric
    )
    user = (
        f"Current rubric:\n{rubric_text}\n\n"
        f"Discussion so far:\n{thread_text or '(none)'}\n\n"
        f"Buyer's new message:\n{message}"
    )
    return await _parse(system, user, ReviseResult)


async def check_falsifiability(rubric: list[RubricItem]) -> FalsifiabilityResult:
    """Guard for confirm-with-edits: admit only falsifiable rubrics."""
    if config.MOCK_LLM:
        vague = [r for r in rubric if not r.checkable_test.strip()]
        return FalsifiabilityResult(
            admissible=not vague,
            detail="(mock) empty checkable_test" if vague else "(mock) ok",
            suggestion=list(rubric),
        )
    system = f"""You audit a rubric a buyer wants to freeze into escrow.

{FALSIFIABILITY_RULE}

Return admissible=true iff EVERY criterion passes the admission rule.
If any fails, set admissible=false, explain which and why in `detail`,
and provide `suggestion`: the same rubric with the failing criteria
translated into admissible form. If admissible, `suggestion` echoes the
input rubric."""
    rubric_text = "\n".join(
        f"- {r.criterion} | test: {r.checkable_test}" for r in rubric
    )
    return await _parse(system, f"Rubric to audit:\n{rubric_text}", FalsifiabilityResult)
