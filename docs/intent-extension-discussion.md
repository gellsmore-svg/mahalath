# MahaLath – Semantic Intent and Context Extension Discussion Summary

**Status:** operator-supplied design discussion, filed verbatim
**Date captured:** 2026-06-11
**Source:** operator-supplied (conceptual guidance, not an implementation spec)
**Evaluation:** see `docs/intent-extension-evaluation.md`

This document captures the operator's discussion summary verbatim,
following the same convention as `docs/requirements-v0.1.md`. Claude's
evaluation against the existing architecture lives in the companion
evaluation doc.

---

## Purpose

This document summarises a design discussion exploring whether MahaLath should extend beyond lexical and contextual meaning into intent-aware semantic modelling.

The purpose is not to prescribe implementation but to provide conceptual guidance for evaluating product changes.

---

# Current Understanding of MahaLath

MahaLath currently:

* Ingests human language and contextual information.
* Uses orchestrated LLM discourse to refine concepts.
* Creates semantic definitions and relationships.
* Builds a lexicon for retrieval and reasoning.
* Distinguishes meaning through context.

Core principle:

> Meaning does not exist in isolation; meaning exists within context.

---

# New Observation – Meaning May Include Intended Outcome

The discussion introduced the possibility that meaning is not only composed of expression and context, but also of inferred intent.

Speech act concepts were used as a reference model.

## Conceptual Chain

### Locution

What was expressed.

Examples:

* Words
* Phrases
* Symbols
* Gestures

Question:

> What was said?

---

### Illocution

The intended outcome of the expression.

Examples:

* Persuade
* Reassure
* Challenge
* Maintain trust
* Prompt reflection
* Create obligation
* Preserve harmony

Question:

> What appears to have been intended?

---

### Interpretation

The meaning reconstructed by the recipient.

Question:

> What meaning is constructed?

---

### Perlocution

The actual outcome.

Question:

> What effect occurred?

---

## Product Scope Decision

Current recommendation:

MahaLath should primarily operate at:

* Locution
* Illocution

Interpretation and Perlocution remain future concerns.

Reason:

Interpretation and actual outcomes require observation, longitudinal feedback, and behavioural modelling.

Locution and Illocution are inferable from source material.

---

# Revised View of Context

Original assumption:

Context supports meaning.

Revised proposal:

> Context is the condition under which locution acquires illocution.

Implication:

The same expression may represent different semantic entities depending on context.

Example:

Expression:

```text
Fine.
```

Context A:

Agreement

Context B:

Suppressed disagreement

Context C:

Withdrawal

Context D:

Reassurance

Result:

Same locution.

Different semantic meaning.

---

# Intentionality as a First-Class Signal

Proposal:

Introduce inferred intentionality.

Intentionality is:

> Estimated degree to which an expression appears deliberately constructed to produce an outcome.

Examples:

Low:

* Impulsive message
* Fragmented speech
* Casual chat

Medium:

* Generated content
* Brainstorming

High:

* Published work
* Legal documents
* Structured essays
* Carefully argued positions

Intentionality is:

NOT:

Objective truth

IS:

An inferred semantic property.

---

## Suggested Model

```yaml
semantic_instance:

  locution:
    raw_expression

  context:
    semantic_environment

  illocution:
    inferred_desired_outcomes

  intentionality:
    score

  confidence:
    confidence_that_intent_attribution_is_correct
```

Example:

```yaml
semantic_instance:

  locution:
    "You should rest."

  context:
    friend_showing_concern

  illocution:

    desired_outcomes:
      - reduce_stress
      - encourage_recovery

  intentionality:
    0.83

  intent_confidence:
    0.72
```

---

# Distinguish Two Confidence Dimensions

Do not collapse these.

## Intent Confidence

Question:

> How confident are we that this was the intended outcome?

## Effectiveness Confidence

Question:

> How likely is the intended outcome to actually occur?

Example:

```yaml
advice:

  expression:
    "Exercise more"

  intent_confidence:
    high

  effectiveness_confidence:
    low
```

---

# Communication as Reinforcement or Transition

Original idea:

Communication causes action.

Refined proposal:

Communication produces:

* State reinforcement
  OR
* State transition

Examples:

Transition:

Teach new idea.

Reinforcement:

Confirm existing trust.

Implication:

Not all communication changes state.

Some communication preserves state.

---

# Relationship to Multilingual Semantics

Translation is insufficient.

Different languages may preserve locution while changing illocution.

Therefore:

```text
Language A
↓

Concept
↓

Context
↓

Illocution
↓

Language B
```

Translation should preserve intended outcomes where possible.

---

# Proposed Product Questions for Evaluation

1. Should intentionality become part of semantic storage?

2. Should retrieval support intent-aware querying?

Example:

> Find concepts intended to encourage ownership.

3. Should semantic definitions include desired outcomes?

4. Should confidence be split into:

* Intent confidence
* Outcome confidence

5. Should multilingual mapping align:

* Words
  OR
* Intended outcomes

6. Should context become hierarchical:

* Linguistic
* Cultural
* Relational
* Temporal
* Intentional

---

# Open Questions

* When does contextual variation become a new concept?
* Should intent alter retrieval ranking?
* Can intentionality be inferred reliably enough?
* Should AI-generated and human-authored content be weighted differently?
* Is communication fundamentally descriptive or transformative?
* Is meaning intrinsic to expression or relational between author and receiver?

---

# Working Principle

> MahaLath should move from:
>
> "What does this term mean?"
>
> toward:
>
> "What does this term appear designed to mean here?"

---

# Suggested Claude Evaluation Tasks

Ask Claude to assess:

1. Architecture changes
2. Schema changes
3. Retrieval implications
4. Ingestion implications
5. Performance implications
6. Migration strategy
7. Risks of semantic overfitting
8. Risks of ontology explosion

Goal:

Translate philosophical conclusions into concrete product decisions without prematurely locking implementation.
