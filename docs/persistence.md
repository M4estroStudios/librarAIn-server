# Persistenza SQLite

Database: `<DATA_ROOT>/db/biblioteca.db`.

Inizializzazione: `init_books_schema` in `src/persistence/book_sqlite.py` (crea tabelle + migrazioni in `_schema_migrations`).

## Tabelle

### `books`

Catalogo libri. PK = `source_sha256`.

Campi tipici: `schema_version`, `title`, `subtitle`, `authors_json`, `publisher`, `publication_year`, `isbn`, timestamp `created_at` / `updated_at` / `last_seen_at`, `last_error`, più colonne opzionali da migrazioni.

Usata dal **gate hash**: se lo SHA è presente, un nuovo ingest dello stesso PDF viene saltato.

### `book_metadata_audit`

Audit degli upsert metadati REICAT.

### `pipeline_runs`

Run di ingest: `request_id`, status, conteggi pagine, errori, timing. Scrittura da `src/persistence/pipeline_runs.py`.

### `research_runs`

Run di research: query, `poh_id`, status, context books JSON, citations count, timestamp. Scrittura da `src/persistence/research_runs.py`.

### `subject_embeddings`

Vettori embedding per `canonical_id` (matcher POH e research lookup). Modello e dimensione legati a `MATCHER_EMBEDDING_MODEL`.

### `subject_match_audit`

Audit decisioni di matching soggetto (new / merge / existing).

## Cosa non sta in SQLite

| Dato | Dove |
|------|------|
| Markdown pagine / libro | `data/output/<sha>/` |
| Polyindex | `data/polyindex/*.json` |
| Articoli research | `data/research/` |
| Cache stage / PNG | `data/tmp/<sha>/` |
| Job in-memory | `JobRegistry` (con history anche su disco/SQLite a seconda del tipo) |

## Concorrenza

Connessioni per operazione; nessun WAL forzato di default. Carico tipico: pochi job concorrenti (semafori a 1). In caso di `database is locked` sotto carico, valutare WAL e riduzione parallelismo.

## Helper utili

- `insert_book_minimal` — insert test / bootstrap
- `run_ingest_gate_phase` — gate + eventuale upsert
- `validate_source_sha256` — digest valido prima di path/DB
