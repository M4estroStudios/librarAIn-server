# librarAIn-server

Server e pipeline per ingestione libri, persistenza metadati e (fase successiva) ricerca. Requisiti di prodotto della Fase 1: `[PRD-Fase1.md](PRD-Fase1.md)`.

Pipeline Fase 1 **completa** (OCR → Vision → Editor → artefatti libro → polyindex). Entrypoint operatore: web UI `web/index.html` e `POST /api/ingest/submit` (risposta `202` + SSE). Admin merge soggetti POH: `web/admin.html`. Fase 2 (ricerca) non ancora implementata.

## Struttura del repository (bilanciata)

Obiettivo: **tre pilastri** chiari (ingestione, ricerca, dati) senza minimalismo sterile e senza labirinti di sottocartelle. Poche cartelle di primo livello sotto `src/`, massimo **un livello** di annidamento oltre quello.

### Principi

- **`ingestion/`**: tutta la Fase 1 (PDF → MD, TOC/INDEX, matching AI per `INDEX.json`, indice temporale `TIME_INDEX.json`).
- **`search/`**: Fase 2 (ricerca e generazione articoli) — cartella dedicata fin da subito così il codice non si mescola con l'ingestione; all'inizio può contenere solo entrypoint/stub o essere vuota fino all'implementazione.
- **`persistence/`** (o `data_layer/`): accesso a SQLite, file JSON di biblioteca, registro hash — "dove stanno i dati" lato codice, separato dalla pipeline.
- **`core/`**: configurazione `.env`, logging, costanti condivise — sottile, non una copia del progetto.
- **`api/`**: HTTP/CLI che smista verso `ingestion` e (poi) `search`, senza logica di dominio pesante.
- **`data/` (root)**: solo file su disco (input PDF, output libri, `biblioteca.csv`, `TOC.json` / `INDEX.json`) — non confonderla con `src/...`.

### Albero indicativo (solo cartelle, con commenti)

```text
librarAIn-server/ # radice repository
├── src/ # tutto il codice applicativo
│   ├── api/ # entrypoint HTTP/CLI verso ingestione e ricerca
│   ├── core/ # configurazione .env, logging, shared di base
│   ├── ingestion/ # pipeline Fase 1: PDF → MD, TOC/INDEX, merge artefatti
│   │   └── pipeline/ # OCR, Vision, Editor; `prompts/` contiene i .md di sistema (Vision/Editor)
│   ├── models/ # tipi e contratti condivisi tra moduli
│   ├── persistence/ # SQLite, JSON biblioteca, hash/stato run
│   └── search/ # Fase 2: ricerca e generazione articoli (stub o futuro)
├── data/ # solo dati su disco, mai codice
│   ├── db/ # database principale e snapshot checkpoint
│   │   ├── checkpoints/ # snapshot/versioni storiche del db
│   │   │   ├── biblioteca.<yyyy>.<mm>.<dd>.csv # checkpoint giornaliero (copia di biblioteca.csv)
│   │   │   └── ... # altri checkpoint (es. più date/versioni)
│   │   └── biblioteca.csv # database SQLite corrente (nome file convenzionale)
│   ├── input/ # PDF sorgente in ingresso
│   │   ├── raw/ # file originali caricati dall'operatore
│   │   │   ├── <libro>.pdf # nome file originale
│   │   │   └── ... # altri PDF originali
│   │   └── processed/ # PDF normalizzati/allineati dopo preprocessing
│   │       ├── <hash libro>.pdf # nome basato su hash SHA-256 del libro
│   │       └── ... # altri PDF processati
│   ├── polyindex/ # artefatti globali correnti e snapshot storici
│   │   ├── checkpoints/ # snapshot giornalieri di TOC/INDEX
│   │   │   ├── <yyyy>.<mm>.<dd>.INDEX.json # snapshot INDEX del giorno
│   │   │   ├── <yyyy>.<mm>.<dd>.TOC.json # snapshot TOC del giorno
│   │   │   └── ... # altri snapshot storici
│   │   ├── INDEX.json # indice soggetti POH globale corrente
│   │   ├── TIME_INDEX.json # indice temporale globale (anni/periodi + date → libri/pagine)
│   │   └── TOC.json # toc globale corrente
│   ├── output/ # output organizzati per libro processato
│   │   ├── <hash libro>/ # artefatti del singolo libro
│   │   │   ├── pages/ # pagine markdown singole del libro
│   │   │   │   ├── p.<NNNN>.<libro>.md # singola pagina (numero pagina zero-padded)
│   │   │   │   └── ... # altre pagine markdown
│   │   │   ├── <libro>.md # file markdown unificato del libro
│   │   │   ├── INDEX.md # indice del libro aggregato
│   │   │   └── TOC.md # table of contents aggregata del libro
│   │   └── ... # altri libri processati
│   └── tmp/ # temporanei, cache, lavorazioni intermedie
│       ├── <hash libro>/ # temporanei relativi al singolo libro
│       │   ├── stage1OCR/ # output testuale raw OCR
│       │   │   ├── p.<NNNN>.<libro>.txt # pagina OCR raw
│       │   │   └── ... # altre pagine OCR raw
│       │   ├── stage2Vision/ # output markdown dopo refinement vision
│       │   │   ├── p.<NNNN>.<libro>.md # pagina dopo stage vision
│       │   │   └── ... # altre pagine stage vision
│       │   ├── stage3Editor/ # output markdown finale dopo editor
│       │   │   ├── p.<NNNN>.<libro>.md # pagina dopo stage editor
│       │   │   └── ... # altre pagine stage editor
│       │   └── ... # altri temporanei del libro
│       └── ... # altri temporanei
├── scripts/ # script operativi, bootstrap, utility
├── tests/ # test e smoke (POI, quando introdotti)
└── web/ # pagina HTML punto unico di input operatore
```

### Linee guida pratiche

- Non aggiungere sottocartelle finché un modulo non supera ~500 LOC o non serve davvero un boundary chiaro.
- `src/search/` resta il posto naturale per la ricerca senza rompere `ingestion/` quando passerai alla Fase 2.
- `src/persistence/` è l'unico posto in cui si concentra SQLite + JSON di biblioteca + (se serve) tracciamento run/hash.
- `src/core/config.py` legge solo `.env` / `example.env`.
- `data/` non contiene codice, solo artefatti runtime.
- `web/index.html` resta il punto unico di input per l'ingest; `web/admin.html` per il merge manuale dei soggetti POH tra libri.

## Setup e comandi

```bash
cp example.env .env   # poi adatta DATA_ROOT, modelli OpenAI, ecc.
make setup-env        # crea venv, installa torch (MPS/CUDA/CPU) e dipendenze
make test             # 256+ test unitari
make lint             # ruff su src/, tests/, scripts/
make run-server       # HTTP server su http://127.0.0.1:8765
```

CI GitHub Actions (`.github/workflows/ci.yml`): lint + test su ogni push/PR.

## Configurazione runtime `.env` (T3)

La configurazione runtime centralizzata è gestita da:

- `src/models/settings.py`: modello Pydantic `Settings`.
- `src/core/config.py`: loader `load_settings(env_file=".env")`.
- `example.env`: template di riferimento per le variabili.

`load_settings` carica prima il file `.env`, poi applica override da variabili d'ambiente già presenti nel processo.

### Variabili obbligatorie

- `DATA_ROOT` (root runtime dati, es. `data`)
- `OPENAI_PROVIDER` (`local` oppure `remote`)

### Variabili opzionali (con default)

- `MAX_PARALLEL_REQUEST` (default `2`)
- `PAGE_RANGE_PER_THREAD` (default `10`, pagine PDF sorgenti per lettore nel parallelismo di allineamento)
- `TIMEOUT_SECONDS` (default `120`)
- `RETRY_ATTEMPTS` (default `2`)
- `RATE_LIMIT_PER_MINUTE` (default `60`)
- `VISION_MODEL` (default `None`)
- `EDITOR_MODEL` (default `None`)
- `MATCHER_EMBEDDING_MODEL`, `MATCHER_LLM_MODEL`, `MATCHER_SIMILARITY_THRESHOLD`, `MATCHER_USE_AI` (subject matching POH in `INDEX.json`)
- `TIME_INDEX_LLM_MODEL` (default `None`; fallback: `MATCHER_LLM_MODEL` → `EDITOR_MODEL` → `gpt-4.1-mini`)
- `TIME_INDEX_USE_LLM` (default `true`; se `false`, estrazione temporale solo regex)
- `INGEST_HTTP_HOST` (default `127.0.0.1`), `INGEST_HTTP_PORT` (default `8765`)
- `INGEST_API_TOKEN` (opzionale; se impostato, richiesto su tutti gli endpoint `/api/*`)
- `INGEST_MAX_CONCURRENT_JOBS` (default `1`; job extra restano in coda)
- `INGEST_MAX_UPLOAD_BYTES` (default 512 MiB)

### Vincoli semantici

- Se `OPENAI_PROVIDER=remote`, diventano obbligatorie:
  - `OPENAI_BASE_URL`
  - `OPENAI_API_KEY`

### Errore di configurazione

In caso di variabili mancanti o invalide, il loader fallisce in modo esplicito con messaggio aggregato e riferimento a `example.env`.

`sqlite_path` viene derivato automaticamente come `<DATA_ROOT>/db/biblioteca.csv` (file binario SQLite; l'estensione `.csv` è solo convenzione di naming richiesta dal prodotto, non un export CSV).

I PDF sorgente con le pagine indicate in `pages_to_remove` già rimosse (PDF allineato / normalizzati) devono essere scritti sotto `<DATA_ROOT>/input/processed` (di default `data/input/processed`); nel codice questo path è disponibile come `Settings.processed_pdf_input_dir`.

## Schema richiesta ingestione (MVP v1.0)

Il contratto canonico dell'input ingestione è definito nel modello Pydantic `IngestRequest` in `src/models/request.py`.
Qualunque entrypoint (CLI/API/UI) deve validare e normalizzare i dati in questo schema prima di avviare la pipeline.
Per T2 è disponibile anche `validate_and_enrich_request(payload)` in `src/ingestion/request_validation.py`, che valida il payload, verifica il file PDF e calcola subito `source_sha256`.

### Campi principali

- `schema_version`: versione contratto, bloccata a `1.0`.
- `source_pdf_path`: path del PDF sorgente.
- `pages_to_remove`: pagine 1-based da eliminare, normalizzate (ordinate + deduplicate).
- `toc_range`: intervallo pagine TOC (`start`, `end`).
- `index_range`: intervallo pagine INDEX (`start`, `end`).
- `reicat`: metadati bibliografici REICAT.
- `options`: opzioni runtime facoltative.

#### Campi `reicat`

- `titolo`
- `sottotitolo`
- `complementi_del_titolo`
- `autore`
- `curatore`
- `traduttore`
- `numero_edizione`
- `anno_di_pubblicazione`
- `tipo_di_pubblicazione`
- `luogo_di_pubblicazione`
- `editore`
- `numero_pagine`
- `titolo_collana`
- `numero_nella_collana`
- `isbn`

### Regole di validazione

- Le pagine sono sempre 1-based.
- Ogni range richiede `start <= end`.
- `pages_to_remove` accetta solo interi positivi.
- Le pagine rimosse non possono sovrapporsi a `toc_range` o `index_range`.
- Il campo `reicat` richiede almeno `titolo` e almeno un elemento in `autore`.
- Il file `source_pdf_path` deve esistere ed essere leggibile.
- La `sha256` viene calcolata immediatamente dopo la validazione del file, prima della decisione di hash-gate.

## SourceHashGate (T5)

Il gate hash sorgente è implementato in `src/persistence/book_sqlite.py` con `source_hash_gate(source_sha256, sqlite_path)`.

- Input: digest SHA-256 (`source_sha256`) e path del DB SQLite (`sqlite_path`).
- Output: `SourceHashGateResult` con `status`, `source_sha256` e `should_skip_pipeline`.
- Stati supportati:
  - `new_hash`: hash mai visto, pipeline da eseguire.
  - `duplicate_source_hash`: hash già noto, pipeline da saltare.

Il gate legge la tabella `books` nello SQLite. Se non trova la hash, restituisce `new_hash`; se la trova, restituisce `duplicate_source_hash`.

## Schema SQLite minimo (T6)

Lo schema minimo è inizializzato da `init_books_schema(sqlite_path)` in `src/persistence/book_sqlite.py`. I run della pipeline sono tracciati in `pipeline_runs` via `src/persistence/pipeline_runs.py`.

La tabella `books` usa direttamente `source_sha256` come identificativo:

- `source_sha256 TEXT PRIMARY KEY`
- `schema_version TEXT NOT NULL`
- `title TEXT NOT NULL`
- `subtitle TEXT`
- `authors_json TEXT NOT NULL`
- `publisher TEXT`
- `publication_year INTEGER`
- `isbn TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `last_seen_at TEXT NOT NULL`
- `last_error TEXT`

Per i test e per l'inserimento minimo è disponibile `insert_book_minimal(...)` in `src/persistence/book_sqlite.py`, che fallisce in modo esplicito su hash duplicata.

### Esempio payload valido

```json
{
  "schema_version": "1.0",
  "source_pdf_path": "data/input/raw/libro.pdf",
  "pages_to_remove": [1, 2, 5],
  "toc_range": { "start": 11, "end": 18 },
  "index_range": { "start": 301, "end": 324 },
  "reicat": {
    "titolo": "Titolo libro",
    "sottotitolo": "Sottotitolo",
    "complementi_del_titolo": "Complementi",
    "autore": ["Nome Cognome"],
    "curatore": ["Curatore Nome"],
    "traduttore": ["Traduttore Nome"],
    "numero_edizione": "2",
    "anno_di_pubblicazione": 2024,
    "tipo_di_pubblicazione": "Monografia",
    "luogo_di_pubblicazione": "Milano",
    "editore": "Editore",
    "numero_pagine": 350,
    "titolo_collana": "Nome Collana",
    "numero_nella_collana": "12",
    "isbn": "9780000000000"
  },
  "options": {
    "force_metadata_update_on_duplicate_hash": true
  }
}
```

## Flusso asincrono con progresso (job_id + SSE)

`POST /api/ingest/submit` **non aspetta il completamento** della pipeline. Risponde immediatamente `202` con:

```json
{
  "ok": true,
  "job_id": "<hex-32-char>",
  "events_url": "/api/ingest/<job_id>/events",
  "status_url": "/api/ingest/<job_id>/status"
}
```

La pipeline gira in un thread di background. Lo stato è consultabile in due modi:

### Stream SSE — `GET /api/ingest/<job_id>/events`

`Content-Type: text/event-stream`. Supporta l'header `Last-Event-ID` per replay parziale (valore = `seq` dell'ultimo evento ricevuto). Ogni frame ha `event: <status>` e `data: <json>`.

Sequenza tipica degli eventi:

| `phase` | `status` | `counts_as_step` | note |
|---|---|---|---|
| `pipeline` | `pipeline_total` | — | emesso dopo l'enumerazione pagine; porta `global_total` |
| `validation` | `started` / `completed` / `error` | — | |
| `gate_hash` | `started` / `completed` / `error` | — | `gate_status`, `pipeline_skipped` |
| `pdf_alignment` | `started` / `completed` / `error` | `true` se ha girato | `skipped` |
| `page_enumeration` | `started` / `completed` / `error` | — | `n_pages` |
| `stage1_ocr` | `started` | — | `page_total` |
| `stage1_ocr` | `page_progress` / `page_skipped` / `page_failed` | `true` | `page_index`, `page_total`, `aligned_page`, `original_page` |
| `stage1_ocr` | `completed` / `failed` | — | |
| `stage2_vision` | `started` / `page_*` / `completed` | `true` per pagina | |
| `stage3_editor` | `started` / `page_*` / `completed` | `true` per pagina | |
| `polyindex_toc` / `polyindex_index` / `time_index` | `started` / `completed` | — | sync `TOC.json`, `INDEX.json`, `TIME_INDEX.json`; su `time_index` completed: `n_years`, `n_dates`, `time_index_path` |
| `pipeline` | `done` | — | **terminale**; porta `result` (payload completo) |
| `pipeline` | `error` | — | **terminale**; porta `message` |

Gli eventi con `counts_as_step: true` includono `global_step` e `global_total`. Formula: `global_total = 1 (alignment, se eseguito) + 3 × N` (Stage 1 + Vision + Editor per ogni pagina utile).

### Snapshot JSON — `GET /api/ingest/<job_id>/status`

```json
{
  "ok": true,
  "job_id": "...",
  "status": "running",
  "global_step": 14,
  "global_total": 34,
  "events": [...],
  "result": null,
  "error": null,
  "created_at": "...",
  "updated_at": "..."
}
```

### Consumo da CLI

```bash
curl -N http://127.0.0.1:8765/api/ingest/<job_id>/events
curl -s http://127.0.0.1:8765/api/ingest/<job_id>/status | jq '{status,global_step,global_total}'
```

### Autenticazione API

Se `INGEST_API_TOKEN` è impostato, tutti gli endpoint `/api/*` richiedono il token via header `X-API-Token`, `Authorization: Bearer <token>` o query `?token=`. Le pagine statiche (`/`, `/admin`) restano aperte; la UI salva il token in `localStorage`.

### Admin POH — `/admin`

`GET /api/admin/subjects?min_books=2` elenca i soggetti presenti in almeno N libri. `POST /api/admin/subjects/merge` unisce soggetti duplicati (`target_id`, `source_ids`).

### Payload `result` (evento `done`)

Il campo `result` nell'evento SSE terminale `done` include: `ingest_gate_phase`, `pdf_alignment`, `useful_pages_enumeration`, `stage1`, `stage2`, `stage3`, percorsi output (`book_md`, `toc_md`, `index_md`) e statistiche polyindex.

Artefatti per libro in `data/output/<source_sha256>/`: `pages/`, `<slug>.md`, `TOC.md`, `INDEX.md`, `manifest.json`. Polyindex globale in `data/polyindex/` (`TOC.json`, `INDEX.json`, `TIME_INDEX.json`).

### `TIME_INDEX.json` (indice temporale)

Ultimo passo del polyindex, dopo `INDEX.json`: rilettura pagina per pagina del markdown del libro.

- **LLM** (`TIME_INDEX_USE_LLM=true`): estrae anni espliciti, periodi testuali (`Quattrocento`, `XIV secolo`, …) e date di calendario; prompt in `src/ingestion/polyindex/prompts/time_index_extract_prompt.md`.
- **Regex**: integrazione/fallback per date numeriche e filtro su numeri di pagina (`p.`, `pp.`).
- Le **`page_notes`** del form ingest (note sulla formattazione delle pagine) vengono propagate nel system prompt LLM, come per Vision/Editor.
- Parallelismo: `MAX_PARALLEL_REQUEST` (una chiamata LLM per pagina utile).
- Schema: sezioni `years` e `dates`; ogni voce mappa a `books[<sha256>]` con `title`, `slug`, `aligned_pages`, `original_pages` (stesso pattern di `INDEX.json`).

Backfill su libri già processati:

```bash
python -m scripts.backfill_time_index [--data-root data]
```

Cache intermedie in `data/tmp/<source_sha256>/`: `render/` (PNG lazy, solo pagine utili), `stage1OCR/`, `stage2Vision/`, `stage3Editor/`, `stage4TocIndexRefine/`.
