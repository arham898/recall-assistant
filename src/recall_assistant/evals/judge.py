"""LLM judge for answer correctness.

Compares a candidate answer against a reference answer and returns a
structured verdict.  The judge sees the question, the reference and the
candidate — never the retrieved cards.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from recall_assistant.llm import LLMClient

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeResult:
    """Verdict and reasoning from the LLM judge."""

    verdict: str  # "pass", "fail", or "error"
    reason: str


# ---------------------------------------------------------------------------
# Prompt registry
# ---------------------------------------------------------------------------

JUDGE_PROMPT_V1 = """\
You are an evaluation judge for a vehicle-recall question-answering system.

You will be given a question, a reference answer (the ground truth), and a
candidate answer (the system's response).

Your task: decide whether the candidate answer is acceptable.

An answer PASSES if it conveys the key facts the question asks for:
- For a question about a defect: the candidate describes the correct defect.
- For a question about a remedy or fix: the candidate describes the correct remedy.
- For a question about a consequence: the candidate describes the correct consequence.
- For a question about a count: the candidate states the correct count.
- For a question about whether a recall exists: the candidate correctly confirms or denies it.
- Paraphrasing is fine; exact wording is not required.
- Bracketed citation IDs (e.g. [24V123000]) are NOT required for a pass.
  A missing or wrong campaign ID is a fail only when the question specifically
  asks which recall(s).
- Minor extra information is acceptable as long as the core facts are correct.

An answer FAILS if:
- It omits the key facts that the question asks for.
- It states incorrect facts about the recall (wrong defect, wrong remedy, \
wrong vehicle, wrong count).
- It declines to answer when the reference answer has substantive content.
- It answers substantively when the reference answer indicates no matching \
recall exists.

Respond with exactly one JSON object, no other text:
{"verdict": "pass" or "fail", "reason": "one-sentence explanation"}\
"""

JUDGE_PROMPT_V2 = """\
You are an evaluation judge for a vehicle-recall question-answering system.

You will be given a question, a reference answer (the ground truth), and a
candidate answer (the system's response).

Your task: decide whether the candidate answer is acceptable.

An answer PASSES if it conveys the key facts the question asks for:
- For a question about a defect: the candidate describes the correct defect.
- For a question about a remedy or fix: the candidate describes the correct remedy.
- For a question about a consequence: the candidate describes the correct consequence.
- For a question about a count: the candidate states the correct count.
- For a question about whether a recall exists: the candidate correctly confirms or denies it.
- Paraphrasing is fine; exact wording is not required.
- Minor extra information is acceptable as long as the core facts are correct.

The judge cannot verify whether a campaign ID is real. When the question asks \
which recalls apply, a list of campaign IDs matching the reference is a pass. \
For every other question type, IDs are not required and a missing ID is not \
a fail.

The reference answer is the minimum correct content, not the maximum. An answer \
that contains the reference's facts and adds further true-sounding detail, or \
mentions additional recalls, passes. Extra material fails only if it contradicts \
the reference.

Judge against what the question asks for. If the question asks whether a recall \
exists, matching the recall and its defect is enough; a missing remedy or \
consequence is not a fail unless the question asked for it. If the question asks \
for a count, the number alone decides. If the question asks for all recalls, a \
missing one fails.

The exact string NO_RELEVANT_RECALL_FOUND means the assistant found no \
applicable recall. When the reference says no recall was found, that string is \
a pass and any other answer is a fail. When the reference describes a recall, \
that string is a fail.

An answer FAILS if:
- It omits the key facts that the question asks for.
- It states incorrect facts about the recall (wrong defect, wrong remedy, \
wrong vehicle, wrong count).
  An additional recall mentioned alongside the correct one is not a \
wrong-vehicle error. Wrong vehicle means the candidate's main answer describes \
a different vehicle's recall instead of the reference's.
- It declines to answer when the reference answer has substantive content.
- It answers substantively when the reference answer indicates no matching \
recall exists.

Respond with exactly one JSON object, no other text:
{"verdict": "pass" or "fail", "reason": "one-sentence explanation"}\
"""

JUDGE_PROMPTS: dict[str, str] = {"v1": JUDGE_PROMPT_V1, "v2": JUDGE_PROMPT_V2}

# ---------------------------------------------------------------------------
# Prompt hashing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


def judge_prompt_hash(prompt_text: str) -> str:
    """SHA-256 of the prompt text, first 12 hex chars."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_judge_response(text: str) -> JudgeResult:
    """Parse the judge's JSON response.

    Returns a result with verdict ``"error"`` on malformed output.
    """
    cleaned = text.strip()
    m = _FENCE_RE.match(cleaned)
    if m:
        cleaned = m.group(1).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return JudgeResult(verdict="error", reason=f"JSON parse error: {exc}")
    if not isinstance(data, dict):
        return JudgeResult(verdict="error", reason="JSON parse error: expected object")
    verdict = data.get("verdict")
    if verdict not in ("pass", "fail"):
        return JudgeResult(
            verdict="error",
            reason=f"JSON parse error: invalid verdict {verdict!r}",
        )
    reason = str(data.get("reason", ""))
    return JudgeResult(verdict=verdict, reason=reason)


# ---------------------------------------------------------------------------
# Judge function
# ---------------------------------------------------------------------------


def judge(
    question: str,
    reference_answer: str,
    candidate_answer: str,
    *,
    llm_client: LLMClient,
    model: str,
    prompt_version: str = "v1",
) -> JudgeResult:
    """Ask the LLM judge whether *candidate_answer* is acceptable.

    Raises ``ValueError`` if *prompt_version* is not in ``JUDGE_PROMPTS``.
    """
    if prompt_version not in JUDGE_PROMPTS:
        raise ValueError(
            f"Unknown prompt version {prompt_version!r}; "
            f"available: {sorted(JUDGE_PROMPTS)}"
        )
    prompt_text = JUDGE_PROMPTS[prompt_version]

    messages = [
        {"role": "system", "content": prompt_text},
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Reference answer: {reference_answer}\n\n"
                f"Candidate answer: {candidate_answer}"
            ),
        },
    ]
    result = llm_client.generate(messages, model=model, max_tokens=256)
    return _parse_judge_response(result.text)
