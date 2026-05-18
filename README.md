# librarAIn-server

Server e pipeline per ingestione libri, persistenza metadati e (fase successiva) ricerca. Requisiti di prodotto della Fase 1: `[PRD-Fase1.md](PRD-Fase1.md)`.

> ⚠️ **Stato attuale: pipeline temporanea, parziale e incompleta (solo sviluppo).**
> L'ingestione esposta da `POST /api/ingest/submit` (e dalla web UI `web/index.html`) oggi esegue **solo lo Stage 1 OCR** e si considera completata a fine OCR (T11.5). Stage 2 Vision, Stage 3 Editor, writer pagine per libro, `TOC.md`, `INDEX.md`, file aggregato `<libro>.md` e polyindex globale **non sono ancora attivi**. La risposta dell'endpoint include solo i risultati fino allo Stage 1 (`stage1`). Le cartelle `data/tmp/<sha>/stage2Vision/` e `stage3Editor/` mostrate nell'albero sotto si materializzeranno solo con le task future T12.5+/T13+.

## Struttura del repository (bilanciata)

Obiettivo: **tre pilastri** chiari (ingestione, ricerca, dati) senza minimalismo sterile e senza labirinti di sottocartelle. Poche cartelle di primo livello sotto `src/`, massimo **un livello** di annidamento oltre quello.

### Principi

- **`ingestion/`**: tutta la Fase 1 (PDF → MD, TOC/INDEX, matching AI per `INDEX.json`).
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
│   ├── polyndex/ # artefatti globali correnti e snapshot storici
│   │   ├── checkpoints/ # snapshot giornalieri di TOC/INDEX
│   │   │   ├── <yyyy>.<mm>.<dd>.INDEX.json # snapshot INDEX del giorno
│   │   │   ├── <yyyy>.<mm>.<dd>.TOC.json # snapshot TOC del giorno
│   │   │   └── ... # altri snapshot storici
│   │   ├── INDEX.json # indice globale corrente
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
- `web/index.html` (o nome equivalente) resta il punto unico di input coerente per l'operatore.

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

Il gate hash sorgente è implementato in `src/ingestion/request_validation.py` con `source_hash_gate(source_sha256, sqlite_path)`.

- Input: digest SHA-256 (`source_sha256`) e path del DB SQLite (`sqlite_path`).
- Output: `SourceHashGateResult` con `status`, `source_sha256` e `should_skip_pipeline`.
- Stati supportati:
  - `new_hash`: hash mai visto, pipeline da eseguire.
  - `duplicate_source_hash`: hash già noto, pipeline da saltare.

Il gate legge la tabella `books` nello SQLite. Se non trova la hash, restituisce `new_hash`; se la trova, restituisce `duplicate_source_hash`.

## Schema SQLite minimo (T6)

Lo schema minimo è inizializzato da `init_books_schema(sqlite_path)` nello stesso modulo `src/ingestion/request_validation.py`.

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

Per i test e per l'inserimento minimo è disponibile `insert_book_minimal(...)`, che fallisce in modo esplicito su hash duplicata.

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
| `stage1_ocr` | `done` | — | **terminale**; porta `result` (payload completo) |
| `pipeline` | `error` | — | **terminale**; porta `message` |

Gli eventi con `counts_as_step: true` includono `global_step` e `global_total` per aggiornare la barra di avanzamento. Formula attuale: `global_total = 1 (alignment) + N (pagine Stage 1)`. Con Stage 2 e Stage 3 cablati diventerà `1 + 3N` senza alcuna modifica al client.

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

### Payload `result` (evento `done`)

> Nota stato attuale (dev-only, T11.5): la pipeline termina al completamento dello Stage 1 OCR. Stage 2 Vision (T12.5) ed Editor (T13) non sono ancora cablati: il payload `result` non include `stage2`/`stage3` e i file finali per libro (`<libro>.md`, `TOC.md`, `INDEX.md`, polyindex) non vengono prodotti.

Il campo `result` incluso nell'evento SSE terminale `done` ha la stessa struttura della precedente risposta sincrona: include `ingest_gate_phase`, `pdf_alignment` (o `null` su hash duplicato), `useful_pages_enumeration` e `stage1`.

`pdf_alignment` include il path assoluto del PDF allineato sotto `<DATA_ROOT>/input/processed` (`<source_sha256>.pdf`) e le mappe `original_page_to_aligned_page` / `aligned_page_to_original_page` (pagine 1-based).

`useful_pages_enumeration` (T10): elenco ordinato delle pagine originali utili, mappe bidirezionali allineamento 1-based, e `toc_range_aligned` / `index_range_aligned` (range TOC/INDEX proiettati sul PDF allineato).

`stage1` (T11.5): risultato dello Stage 1 OCR per le pagine utili enumerate. I file di testo grezzi vengono scritti in `data/tmp/<source_sha256>/stage1OCR/p.<NNNN>.<slug>.txt` (uno per pagina), con `<NNNN>` zero-padded sul numero pagina allineata e `<slug>` derivato dal titolo REICAT.
