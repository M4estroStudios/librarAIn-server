# API HTTP

Server: `src.api.ingest_http_server` su **`<INGEST_HTTP_HOST>:<INGEST_HTTP_PORT>`** (default `127.0.0.1:8765`).

Convenzioni:

- JSON: `{ "ok": true|false, … }`
- Job async: risposta **202** + `job_id` + URL status/events
- POST cross-origin → **403** (vedi [security.md](security.md))
- Nessuna autenticazione

## System

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/health` | Liveness |
| GET | `/api/system/preflight?operation=` | VRAM + load modello (`ingest`, `research`, `research-merge`, `repair`, `repair-all`; `chat`→`research`) |
| GET | `/api/system/status` | VRAM, modelli, job attivi, summary research |
| GET | `/api/system/jobs` | Job attivi |
| GET | `/api/system/jobs/history` | Storico |
| GET | `/api/system/jobs/<id>` | Summary |
| GET | `/api/system/jobs/<id>/events` | SSE unificato |

## Ingest

| Metodo | Path | Descrizione |
|--------|------|-------------|
| POST | `/api/ingest/submit` | Multipart PDF+form → pipeline EasyOCR |
| POST | `/api/ingest2/submit` | Multipart → pipeline GLM-OCR |
| POST | `/api/ingest/reicat-suggest` | Suggest REICAT da Vision |
| GET | `/api/ingest/<job_id>/status` | Snapshot JSON |
| GET | `/api/ingest/<job_id>/events` | SSE (`Last-Event-ID` supportato) |

## Research

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/api/research/search?q=` | Search POH/articoli |
| GET | `/api/research/books` | Libri ingestiti |
| GET | `/api/research/books/meta` | Meta + viewer_pages (`source_sha256`) |
| GET | `/api/research/book-pages/render` | PNG pagina |
| GET | `/api/research/status` | Summary catalogo |
| GET | `/api/research/missing` | POH senza articolo |
| GET | `/api/research/articles/audit` | Health articoli |
| GET | `/api/research/poh-overlaps?book_sha=` | Overlap POH |
| GET | `/api/research/<id>` | Status job |
| GET | `/api/research/<id>/article` | Articolo se succeeded |
| GET | `/api/research/<id>/events` | SSE |
| GET | `/api/research/generate/status?job_id=` | Status batch |
| POST | `/api/research/submit` | Avvia research |
| POST | `/api/research/generate` | Batch articoli |
| POST | `/api/research/generate/resume` | Resume batch |
| POST | `/api/research/generate/abort` | Abort batch |
| POST | `/api/research/merge-article` | Merge in articolo esistente |

## Admin soggetti / embeddings

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/api/admin/subjects` | Soggetti (`min_books`) |
| GET | `/api/admin/subject?canonical_id=` | Dettaglio |
| POST | `/api/admin/subjects/merge` | Merge POH |
| POST | `/api/admin/subject/update` | Alias / time_range |
| POST | `/api/admin/subject/pages` | Aggiorna pagine |
| POST | `/api/admin/subject/book/remove` | Rimuove libro da soggetto |
| POST | `/api/admin/subject/delete` | Cancella soggetto |
| GET | `/api/admin/subjects/dedup/suggestions` | Cluster aperti |
| POST | `/api/admin/subjects/dedup/scan` | Scan async |
| POST | `/api/admin/subjects/dedup/dismiss` | Dismiss |
| GET | `/api/admin/embeddings/status` | Coverage embeddings |
| POST | `/api/admin/embeddings/generate` | Backfill job |

## Admin pagine libro

| Metodo | Path | Descrizione |
|--------|------|-------------|
| GET | `/api/admin/book-pages-audit` | Audit (`source_sha256` opzionale) |
| GET | `/api/admin/book-pages/render` | PNG |
| GET | `/api/admin/book-pages/transcript` | Leggi transcript |
| POST | `/api/admin/book-pages/transcript` | Salva transcript |
| POST | `/api/admin/book-pages/transcript/confirm` | Conferma → stage3/output |
| POST | `/api/admin/book-pages/exclude` | Escludi pagina |
| POST | `/api/admin/book-pages/repair` | Repair pagina |
| POST | `/api/admin/book-pages/repair-all` | Repair gap |

`source_sha256` deve essere digest hex a 64 caratteri.

## Chat / E-TALY

| Metodo | Path | Descrizione |
|--------|------|-------------|
| POST | `/api/chat/completions` | Chat streaming + tools |
| GET | `/api/etaly/export/list` | Lista slug/mapping |
| POST | `/api/etaly/export/propose` | Proposta metadata |
| POST | `/api/etaly/export/confirm` | Conferma mapping |
| POST | `/api/etaly/export/build` | Build ZIP |

## Pagine statiche (selezione)

`/`, `/index.html`, `/index2.html`, `/dashboard`, `/admin`, `/jobs`, `/ricerca`, `/etaly-export`, `/articolo/<name>.html`, `/mockup/*`.

## Esempio curl

```bash
# health
curl -s http://127.0.0.1:8765/health

# SSE ingest
curl -N http://127.0.0.1:8765/api/ingest/<job_id>/events

# search
curl -s "http://127.0.0.1:8765/api/research/search?q=Marco%20Polo"
```
