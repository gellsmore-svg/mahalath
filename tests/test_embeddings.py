"""Cross-language fingerprinting / candidate shortlisting tests (M-C).

Covers: cosine; backfill (dry-run writes nothing, apply stores, skips
unchanged, re-embeds after a definition change); shortlisting ranks by
meaning-closeness and never mixes models/dimensions; generate_mappings
uses the embedding shortlist when vectors exist (and the prompt stage
otherwise). All with the MockAdapter — no live embedding model.
"""

from __future__ import annotations

import json

from mahalath.adapters import MockAdapter
from mahalath.config import AppConfig, MongoConfig, RuntimeConfig
from mahalath.db.models import DefinitionVersion, OntologyEntry
from mahalath.db.repositories import OntologyEntryRepository
from mahalath.embeddings import (
    backfill_embeddings,
    cosine,
    get_embedding,
    shortlist_candidates,
    store_embedding,
)


def test_cosine_basics() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert round(cosine([1.0, 0.0], [-1.0, 0.0]), 6) == -1.0
    assert cosine([1.0, 0.0], [1.0]) == 0.0          # length mismatch
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0     # zero vector


def _entry(label: str, term: str, text: str, language: str) -> OntologyEntry:
    return OntologyEntry(
        mpl_label=label, canonical_term=term, language=language, confidence=8.0,
        definitions=[DefinitionVersion(text=text, language=language)],
    )


def test_backfill_dry_run_then_apply_then_skip(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-117", "Kopplung", "Verbindung zweier Systeme.", "de"))
    adapter = MockAdapter(embeddings={"Kopplung": [0.1, 0.2, 0.3]})

    dry = backfill_embeddings(mongo_db, adapter, model="bge-m3", apply=False)
    assert dry.scanned == 1 and dry.embedded == 1
    assert get_embedding(mongo_db, "MPL-117") is None  # nothing written

    applied = backfill_embeddings(mongo_db, adapter, model="bge-m3", apply=True)
    assert applied.embedded == 1
    rec = get_embedding(mongo_db, "MPL-117")
    assert rec["vector"] == [0.1, 0.2, 0.3] and rec["dim"] == 3
    assert rec["language"] == "de" and rec["model"] == "bge-m3"

    # Unchanged entry + same model → skipped, not re-embedded.
    again = backfill_embeddings(mongo_db, adapter, model="bge-m3", apply=True)
    assert again.embedded == 0 and again.skipped_current == 1


def test_backfill_reembeds_after_definition_change(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-117", "Kopplung", "Erste Definition.", "de"))
    adapter = MockAdapter()  # hash-derived vectors: text change → vector change
    backfill_embeddings(mongo_db, adapter, model="bge-m3", apply=True)
    first = get_embedding(mongo_db, "MPL-117")["vector"]

    # Append a new definition (the latest wins for the fingerprint).
    mongo_db.ontology_entries.update_one(
        {"_id": "MPL-117"},
        {"$push": {"definitions": {"text": "Klarere Definition.", "language": "de"}}},
    )
    res = backfill_embeddings(mongo_db, adapter, model="bge-m3", apply=True)
    assert res.embedded == 1 and res.skipped_current == 0
    assert get_embedding(mongo_db, "MPL-117")["vector"] != first


def test_shortlist_ranks_by_closeness_and_guards_model(mongo_db) -> None:
    repo = OntologyEntryRepository(mongo_db)
    # Source (de) and three en targets; we control vectors directly.
    repo.insert(_entry("MPL-117", "Kopplung", "x", "de"))
    for lbl, term in [("MPL-038", "coupling"), ("MPL-054", "Resonance"),
                      ("MPL-097", "Resurrection")]:
        repo.insert(_entry(lbl, term, "x", "en"))

    store_embedding(mongo_db, "MPL-117", language="de", model="bge-m3",
                    vector=[1.0, 0.0, 0.0], source_text="a")
    store_embedding(mongo_db, "MPL-038", language="en", model="bge-m3",
                    vector=[0.9, 0.1, 0.0], source_text="b")   # nearest
    store_embedding(mongo_db, "MPL-054", language="en", model="bge-m3",
                    vector=[0.3, 0.7, 0.0], source_text="c")   # middle
    store_embedding(mongo_db, "MPL-097", language="en", model="bge-m3",
                    vector=[0.0, 0.0, 1.0], source_text="d")   # far
    # A different-model vector must be ignored, not mis-compared.
    store_embedding(mongo_db, "MPL-099", language="en", model="other-model",
                    vector=[1.0, 0.0, 0.0], source_text="e")

    cands = shortlist_candidates(mongo_db, "MPL-117", "en", top_k=2)
    assert [c.label for c in cands] == ["MPL-038", "MPL-054"]
    assert cands[0].score > cands[1].score
    assert "MPL-099" not in {c.label for c in cands}  # cross-model excluded

    # No source vector → empty (caller falls back to prompt).
    assert shortlist_candidates(mongo_db, "MPL-999", "en") == []


def _cfg() -> AppConfig:
    return AppConfig(mongo=MongoConfig(database="mahalath_pytest"),
                     runtime=RuntimeConfig())


def test_generate_mappings_uses_embedding_shortlist(mongo_db) -> None:
    from mahalath.mappings import generate_mappings, seed_mapping_relations

    seed_mapping_relations(mongo_db)
    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-117", "Kopplung", "Verbindung zweier Systeme.", "de"))
    repo.insert(_entry("MPL-038", "coupling", "Constraint between configs.", "en"))
    # Fingerprints make MPL-038 the obvious match for MPL-117.
    store_embedding(mongo_db, "MPL-117", language="de", model="bge-m3",
                    vector=[1.0, 0.0], source_text="a")
    store_embedding(mongo_db, "MPL-038", language="en", model="bge-m3",
                    vector=[1.0, 0.0], source_text="b")

    # Adapter has NO candidate prompt response — if the embedding path is
    # used, generation never needs build_candidate_prompt. It only answers
    # the attribution passes.
    adapter = MockAdapter(default_response=json.dumps(
        {"relationship": "partial_overlap", "confidence": 9.0, "rationale": "ok"}))

    result = generate_mappings(
        _cfg(), mongo_db, adapter,
        source_language="de", target_language="en",
        candidate_source="embedding", top_k=5, apply=True,
    )
    assert result.candidate_pairs == 1
    assert result.accepted == 1
    # No candidate-prompt call was made (embedding shortlist replaced it).
    assert all("mapping_candidates" not in (c.get("prompt") or "")
               for c in adapter.calls)


def test_backfill_falls_back_then_skips_on_nan(mongo_db) -> None:
    """A model NaN on the term+def input retries with def-only; if that
    also NaNs, the entry is skipped (not crashed)."""
    from mahalath.adapters.base import EmbeddingNaNError

    repo = OntologyEntryRepository(mongo_db)
    repo.insert(_entry("MPL-201", "alpha", "Definition of alpha.", "en"))   # fallback works
    repo.insert(_entry("MPL-202", "beta", "Definition of beta.", "en"))     # always NaN

    class NaNAdapter:
        name = "nan"
        default_model = "bge-m3"

        def generate(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

        def embed(self, text, *, model=None, timeout_seconds=None):
            # term-prefixed input always NaNs; def-only works for alpha,
            # but beta NaNs on every variant.
            if text.startswith("alpha:") or "beta" in text:
                raise EmbeddingNaNError("non-finite")
            from mahalath.adapters.base import EmbeddingResponse
            return EmbeddingResponse(vector=[0.1, 0.2], model="bge-m3", dim=2)

    res = backfill_embeddings(mongo_db, NaNAdapter(), model="bge-m3", apply=True)
    assert res.embedded == 1 and res.embedded_via_fallback == 1   # alpha, via def-only
    assert res.skipped_nan == 1                                    # beta, unembeddable
    assert get_embedding(mongo_db, "MPL-201") is not None
    assert get_embedding(mongo_db, "MPL-202") is None


def test_init_bootstrap_is_idempotent(mongo_db) -> None:
    """`init` creates collections+indexes and seeds the taxonomies; a
    second run is a clean no-op (fresh-install bootstrap, v1)."""
    from mahalath.intents import seed_intents
    from mahalath.mappings import seed_mapping_relations
    from mahalath.db.indexes import ensure_indexes

    created = ensure_indexes(mongo_db)
    assert "entry_embeddings" in created  # the new M-C collection is indexed

    first_i = seed_intents(mongo_db)
    first_r = seed_mapping_relations(mongo_db)
    assert first_i.inserted and first_r["inserted"]

    second_i = seed_intents(mongo_db)
    second_r = seed_mapping_relations(mongo_db)
    assert second_i.inserted == [] and second_r["inserted"] == []
    assert len(second_i.skipped_existing) == len(first_i.inserted)
    assert len(second_r["skipped_existing"]) == len(first_r["inserted"])
