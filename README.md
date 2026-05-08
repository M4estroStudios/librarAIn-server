# librarAIn-server

Server e pipeline per ingestione libri, persistenza metadati e (fase successiva) ricerca. Requisiti di prodotto della Fase 1: `[PRD-Fase1.md](PRD-Fase1.md)`.

## Struttura del repository (bilanciata)

Obiettivo: **tre pilastri** chiari (ingestione, ricerca, dati) senza minimalismo sterile e senza labirinti di sottocartelle. Poche cartelle di primo livello sotto `src/`, massimo **un livello** di annidamento oltre quello.

### Principi

- **`ingestion/`**: tutta la Fase 1 (PDF → MD, TOC/INDEX, matching AI per `INDEX.json`).
- **`search/`**: Fase 2 (ricerca e generazione articoli) — cartella dedicata fin da subito così il codice non si mescola con l’ingestione; all’inizio può contenere solo entrypoint/stub o essere vuota fino all’implementazione.
- **`persistence/`** (o `data_layer/`): accesso a SQLite, file JSON di biblioteca, registro hash — “dove stanno i dati” lato codice, separato dalla pipeline.
- **`core/`**: configurazione `.env`, logging, costanti condivise — sottile, non una copia del progetto.
- **`api/`**: HTTP/CLI che smista verso `ingestion` e (poi) `search`, senza logica di dominio pesante.
- **`data/` (root)**: solo file su disco (input PDF, output libri, `library.db`, `TOC.json` / `INDEX.json`) — non confonderla con `src/...`.

### Albero indicativo (solo cartelle, con commenti)

```text
librarAIn-server/ # radice repository
├── src/ # tutto il codice applicativo
│   ├── api/ # entrypoint HTTP/CLI verso ingestione e ricerca
│   ├── core/ # configurazione .env, logging, shared di base
│   ├── ingestion/ # pipeline Fase 1: PDF → MD, TOC/INDEX, merge artefatti
│   │   └── ocr/ # stadi OCR, Vision, Editor e run per pagina
│   ├── models/ # tipi e contratti condivisi tra moduli
│   ├── persistence/ # SQLite, JSON biblioteca, hash/stato run
│   └── search/ # Fase 2: ricerca e generazione articoli (stub o futuro)
├── data/ # solo dati su disco, mai codice
│   ├── db/ # database principale e snapshot checkpoint
│   │   ├── checkpoints/ # snapshot/versioni storiche del db
│   │   │   ├── library.<yyyy>.<mm>.<dd>.db # checkpoint giornaliero
│   │   │   └── ... # altri checkpoint (es. più date/versioni)
│   │   └── library.db # database SQLite corrente
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
- `src/persistence/` è l’unico posto in cui si concentra SQLite + JSON di biblioteca + (se serve) tracciamento run/hash.
- `src/core/config.py` legge solo `.env` / `example.env`.
- `data/` non contiene codice, solo artefatti runtime.
- `web/index.html` (o nome equivalente) resta il punto unico di input coerente per l’operatore.

## Configurazione runtime `.env` (T3)

La configurazione runtime centralizzata è gestita da:

- `src/models/settings.py`: modello Pydantic `Settings`.
- `src/core/config.py`: loader `load_settings(env_file=".env")`.
- `example.env`: template di riferimento per le variabili.

`load_settings` carica prima il file `.env`, poi applica override da variabili d’ambiente già presenti nel processo.

### Variabili obbligatorie

- `DATA_ROOT` (root runtime dati, es. `data`)
- `OPENAI_PROVIDER` (`local` oppure `remote`)

### Variabili opzionali (con default)

- `MAX_PARALLEL` (default `2`)
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

`sqlite_path` viene derivato automaticamente come `<DATA_ROOT>/db/library.db`.

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

