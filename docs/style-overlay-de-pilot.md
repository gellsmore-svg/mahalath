# Voice notes — German oscillation pilot corpus (M-B)

This overlay accompanies the German pilot document
(`schwingung-pilot-de.md`). It is the per-document overlay for the de
lexicon's first corpus; the runtime-level RS overlay must NOT apply to
German debates.

**The single most important rule:**

- Definitions MUST be written in German. The de lexicon represents
  meaning as it emerges within German (ADR-028); an English definition
  of a German term would be a translation, which is forbidden at
  ingestion. All `definition` / `new_definition` JSON string values:
  German. JSON keys and structure: unchanged (English keys as
  specified in the task).

**Voice**

- Declarative ontology: "Die Resonanz ist …", never "Resonanz kann
  verstanden werden als …". Keine Hedges.
- Physikalische Fachsprache, präzise und knapp. Der Ton des Korpus
  ist lehrbuchhaft-nüchtern mit gelegentlichem Bild ("geduldige
  Aufsummierung"); Bilder sind zulässig, wenn sie begrenzt bleiben.
- Ein Begriff, ein Satzkern: erst die Gattung (Schwingung, Größe,
  Verbindung, Vorgang), dann die Differenz.

**Term notes**

- "Eigenfrequenz" ist eine Eigenschaft des Systems, nicht des
  Antriebs.
- "Kopplung" bezeichnet die VERBINDUNG zweier Systeme, nicht den
  Energiefluss selbst.
- "Rückkopplung" umfasst Mitkopplung UND Gegenkopplung; keine der
  beiden ist die "eigentliche" Rückkopplung.
- "Schwebung" ist ein Überlagerungsphänomen, kein eigener
  Schwingungstyp.

**Frames**

- Definitions from this corpus speak almost always within the
  `physical` frame; use `structural` only for claims about the form of
  systems in general rather than physical behaviour.
