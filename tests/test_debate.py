"""Debate loop tests with a MockAdapter.

These exercise the orchestration: turn order, confidence aggregation,
accept-on-threshold, undecided-on-cap. The live Ollama smoke is run
separately as part of the Stage 1.7 end-to-end harness.
"""

from __future__ import annotations

import json

import pytest

from mahalath.adapters import MockAdapter
from mahalath.config import RuntimeConfig
from mahalath.debate import (
    DebateError,
    PRECISION_CRITIC,
    SPEAKER_TAG_PRECISION_CRITIC,
    SPEAKER_TAG_SYNTHESIS_EXPLORER,
    SYNTHESIS_EXPLORER,
    parse_opinion,
    run_debate,
)


def _runtime(*, max_iterations: int = 5, threshold: float = 8.0) -> RuntimeConfig:
    return RuntimeConfig(
        max_iterations_per_term=max_iterations,
        confidence_threshold=threshold,
    )


def _json(definition: str, confidence: float, *, key: str = "critique") -> str:
    return json.dumps(
        {"definition": definition, key: "ok", "confidence": confidence}
    )


def test_parse_opinion_happy_path() -> None:
    raw = '{"definition": "An agonist activates a receptor.", "critique": "needs sharper boundary", "confidence": 7.5}'
    opinion = parse_opinion(raw)
    assert opinion.definition == "An agonist activates a receptor."
    assert opinion.explanation == "needs sharper boundary"
    assert opinion.confidence == 7.5


def test_parse_opinion_accepts_rationale_field() -> None:
    raw = '{"definition": "X", "rationale": "broadened", "confidence": 8.2}'
    opinion = parse_opinion(raw)
    assert opinion.explanation == "broadened"


def test_parse_opinion_clamps_confidence() -> None:
    raw = '{"definition": "X", "critique": "", "confidence": 12.5}'
    assert parse_opinion(raw).confidence == 10.0
    raw2 = '{"definition": "X", "critique": "", "confidence": -3.0}'
    assert parse_opinion(raw2).confidence == 0.0


def test_parse_opinion_coerces_non_numeric_confidence_to_zero() -> None:
    raw = '{"definition": "X", "critique": "", "confidence": "not-a-number"}'
    assert parse_opinion(raw).confidence == 0.0


def test_parse_opinion_tolerates_preamble() -> None:
    raw = (
        'Here is the JSON output:\n'
        '{"definition": "An agonist.", "critique": "ok", "confidence": 9.0}'
    )
    opinion = parse_opinion(raw)
    assert opinion.confidence == 9.0


def test_parse_opinion_raises_on_no_json() -> None:
    with pytest.raises(DebateError):
        parse_opinion("just prose, no JSON")


def test_debate_accepts_when_both_agents_clear_threshold() -> None:
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json("Sharp definition.", 9.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json("Refined definition.", 8.5, key="rationale"),
        }
    )
    result = run_debate(
        term="agonist",
        context="An agonist activates a receptor.",
        source_document_id="doc-1",
        adapter=adapter,
        runtime=_runtime(),
    )
    assert result.outcome == "accepted"
    assert result.iterations_used == 1
    assert result.final_definition == "Refined definition."
    assert result.final_confidence == 8.5  # min(9.0, 8.5)
    # Two exchanges per iteration (one per agent), exactly one iteration ran.
    assert len(result.exchanges) == 2
    assert len(result.messages) == 2
    assert result.exchanges[0].role == PRECISION_CRITIC
    assert result.exchanges[1].role == SYNTHESIS_EXPLORER


def test_debate_undecided_when_below_threshold_after_cap() -> None:
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json("Definition.", 5.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json("Definition.", 6.0, key="rationale"),
        }
    )
    runtime = _runtime(max_iterations=3, threshold=8.0)
    result = run_debate(
        term="murky",
        context="murky context",
        source_document_id="doc-2",
        adapter=adapter,
        runtime=runtime,
    )
    assert result.outcome == "undecided"
    assert result.iterations_used == 3
    assert result.final_confidence == 5.0  # min(5.0, 6.0) on the last iteration
    assert len(result.exchanges) == 6  # 3 iterations * 2 agents
    assert len(result.messages) == 6


def test_debate_min_confidence_governs_aggregate() -> None:
    """Even if one agent is very confident, the other's low score blocks accept."""
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json("D.", 9.5),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json("D.", 7.5, key="rationale"),
        }
    )
    result = run_debate(
        term="t",
        context="c",
        source_document_id="doc-3",
        adapter=adapter,
        runtime=_runtime(max_iterations=2, threshold=8.0),
    )
    # 7.5 is below threshold, so iteration 1 doesn't accept.
    # Subsequent iterations return the same responses (mock is stateless), so
    # the debate completes its cap and returns undecided.
    assert result.outcome == "undecided"
    assert result.iterations_used == 2


def test_debate_messages_record_iteration_and_role() -> None:
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json("d.", 9.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json("d.", 9.0, key="rationale"),
        }
    )
    result = run_debate(
        term="t", context="c", source_document_id="d",
        adapter=adapter, runtime=_runtime(),
    )
    assert [m.iteration for m in result.messages] == [1, 1]
    assert [m.role for m in result.messages] == [PRECISION_CRITIC, SYNTHESIS_EXPLORER]


def test_debate_exchanges_share_decision_log_id() -> None:
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json("d.", 9.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json("d.", 9.0, key="rationale"),
        }
    )
    result = run_debate(
        term="t", context="c", source_document_id="d",
        adapter=adapter, runtime=_runtime(),
    )
    ids = {ex.decision_log_id for ex in result.exchanges}
    assert ids == {result.decision_log_id}


def test_debate_each_iteration_sees_prior_history() -> None:
    """The second iteration's prompts should embed iteration 1's exchanges."""
    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: _json("d.", 6.0),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: _json("d.", 6.0, key="rationale"),
        }
    )
    run_debate(
        term="t", context="c", source_document_id="d",
        adapter=adapter, runtime=_runtime(max_iterations=2),
    )
    # 4 adapter calls total: PC1, SE1, PC2, SE2
    assert len(adapter.calls) == 4
    pc2_prompt = adapter.calls[2]["prompt"]
    assert "Iteration 1:" in pc2_prompt
    assert "precision_critic" in pc2_prompt
    assert "synthesis_explorer" in pc2_prompt


# --- per-role models (DQ-010, S2.46) ------------------------------------------


def test_debate_uses_per_role_models() -> None:
    import json
    from mahalath.adapters import MockAdapter
    from mahalath.config import AgentRoleConfig, AgentRolesConfig, RuntimeConfig
    from mahalath.debate import (
        SPEAKER_TAG_PRECISION_CRITIC,
        SPEAKER_TAG_SYNTHESIS_EXPLORER,
        run_debate,
    )

    adapter = MockAdapter(
        responses={
            SPEAKER_TAG_PRECISION_CRITIC: json.dumps(
                {"definition": "An agonist activates a receptor.",
                 "critique": "ok", "confidence": 9.0}),
            SPEAKER_TAG_SYNTHESIS_EXPLORER: json.dumps(
                {"definition": "An agonist activates a receptor.",
                 "rationale": "ok", "confidence": 8.5}),
        },
    )
    runtime = RuntimeConfig(
        model="family-b",
        agents=AgentRolesConfig(
            precision_critic=AgentRoleConfig(model="family-a"),
        ),
    )
    result = run_debate(
        term="agonist", context="activates a receptor",
        source_document_id="doc-1", adapter=adapter, runtime=runtime,
    )
    assert result.outcome == "accepted"
    models = [c["model"] for c in adapter.calls]
    assert models[0] == "family-a"   # PrecisionCritic on its own family
    assert models[1] == "family-b"   # SynthesisExplorer on the default
    # The transcript records who said what on which model.
    assert result.messages[0].model == "family-a"
    assert result.messages[1].model == "family-b"
