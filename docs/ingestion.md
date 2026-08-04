# Pipeline di ingestione

## Contratto di input

Schema Pydantic `IngestRequest` in `src/models/request.py` (versione `1.0`). Validazione e arricchimento: `validate_and_enrich_request` calcola subito `source_sha256`.

Campi principali:

- `source_pdf_path` — PDF sorgente
- `pages_to_remove` — pagine 1-based da eliminare (non possono sovrapporsi a TOC/INDEX)
- `toc_range` / `index_range` — intervalli `{start, end}`
- `reicat` — metadati (almeno `titolo` + un `autore`)
- `notes`, `index_notes`, `page_notes` — note propagate ai prompt LLM delle fasi giuste
- `options` — es. `force_metadata_update_on_duplicate_hash`

UI: form multipart su `POST /api/ingest/submit` (EasyOCR) o `POST /api/ingest2/submit` (GLM).

## Ordine delle fasi

```
validation → gate_hash → pdf_alignment → page_enumeration
  → stage1_ocr (o stage1_glm_ocr)
  → stage2_vision          # solo pipeline classica
  → stage3_editor
  → output_writer → book_md → toc → toc_refine → index → index_refine
  → polyindex_toc → polyindex_index → time_index
```

Implementazione: `src/api/ingest_pipeline_runner.py` (+ `_glm`) → `src/ingestion/orchestrator.py`.

### Validation

Controlla PDF leggibile, range, REICAT; calcola SHA-256.

### Gate hash

`source_hash_gate` su SQLite. Se l'hash è già in `books`, la pipeline viene saltata (salvo opzioni di force metadata).

### PDF alignment

Rimuove `pages_to_remove`, scrive `data/input/processed/<sha>.pdf`, costruisce mapping aligned↔original.

### Page enumeration

Elenca le pagine utili da processare (escluse TOC/INDEX/rimosse secondo le regole di prodotto).

### Stage 1 — OCR (classica)

EasyOCR sulle PNG renderizzate → `tmp/<sha>/stage1OCR/p.NNNN.<slug>.txt`.

### Stage 1 — GLM OCR (variante)

`/api/ingest2/submit` e `web/index2.html`: OCR+markdown combinati (`glm_ocr_stage`), poi Editor. Nessuno Stage 2 Vision separato.

### Stage 2 — Vision

LLM multimodale raffina OCR con immagine pagina → `stage2Vision/*.md`. Usa `VISION_MODEL` e prompt in `pipeline/prompts/`.

### Stage 3 — Editor

LLM testo raffina il markdown → `stage3Editor/*.md`. Usa `EDITOR_MODEL`.

### Artefatti libro

- Copia pagine in `output/<sha>/pages/`
- `manifest.json`
- `<slug>.md`, `TOC.md`, `INDEX.md`
- Refine LLM di TOC/INDEX (`stage4TocIndexRefine`)

### Polyindex

Sync in `TOC.json`, `INDEX.json` (subject matching), `TIME_INDEX.json`. Dettagli: [polyindex.md](polyindex.md).

## Progresso e job

`POST /api/ingest/submit` risponde **202** con:

```json
{
  "ok": true,
  "job_id": "<hex>",
  "events_url": "/api/ingest/<job_id>/events",
  "status_url": "/api/ingest/<job_id>/status"
}
```

SSE: `GET …/events` (`Content-Type: text/event-stream`, supporta `Last-Event-ID`).  
Eventi tipici: `pipeline_total`, `started`/`completed`/`error` per fase, `page_progress`, terminale `done` o `error`.

`global_total` ≈ `1` (alignment se eseguito) + `3 × N` pagine (OCR+Vision+Editor) nella pipeline classica.

## Note e prompt

| Campo form | Dove arriva |
|------------|-------------|
| `notes` / `index_notes` / `page_notes` | Solo input per generare `ai_page_guidance` (manuale o auto al submit) |
| `ai_page_guidance` | Unico testo aggiunto ai system prompt LLM dell'ingest (Vision/GLM/Editor, refine TOC/INDEX, matcher, TIME_INDEX, biblio) |
| `annotations_json` | Solo per generazione consiglio AI (immagini annotate + sample); non entra nella pipeline pagina |

## Repair e manutenzione post-ingest

Dall'admin (`web/admin.html`):

- audit pagine (`/api/admin/book-pages-audit`)
- repair singola / repair-all
- exclude pagina da indici
- edit transcript + confirm verso stage3/output

## Preflight

Prima di operazioni pesanti la dashboard chiama `GET /api/system/preflight?operation=ingest` (o `repair`, …): verifica VRAM e può caricare il modello LM Studio richiesto.
