# Polyindex

Il polyindex è l'insieme di indici **globali cross-libro** in `data/polyindex/`. Viene aggiornato a fine ingest e da operazioni admin.

## File

| File | Contenuto |
|------|-----------|
| `TOC.json` | Capitoli per libro |
| `INDEX.json` | Soggetti POH → libri/pagine |
| `TIME_INDEX.json` | Anni e date → libri/pagine |

Lock file sotto la stessa directory (es. `.index.lock`) serializzano le scritture. Matching LLM/embedding avviene **fuori** dal lock.

## TOC.json

Schema (`PolyindexTocDocument`):

```json
{
  "schema_version": "1.0",
  "books": {
    "<source_sha256>": {
      "title": "…",
      "slug": "…",
      "chapters": [
        {
          "label": "Capitolo I",
          "aligned_page_start": 10,
          "aligned_page_end": 40,
          "original_page_start": 12,
          "original_page_end": 42
        }
      ]
    }
  }
}
```

Aggiornato da `sync_polyindex_toc_from_book` nell'orchestrator.

## INDEX.json

Cuore del modello POH:

```json
{
  "schema_version": "1.0",
  "subjects": {
    "<canonical_id>": {
      "canonical_label": "Marco Polo",
      "aliases": ["Marco Polo", "Messer Marco"],
      "time_range": { "start": "…", "end": "…" },
      "books": {
        "<source_sha256>": {
          "title": "…",
          "slug": "…",
          "aligned_pages": [10, 11, 55],
          "original_pages": [12, 13, 57]
        }
      }
    }
  }
}
```

### Subject matching

Durante l'ingest, ogni lemma di `INDEX.md` del libro passa da `match_subject`:

1. Match deterministico (id/label/alias).
2. Opzionale: embedding (`MATCHER_EMBEDDING_MODEL`) + soglia `MATCHER_SIMILARITY_THRESHOLD`.
3. Opzionale: LLM dirimitore (`MATCHER_LLM_MODEL`) se ambiguo.

Decisioni auditate in SQLite (`subject_match_audit`). Embeddings in `subject_embeddings`.

### Admin

- Lista soggetti multi-libro: `GET /api/admin/subjects?min_books=2`
- Merge: `POST /api/admin/subjects/merge` `{target_id, source_ids[]}`
- Update alias / time_range / pages
- Delete soggetto (con scrub correlato su TIME_INDEX)
- Dedup suggestions: scan async + dismiss
- Backfill embeddings: `/api/admin/embeddings/*`

## TIME_INDEX.json

```json
{
  "schema_version": "1.0",
  "years": {
    "1271": {
      "books": {
        "<sha>": {
          "title": "…",
          "slug": "…",
          "aligned_pages": [10],
          "original_pages": [12]
        }
      }
    }
  },
  "dates": { }
}
```

Estrazione a fine ingest, pagina per pagina sul markdown:

- LLM se `TIME_INDEX_USE_LLM=true` (prompt in `polyindex/prompts/`)
- Regex/euristiche come integrazione o fallback
- Parallelismo: `MAX_PARALLEL_REQUEST`

Backfill su libri già processati:

```bash
python -m scripts.backfill_time_index [--data-root data]
```

## Relazione con i file per libro

| Per libro | Globale |
|-----------|---------|
| `TOC.md` | `TOC.json` |
| `INDEX.md` | `INDEX.json` (merge soggetti) |
| testo pagine | `TIME_INDEX.json` |

Il merge POH admin opera sull'aggregato `INDEX.json`; non riscrive automaticamente tutti gli `INDEX.md` dei singoli libri.

## Script di manutenzione

Vedi [scripts.md](scripts.md): `backfill_index_book_meta`, `sort_index_files`, `backfill_time_index`.
