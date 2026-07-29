# Architettura

## Stack

| Layer | Scelta |
|-------|--------|
| Runtime | Python ≥ 3.11 |
| HTTP | `ThreadingHTTPServer` stdlib (non FastAPI) |
| Validazione | Pydantic v2 |
| OCR | EasyOCR + PyTorch (pipeline classica); GLM-OCR opzionale |
| LLM | OpenAI SDK verso LM Studio locale o provider remoto |
| Persistenza | SQLite (`biblioteca.db`) + JSON su disco (polyindex, research) |
| UI | HTML/CSS/JS vanilla in `web/` (nessun bundler) |

## Package sotto `src/`

```
src/
├── api/           # HTTP: routing, job registry, form ingest, research, admin, etaly, chat
├── core/          # config .env, log, hashing, OpenAI client, LM Studio, retry/rate-limit
├── models/        # contratti Pydantic (Settings, IngestRequest, schemi polyindex)
├── ingestion/     # orchestratore Fase 1, PDF align, builder MD, refine TOC/INDEX
│   ├── pipeline/  # stage OCR / Vision / Editor / GLM, render PNG, VRAM
│   └── polyindex/ # sync TOC/INDEX/TIME_INDEX, subject matcher, embeddings, dedup
├── persistence/   # SQLite + audit/repair/exclude/preview pagine
├── search/        # pipeline research 2.0, catalogo articoli, lookup, postprocess
└── export/        # adapter, lint, bundle, registry E-TALY
```

### Regole di layering

- `ingestion/` non importa `api/`.
- `api/` orchestra e smista; la logica di dominio sta in `ingestion/`, `search/`, `persistence/`.
- `core/` è infrastruttura sottile (config, log, client HTTP LLM). I messaggi `Log(...)` devono essere specifici e autoreferenziali: vedi [operations.md](operations.md#logging).
- Esistono dipendenze crociate miti tra `ingestion/polyindex` e `search/` (catalogo articoli, time lookup): vanno trattate con cautela nei refactor.

## Flusso end-to-end

```
Browser (web/*.html)
    │
    ▼
src/api/ingest_http_server.py   ThreadingHTTPServer :8765
    ├─ POST /api/ingest/submit  → thread + JobRegistry + SSE
    │       └─ run_full_pipeline / run_glm_ingest_pipeline
    │               └─ orchestrator.run_pipeline
    │                       └─ stage1 → stage2 → stage3 → builders → polyindex
    ├─ POST /api/research/*     → research_handlers → search.research_runner
    ├─ /api/admin/*             → merge soggetti, audit/repair pagine, embeddings
    └─ GET static               → web/, articoli HTML, mockup
```

## Job e concorrenza

- Ogni lavoro pesante è un **job** con `job_id`, eventi SSE e snapshot `/status`.
- Semaforo ingest: `INGEST_MAX_CONCURRENT_JOBS` (default 1).
- Semaforo research: `RESEARCH_MAX_CONCURRENT_JOBS` (default 1).
- Parallelismo interno alle pagine/LLM: `MAX_PARALLEL_REQUEST`.
- I lock sul polyindex coprono solo lettura/scrittura JSON; matching LLM e embedding avvengono fuori dal lock.

## Entry point

| Comando | Modulo |
|---------|--------|
| `make run-server` | `python -m src.api.ingest_http_server` |
| `make run-mock-server` | `web/mockup/server.py` (porta 8766) |
| Test | `python -m unittest discover -s tests` |

## Documenti correlati

- Pipeline dettagliata: [ingestion.md](ingestion.md)
- API: [api.md](api.md)
- Layout dati: [data-layout.md](data-layout.md)
