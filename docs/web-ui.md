# Interfaccia web

UI vanilla in `web/` (nessun build step). Servita dallo stesso processo HTTP del server.

## Pagine

| URL | File | Scopo |
|-----|------|-------|
| `/` o `/index.html` | `web/index.html` | Form ingest **EasyOCR** + progresso SSE + audit |
| `/index2.html` | `web/index2.html` | Form ingest **GLM-OCR** |
| `/dashboard` | `web/dashboard.html` + `web/dashboard/*` | Lab unificato |
| `/admin` | `web/admin.html` | Merge POH, dedup, audit/repair pagine, embeddings, articoli |
| `/jobs` | `web/jobs.html` | Monitor job / history |
| `/ricerca` | `web/ricerca.html` | Ricerca e chat standalone |
| `/etaly-export` | `web/etaly_export.html` | Export E-TALY |
| `/articolo/<id>.html` | (generato in `data/research/articles/`) | Articolo pubblicato |

## Dashboard lab

`web/dashboard.html` incorpora:

- ricerca Google-like (`/api/research/search`)
- chat Perplexity-like (`/api/chat/completions`)
- iframe ingest (`/index.html?embed=1`) e admin (`/admin.html?embed=1`)
- gate preflight (`/api/system/preflight`)
- toggle mock (fixture sotto `/mockup/fixtures/`)

Moduli JS: `web/dashboard/*.js` (api, gate, jobs, search, mock, …).

## Admin

Funzioni tipiche:

- elenco e merge soggetti multi-libro
- editor polyindex (alias, pagine, time range)
- scan dedup soggetti
- audit gap pagine + repair / exclude / transcript
- backfill embeddings
- generazione articoli mancanti

## Mock

- Client-side: checkbox mock nella dashboard (hook su `fetch`)
- Server mock standalone: `make run-mock-server` → porta **8766**
- Fixture JSON/HTML in `web/mockup/fixtures/`

Il server di produzione serve anche `/mockup/*` come statici: utili in lab, non sono un secondo backend.

## Note operative UI

- Modalità embed: `?embed=1` nasconde chrome e comunica altezza al parent via `postMessage`.
- I form ingest accettano note separate (`notes`, `index_notes`, `page_notes`).
- Job lunghi: preferire SSE (`events`) allo polling aggressivo di `/status`.
