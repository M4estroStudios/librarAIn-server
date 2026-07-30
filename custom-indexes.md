# Custom indexes (section_list)

Design note from grilling. **No feature code yet** — only this document.

## Problem

Some books have non-standard indexes beyond TOC / analytic INDEX / BIBLIO / TIME_INDEX: appendices, glossaries, and similar sections. Types are heterogeneous; we do not have a closed taxonomy.

Existing ingest toolbar already marks TOC / Indice / Biblio ranges. Biblio already has a post-ingest-only path. That is **not** this feature.

## Goal (eventual)

A **global polyindex JSON** that can improve search by unifying the *same kind of characteristic* across books (e.g. glossaries from many volumes).

## Decisions (v0)

1. **No new product UI/API for now.** No “+” button, no agent chat, no job.
2. Prototype shape: generic **`section_list`** (roughly covers glossary + appendix), not one schema per exotic type.
3. Before any schema lands in code: **manual exercise** on one real PDF (prompt + ~10 sample entries).
4. Cross-book **merge is deferred**. Heterogeneity makes alignment keys unknowable until we have real samples. First durable form, when built later, should be **per-book blocks** in a global file (`kind` + `book_sha` + `entries`), searchable but not auto-merged.
5. Free-form Cursor-like agent inside ingest is **out of scope** until a deterministic extract job with a stable envelope proves useful.

## Non-goals (v0)

- Covering every index variant
- Auto-merge / global timeline-style aggregation
- Mapping everything into TOC / INDEX / TIME_INDEX
- Shipping the “+” tools surface

## Candidate types

Inventory from editorial practice and from books under `data/input/raw` (via TOC/output). Excludes types already handled: TOC / analytic INDEX / BIBLIO / TIME_INDEX.

- Chronology / chronistory (date → event)
- Name index
- Place / toponym index
- Combined names-and-places index
- Notable things / topics / phenomena index
- Appendix (container for supplementary material)
- Onomastic lists in appendix (emperors, popes, artists, mayors, etc.)
- Institutional / technical lists (urban plans, offices, roles…)
- Itineraries / guided routes
- Nota al testo / reading notes / typographic legend
- Glossary (term → definition)
- Abbreviations and symbols list
- Illustrations index
- Plates index
- Tables / figures / maps index
- Iconographic sources list
- Documentary appendices (texts, norms, excerpts, documents)
- Data appendices (tables, statistics, extended charts)
- Afterword (postfazione)
- Endnotes list (if grouped at volume end)
- Genre-specific indexes (works, cited authors as a corpus, directors, etc.)
- Genealogies / dynastic trees
- Repertories / entry catalogs (monument sheets, bio sheets outside main body)
- Concordances / systematic cross-references between entries

## Suggested later envelope (draft only — validate on a real PDF)

```json
{
  "kind": "section_list",
  "label": "Glossario",
  "book_sha": "<source_sha256>",
  "page_range": { "start": 0, "end": 0 },
  "entries": [
    {
      "label": "termine o titolo voce",
      "text": "definizione o corpo breve",
      "pages": [1]
    }
  ]
}
```

Fields may change after the manual exercise. Opaque extras belong in an optional `payload`, not in the shared envelope.

## Search / aggregation (later)

- **Phase 1 (when implemented):** lookup / context injection on common fields only; no structural merge across books.
- **Phase 2:** when the same `kind` appears on ≥2–3 books, grill that kind alone (schema + alignment key: prefer hybrid attach to existing subject/time IDs, else weak normalized label).

## Next step

Pick one real PDF with a glossary or appendix. Write by hand:

1. a short extraction prompt;
2. ~10 JSON entries in the draft envelope above;

Then decide whether the envelope is good enough to become schema + job + (maybe) a “+” entry point.
