# Research (Fase 2)

Pipeline versione **2.0** in `src/search/research_runner.py`. Produce articoli su soggetti POH a partire da query e dagli indici già costruiti.

## Flusso `run_research`

1. Carica `INDEX.json`, `TOC.json`, `TIME_INDEX.json`.
2. **Subject lookup** — risolve query/POH in soggetti e pagine.
3. **Chapter expand** — allarga il contesto usando i capitoli TOC.
4. **Time lookup** — aggiunge pagine da anni/date rilevanti.
5. **Collect** — legge Markdown da `data/output/<sha>/pages/`.
6. **Filter** — tiene le pagine rilevanti alla query.
7. **Generate article** — LLM stile enciclopedia con citazioni `source:<sha>:aligned:<n>`.
8. **POH links** — inserisce link `[label](poh:<id>)` verso altri soggetti.
9. **Timeline** — sezione `## Cronologia`.
10. **Finalize + postprocess** — verifica citazioni/link, scrive MD/HTML.

## Persistenza articoli

```
data/research/
├── catalog.json
└── articles/
    ├── <poh_id>.md
    └── <poh_id>.html
```

Serviti in lettura da `GET /articolo/<name>.html`.

## API principali

| Endpoint | Ruolo |
|----------|-------|
| `POST /api/research/submit` | Avvia research async (dedup / 429 se saturato) |
| `GET /api/research/<id>/status` | Stato job |
| `GET /api/research/<id>/events` | SSE |
| `GET /api/research/<id>/article` | Payload articolo se succeeded |
| `GET /api/research/search?q=` | Search catalogo + POH |
| `GET /api/research/missing` | POH senza articolo |
| `POST /api/research/generate` | Batch generazione |
| `POST /api/research/generate/resume` | Riprendi batch |
| `POST /api/research/generate/abort` | Abort batch |
| `POST /api/research/merge-article` | Integra materiale nuovo in articolo esistente |

Dettaglio completo: [api.md](api.md).

## Batch generation

`POST /api/research/generate` crea un job padre con figli per ogni POH mancante (o lista richiesta). Stato e progresso via job registry / history. Worker sequenziale con limite `RESEARCH_MAX_CONCURRENT_JOBS`.

## Chat (dashboard / ricerca)

`POST /api/chat/completions` — API OpenAI-compatibile con `stream: true` e tool:

- `search` — ricerca sul catalogo/INDEX
- `readSource` — legge pagina sorgente
- `offerArticleGeneration` — propone generazione articolo

Preflight tipico: `operation=research` (alias `chat`).

## Export E-TALY

Flusso separato in `src/export/` + UI `web/etaly_export.html`:

1. `propose` — metadata LLM
2. `confirm` — salva mapping
3. `build` — ZIP download (non scrive su destinazione E-TALY remota)

## UI

- `web/ricerca.html` — ricerca/generazione standalone
- `web/dashboard.html` — lab con Google-like search + chat Perplexity-like + embed
- `web/admin.html` — sezione generazione articoli mancanti, audit salute articoli
