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
│   ├── db/ # file SQLite del progetto
│   ├── input/ # PDF sorgente in ingresso
│   ├── library/ # artefatti globali (es. TOC.json, INDEX.json)
│   ├── output/ # output per libro (MD pagine, aggregati)
│   └── tmp/ # temporanei, cache, lavorazioni intermedie
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

