# Configurazione

La configurazione runtime è caricata da `.env` (template: `example.env`) tramite `src/core/config.py` → modello Pydantic `Settings` in `src/models/settings.py`.

Ordine: file `.env`, poi override dalle variabili d'ambiente del processo. Errori di validazione → messaggio aggregato con riferimento a `example.env`.

## Obbligatorie

| Variabile | Valori | Ruolo |
|-----------|--------|-------|
| `DATA_ROOT` | path (es. `data`) | Root di tutti i dati runtime |
| `OPENAI_PROVIDER` | `local` \| `remote` | Provider LLM |

Se `OPENAI_PROVIDER=remote` diventano obbligatori anche `OPENAI_BASE_URL` e `OPENAI_API_KEY`.

## Path derivati

| Path | Derivazione |
|------|-------------|
| SQLite | `<DATA_ROOT>/db/biblioteca.db` |
| PDF allineati | `<DATA_ROOT>/input/processed/` (`Settings.processed_pdf_input_dir`) |
| Polyindex | `<DATA_ROOT>/polyindex/` |
| Output libri | `<DATA_ROOT>/output/<sha>/` |
| Tmp stage | `<DATA_ROOT>/tmp/<sha>/` |
| Research | `<DATA_ROOT>/research/` |

## LLM e modelli

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `OPENAI_BASE_URL` | — | Endpoint OpenAI-compatibile (es. `http://localhost:1234/v1`) |
| `OPENAI_API_KEY` | — | Chiave (per locale spesso placeholder) |
| `VISION_MODEL` | — | Stage 2 Vision |
| `EDITOR_MODEL` | — | Stage 3 Editor |
| `GLM_OCR_MODEL` | — | Pipeline ingest GLM (`/index2.html`) |
| `RESEARCH_MODEL` | — | Research + chat (fallback su altri modelli se assente) |
| `RESEARCH_TEMPERATURE` | `0.3` | Temperature generazione articoli |
| `MATCHER_LLM_MODEL` | — | Dirimitore subject matching POH |
| `MATCHER_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding soggetti |
| `TIME_INDEX_LLM_MODEL` | — | Estrazione anni/date (fallback: matcher → editor) |
| `TIME_INDEX_USE_LLM` | `true` | `false` = solo regex |

## Parallelismo, timeout, retry

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `MAX_PARALLEL_REQUEST` | `2` | Parallelismo pagine/LLM |
| `PAGE_RANGE_PER_THREAD` | `10` | Chunk PDF nell'allineamento |
| `TIMEOUT_SECONDS` | `120` | Timeout chiamate LLM stage |
| `RESEARCH_TIMEOUT_SECONDS` | `3600` | Timeout research |
| `RETRY_ATTEMPTS` | `2` | Tentativi retry client |
| `RATE_LIMIT_PER_MINUTE` | `60` | Token bucket client OpenAI |

## OCR e GPU

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `OCR_LANGUAGES` | `it,en` | Lingue EasyOCR |
| `OCR_USE_GPU` | `false` | GPU per OCR |
| `OCR_GPU_DEVICE` | `all` | Selezione device |
| `GPU_VRAM_CHECK_ENABLED` | `true` | Preflight VRAM |
| `GPU_VRAM_MAX_USED_GB` | `4.0` | Soglia used VRAM |

## LM Studio

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `LM_STUDIO_SWAP_MODELS` | `true` | Scarica/carica modelli tra stage |
| `LM_STUDIO_LOAD_TIMEOUT_SECONDS` | `600` | Timeout load modello |

## Subject matching (INDEX)

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `MATCHER_USE_AI` | `true` | Abilita embedding+LLM matching |
| `MATCHER_SIMILARITY_THRESHOLD` | `0.86` | Soglia cosine similarity |

## Reasoning (opzionale)

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `REASONING_EFFORT_VISION` | — | Effort reasoning Vision |
| `REASONING_ENABLE_THINKING_VISION` | — | Enable thinking Vision |
| `REASONING_EFFORT_EDITOR` | — | Effort Editor |
| `REASONING_ENABLE_THINKING_EDITOR` | — | Enable thinking Editor |
| `REASONING_EFFORT_RESEARCH` | — | Effort Research |
| `REASONING_ENABLE_THINKING_RESEARCH` | — | Enable thinking Research |

## HTTP server (lette via `get_env`, non tutte in `Settings`)

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `INGEST_HTTP_HOST` | `127.0.0.1` | Indirizzo di bind (es. `192.168.1.200` in LAN) |
| `INGEST_HTTP_PORT` | `8765` | Porta |
| `INGEST_MAX_CONCURRENT_JOBS` | `1` | Semaforo job ingest/repair |
| `INGEST_MAX_UPLOAD_BYTES` | `536870912` (512 MiB) | Limite upload PDF |
| `RESEARCH_MAX_CONCURRENT_JOBS` | `1` | Semaforo research |
| `RESEARCH_DEDUP_TTL_SECONDS` | `3600` | Dedup richieste research identiche |

## Altro

| Variabile | Default | Ruolo |
|-----------|---------|-------|
| `TMP_KEEP_AFTER_SUCCESS` | `true` | Conserva `tmp/<sha>/` dopo ingest ok |

## Note

- Variabili presenti in `.env` locali ma **non lette dal codice** (es. `INGEST_API_TOKEN`) non hanno effetto: vedi [security.md](security.md).
- Dopo modifiche a `.env` riavviare il server.
