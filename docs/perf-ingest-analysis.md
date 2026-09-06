# Diagnosi prestazioni Ingest / Ingest performance diagnosis

Analisi statica del codice (settembre 2026). Non è un refactor della pipeline: serve a dare priorità misurabili.  
Repo italiano: questo documento è bilingue dove i termini di prodotto restano in italiano.

**Libro di riferimento nel workspace:** `la-grande-guida-dei-monumenti-di` — 816 pagine originali, **798 aligned**, **1681 soggetti INDEX**.

---

## 1. Architettura (sketch)

```
POST /api/ingest/submit          # EasyOCR + Vision + Editor
POST /api/ingest2/submit         # GLM-OCR (Vision combinato) + Editor
        │  202 + job_id
        ▼
thread di background  (semaforo INGEST_MAX_CONCURRENT_JOBS, default 1)
        │
        ▼
run_full_pipeline / run_glm…     # src/api/ingest_pipeline_runner*.py
  validation → gate_hash → pdf_alignment → page_enumeration
        │
        ▼
asyncio.run(run_pipeline)        # src/ingestion/orchestrator.py
  [barriera di stage: TUTTE le pagine finiscono lo stage N prima che inizi N+1]
  render PNG          SEQUENZIALE, 250 DPI, riapre il PDF per ogni pagina
  stage1_ocr          parallelo ≤ MAX_PARALLEL_REQUEST  (EasyOCR, pool di N Reader)
        oppure
  stage1_glm_ocr      parallelo ≤ MAX_PARALLEL_REQUEST  (1 chiamata vision/pagina)
  stage2_vision       parallelo ≤ MAX_PARALLEL_REQUEST  (1 LLM vision + PNG base64/pagina)
  [swap modello LM Studio vision → editor, timeout 600s]
  stage3_editor       parallelo ≤ MAX_PARALLEL_REQUEST  (1 LLM testo/pagina)
  output_writer → book.md → TOC.md → toc_refine (LLM/sezione)
                 → INDEX.md → index_refine (LLM/sezione)
  polyindex_toc       I/O JSON
  polyindex_biblio    1 LLM / pagina biblio, SEQUENZIALE

Job SEPARATI (pagina Admin «Indice»), non dentro run_pipeline:
  polyindex_index     matching soggetti (embedding batch + LLM seriale)
  index_cross_links   1 LLM / lemma regex-fail / pagina contenuto
  time_index          1 LLM / pagina SE TIME_INDEX_USE_LLM=true (default codice: false)
```

**Sync vs async:** l'HTTP risponde subito; la pipeline gira in un thread e dentro usa `asyncio` + `Semaphore`. Le chiamate OpenAI sono **sincrone** (`openai.OpenAI`) eseguite in un `ThreadPoolExecutor` di `max_parallel_request` worker. OCR e render usano `asyncio.to_thread`.

**I/O vs CPU vs LLM:**

| Fase | Dominante | Parallelismo reale |
|------|-----------|-------------------|
| PDF alignment | CPU (ProcessPool, chunk `PAGE_RANGE_PER_THREAD`) | sì |
| Render PNG | CPU + I/O disco | **no** (un pagina alla volta) |
| EasyOCR | GPU/CPU | sì, ma N Reader interi in VRAM |
| Vision / GLM / Editor | attesa API/LLM | sì, tap `MAX_PARALLEL_REQUEST` + token bucket 60/min |
| TOC/INDEX refine | attesa LLM | sì per sezione |
| BIBLIO | attesa LLM | **no** (for-loop await) |
| INDEX matcher | CPU fuzzy + embedding batch + LLM | LLM **seriale** |
| Cross-link | attesa LLM | pagine in parallelo, **lemma seriale nella pagina** |
| TIME_INDEX | CPU regex (default) o LLM/pagina | sì se LLM |

---

## 2. Ipotesi: verdetto

| Ipotesi | Verdetto | Evidenza |
|---------|----------|----------|
| Ingest serializza lavoro per-pagina che potrebbe essere parallelo | **Confermata in parte** | Dentro uno stage le pagine sono parallele (`asyncio.Semaphore`). Tra stage c'è una **barriera totale**. Il render è **completamente seriale**. BIBLIO è seriale. Il matcher INDEX è seriale. |
| Conversione PDF / formattazione MD dominano il wall time | **PDF sì (primo run), MD no** | Render 250 DPI + encoder PNG in Python puro + open/close PDF per pagina. La formattazione markdown è CPU irrilevante. Se la cache PNG/OCR/MD è calda, il render sparisce. |
| Indexing/embedding rielabora tutto il corpus | **Scartata per gli embedding; vera per i link** | Prefetch embedding a batch 64 + cache SQLite per `(canonical_id, model)`. Il matcher **non** re-embedda INDEX.json intero. I cross-link invece **rileggono tutte le pagine MD** e possono fare 1 LLM per lemma non risolto. TIME_INDEX di default è solo regex. |

---

## 3. Colli di bottiglia (con evidenza)

### B1 — Barriera di stage + 2–3 chiamate LLM per pagina (impatto massimo)

Classic, libro da 798 pagine, cache fredda:

| Stage | Chiamate | Note |
|-------|----------|------|
| page_guidance (pre-submit) | 1–2 vision | se `ai_page_guidance` è vuoto |
| Vision | **798** | immagine intera + testo OCR, `max_tokens=4096` |
| Editor | **798** | testo, `max_tokens=4096` |
| toc_refine + index_refine | O(sezioni `---`) | di solito ≪ N pagine |
| biblio | 1 × pagine del range | for-loop await |

**Totale ingest classic ≈ 1600 chiamate LLM** più refine/biblio.  
GLM-OCR elimina EasyOCR+Vision e fa **798** vision-OCR + **798** editor ≈ **1600** resta lo stesso ordine di grandezza (una vision in meno, ma GLM è comunque vision).

Wall time di ordine (ipotesi locali, non misurate qui):

- Vision locale 10–30 s/pagina, `MAX_PARALLEL=4` → **798/4 × 20 s ≈ 66 min**
- Editor locale 5–15 s → **798/4 × 10 s ≈ 33 min**
- EasyOCR GPU 3–10 s → **798/4 × 6 s ≈ 20 min**
- Render seriale 0.5–2 s → **798 × 1 s ≈ 13 min**

**Ordine di grandezza ingest a freddo: 2–3 ore** su un volume da ~800 pagine. Non è “lento perché Python”: è **O(N) round-trip LLM serializzati a 4-wide**.

File: `orchestrator.py` (`_run_stage1_phase` poi `_run_vision_editor_phases`), `stage2.py` / `stage3.py` (`refine_with_*` una volta per pagina).

### B2 — Token bucket `RATE_LIMIT_PER_MINUTE=60`

`AsyncTokenBucket` (capacity = 60, refill 1 token/s) è **condiviso per client OpenAI**. 1596 chiamate / 60 per minuto = **≥ 26.6 minuti solo di coda**, anche se il modello risponde in 1 s.

- Locale lento (15 s/call): il bucket quasi non morde (4 parallele × 4 call/min ≪ 60).
- Cloud veloce (2 s/call): **il rate limit diventa il muro**.

`RATE_LIMIT_PER_MINUTE=0` (o capacity 0) disattiva il bucket — **non è documentato in `example.env`**.

File: `src/core/rate_limit.py`, `example.env` riga `RATE_LIMIT_PER_MINUTE=60`.

### B3 — Render PNG seriale a 250 DPI, encoder Python

`DEFAULT_RENDER_DPI = 250` è **hardcoded**, nessuna variabile `.env`.  
`_render_stage1_pages_sequential` apre/chiude `PdfDocument` **per ogni pagina**.  
`_write_bitmap_png` converte BGR→RGB **pixel per pixel in Python** e fa `zlib.compress` sull'intera bitmap.

A4 @ 250 DPI ≈ 2067×2923 ≈ 18 MB RGB crudi. Loop Python su ~6e6 pixel è CPU-bound visibile. C'è cache sidecar (`.png.json`); al resume è gratis.

File: `src/ingestion/pipeline/render.py`, `stage1.py` `_render_stage1_pages_sequential`.

### B4 — EasyOCR: N Reader completi in VRAM

`prepare_parallel_pool(pool_size=max_parallel_request)` istanzia **N** `easyocr.Reader` (detector+recognizer). Con `MAX_PARALLEL_REQUEST=4` e `GPU_VRAM_MAX_USED_GB=4` il preflight VRAM e il pool si pestano i piedi. Lo stage OCR parte **solo dopo** che il render è finito.

File: `src/ingestion/pipeline/engine.py`.

### B5 — Job INDEX / hyperlink (sibling, spesso “sembra ingest lento”)

Dopo l'ingest, l'operatore lancia Indice. Sul libro campione (**1681 soggetti**, **798 pagine**):

- Embedding: batch 64, cache SQLite — **non** rielabora il corpus.
- `match_subject`: **for-loop sincrono**. Fuzzy su tutti i soggetti globali (O(S_libro × S_global)). LLM di arbitrato **una chiamata per soggetto borderline** (soglia 0.82–0.92).
- Cross-link: per ogni pagina di contenuto, **per ogni lemma che il regex non trova**, una chat completion che rimanda **tutta la pagina**. Peggiore: migliaia di LLM (es. 5 miss/pagina × 780 ≈ **3900**).

File: `index_json.py` (loop `for raw_subject`), `index_cross_links.py` + `index_page_subject_links.py`.

### Altri costi (secondari ma reali)

- **Swap LM Studio** tra Vision ed Editor (`LM_STUDIO_SWAP_MODELS=true`, timeout 600 s): minuti morti + VRAM churn.
- **Pagine TOC/INDEX** passano comunque da OCR+Vision+Editor (`useful_original_pages` = tutte le pagine non rimosse), poi vengono rifinite di nuovo come aggregati.
- **Retry** `RETRY_ATTEMPTS=3` (example) con backoff 0.5–10 s; ogni retry ri-paga il rate limit.
- **Prompt + PNG rilette da disco** a ogni pagina (I/O piccolo rispetto all'LLM).
- **`INGEST_MAX_CONCURRENT_JOBS=1`**: un libro alla volta — corretto se c'è una GPU, ma due ingest non si accavallano.

---

## 4. Config: knob esistenti, default storti, knob assenti

| Knob | Default `Settings` | `example.env` | Nota |
|------|--------------------|---------------|------|
| `MAX_PARALLEL_REQUEST` | **2** | **4** | Il default codice è più conservativo del template. Alzare è il primo esperimento. |
| `RATE_LIMIT_PER_MINUTE` | 60 | 60 | `0` = illimitato (codice). Per locale veloce/cloud è troppo basso. |
| `INGEST_MAX_CONCURRENT_JOBS` | 1 (env HTTP) | 1 | OK su una GPU. |
| `OCR_USE_GPU` | **false** | **true** | Default Settings vs template divergono. |
| `GPU_VRAM_MAX_USED_GB` | 4.0 | 4 | Stretto se EasyOCR×4 + LM Studio. |
| `TIME_INDEX_USE_LLM` | **false** | false | `docs/configuration.md` dice default `true` — **docs sbagliata**. |
| `LM_STUDIO_SWAP_MODELS` | true | true | Necessario se i modelli differiscono; costo alto. |
| `TMP_KEEP_AFTER_SUCCESS` | true | true | La cache stage è il motivo per cui i resume sono veloci. **Non spegnere** per “pulizia” in debug perf. |
| `RETRY_ATTEMPTS` | 2 | 3 | |
| `TIMEOUT_SECONDS` | 120 | 120 | Vision locale su pagina densa può andare in timeout → retry. |
| **`RENDER_DPI`** | — | — | **Assente.** Hardcoded 250. |
| **`EASY_OCR_POOL_SIZE`** | — | — | Accoppiato a `MAX_PARALLEL_REQUEST`. |

`GET /api/admin/llm-metrics` e `pipeline_runs.timing_json` esistono già. Mancavano (fino a questo PR) i `duration_ms` **per pagina** su render/OCR/Vision/Editor.

---

## 5. Top 5 ottimizzazioni (impatto × sforzo)

| # | Azione | Impatto atteso | Sforzo | Perché |
|---|--------|----------------|--------|--------|
| **1** | **Pipeline a pagina** (Vision/Editor sulla pagina *k* appena l'OCR di *k* è pronto) **oppure** default GLM-OCR e saltare EasyOCR+Vision | Alto (taglia la somma delle barriere) | Medio–alto | Oggi wall ≈ Σ(stage). Una coda per-pagina avvicina wall ≈ max(stage) / P. GLM già toglie uno stage LLM. |
| **2** | **Render in parallelo** + encoder PNG nativo (Pillow/`pdfium` save) + knob `RENDER_DPI` (150–200) | Alto sul primo run; nullo in cache | Basso–medio | Oggi seriale + loop Python su ~6e6 pixel/pagina. Esperimento: cronometrare 20 pagine a 150 vs 250. |
| **3** | **`RATE_LIMIT_PER_MINUTE=0` in locale**; alzare `MAX_PARALLEL_REQUEST` solo se VRAM basta; **pool EasyOCR = 1** (o 2) indipendente dal parallelismo LLM | Alto se il bucket o il pool GPU mordono | Basso | Solo `.env`. Verificare con i nuovi `wait_ms` nei log e `duration_ms` SSE. |
| **4** | INDEX: **LLM matcher in parallelo**; cross-link **1 LLM / pagina** (tutti i lemmi miss insieme), non 1 / lemma | Alto sul job Indice | Medio | 1681 soggetti seriali + N miss × M pagine è il secondo mostro dopo Vision. Embedding è già a posto. |
| **5** | **Saltare Vision+Editor su TOC/INDEX/biblio**; non auto-lanciare page_guidance se le note sono vuote; stesso modello vision/editor per evitare swap | Medio | Basso | 14+ pagine × 2 LLM sul campione; swap può costare minuti. |

Non fare ora: riscrivere i prompt, cambiare chunking (non c'è chunking pagina: è 1 pagina = 1 call), “reindex corpus” (gli embedding non sono il problema).

---

## 6. Esperimenti / benchmark successivi

1. **Dopo un ingest reale** (anche 10–20 pagine):  
   `python -m scripts.summarize_ingest_perf`  
   e `GET /api/admin/llm-metrics`. Confrontare `phases.render` vs `stage2_vision` vs `stage3_editor`.
2. **SSE / log:** ogni `page_progress` ora ha `duration_ms`. Distribuzione p50/p95 per fase.
3. **Rate limit:** nei log `chat_completion rate limiter wait done` c'è `wait_ms`. Se p50(wait) ≫ 0, alzare o azzerare il bucket.
4. **A/B DPI:** 20 pagine a 150 vs 250 — tempo render, size PNG, qualità Vision (valutazione umana su 5 pagine).
5. **Contare matcher LLM:** log `subject matcher` / metriche `subject_matcher_llm` vs `n_new`/`n_match`.
6. **Ipotesi swap:** stesso `VISION_MODEL`=`EDITOR_MODEL` su 30 pagine vs modelli diversi — delta su `LM_STUDIO_LOAD_TIMEOUT`.
7. **Microbench encoder:** 1 pagina, `_write_bitmap_png` vs `PIL.Image.save`. Se il ratio è >5×, il fix PNG è “tiny e ovvio”.

### Come leggere i numeri

- `pipeline_runs.timing.phases.*.seconds` < 10 su decine di pagine = **cache hit**, non throughput modello (`llm_metrics` lo marca già `legacy_cached`).
- `logical_calls ≈ n_pages` per stage2/3 = cache fredda.
- `pages_per_second` da metriche LLM è throughput **del modello**, non del render/OCR.

---

## 7. Strumentazione aggiunta in questo PR

- `duration_ms` sugli eventi SSE `page_progress` / `page_failed` / `page_skipped` di render, OCR, GLM-OCR, Vision, Editor.
- `toc_refine` / `index_refine` emettono `started`/`completed` → compaiono in `timing.phases`.
- Log `wait_ms` sul token bucket.
- `python -m scripts.summarize_ingest_perf` legge `pipeline_runs` + `llm_call_metrics` e stampa un report testuale.
