## Prompt per ciascun sottotask

> Convenzione: ogni prompt è auto-contenuto, indica il file/i target, i contratti già presenti da rispettare e il livello di test atteso. Pensali come messaggi singoli da incollare nel rispettivo modello.

### PRE-A — Sonnet — `ProcessPoolExecutor` in `pdf_alignment.py`
```text
Repo: librarAIn-server. Modifica src/ingestion/pdf_alignment.py.
Obiettivo: il merge a chunk usa ThreadPoolExecutor su pypdf, che è CPU-bound puro Python e quindi GIL-bound; sostituiscilo con ProcessPoolExecutor.
Vincoli:
- Mantieni firma e contratto pubblico: build_aligned_pdf, maybe_run_pdf_alignment, build_page_removal_mapping, _alignment_chunk_specs.
- Mantieni l'errore IngestInputValidationError(code=PDF_ALIGNMENT_FAILED, ...) serializzato come oggi.
- Aggiungi una soglia: se len(chunk_specs) == 1 OPPURE original_page_count <= 32, esegui sequenzialmente nel processo corrente (evita overhead di spawn).
- Worker count: max(1, min(len(chunk_specs), os.cpu_count() or 4)).
- _write_aligned_pdf_chunk DEVE essere top-level (già lo è) e accettare solo tipi picklable (lo è già).
- Aggiorna i test in tests/test_pdf_alignment.py se necessario, mantenendo le asserzioni esistenti; aggiungi UN test che forzi il ramo multi-chunk con pages_to_remove non vuoto e verifichi il page count finale.
- Non introdurre nuove dipendenze.
- Aggiorna README solo se cambia un comportamento osservabile dall'utente (non dovrebbe).
Definition of done: tutti i test esistenti passano (`make test`), il nuovo test passa, niente import di concurrent.futures.ThreadPoolExecutor rimanente.
```
OK

### PRE-B — Sonnet — `_schema_version` + migrations
```text
Repo: librarAIn-server. Modifica src/persistence/book_sqlite.py.
Obiettivo: sostituire la migrazione informale di `_ensure_books_legacy_columns` con uno schema versioning esplicito.
Implementa:
- Tabella `_schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)`.
- Funzione `apply_migrations(conn) -> int` che applica in ordine le migration registrate in una lista MIGRATIONS = [(1, sql_or_callable), (2, ...)] e ritorna la versione corrente.
- Migration 1 = CREATE TABLE books + book_metadata_audit nello stato attuale.
- Migration 2 (se necessaria) = aggiunta colonne legacy oggi gestite a runtime, in modo idempotente.
- `init_books_schema(sqlite_path)` deve solo aprire la connessione, garantire `_schema_migrations`, e chiamare `apply_migrations`.
- Mantieni tutta l'API pubblica esistente (init_books_schema, source_hash_gate, upsert_book_reicat, run_ingest_gate_phase, insert_book_minimal, verify_source_pdf_digest_matches).
- Aggiungi 2 unit test in tests/test_request_validation.py (o nuovo file `tests/test_book_sqlite_migrations.py`):
  1) DB vuoto -> dopo init_books_schema, _schema_migrations contiene la versione finale.
  2) DB pre-esistente con tabella books legacy -> migrations applicate idempotentemente, dati preservati.
- Niente Alembic, solo Python+sqlite3 stdlib.
DoD: i test esistenti passano, i nuovi test passano, `_ensure_books_legacy_columns` non è più chiamato direttamente fuori dalle migrations.
```
OK

### PRE-C — Composer 2 — `pyproject.toml` + dependencies
```text
Repo: librarAIn-server. 
Crea `pyproject.toml` con:
- [project] name="librarain-server", requires-python=">=3.12,<3.14", version="0.1.0".
- dependencies = runtime completo del prodotto: lista da requirements.txt (annotated-types, pypdf, pydantic, pydantic-core, typing-inspection, typing-extensions) + dipendenze necessarie alla pipeline completa (fastapi>=0.115, uvicorn>=0.30, python-multipart>=0.0.9, openai>=1.50, Pillow>=10, pypdfium2>=4.30, easyocr>=1.7).
- [project.optional-dependencies]:
  - dev = ["pytest>=8", "ruff>=0.6"]
- Non introdurre mypy in questa fase.
- [tool.ruff] line-length=100, lint.select=["E","F","I","B","UP"], lint.ignore=["E501"], target-version="py312".
- [tool.pytest.ini_options] testpaths=["tests"], addopts="-ra -q".
Aggiorna Makefile:
- `setup-env` deve fare `pip install -e .[dev]` perché il runtime completo è in [project].dependencies.
- `test` deve usare `pytest`.
- Aggiungi target `lint` -> `ruff check src tests`.
Aggiorna `requirements.txt` con un commento "see pyproject.toml" mantenendolo come pinning runtime minimo (oppure svuotalo lasciando una riga di puntamento).
Aggiorna README sezione setup di conseguenza.
DoD: `make setup-env && make test && make lint` hanno senso e sono coerenti; nessun riferimento a Python 3.13 contraddice pyproject.
```
OK

### PRE-D — Opus — Patch del PRD
```text
Repo: librarAIn-server. Aggiorna PRD-Fase1.md preservando struttura ed executive summary.
Modifiche puntuali richieste:
1) §3 e §4: sostituisci ogni riferimento a `/Users/oni/Desktop/RagAIO.py` con riferimenti in-repo. Introduci come fonte di verità il modulo `src/ingestion/pipeline/` e il documento `docs/reference_ocr_pipeline.md` (da creare con uno scheletro a parte).
2) §4 Architecture: aggiungi un sotto-paragrafo "Modello di esecuzione" che fissa:
   - POST /api/ingest/submit ritorna immediatamente {request_id, status:"accepted", source_sha256?}.
   - GET /api/ingest/{id} ritorna stato pipeline (accepted|running|succeeded|failed|skipped_duplicate) + pipeline_version + ultimi N eventi.
   - GET /api/ingest/{id}/artifacts elenca path relativi a data/output/<sha256>/.
   - Backend job MVP: in-process via asyncio TaskGroup; nessun broker esterno.
3) §4 Security & Privacy: aggiungi vincolo "log strutturato JSON, nessuna chiave/api_key mai loggata, mai stampato il contenuto OCR oltre 200 caratteri".
4) §5 MVP: aggiungi bullet "Telemetria minima: tabella pipeline_runs + logging strutturato per ogni stage/pagina".
5) §7 Backlog: aggiungi
   - [ ] T18.5 — Refactor HTTP a modello async + job_id (FastAPI/Starlette, upload streaming).
   - [ ] T21 — Smoke E2E reale via HTTP (PDF 4-6 pagine, endpoint LLM mock locale, asserzioni su TOC.md/INDEX.md).
6) §6: nessuna modifica al rinvio al README.
Vincoli:
- Non introdurre nuove sezioni numerate diverse da quelle esistenti.
- Italiano coerente con il resto del PRD.
- Ogni nuovo bullet deve restare misurabile (acceptance criteria style).
DoD: il PRD aggiornato è coerente, non contiene path utente assoluti, esplicita il modello async e include T18.5/T21 nel backlog con stato [ ].
```
MOT OK (ho una lib di logging già io)

### T18.5 — Opus — HTTP async + job model (4 sub-prompt)

**T18.5(a) — Bootstrap FastAPI**
```text
Repo: librarAIn-server. Sostituisci src/api/ingest_http_server.py con un'app FastAPI in src/api/app.py + uno script src/api/main.py per uvicorn.
- Endpoint health GET /health -> {ok: true, version}.
- Servire web/index.html su GET / (StaticFiles o FileResponse).
- Niente endpoint di ingest in questo step (placeholder /api/ingest/submit -> 501).
- Settings: leggere INGEST_HTTP_HOST, INGEST_HTTP_PORT come oggi.
- Aggiorna Makefile run-server -> `uvicorn src.api.app:app --host $(HOST) --port $(PORT)`.
- Aggiungi tests/test_app_health.py con TestClient di FastAPI.
- Mantieni requirements.txt/pyproject coerenti (PRE-C).
DoD: `make run-server` parte, GET / e GET /health rispondono, test passa.
```
ON HOLD (non necessario adesso)

**T18.5(b) — Submit con upload streaming**
```text
Repo: librarAIn-server. Implementa POST /api/ingest/submit in src/api/app.py usando UploadFile (python-multipart) STREAMING su disco.
- Salva il PDF a chunk in <DATA_ROOT>/input/raw/<token>_<safe_name>.pdf, mai tutto in RAM.
- Accetta tutti i campi form già supportati da build_ingest_payload_from_form (T1 schema).
- Riusa build_ingest_payload_from_form da src/api/ingest_http_server.py (estrai in src/api/form_mapping.py senza cambiarne il comportamento; aggiungi __init__.py adeguati).
- A fronte di errori di parsing/validazione, ritorna 400 con il modello IngestInputValidationError (alias coerenti).
- In questo step non avvii ancora la pipeline async: ritorna 202 con {request_id: uuid, source_pdf_path, status: "validated"}.
- Aggiungi test tests/test_submit_streaming.py con TestClient + UploadFile finto (PDF 1MB generato con pypdf).
DoD: PDF da 50MB salvato senza esplodere la RAM; test passa.
```
? (da approfondire)

**T18.5(c) — Job model in-process**
```text
Repo: librarAIn-server. Crea src/api/job_registry.py con:
- IngestJob(BaseModel): request_id, source_sha256?, status: Literal["accepted","running","succeeded","failed","skipped_duplicate"], created_at, updated_at, pipeline_version, last_error?, events: list[IngestJobEvent].
- IngestJobEvent: at, level, stage, message, payload?
- JobRegistry: dict thread-safe (asyncio.Lock o threading.RLock) con add/update/append_event/get/list_recent.
- run_pipeline_in_background(request_id, enriched, settings, registry, sqlite_path): coroutine che oggi esegue solo gate phase + alignment + enumeration (richiama codice già scritto) e marca succeeded; tutta la sequenza è incapsulata e ogni step pubblica un IngestJobEvent.
Aggiorna POST /api/ingest/submit:
- Dopo la validate, registra il job in stato "accepted" e schedula run_pipeline_in_background con asyncio.create_task (o anyio.from_thread se servono blocking call); ritorna 202 con request_id immediatamente.
Test: tests/test_job_registry.py copre add/update/append/get; tests/test_submit_async.py: la POST ritorna in <200ms anche con un mock di pipeline che dorme 2s.
DoD: la POST non blocca mai sul lavoro pesante; lo stato del job evolve.
```
OK

**T18.5(d) — Status + artifacts endpoints**
```text
Repo: librarAIn-server. Aggiungi a src/api/app.py:
- GET /api/ingest/{request_id} -> IngestJob completo (404 se non trovato).
- GET /api/ingest/{request_id}/events -> ultimi N eventi paginati.
- GET /api/ingest/{request_id}/artifacts -> {output_dir, files: [relpath]}, costruito da data/output/<source_sha256>/ se esistente; 409 se status != "succeeded".
Vincoli:
- Mai esporre path assoluti contenenti l'utente di sistema; relativizza a DATA_ROOT.
- Tutte le response JSON serializzano via Pydantic (mode="json", by_alias=True).
Test:
- tests/test_status_endpoints.py: dopo una submit, polling restituisce stato finale entro N secondi; il caso 404/409 è coperto.
DoD: contratto del PRD §4 "Modello di esecuzione" rispettato.
```
OK** (non veramente necessario ma comodo)

### T11 — Sonnet — Stage OCR base (3 sub-prompt)

**T11(a) — Scelta motore + wrapper**
```text
Repo: librarAIn-server. Crea src/ingestion/pipeline/__init__.py e src/ingestion/pipeline/engine.py.
- Definisci Protocol OCRPageEngine: ocr_page(image_path: Path, *, lang: list[str]) -> str (testo grezzo).
- Implementazione di default EasyOCRPageEngine (lazy import easyocr; cache reader per (lang_tuple, gpu)).
- Configurazione lingua via settings: aggiungi a src/models/settings.py il campo OCR_LANGUAGES (default "it,en", normalizzato in list[str]) e OCR_USE_GPU (bool, default False).
- Aggiungi test tests/test_ocr_engine.py con un fake engine che ritorna "page text" per verificare il contratto (Protocol-friendly), niente download di modelli reali nei test.
DoD: easyocr non viene mai importato a meno che EasyOCRPageEngine venga istanziato; test contrattuale passa.
```
OK

**T11(b) — PDF page renderer**
```text
Repo: librarAIn-server. Crea src/ingestion/pipeline/render.py.
- Funzione render_pdf_page_to_png(pdf_path: Path, page_index_zero: int, target_path: Path, *, dpi: int = 200) -> Path usando pypdfium2 (preferito: stdlib-friendly, niente Poppler).
- Funzione render_aligned_pdf_pages(aligned_pdf_path, target_dir, dpi) -> list[(aligned_page_1based, png_path)] che produce data/tmp/<sha>/render/p.NNNN.png.
- Idempotente: se PNG esiste con stesso DPI marker (sidecar JSON), skip.
- Test: tests/test_render.py genera un PDF in-memory e verifica che venga creato il PNG atteso, e che la seconda chiamata sia no-op.
DoD: nessuna dipendenza da Poppler/system; test passano in CI senza GPU.
```
OK

**T11(c) — Persistenza Stage 1 OCR + cache**
```text
Repo: librarAIn-server. Crea src/ingestion/pipeline/stage1.py.
- Funzione run_stage1_ocr(aligned_pdf_path, source_sha256, useful_pages_enumeration, settings, engine: OCRPageEngine) -> Stage1Result.
- Per ogni pagina ALIGNED 1-based: render PNG (T11b), chiama engine.ocr_page, scrive testo in data/tmp/<sha>/stage1OCR/p.NNNN.<libro_slug>.txt.
- "libro_slug" ricavato da reicat.title via slugify deterministico (max 32 char, [a-z0-9-]).
- Stage1Result: {pages: [{aligned_page, original_page, txt_path, char_count}], skipped_existing: int, missing: list[int]}.
- Cache: se file txt esiste e dimensione > 0, NON rifare OCR (idempotente). Aggiungi flag force_recompute=False.
- Errori: se engine fallisce su una pagina, registra last_error nel risultato e continua; al termine, se >= 50% pagine fallite -> raise IngestInputValidationError code=OCR_STAGE_FAILED (aggiungi il code in src/models/request.py).
- Test tests/test_stage1.py con un fake engine deterministico (mappa page->testo); verifica idempotenza, slug, threshold di errore.
DoD: stage1 produce file deterministicamente, cache funziona, threshold di errore tracciata.
```
OK

### T12 — Sonnet — Stage Vision (3 sub-prompt)

**T12(a) — Client OpenAI-compatible centralizzato**
```text
Repo: librarAIn-server. Crea src/core/openai_client.py.
- Funzione build_openai_client(settings: Settings) -> openai.OpenAI con base_url, api_key, timeout=settings.timeout_seconds.
- Wrapper async chat_completion_with_retry(client, *, model, messages, temperature=0.1, max_tokens, request_id, stage, page) -> str:
  - retry = settings.retry_attempts, backoff esponenziale con jitter.
  - distinguere errori transienti (RateLimitError, APIConnectionError, APITimeoutError) da permanenti (BadRequestError, AuthenticationError -> rilancia subito).
  - rate-limit token-bucket condiviso a livello di client (settings.rate_limit_per_minute), va bene asyncio.Semaphore + sleep.
  - log strutturato JSON di ogni tentativo (T18 ancora non c'è: usa logging stdlib con extra dict; verrà sostituito).
- Niente prompt qui dentro.
- Test tests/test_openai_client.py con un fake httpx-like response per verificare retry/backoff e classificazione errori (usa monkeypatch su client.chat.completions.create).
DoD: nessuna chiamata di rete nei test; il client riusa la stessa istanza per tutta la run.
```
OK

**T12(b) — refine_with_vision**
```text
Repo: librarAIn-server. In `src/ingestion/pipeline/stage2.py` definisci `refine_with_vision` (T12b; non file separato).
- Funzione refine_with_vision(client, *, model, page_image_path: Path, raw_ocr_text: str, request_id, page) -> str.
- Costruisce un messaggio multimodale: [system con testo letto da `src/ingestion/pipeline/prompts/vision_prompt.md`, user con image_url base64 della PNG + raw_ocr_text].
- temperature default 0.1 (fissa nel codice ma sovrascrivibile via parametro).
- Un **solo** file Vision in `pipeline/prompts/`; aggiornamenti = commit Git, niente numerazione dei prompt.
- Test tests/test_stage2.py (`TestRefineWithVision`) con un fake client che ritorna stringa fissa; verifica che image+text siano correttamente passati e che il system prompt coincida col file.
DoD: niente prompt hardcoded nel codice Python.
```
OK

**T12(c) — Persistenza Stage 2 + audit prompt**
```text
Repo: librarAIn-server. Nello stesso `src/ingestion/pipeline/stage2.py` aggiungi `run_stage2_vision`, `Stage2Result`, ecc. (T12c).
- Funzione run_stage2_vision(stage1_result, source_sha256, settings, client) -> Stage2Result.
- Per ogni pagina di stage1: chiama refine_with_vision; scrive markdown in data/tmp/<sha>/stage2Vision/p.NNNN.<slug>.md.
- Cache idempotente come stage1 + sidecar JSON {"model": "...", "completed_at": "..."}; se stesso modello, skip.
- Test tests/test_stage2.py con fake client; verifica idempotenza con stesso model, ricomputo se model cambia.
DoD: cambio di model invalida correttamente la cache; per rigenerare dopo modifiche a `prompts/vision_prompt.md` usare force_recompute o cancellare gli artefatti Stage 2.
```
OK

### T13 — Sonnet — Stage Editor (2 sub-prompt)

**T13(a) — refine_with_editor**
```text
Repo: librarAIn-server. In `src/ingestion/pipeline/stage3.py` (stesso modulo di T13b) definisci `refine_with_editor`.
- Funzione refine_with_editor(client, *, model, stage2_md: str, request_id, page) -> str: chat-completions text-only.
- System prompt letto da `src/ingestion/pipeline/prompts/editor_prompt.md` ("normalizza markdown, fix spaziature, NON cambiare semantica, NON aggiungere contenuto").
- Stessa policy di temperature 0.1, retry/rate-limit del client centralizzato.
- Test tests/test_stage3.py (classe TestRefineWithEditor) con fake client.
DoD: testo di sistema solo da file in repo (Git), niente stringhe prompt nel Python.
```
OK

**T13(b) — Persistenza Stage 3 + diff per pagina**
```text
Repo: librarAIn-server. Nello stesso `src/ingestion/pipeline/stage3.py` aggiungi run_stage3_editor, Stage3Result, ecc.
- Funzione run_stage3_editor(stage2_result, source_sha256, settings, client) -> Stage3Result.
- Per ogni pagina di stage2: refine_with_editor, scrive in data/tmp/<sha>/stage3Editor/p.NNNN.<slug>.md.
- Sidecar JSON: model, completed_at, stage2_char_count, stage3_char_count, char_delta.
- Cache idempotente identica a stage2 (stesso modello).
- Test tests/test_stage3.py: idempotenza, sidecar coerente, char_delta calcolato.
DoD: il pipeline è ora rieseguibile a stage in modo idempotente.
```
OK** (comodo ma necessario poi dovrà esere aggiustato in relazione alla mia libreria)

### T14 — Opus — Orchestrazione concorrente (4 sub-prompt)

**T14(a) — Coda di job per pagina**
```text
Repo: librarAIn-server. Crea src/ingestion/orchestrator.py.
- Definisci PageJob: aligned_page, original_page, png_path, txt_path, stage2_md_path, stage3_md_path, status, last_error.
- Funzione async run_pipeline(enriched, alignment, useful_pages, settings, sqlite_path, registry, request_id) che:
  1) renderizza tutte le pagine (T11b),
  2) crea PageJob list,
  3) esegue stage1 → stage2 → stage3 con asyncio.gather + asyncio.Semaphore(settings.max_parallel_request).
- Ogni transizione di pagina pubblica un IngestJobEvent al registry (T18.5c).
- Niente retry qui dentro (responsabilità del client OpenAI per stage 2/3; per stage 1 implementa retry locale con max=settings.retry_attempts).
- Test tests/test_orchestrator.py con stage1/2/3 mockati: 8 pagine, MAX_PARALLEL_REQUEST=3 -> assertare che la concorrenza non supera 3 (usando un asyncio.Semaphore di test che traccia max_in_flight).
DoD: la concorrenza è osservabile e configurabile da .env senza modificare il codice.
```
OK

**T14(b) — Retry + classificazione errori**
```text
Repo: librarAIn-server. Crea src/core/retry.py:
- Funzione async retry_async(coro_factory, *, max_attempts, base_delay=0.5, max_delay=10, jitter=True, retry_on=(TransientError,), giveup_on=(PermanentError,)).
- Definisci esczioni TransientError / PermanentError in src/core/errors.py e mappa le eccezioni openai in queste classi via classify_openai_exception(exc) -> type.
- Refactor src/core/openai_client.py per usare retry_async.
- Refactor src/ingestion/pipeline/stage1.py per usare retry_async sulle chiamate al engine OCR (max_attempts=settings.retry_attempts).
- Test tests/test_retry.py: TransientError causa retry, PermanentError no-retry, max_attempts rispettato, backoff cresce.
DoD: nessun retry ad-hoc disperso, una sola implementazione.
```
OK

**T14(c) — Token-bucket rate-limit**
```text
Repo: librarAIn-server. Aggiungi a src/core/rate_limit.py:
- AsyncTokenBucket(rate_per_minute, capacity=None) con metodo async acquire(n=1).
- Implementazione interna SOLO con stdlib (asyncio + time.monotonic): niente librerie esterne tipo aiolimiter, asyncio-throttle, ratelimit o equivalenti. Lock interno via asyncio.Lock; refill calcolato on-demand su time.monotonic in base a rate_per_minute.
- Singleton lazy a livello di processo (chiave: id(client) o "global"); 0 capacity -> illimitato (per test).
- Integra l'acquire prima di ogni chat_completion in src/core/openai_client.py.
- Test tests/test_rate_limit.py: 60/minuto -> 60 acquire devono essere immediati nella prima finestra; il 61° aspetta; in test, monkeypatch time.monotonic per non rallentare la suite.
DoD: rate-limit rispettato e testato senza wallclock reale; nessuna nuova dipendenza in pyproject.toml.
```


**T14(d) — pipeline_runs + propagazione request_id**
```text
Repo: librarAIn-server. Aggiungi migration 3 in src/persistence/book_sqlite.py (MIGRATIONS list, vedi PRE-B):
- pipeline_runs(id INTEGER PK, request_id TEXT NOT NULL UNIQUE, source_sha256 TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, pipeline_version TEXT NOT NULL, last_error TEXT, total_pages INTEGER, succeeded_pages INTEGER, failed_pages INTEGER).
- Funzioni create_pipeline_run, mark_pipeline_run_finished, get_pipeline_run_by_request_id.
- Integra in src/ingestion/orchestrator.py: create al T0, update finale (succeeded|failed) con counters.
- request_id deve essere presente in TUTTI gli IngestJobEvent e in TUTTI i log strutturati emessi nelle stage1/2/3.
- Test tests/test_pipeline_runs.py: crea, aggiorna, recupera; verifica unicità request_id.
DoD: ogni run ha una riga in pipeline_runs con stato finale e contatori; request_id correlabile cross-stage.
```
OK

### T15 — Composer 2 — Persistenza pagine `.md`
```text
Repo: librarAIn-server. Crea src/ingestion/output_writer.py.
- Funzione materialize_book_pages(stage3_result, enriched, source_sha256) -> BookOutput:
  - Copia (rename atomico via os.replace) ogni stage3 md in data/output/<sha>/pages/p.NNNN.<slug>.md.
  - Scrive data/output/<sha>/manifest.json con: source_sha256, slug, original_page_count, aligned_page_count, pages: [{aligned, original, file}], reicat (alias italiani), pipeline_version, generated_at.
  - Idempotente: se file esiste con stesso byte hash, no-op.
- Test tests/test_output_writer.py con stage3 mockato a 5 pagine: manifest atteso, file presenti.
DoD: nessuna scrittura non-atomica, manifest coerente.
```

### T16 — Composer 2 — Builder `TOC.md`
```text
Repo: librarAIn-server. Crea src/ingestion/toc_builder.py.
- Funzione build_toc_md(book_output, useful_pages_enumeration) -> Path:
  - Concatena, in ordine ascendente di aligned_page, il contenuto dei file pages/p.NNNN.<slug>.md le cui aligned_page rientrano in useful_pages_enumeration.toc_range_aligned.
  - Scrive data/output/<sha>/TOC.md con header "# TOC — <title>\n\n" + body concatenato (separa pagine con "\n\n---\n\n").
  - Idempotente (rewrite atomico).
- Test tests/test_toc_builder.py: 3 pagine in range, output ha 3 sezioni.
DoD: file presente, ordine deterministico.
```
OK

### T17 — Composer 2 — Builder `INDEX.md`
```text
Repo: librarAIn-server. Crea src/ingestion/index_builder.py.
- Funzione build_index_md(book_output, useful_pages_enumeration) -> Path: identico a T16 ma con index_range_aligned, header "# INDEX — <title>\n\n", scrive data/output/<sha>/INDEX.md.
- Test tests/test_index_builder.py speculare a T16.
DoD: comportamento identico a T16, applicato a INDEX.
```
OK

### T18 — Sonnet — Logging + audit (2 sub-prompt)

**T18(a) — Logger JSON strutturato**
```text
Repo: librarAIn-server. Crea src/core/logging.py.
- Funzione configure_logging(level: str = "INFO") che installa un handler con un Formatter JSON custom (no librerie esterne):
  - Campi standard: ts (ISO UTC), level, logger, message, request_id?, source_sha256?, stage?, page?, event?, duration_ms?
- Espone get_logger(name) -> logging.LoggerAdapter che inietta extra={"request_id":..., "source_sha256":...} via contextvars.
- Mai loggare openai_api_key, mai loggare il testo OCR oltre 200 char (helper safe_text(s, 200)).
- Refactor: tutti i print() in src/api/* e gli usi di logging.* nelle stage devono passare da get_logger.
- Test tests/test_logging.py cattura stdout e verifica che il record sia JSON valido con i campi attesi.
DoD: nessun print() applicativo residuo; un grep `print\(` su src/ non ritorna applicativo.
```
NOT OK**(C'è già un lib mia che fara il lavoro)

**T18(b) — Persistenza pipeline_runs + propagazione**
```text
Già coperta da T14(d). In questo prompt: cablaggio finale.
Repo: librarAIn-server.
- Verifica che ogni get_logger sia correttamente arricchito con request_id e source_sha256 via contextvars all'inizio di run_pipeline (orchestrator.py).
- Aggiungi un decorator/utility `log_stage(stage_name)` che loggi start/end con duration_ms a livello DEBUG/INFO.
- Aggiungi tests/test_logging_propagation.py: simula un run e cattura stdout; verifica che ogni record abbia request_id e source_sha256.
DoD: trace completa di una run è ricostruibile dai soli log + pipeline_runs.
```
OK**(va bene ma andrà fatto con la mia libreria)

### T19' — Sonnet — Smoke E2E nuovo hash (vero E2E)
```text
Repo: librarAIn-server. Crea tests/e2e/test_ingest_e2e_new_hash.py.
- Genera un PDF reale con 4 pagine (pypdf), scrive un breve testo per pagina renderizzato come immagine (Pillow) PRIMA di salvare il PDF (per avere OCR meaningful).
- Mocka il client OpenAI (monkeypatch) per ritornare markdown deterministico per stage2/stage3.
- Esegui pipeline end-to-end via orchestrator.run_pipeline (NON via HTTP in questo test).
- Asserzioni: data/output/<sha>/pages contiene 4 file md, TOC.md e INDEX.md non vuoti, pipeline_runs ha riga succeeded con succeeded_pages=4.
DoD: il test gira sotto pytest in <30s senza GPU e senza rete.
```
OK

### T21 — Sonnet — Smoke E2E reale via HTTP (2 sub-prompt)

**T21(a) — Test del form mapping HTTP**
```text
Repo: librarAIn-server. Crea tests/test_form_mapping.py.
- Importa build_ingest_payload_from_form da src.api.form_mapping.
- Casi: payload completo, ranges con sintassi virgole/intervalli, boolean truthy/falsy, autori multipli, error path (toc non contiguo, page invalid).
DoD: 100% del path build_ingest_payload_from_form e dei suoi helper coperto.
```
OK

**T21(b) — E2E HTTP submit→poll→artifacts**
```text
Repo: librarAIn-server. Crea tests/e2e/test_http_e2e.py con fastapi.testclient.TestClient.
- Avvia app FastAPI in-process (vedi T18.5).
- Mocka client OpenAI a livello di src/core/openai_client.build_openai_client.
- POST /api/ingest/submit multipart con un PDF generato a runtime (4 pagine), ottieni request_id.
- Polling GET /api/ingest/{id} con timeout 30s fino a status=succeeded.
- Asserzioni: GET /api/ingest/{id}/artifacts elenca pages/, TOC.md, INDEX.md, manifest.json.
- Test secondo run con stesso PDF: GET dello stato deve indicare skipped_duplicate (pipeline non rieseguita); pipeline_runs ha 1 sola riga succeeded e una skipped (o equivalente come da T18.5d/T14d).
DoD: smoke E2E reale verde in CI.
```
OK

---

## Estensione MVP — completamento della Fase 1 Upload

> Allineata al PRD `PRD-Fase1.md` rev. corrente. Prompt auto-contenuti, con modello consigliato (Opus = ragionamento complesso/architettura; Sonnet = codice deterministico + test; Composer 2 = scaffolding e IO).

### T22 — Composer 2 — Builder `<slug>.md` (Σ pages per libro)
```text
Repo: librarAIn-server. Crea src/ingestion/book_md_builder.py.
- Funzione build_book_md(book_output: BookOutput, useful_pages_enumeration) -> Path:
  - Concatena, in ordine ascendente di aligned_page, TUTTI i file data/output/<sha>/pages/p.NNNN.<slug>.md presenti nel manifest.
  - Scrive data/output/<sha>/<slug>.md con header "# <reicat.titolo>\n\n_<reicat.autore[0]> — <reicat.anno_di_pubblicazione>_\n\n" + body concatenato (separa pagine con "\n\n---\n\n<!-- p.<aligned> (orig. p.<original>) -->\n\n").
  - Idempotente (rewrite atomico via tempfile + os.replace; no-op se byte hash identico).
- Integra T22 nell'orchestratore: viene eseguito DOPO T15 e PRIMA di T16/T17.
- Test tests/test_book_md_builder.py con 3 pagine mock: file presente, ordine deterministico, byte-equality alla seconda invocazione.
DoD: il file <slug>.md esiste, contiene tutte le pagine in ordine, è rigenerabile in modo idempotente.
```

### T23 — Sonnet — Polyindex TOC.json updater
```text
Repo: librarAIn-server. Crea src/ingestion/polyindex/__init__.py e src/ingestion/polyindex/toc_json.py.
- Definisci la struttura canonica:
    {
      "schema_version": "1.0",
      "books": {
        "<source_sha256>": {
          "title": "...",
          "slug": "...",
          "chapters": [{"label": str, "aligned_page_start": int, "aligned_page_end": int, "original_page_start": int, "original_page_end": int}]
        }
      }
    }
- Funzione parse_chapters_from_toc_md(toc_md_path: Path, useful_pages_enumeration) -> list[ChapterEntry]:
  - Parsing deterministico delle righe del TOC: riconosce pattern "Capitolo <N>", "Cap. <N>", "<titolo capitolo> ... <numero pagina>" (regex configurabili in chapter_patterns.py).
  - Le pagine nel TOC sono ORIGINAL; converti in aligned via useful_pages_enumeration.
  - Heuristic: pagine non presenti nel mapping (es. fuori range stampato) -> log warning + skip riga.
- Funzione update_polyindex_toc(polyindex_dir: Path, source_sha256: str, book_entry: dict) -> Path:
  - Acquisisce file lock su polyindex_dir/.toc.lock (fcntl.LOCK_EX su Unix; fallback noop su Windows con TODO).
  - Carica polyindex/TOC.json se esiste, altrimenti dict iniziale; merge per source_sha256 (sovrascrive l'entry esistente, mai duplica).
  - Scrive atomicamente: scrittura su polyindex/TOC.json.tmp + os.replace.
  - Bumpa "schema_version" se necessario (per ora 1.0 fisso).
- Test tests/test_polyindex_toc.py:
  1) parser su TOC.md mock con 5 capitoli -> entries attese.
  2) update_polyindex_toc su file vuoto -> 1 libro, struttura corretta.
  3) update_polyindex_toc su file esistente con stesso sha -> sostituzione idempotente (no crescita).
  4) due chiamate concorrenti (thread) -> nessuna corruzione (semplice mock di scrittura serializzata).
DoD: TOC.json prodotto e aggiornato, niente duplicati, scrittura atomica, parser tollerante a righe sporche.
```

### T24 — Sonnet — Parser deterministico `INDEX.md` → soggetti grezzi
```text
Repo: librarAIn-server. Crea src/ingestion/polyindex/index_md_parser.py.
- Funzione parse_index_md(index_md_path: Path, useful_pages_enumeration) -> list[RawSubject]:
  - RawSubject = {"raw_label": str, "original_pages": list[int], "aligned_pages": list[int]}
  - Riconosce pattern tipici degli INDEX analitici italiani:
    - "Lemma, 12, 45, 88"
    - "Lemma — 12-15, 22"
    - "Lemma vedi Altro Lemma" (cross-reference: registra come alias del target)
  - Normalizzazione leggera: trim, collapse spazi multipli, gestione delle virgole vs punti come separatori di pagina.
  - Espansione range "12-15" -> [12,13,14,15].
  - Conversione pagine ORIGINAL -> ALIGNED via useful_pages_enumeration; pagine fuori mapping -> log warning + skip.
- Funzione normalize_label(raw: str) -> str: lowercase NFKD senza accenti, collapse spazi, strip punteggiatura terminale; usata come chiave di confronto deterministico nel matcher (T25).
- Test tests/test_index_md_parser.py: 6 casi tra cui range, virgole, "vedi", caratteri accentati.
DoD: parser robusto a 6+ varianti di formattazione, output sempre con aligned_pages valido o riga scartata con motivo.
```

### T25 — Opus — AI Subject Matcher (normalization + embeddings + LLM dirimitore)
```text
Repo: librarAIn-server. Crea src/ingestion/polyindex/subject_matcher.py e src/ingestion/polyindex/prompts/subject_matcher_prompt.md.
Obiettivo: dato un RawSubject (T24) e lo stato corrente di polyindex/INDEX.json, decidere se è (a) match con un canonical esistente, (b) nuovo canonical, (c) alias di un canonical esistente.

Pipeline a 2 stadi:
1) DETERMINISTIC FIRST PASS
   - normalize_label() identico tra raw e canonical -> hit secco -> canonical match.
   - normalize_label() del raw matcha uno qualsiasi degli aliases di un canonical -> alias hit -> canonical match.
   - Altrimenti -> candidate set via similarità lessicale (rapidfuzz token_sort_ratio >= 90) tra normalized -> "borderline".
   - Se nessun candidate e MATCHER_USE_AI=false -> registra come nuovo canonical.

2) AI SECOND PASS (eseguito solo su borderline OPPURE quando MATCHER_USE_AI=true e nessun hit del primo stadio)
   - Embedding del raw_label via openai.embeddings(model=settings.matcher_embedding_model).
   - Per ciascun canonical candidato (top-K=10 dei più simili lessicalmente o dei più recenti se K corti): embedding cache-ato in tabella SQLite subject_embeddings (vedi sotto). Distanza coseno.
   - Se max_sim >= settings.matcher_similarity_threshold (default 0.86) -> proponi merge.
   - LLM dirimitore (solo se 0.82 <= max_sim < 0.92): chat completion con testo di sistema da `subject_matcher_prompt.md` (italiano, vincolante): "decidi se due lemmi indicano la stessa entità storica/concettuale. Rispondi SOLO con JSON {\"same\": bool, \"reason\": str}". temperature=0.1.
   - Se dirimitore "same" -> canonical match + aggiungi raw_label originale agli aliases.
   - Altrimenti -> nuovo canonical (id = uuid stabile da label normalizzata + timestamp, oppure slug; opta per slug + counter su collisione).

Persistence:
- Aggiungi migration 4 in src/persistence/book_sqlite.py: tabella subject_embeddings(canonical_id TEXT PRIMARY KEY, label TEXT, embedding BLOB, model TEXT, created_at TEXT).
- Funzioni get/set embedding (BLOB = np.array float32 -> bytes; se non vuoi numpy, usa struct.pack).
- Audit: tabella subject_match_audit(id INTEGER PK, request_id TEXT, raw_label TEXT, normalized TEXT, decision TEXT, canonical_id TEXT, similarity REAL, ai_used INTEGER, ai_reason TEXT, created_at TEXT).

API funzionale:
- match_subject(raw_subject: RawSubject, polyindex_state: dict, client, sqlite_path, settings, request_id) -> MatchDecision:
    {action: "match" | "new" | "alias", canonical_id: str, similarity?: float, ai_used: bool}

Test tests/test_subject_matcher.py:
- Fake openai client deterministico: ritorna embedding fisso per label specifiche; ritorna {"same": true|false} su input fissi.
- Casi: hit secco normalizzazione, alias hit, borderline lessicale risolto deterministicamente, borderline risolto da LLM "same", borderline risolto da LLM "different".
- Idempotenza: due chiamate consecutive sullo stesso raw_subject ritornano la stessa decision (cache embedding usata).

DoD: matcher con 5 casi testati green; audit log popolato; embedding cache funziona; istruzioni LLM solo nel file prompt in repo; mai chiamate di rete reali nei test.
```

### T26 — Opus — Polyindex INDEX.json updater (merge atomico cross-book)
```text
Repo: librarAIn-server. Crea src/ingestion/polyindex/index_json.py.
- Struttura canonica di polyindex/INDEX.json:
    {
      "schema_version": "1.0",
      "subjects": {
        "<canonical_id>": {
          "canonical_label": str,
          "aliases": [str, ...],
          "books": {
            "<source_sha256>": {"aligned_pages": [int, ...], "original_pages": [int, ...]}
          }
        }
      }
    }
- Funzione update_polyindex_index(polyindex_dir, source_sha256, raw_subjects, client, sqlite_path, settings, request_id) -> Path:
  1) Acquisisce file lock su polyindex_dir/.index.lock (vedi T23).
  2) Carica INDEX.json corrente (o struttura iniziale).
  3) Per ogni RawSubject (T24): chiama match_subject (T25) passando lo stato corrente.
  4) Applica la decisione:
     - "match": aggiunge {source_sha256: {aligned_pages, original_pages}} al canonical (merge ordinato e deduplicato).
     - "alias": aggiunge raw_label agli aliases del canonical + merge pagine come "match".
     - "new": crea nuovo canonical_id; aggiunge il primo book entry.
  5) Per ogni book entry esistente con stesso source_sha256 ma soggetto NON più presente nel nuovo INDEX.md: NON rimuovere (preserva storico cross-run). Documentalo nel docstring come scelta esplicita.
  6) Scrive atomicamente (tempfile + os.replace).
  7) Ritorna il path scritto e statistiche {n_new, n_match, n_alias}.
- Integra T26 nell'orchestratore DOPO T17 (INDEX.md prodotto) e DOPO T23 (TOC.json), prima dello snapshot (T27).
- Test tests/test_polyindex_index.py:
  - 2 libri sintetici con 4 soggetti ciascuno, di cui 2 sovrapposti (stesso canonical).
  - Dopo run su libro A: 4 canonical, 4 entry libro A.
  - Dopo run su libro B: 4 canonical (2 condivisi + 2 nuovi), libro B presente nei condivisi.
  - Idempotenza: re-run libro A senza modifiche -> file byte-identico.
DoD: INDEX.json cross-book corretto su 2 libri, idempotenza confermata, lock-protected.
```

### T27 — Composer 2 — Checkpoint daily + on-demand (DB + polyindex)
```text
Repo: librarAIn-server. Crea src/core/checkpoints.py.
- Funzione snapshot_now(reason: Literal["daily","on_demand","manual"], settings) -> SnapshotResult:
  - Acquisisce lock di scrittura su polyindex (stessi file lock di T23/T26, accodandosi) per garantire coerenza.
  - Copia data/db/biblioteca.csv -> data/db/checkpoints/biblioteca.<YYYY-MM-DD>.csv (overwrite consentito; se reason="on_demand" e file del giorno esiste, suffisso .HHMMSS).
  - Copia polyindex/TOC.json -> polyindex/checkpoints/<YYYY-MM-DD>.TOC.json (stessa policy).
  - Copia polyindex/INDEX.json -> polyindex/checkpoints/<YYYY-MM-DD>.INDEX.json.
  - Pulisce file più vecchi di settings.checkpoint_retention_days (default 30); skip se 0.
  - Ritorna SnapshotResult: {db_path, toc_path, index_path, retained_count, deleted_count, took_ms}.
- Scheduler MVP: asyncio task in src/api/app.py che ogni `settings.checkpoint_period_seconds` (default 86400) chiama snapshot_now("daily"). Disabilitato se CHECKPOINT_DAILY_ENABLED=false.
- Endpoint POST /api/admin/checkpoint -> 202 + risultato sincrono in body (il job è breve).
- Test tests/test_checkpoints.py:
  - Cartelle vuote -> snapshot crea i file attesi.
  - Retention: crea 35 file fittizi, dopo snapshot ne restano 30.
  - Lock: snapshot durante un mock di scrittura polyindex aspetta correttamente (semplice asserto sull'ordine di acquisizione).
DoD: checkpoint giornaliero e on-demand funzionanti, retention rispettata, integrato con il lock polyindex.
```

### T28 — Composer 2 — Cleanup `data/tmp/<sha>/` policy
```text
Repo: librarAIn-server. Crea src/ingestion/tmp_cleanup.py.
- Funzione cleanup_tmp_after_success(source_sha256: str, settings) -> CleanupResult:
  - Se settings.tmp_keep_after_success=true (default true in MVP), no-op + return.
  - Altrimenti, rimuove ricorsivamente data/tmp/<sha>/ se esiste; log strutturato del numero di file rimossi e bytes liberati.
  - Mai rimuove se ci sono file in scrittura (controllo file lock semplice: se file *.tmp aperti, skip + warning).
- Aggiungi settings TMP_KEEP_AFTER_SUCCESS (bool, default true).
- Integra T28 nell'orchestratore come ULTIMO step prima della chiusura del job (dopo T26 e snapshot opportunistico).
- Test tests/test_tmp_cleanup.py:
  - Con flag=true -> tmp resta.
  - Con flag=false -> tmp viene rimossa; verifica bytes_freed > 0 e log evento.
  - Con file .tmp aperto simulato -> cleanup salta e logga warning.
DoD: opt-in/out via .env; nessuna perdita di artefatti finali in data/output/.
```

### T29 — Composer 2 — `web/index.html` form unico operatore
```text
Repo: librarAIn-server. Crea/aggiorna web/index.html (oggi probabilmente segnaposto).
- Form POST multipart verso /api/ingest/submit con i campi:
  - file: input type="file" accept="application/pdf" required.
  - reicat.titolo, reicat.sottotitolo, reicat.complementi_del_titolo (text).
  - reicat.autore (text area: 1 autore per riga, lato client split su \n).
  - reicat.curatore, reicat.traduttore (idem).
  - reicat.numero_edizione, reicat.anno_di_pubblicazione (number), reicat.tipo_di_pubblicazione (select), reicat.luogo_di_pubblicazione, reicat.editore, reicat.numero_pagine (number), reicat.titolo_collana, reicat.numero_nella_collana, reicat.isbn.
  - pages_to_remove (text, sintassi "1,3,5-7").
  - toc_start, toc_end, index_start, index_end (number).
  - options.force_metadata_update_on_duplicate_hash (checkbox).
- Dopo submit:
  - mostra request_id e link "GET /api/ingest/<id>".
  - polling JS ogni 3s su /api/ingest/<id> finché status in {succeeded, failed, skipped_duplicate}.
  - mostra in coda l'elenco file da /api/ingest/<id>/artifacts.
- UI minimale, vanilla HTML/CSS/JS, no framework, no build step.
- Servire `/` come questa pagina (già coperto da T18.5a) con StaticFiles o FileResponse.
- Test tests/test_web_index_static.py: GET / ritorna 200 e content-type text/html; presenza dei field name attesi nel body.
DoD: pagina funzionante in locale, niente dipendenze JS esterne.
```

### T30 — Opus — Orchestratore end-to-end Upload (cablaggio finale)
```text
Repo: librarAIn-server. Modifica src/ingestion/orchestrator.py (creato in T14a) per cablare l'intero Upload.
Sequenza definitiva di run_pipeline(enriched, alignment, useful_pages, settings, sqlite_path, registry, request_id):
  1) create_pipeline_run (T14d).
  2) Render PNG di tutte le pagine aligned (T11b) — concurrency = max_parallel_request.
  3) PageJob list (T14a).
  4) Stage1 OCR per pagina, idempotente (T11c).
  5) Stage2 Vision per pagina (T12c), concurrent + rate-limited (T12a).
  6) Stage3 Editor per pagina (T13b), concurrent + rate-limited.
  7) Output writer per libro (T15) -> data/output/<sha>/pages/* + manifest.json.
  8) Builder <slug>.md (T22).
  9) Builder TOC.md (T16).
  10) Builder INDEX.md (T17).
  11) Polyindex TOC.json updater (T23) -- richiede file lock.
  12) Index.md parser (T24) -> raw_subjects.
  13) Subject matcher + INDEX.json updater (T25 + T26) -- richiede file lock; condivide lock con T23 quando necessario.
  14) Cleanup tmp (T28).
  15) mark_pipeline_run_finished("succeeded", counters).
  16) Pubblica evento finale al job registry (T18.5c) con stato "succeeded".

Vincoli:
- Ogni transizione di stage publica un IngestJobEvent al registry.
- Failure di un singolo step "soft" (es. parser INDEX trova 0 soggetti) NON fallisce la run; viene loggato e marcato in counters.
- Failure "hard" (es. OCR > 50% pagine fallite) fa fallire la run con last_error preservato.
- Pipeline_version: lettura da pyproject.toml o da costante in `src/api/`; riflette **versione del software della pipeline**, non dei file prompt (quelli sono già tracciati da Git).

Test tests/test_orchestrator_e2e_mocked.py:
- Mocka client OpenAI, easyocr engine, pypdfium2.
- 4 pagine, 2 soggetti.
- Verifica che a fine run tutti i file attesi esistano e che il registry mostri stato succeeded + N eventi attesi (>=12).

DoD: la run finale produce per-book artifacts + aggiorna polyindex; rieseguita con stesso PDF, skip duplicate hash; tutto loggato e auditabile.
```

### T31 — Sonnet — E2E cross-book polyindex
```text
Repo: librarAIn-server. Crea tests/e2e/test_polyindex_crossbook.py.
- Genera 2 PDF reali (pypdf + Pillow), ciascuno con:
  - 1 pagina TOC ("Capitolo 1 .... 3") e 1 pagina INDEX (lemmi mockati con 2 soggetti condivisi tra i due libri, es. "Marco Polo, 2", "Venezia, 3").
  - 2 pagine di contenuto mock.
- Mocka client OpenAI per stage2/stage3 (markdown deterministico) e per subject_matcher (LLM ritorna {"same": true} su pair specifico).
- Esegui orchestrator.run_pipeline per libro A; poi per libro B.
- Asserzioni:
  - data/polyindex/TOC.json contiene 2 books con i loro chapters.
  - data/polyindex/INDEX.json contiene N canonical, di cui 2 con entrambi i books.
  - subject_match_audit ha righe per ciascuna decisione.
  - pipeline_runs ha 2 righe succeeded.
DoD: smoke E2E cross-book green in <60s senza rete e senza GPU.
```

---

## Roadmap successiva (Fase 2 Ricerca)

Dettaglio task e numerazione: `PRD-Fase1.md` §7 (**F2-T1..F2-T10**). I prompt operativi per F2 verranno aggiunti in questo file quando l'Upload è chiuso.

### Ricerca MVP — convenzioni Markdown (implementazione)

Fonte normativa: `PRD-Fase1.md` §2.5.1. In sintesi per chi scrive prompt/parser:

- **Fonti**: `[testo](source:<sha256>:aligned:<p>)` — validare contro `manifest.json`; niente `<a href>`.
- **POH**: `[etichetta](poh:<poh_id>)` oppure `poh:unknown-<slug>` + sezione `## Annotazioni` con TODO.
- **Cronologia (vertical time bar in file)**: blocco finale `## Cronologia` + tabella GFM esattamente `| Periodo | Evento | Fonti |`, righe ordinate dal più antico al più recente; ogni riga datata con almeno un `source:` valido (eccezione massimo 1 riga `—` come da PRD).
- **CommonMark**: link inline standard; niente spazi raw dentro `(...)`; escape `_` `*` se necessario nel testo delle celle.

## Sintesi finale aggiornata

- **Completati (12)**: T1–T10 + T19 + T20.
- **In coda priorità alta (sblocco MVP Upload)**: PRE-A → PRE-B → PRE-C → T11(a..c) → T12(a..c) → T13(a..b) → T14(a..d) → T18.5(a..d) → T15 → T22 → T16 → T17 → T23 → T24 → T25 → T26 → T27 → T28 → T29 → T30 → T19' → T21(a) → T21(b) → T31.
- **Stima parallelizzabili**: T11(a), T11(b), T12(a) in parallelo; T22/T16/T17 in parallelo dopo T15; T23/T24 in parallelo dopo T22; T25 prima di T26; T27/T28/T29 in parallelo dopo T26.
- **Modello consigliato per quota Opus**: PRE-D (skip se inutile), T14(a), T18.5(c), T25, T26, T30. Tutti gli altri delegabili a Sonnet/Composer 2.
