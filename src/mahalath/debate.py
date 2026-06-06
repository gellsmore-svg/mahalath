"""Agent debate loop for one candidate term.

The two Stage 1 agents take alternating turns per iteration:

  Iteration N:
    1. PrecisionCritic sees the running history and emits a refined
       definition + remaining concerns + confidence.
    2. SynthesisExplorer sees the critic's latest turn and emits its
       own refined definition + rationale + confidence.

Per DQ-003 the aggregate confidence for the iteration is the **minimum**
of the two agents' final scores (the more pessimistic agent governs).
Per DQ-001 the Moderator is off by default in Stage 1 — escalation lands
the term in the undecided queue instead.

`run_debate` is pure: no MongoDB, no filesystem. It builds DebateMessage
and AgentExchange records and returns them to the caller, who is
responsible for persistence. This keeps the loop testable against a
MockAdapter with no infrastructure setup.

Outcomes:
  - accepted: min confidence >= threshold at the end of some iteration
  - undecided: iteration cap reached without convergence

Future stages will add:
  - rejected (both agents recommend dropping the term)
  - split (debate concludes the term needs contradistinct variants)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from mahalath.adapters.base import Adapter, AdapterError
from mahalath.config import RuntimeConfig
from mahalath.db.models import AgentExchange, DebateMessage

PRECISION_CRITIC = "precision_critic"
SYNTHESIS_EXPLORER = "synthesis_explorer"

# Unique markers placed only in the current speaker's preamble — never in the
# history block — so tests (and any prompt-routing tooling) can identify
# whose turn it is by substring search without ambiguity.
SPEAKER_TAG_PRECISION_CRITIC = "[Speaker: precision_critic]"
SPEAKER_TAG_SYNTHESIS_EXPLORER = "[Speaker: synthesis_explorer]"


class DebateError(Exception):
    """Raised when an unrecoverable problem stops the debate loop."""


@dataclass(frozen=True)
class AgentOpinion:
    definition: str
    explanation: str  # critique (PC) or rationale (SE)
    confidence: float


@dataclass
class DebateResult:
    decision_log_id: str
    term: str
    source_document_id: str
    outcome: str  # accepted | undecided
    final_definition: str | None
    final_confidence: float | None
    iterations_used: int
    messages: list[DebateMessage] = field(default_factory=list)
    exchanges: list[AgentExchange] = field(default_factory=list)


def run_debate(
    term: str,
    context: str,
    source_document_id: str,
    adapter: Adapter,
    runtime: RuntimeConfig,
) -> DebateResult:
    """Run the PrecisionCritic/SynthesisExplorer loop on a single term."""
    decision_log_id = str(uuid4())
    messages: list[DebateMessage] = []
    exchanges: list[AgentExchange] = []

    last_definition: str | None = None
    last_min_confidence: float | None = None

    for iteration in range(1, runtime.max_iterations_per_term + 1):
        # PrecisionCritic
        pc_prompt = _build_critic_prompt(term, context, history=messages)
        pc_opinion, pc_exchange = _call_agent(
            adapter,
            role=PRECISION_CRITIC,
            iteration=iteration,
            model=runtime.model,
            prompt=pc_prompt,
            decision_log_id=decision_log_id,
        )
        exchanges.append(pc_exchange)
        messages.append(
            DebateMessage(
                iteration=iteration,
                role=PRECISION_CRITIC,
                content=_format_opinion_for_history(pc_opinion),
                confidence=pc_opinion.confidence,
                model=runtime.model,
            )
        )

        # SynthesisExplorer
        se_prompt = _build_explorer_prompt(term, context, history=messages)
        se_opinion, se_exchange = _call_agent(
            adapter,
            role=SYNTHESIS_EXPLORER,
            iteration=iteration,
            model=runtime.model,
            prompt=se_prompt,
            decision_log_id=decision_log_id,
        )
        exchanges.append(se_exchange)
        messages.append(
            DebateMessage(
                iteration=iteration,
                role=SYNTHESIS_EXPLORER,
                content=_format_opinion_for_history(se_opinion),
                confidence=se_opinion.confidence,
                model=runtime.model,
            )
        )

        # Aggregate: minimum confidence per DQ-003.
        min_confidence = min(pc_opinion.confidence, se_opinion.confidence)
        last_min_confidence = min_confidence
        # SynthesisExplorer's definition wins on accept; it has the last word.
        last_definition = se_opinion.definition

        if min_confidence >= runtime.confidence_threshold:
            return DebateResult(
                decision_log_id=decision_log_id,
                term=term,
                source_document_id=source_document_id,
                outcome="accepted",
                final_definition=last_definition,
                final_confidence=min_confidence,
                iterations_used=iteration,
                messages=messages,
                exchanges=exchanges,
            )

    # Iteration cap reached without convergence.
    return DebateResult(
        decision_log_id=decision_log_id,
        term=term,
        source_document_id=source_document_id,
        outcome="undecided",
        final_definition=last_definition,
        final_confidence=last_min_confidence,
        iterations_used=runtime.max_iterations_per_term,
        messages=messages,
        exchanges=exchanges,
    )


# --- Prompt builders ---------------------------------------------------------

_CRITIC_PREAMBLE = (
    f"{SPEAKER_TAG_PRECISION_CRITIC}\n"
    "You are the PrecisionCritic in a glossary debate. Your role is to "
    "demand definitional sharpness: precise wording, clear distinction "
    "from neighboring concepts, no hidden ambiguity. Be conservative "
    "with confidence; only score 8.0 or higher if you would defend the "
    "definition against a domain expert."
)

_EXPLORER_PREAMBLE = (
    f"{SPEAKER_TAG_SYNTHESIS_EXPLORER}\n"
    "You are the SynthesisExplorer in a glossary debate. Your role is to "
    "integrate the PrecisionCritic's concerns, refine the definition, "
    "and where useful broaden its framing (related concepts, scope, "
    "applications). Aim for a definition that is both precise AND "
    "captures the meaningful breadth of the concept."
)

_OUTPUT_CONTRACT_CRITIC = (
    'Output ONLY a JSON object of the form '
    '{"definition": "<one-sentence definition>", '
    '"critique": "<remaining weaknesses or open questions>", '
    '"confidence": <number 0.0-10.0>}. '
    "No preamble, no Markdown, no commentary outside the JSON."
)

_OUTPUT_CONTRACT_EXPLORER = (
    'Output ONLY a JSON object of the form '
    '{"definition": "<refined one-sentence definition>", '
    '"rationale": "<how you addressed the critic\'s points>", '
    '"confidence": <number 0.0-10.0>}. '
    "No preamble, no Markdown, no commentary outside the JSON."
)


def _build_critic_prompt(
    term: str, context: str, history: list[DebateMessage]
) -> str:
    parts = [
        _CRITIC_PREAMBLE,
        "",
        f'Candidate term: "{term}"',
        "",
        "Context from the source document:",
        context.strip() or "(no context provided)",
    ]
    if history:
        parts.extend(["", "Debate so far:", _format_history(history)])
    parts.extend(["", _OUTPUT_CONTRACT_CRITIC])
    return "\n".join(parts)


def _build_explorer_prompt(
    term: str, context: str, history: list[DebateMessage]
) -> str:
    parts = [
        _EXPLORER_PREAMBLE,
        "",
        f'Candidate term: "{term}"',
        "",
        "Context from the source document:",
        context.strip() or "(no context provided)",
        "",
        "Debate so far:",
        _format_history(history),
        "",
        _OUTPUT_CONTRACT_EXPLORER,
    ]
    return "\n".join(parts)


def _format_history(messages: list[DebateMessage]) -> str:
    """Render the debate transcript for inclusion in an agent prompt."""
    if not messages:
        return "(no prior turns)"
    lines: list[str] = []
    current_iter = None
    for m in messages:
        if m.iteration != current_iter:
            current_iter = m.iteration
            lines.append(f"Iteration {m.iteration}:")
        conf = f" (confidence: {m.confidence:.1f})" if m.confidence is not None else ""
        lines.append(f"  {m.role}{conf}:")
        lines.append(f"    {m.content}")
    return "\n".join(lines)


def _format_opinion_for_history(opinion: AgentOpinion) -> str:
    return f"definition={opinion.definition!r}; note={opinion.explanation!r}"


# --- Adapter call + opinion parsing -----------------------------------------


def _call_agent(
    adapter: Adapter,
    *,
    role: str,
    iteration: int,
    model: str,
    prompt: str,
    decision_log_id: str,
) -> tuple[AgentOpinion, AgentExchange]:
    try:
        response = adapter.generate(prompt, want_json=True, model=model)
    except AdapterError as exc:
        raise DebateError(
            f"Adapter failed during {role} iteration {iteration}: {exc}"
        ) from exc

    opinion = parse_opinion(response.text)
    exchange = AgentExchange(
        decision_log_id=decision_log_id,
        iteration=iteration,
        role=role,
        model=response.model,
        prompt=prompt,
        response=response.text,
        confidence=opinion.confidence,
        duration_ms=response.duration_ms,
    )
    return opinion, exchange


def parse_opinion(response_text: str) -> AgentOpinion:
    """Parse an agent JSON response into AgentOpinion.

    Tolerates the same prose-preamble failure mode as
    `mahalath.extraction.parse_candidates`: if the model emits
    "Here's the JSON: {...}" we still recover the JSON object.
    """
    payload = _extract_json_object(response_text)
    definition = str(payload.get("definition", "")).strip()
    explanation = str(
        payload.get("critique") or payload.get("rationale") or ""
    ).strip()
    confidence = _coerce_confidence(payload.get("confidence"))
    return AgentOpinion(
        definition=definition,
        explanation=explanation,
        confidence=confidence,
    )


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence != confidence:  # NaN check
        return 0.0
    return max(0.0, min(10.0, confidence))


def _extract_json_object(text: str) -> dict:
    """Find and parse the first balanced JSON object in text.

    Duplicated from extraction.py rather than imported to keep the
    debate module self-contained; the two callers want slightly
    different error wrapping.
    """
    text = text.strip()
    if not text:
        raise DebateError("Adapter returned empty response.")

    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise DebateError(f"No JSON object found in response: {text[:200]!r}")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise DebateError(
                        f"JSON-like object found but parse failed: {exc}"
                    ) from exc
    raise DebateError(f"Unbalanced braces in response: {text[:200]!r}")
