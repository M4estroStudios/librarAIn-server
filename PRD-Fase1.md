# PRD — librarAIn (Fasi 1 Ingestione e 2 Ricerca)

> Documento unico di prodotto. Sostituisce la precedente versione limitata alla sola Fase 1.
> Allineato al manoscritto in `trascrizione-fogli-manoscritti.md` e alla struttura repo in `README.md`.

## 0. Assunzioni di scoping (da confermare in PR review)

Queste assunzioni sono frutto di discovery non completata. Vanno confermate o ribaltate prima di chiudere l'MVP. Sono evidenziate qui in apertura per essere trovate subito.

- **A1 — Definizione di POH**: entità generica del dominio (persona, luogo, evento o concetto storico), con `time_range` opzionale, `id` stabile e `aliases[]`. Non si forza una tassonomia rigida in MVP.
- **A2 — Search MVP**: in MVP la Fase 2 implementa **tutti** i passi del manoscritto: `a` articolo, `b` citazioni alle fonti, `c` hyperlink agli altri POH menzionati, `d` vertical time bar (in file Markdown = sezione cronologica strutturata; il renderer UI può poi mapparla su una barra laterale). Dettaglio formattazione in §2.5.1.
- **A3 — Stack ricerca**: nessun Milvus, nessun RAG vettoriale dedicato. La ricerca usa **polyindex** (`TOC.json` + `INDEX.json`) + LLM per generazione articolo. Eventuali embeddings sono interni al subject matcher di `INDEX.json`, non un secondo backbone di retrieval.
- **A4 — AI Subject Matcher**: pipeline 2-stadi: normalizzazione deterministica (lowercase, accenti, lemmatizzazione minimale) → AI matching (embeddings + LLM dirimitore) solo sui residui ambigui.
- **A5 — Checkpoints**: snapshot giornaliero schedulato + on-demand; retention configurabile (default 30 giorni).
- **A6 — UI**: in MVP solo pagina HTML singola per l'upload operatore. UI di ricerca (`web/search.html`) è v1.1.
- **A7 — Filename DB**: `data/db/biblioteca.csv` — **file binario SQLite**; l’estensione `.csv` è convenzione di naming del prodotto, **non** un export tabulare. Snapshot in `data/db/checkpoints/biblioteca.YYYY-MM-DD.csv` (stessa natura). Codice e path canonico: vedere `Settings.sqlite_path`.
- **A8 — Provider AI**: unico modello configurabile via `.env` (OpenAI-compatible) per Vision, Editor, Subject Matcher e Research; ogni stage può avere model id override.

## 1. Executive Summary

- **Problem Statement**: oggi i libri scansionati non si trasformano in conoscenza interrogabile in modo coerente: l'ingestione è parziale, la catalogazione bibliografica vive separata dal contenuto, e non esiste una "biblioteca semantica" navigabile cross-book.
- **Proposed Solution**: pipeline end-to-end deterministica che (1) ingesce PDF + REICAT in pagine Markdown allineate, (2) costruisce per ogni libro `TOC.md`/`INDEX.md`/`<NomeLibro>.md`, (3) aggrega cross-book in `polyindex/TOC.json` e `polyindex/INDEX.json` con riconciliazione AI dei soggetti, (4) espone una API di **Ricerca** che, data una query (eventualmente collegata a un POH), produce un **unico file Markdown** in stile Wikipedia con citazioni come link MD alle fonti (passi `a`–`b`), hyperlink agli altri POH in sintassi CommonMark (passo `c`) e sezione `## Cronologia` tabellare verticale per la linea temporale (passo `d`).
- **Success Criteria (cross-fase)**:
  - 100% dei libri ingestiti produce: cartella `data/output/<sha256>/` con `pages/`, `<slug>.md`, `TOC.md`, `INDEX.md`, `manifest.json`.
  - 100% delle ingestioni con esito `succeeded` aggiorna `polyindex/TOC.json` e `polyindex/INDEX.json` in modo idempotente (riesecuzione → zero duplicati).
  - Per la ricerca: ≥80% di una gold set di 20 query produce un **Markdown** che soddisfa **tutti** i passi a–d (articolo, link fonti, link POH, sezione Cronologia); almeno 1 fonte valida per articolo; precisione citazioni (pagine esistenti) ≥95%; ogni link `poh:` punta a un `poh_id` noto nel registro POH o è marcato esplicitamente come `poh:unknown-<slug>` con TODO in coda documento.
  - Tempo medio end-to-end Upload (PDF 200 pagine, 1 vCPU + endpoint AI raggiungibile) < 30 min con `MAX_PARALLEL_REQUEST=4`.
  - 100% delle esecuzioni produce una riga in `pipeline_runs` con stato finale e contatori.
  - 0 chiavi API stampate nei log; 0 file `.env` committati.

## 2. User Experience & Functionality

### 2.1 Personas

- **Operatore di ingestione**: bibliotecario/operatore che carica un PDF e compila REICAT. Vuole un singolo punto di input, errori chiari, idempotenza.
- **Storico/Ricercatore**: utente finale che pone domande di dominio (eventualmente legate a un POH) e si aspetta un articolo coerente con citazioni puntuali.
- **Pipeline Orchestrator**: servizio automatico che esegue Upload e Ricerca senza intervento umano (per batch e test).

### 2.2 User Stories — Fase 1 Upload

- Come operatore, voglio caricare PDF + REICAT + range TOC/INDEX in un unico form HTTP, così da avviare l'ingestione senza più canali sincronizzati a mano.
- Come orchestratore, voglio che lo stesso PDF (stesso `sha256`) non venga rielaborato OCR/LLM, così da evitare costi e tempi inutili.
- Come orchestratore, voglio che ogni stage (OCR, Vision, Editor) sia idempotente per pagina, così da poter riprendere senza ripartire da zero.
- Come sistema biblioteca, voglio che ogni libro processato aggiorni `polyindex/TOC.json` e `polyindex/INDEX.json` in modo atomico, così da mantenere coerenza cross-book.
- Come team, voglio snapshot giornalieri di `biblioteca.csv` e `polyindex/*.json`, così da poter rollback a uno stato noto.

### 2.3 User Stories — Fase 2 Ricerca

- Come ricercatore, voglio inviare una query in linguaggio naturale (con riferimento opzionale a un POH), così da ricevere un articolo che sintetizzi le informazioni rilevanti tratte dai libri indicizzati.
- Come ricercatore, voglio che ogni affermazione non triviale dell'articolo riporti una citazione verificabile tramite **link Markdown** `source:` verso la pagina libro, così da aprire la fonte senza HTML.
- Come sistema, voglio individuare deterministicamente i capitoli e le pagine candidate via `polyindex/INDEX.json` (lookup per soggetto) e `polyindex/TOC.json` (struttura capitoli), così da limitare il contesto del LLM e ridurre allucinazioni.
- Come orchestratore, voglio che la ricerca sia asincrona (job model identico all'ingest), così da gestire query lunghe senza bloccare il client.
- Come ricercatore, voglio che i POH menzionati nel testo diversi dal soggetto principale siano **hyperlink in Markdown**, così da navigare verso altri articoli o risolvere da tooling.
- Come ricercatore, voglio una **cronologia verticale** (linea temporale) nel documento, così da vedere subito l’ordine degli eventi citati.

### 2.4 Acceptance Criteria — Fase 1 Upload

Conservati e raffinati rispetto alla versione precedente del PRD; di seguito solo le **modifiche/aggiunte sostanziali** rispetto al passato.

- Output per libro includono ora anche `<slug>.md` (Σ pages, concatenazione ordinata di `pages/p.NNNN.<slug>.md` con separatore `\n\n---\n\n`).
- `polyindex/TOC.json` aggiornato al completamento di ogni ingest con struttura:
  ```json
  {
    "<source_sha256>": {
      "title": "...",
      "slug": "...",
      "chapters": [
        {"label": "Capitolo I", "aligned_page_start": 12, "aligned_page_end": 34, "original_page_start": 14, "original_page_end": 36}
      ]
    }
  }
  ```
- `polyindex/INDEX.json` aggiornato con struttura:
  ```json
  {
    "<canonical_subject_id>": {
      "canonical_label": "Marco Polo",
      "aliases": ["M. Polo", "Polo, Marco"],
      "books": {
        "<source_sha256>": {"aligned_pages": [12, 18, 22], "original_pages": [14, 20, 24]}
      }
    }
  }
  ```
- Aggiornamento di `polyindex/*.json` è **atomico**: scrittura su file temporaneo + `os.replace`; lettura→merge→scrittura protetta da lock (file lock o asyncio.Lock).
- AI subject matcher è in MVP: usato in T26 per associare un soggetto nuovo a un canonical esistente; sempre fallback deterministico se endpoint AI non risponde.
- Snapshot giornaliero (cron interno via APScheduler stdlib-friendly o task asyncio) salva copia di `biblioteca.csv` in `data/db/checkpoints/biblioteca.YYYY-MM-DD.csv` e copie di `polyindex/*.json` in `data/polyindex/checkpoints/YYYY-MM-DD.*.json`. Endpoint `POST /api/admin/checkpoint` per snapshot on-demand.
- Telemetria minima: tabella `pipeline_runs` con `request_id`, `source_sha256`, stato finale, contatori, `pipeline_version`.

### 2.5 Acceptance Criteria — Fase 2 Ricerca (MVP: passi **a, b, c, d** del manoscritto)

- Endpoint `POST /api/research/submit` accetta body JSON `{query: str, poh?: {id, label, time_range?}, options?: {max_books?, max_pages_per_book?}}`. Ritorna 202 con `{request_id, status: "accepted"}`.
- Endpoint `GET /api/research/{request_id}` ritorna stato job (`accepted|running|succeeded|failed`) + `pipeline_version` + `last_error?` + ultimi N eventi.
- Endpoint `GET /api/research/{request_id}/article` ritorna il prodotto finale: `{markdown, citations: [...], pohs_referenced: [{poh_id, label, linked_from_count}], timeline_rows: [{period, event, source_links[]}]}`. I campi strutturati duplicano ciò che è già nel Markdown per consumo programmatico.
- Pipeline di ricerca deterministica nel pre-filtro:
  1. Lookup in `polyindex/INDEX.json`: estrazione candidati `{libro_sha256: [pagine]}` dai soggetti rilevanti per la query (normalizzazione + AI matching dei soggetti della query con i `canonical_label`/`aliases`).
  2. Espansione capitoli via `polyindex/TOC.json`: per ogni pagina candidata, recupero del capitolo che la contiene; aggiunta delle pagine vicine se il capitolo è < 6 pagine.
  3. Caricamento contenuti: lettura dei `pages/p.NNNN.<slug>.md` corrispondenti da `data/output/<sha>/`.
  4. **Passo `a`–`b`**: generazione bozza articolo (1+ chiamate LLM) con `src/search/prompts/article_prompt.md`: stile Wikipedia; **solo** link Markdown alle fonti (CommonMark), niente `<a href>`.
  5. **Passo `c`**: pass successivo dedicato (`src/search/prompts/poh_links_prompt.md`) **oppure** stesso turno se il prompt unico include istruzioni esplicite: ogni menzione di un POH (identificato da elenco `poh_candidates` derivato da INDEX + query) diventa `[etichetta visibile](poh:<poh_id>)`. Il POH principale della request **non** va linkato a se stesso nel primo paragrafo di lead; ripetizioni successive sì. Regole complete in §2.5.1.
  6. **Passo `d`**: generazione o validazione blocco `## Cronologia` (vedi §2.5.1) con LLM + vincolo strutturale (tabella GFM) e validazione post-hoc (date non inventate senza fonte linkata nella stessa riga).
  7. Post-processing: parsing di tutti i link `(...)` nel Markdown, validazione URL `source:` e `poh:`, allineamento con `citations` JSON.
- L'articolo è prodotto in **italiano**. Output principale = stringa Markdown UTF-8; niente HTML come formato primario (eccezione: entità già presenti nelle fonti restano escaped come nel sorgente).
- Costi predicibili: pre-filtro deterministico produce un budget di contesto bounded (`max_books`, `max_pages_per_book`, default 5 libri × 8 pagine).
- Idempotenza: stessa query + stesso stato polyindex → stesso `request_id` se ripetuta entro 1h (hash query+poh+polyindex_version come dedup key), opzionale via flag.

#### 2.5.1 Formattazione Markdown (fonti, POH, cronologia)

Convenzione **CommonMark** + **GitHub Flavored Markdown** per tabelle. Tutti i link usano la forma `[testo destinazione](URL)` dove `URL` è uno dei seguenti schemi (nessuno spazio non encoded dentro le parentesi).

**B — Link alle fonti (pagine libro)**  
- Forma canonica consigliata per il file su disco e per tooling interno:
  `source:<source_sha256>:aligned:<p>` dove `<p>` è la **pagina allineata** 1-based (coerente con i file `p.NNNN.<slug>.md`).
  Esempio nel Markdown: `[Battaglia di Curzola, pp. 112–114](source:a1b2…f00:aligned:112)`.
- **Descrizione umana** dentro `[]`: titolo breve del fatto + riferimento pagina; ripetere il link a ogni paragrafo che dipende da quella pagina **oppure** usare riferimenti a nota a piè con secondo round di post-processing (v1.1 se non in MVP: in MVP basta link inline ripetuto o frase “Vedi fonti in Cronologia”).
- Il post-processore **deve** risolvere ogni `source:` contro `manifest.json` del libro; link con sha o pagina invalida → rimossi e sostituiti con `*[[fonte non verificabile]]*` + log.

**C — Link ad altri POH**  
- Forma: `[Nome leggibile](poh:<poh_id>)` dove `<poh_id>` è stabile (es. `subj_marco_polo` allineato al `canonical_subject_id` in `INDEX.json`, oppure `poh.uuid…` se generato).  
- Non usare URL `http(s):` verso articoli POH in MVP (non esistono ancora host stabili); lo schema `poh:` è un **placeholder risolvibile** da viewer/CLI (`research open poh:…`).  
- Se l’entità non è nel registro: `[Nome](poh:unknown-<slug-normalizzato>)` e in coda al documento una sezione `## Annotazioni` con bullet `TODO: risolvere poh:unknown-…`.

**D — Vertical time bar come Markdown**  
Nel file, la “barra” è la **lista verticale** ordinata dal più antico al più recente. Obbligo di sezione:

```markdown
## Cronologia

| Periodo | Evento | Fonti |
|---------|--------|-------|
| 1271–1295 | Marco Polo intraprende il viaggio verso la Cina. | [Sintesi dalle fonti](source:…:aligned:…) |
```

Regole:
- Titolo sezione esattamente `## Cronologia` (H2, UTF-8).
- Tabella con **esattamente** tre colonne nell’ordine indicato; intestazioni fisse (`Periodo`, `Evento`, `Fonti`).
- Ogni riga della colonna **Fonti** contiene almeno un link `source:` valido **oppure** `—` se l’evento è solo contesto temporale desunto da una fonte già citata nella riga precedente (massimo 1 riga consecutiva così; altrimenti Ogni evento ha fonte).
- Ordine righe: cronologico crescente (evento più vecchio in alto). Questo ordine verticale è ciò che la UI può proiettare su una time bar laterale senza cambiare il sorgente.
- Vietato Mermaid o HTML per la tabella in MVP (solo pipe table GFM).

### 2.6 Non-Goals

- Nessun Milvus, FAISS o backbone di retrieval vettoriale dedicato (eliminato dalla visione di prodotto).
- Nessuna UI di ricerca avanzata in MVP (solo API).
- Nessuna multi-tenancy.
- Nessuna autenticazione utente (deployment è interno/single-user in MVP).
- Nessuna migrazione di DB diversi da SQLite in MVP.
- Nessun fine-tuning di modelli; usiamo modelli generalisti via endpoint OpenAI-compatible.

## 3. AI System Requirements

### 3.1 Modelli e usi

| Stage | Tipo | Modello (config) | Output atteso |
|---|---|---|---|
| OCR (Stage 1) | OCR locale (no LLM) | `easyocr` | Testo grezzo per pagina |
| Vision refine (Stage 2) | LLM multimodale | `VISION_MODEL` | Markdown fedele alla pagina |
| Editor refine (Stage 3) | LLM testuale | `EDITOR_MODEL` | Markdown normalizzato |
| Subject Matcher | Embeddings + LLM dirimitore | `MATCHER_EMBEDDING_MODEL` + `MATCHER_LLM_MODEL` | `canonical_subject_id` |
| Research Article | LLM testuale (long context utile) | `RESEARCH_MODEL` | Markdown completo (a–d): corpo + link `source:` + link `poh:` + `## Cronologia` |

Tutti i modelli sono raggiungibili via client OpenAI-compatible. Stessa istanza centralizzata in `src/core/openai_client.py` (T12a).

### 3.2 Prompt di sistema (file in repository)

I testi di sistema per gli LLM sono **file Markdown nel repo**; la cronologia delle modifiche è quella di **Git**, non suffissi tipo `v1`/`v2` né variabili d’ambiente `*_PROMPT_VERSION`.

- `src/ingestion/pipeline/prompts/vision_prompt.md` — Stage 2 Vision
- `src/ingestion/pipeline/prompts/editor_prompt.md` — Stage 3 Editor (T13)
- `src/ingestion/polyindex/prompts/subject_matcher_prompt.md` — Subject matcher (T25)
- `src/search/prompts/article_prompt.md` — ricerca: articolo (`a`–`b`)
- `src/search/prompts/poh_links_prompt.md` — ricerca: link POH (`c`); opzionalmente fuso nello stesso turno di `article_prompt.md`
- `src/search/prompts/timeline_prompt.md` — ricerca: sezione `## Cronologia` (`d`)

Mai prompt hardcoded in Python. La cache Stage 2 è legata al modello Vision; dopo modifiche a `prompts/vision_prompt.md` usa `force_recompute` o cancella `stage2Vision/` sotto `tmp` se serve rigenerare tutto.

### 3.3 Evaluation Strategy

- **MVP**: smoke E2E con 1 libro reale ridotto (4–6 pagine) + 5 query mock; verifica passi **a–d**: presenza `## Cronologia` con tabella valida, almeno un `source:` per riga datata, almeno un `poh:` se il testo menziona un secondo soggetto noto in INDEX.
- **v1.1**: gold set di 20 query con expected_books/expected_subjects; metriche: subject recall (matcher), citation precision (research), article informativeness (rating umano 1–5).
- **POI**: benchmark periodico (mensile) sulla gold set + regression test.

### 3.4 Safety/Guardrail

- System prompt vincolante in stile "rispondi solo se sostenuto dalle pagine fornite, altrimenti dichiara l'incertezza".
- `temperature` default 0.1 per Vision/Editor, 0.3 per Research (più libertà narrativa ma stesso vincolo di fonti).
- Mai loggare testo OCR > 200 caratteri, mai loggare chiavi API.

## 4. Technical Specifications

### 4.1 Architettura — Fase 1 Upload

```mermaid
flowchart TD
  form[Form HTTP / API] --> validate[validate_and_enrich_request]
  validate --> hashGate[SourceHashGate]
  hashGate -->|new_hash| align[PdfAlignment]
  hashGate -->|duplicate| auditOnly[AuditMetadataOnly]
  align --> render[RenderPagesPNG]
  render --> stage1[Stage1 OCR easyocr]
  stage1 --> stage2[Stage2 Vision LLM]
  stage2 --> stage3[Stage3 Editor LLM]
  stage3 --> writer[OutputWriter pages/ + slug.md]
  writer --> tocMd[TOC.md]
  writer --> indexMd[INDEX.md]
  tocMd --> polyTocUpdater[Polyindex TOC.json Updater]
  indexMd --> indexParser[INDEX.md Parser]
  indexParser --> subjectMatcher[AI Subject Matcher]
  subjectMatcher --> polyIndexUpdater[Polyindex INDEX.json Updater]
  polyTocUpdater --> snapshot[Daily Snapshot]
  polyIndexUpdater --> snapshot
  writer --> reicatStore[SQLite books + pipeline_runs]
  auditOnly --> reicatStore
```

### 4.2 Architettura — Fase 2 Ricerca

```mermaid
flowchart TD
  q[POST /api/research/submit] --> qval[ResearchInputValidation]
  qval --> qreg[ResearchJobRegistry]
  qreg --> lookup[INDEX.json Subject Lookup]
  lookup --> expand[TOC.json Chapter Expansion]
  expand --> loader[Pages Markdown Loader]
  loader --> llm1[LLM Article a+b]
  llm1 --> llm2[LLM POH links c]
  llm2 --> llm3[LLM Timeline d]
  llm3 --> postproc[Link Parser + Validator]
  postproc --> response[GET /api/research/id/article]
```

Nota: i tre passi LLM possono essere **fusi** in una o due chiamate se i prompt lo consentono; il diagramma descrive la **responsabilità logica** richiesta in output.

### 4.3 Modello di esecuzione HTTP

- **Upload**: `POST /api/ingest/submit` (multipart streaming) → 202 con `request_id`. `GET /api/ingest/{id}` ritorna stato. `GET /api/ingest/{id}/artifacts` elenca file in `data/output/<sha256>/`.
- **Research**: `POST /api/research/submit` (JSON) → 202 con `request_id`. `GET /api/research/{id}` stato. `GET /api/research/{id}/article` prodotto finale.
- **Admin**: `POST /api/admin/checkpoint` → 202; `GET /health`.
- Backend job in-process via asyncio TaskGroup; nessun broker esterno. Job registry separati per `ingest` e `research`, stessa struttura base (`request_id`, `status`, `events`, `pipeline_version`).

### 4.4 Integration Points

- **OCR/LLM**: `src/core/openai_client.py` come unica fabbrica di client (T12a). Endpoint configurato via `.env`.
- **Persistence**: `data/db/biblioteca.csv` (SQLite binario; estensione `.csv` = naming prodotto). Tabelle: `books`, `book_metadata_audit`, `pipeline_runs`, `_schema_migrations`, (future) `research_runs`, `subject_embeddings`. Schema versioning esplicito (PRE-B).
- **Artifacts cross-book**: `data/polyindex/TOC.json`, `data/polyindex/INDEX.json`, con `checkpoints/` giornalieri.
- **Per-book artifacts**: `data/output/<sha256>/{pages/, <slug>.md, TOC.md, INDEX.md, manifest.json}`.

### 4.5 Runtime configuration (`.env` esteso)

Nuove variabili rispetto all'attuale `example.env`:

```
# --- Polyindex / Subject matcher ---
MATCHER_EMBEDDING_MODEL=text-embedding-3-small
MATCHER_LLM_MODEL=gpt-4.1-mini
MATCHER_SIMILARITY_THRESHOLD=0.86
MATCHER_USE_AI=true

# --- Research ---
RESEARCH_MODEL=gpt-4.1-mini
RESEARCH_MAX_BOOKS=5
RESEARCH_MAX_PAGES_PER_BOOK=8
RESEARCH_TEMPERATURE=0.3

# --- Checkpoints ---
CHECKPOINT_DAILY_ENABLED=true
CHECKPOINT_RETENTION_DAYS=30

# Prompt: file .md nel repository (vedi §3.2); nessuna variabile *_PROMPT_VERSION.
```

### 4.6 Security & Privacy

- Chiavi/API key esclusivamente in `.env`, mai loggate, mai stampate.
- Log strutturato opzionale su file giornaliero via `Log(..., to_file=True)` in `src/core/log.py` (JSONL sotto `log_dir`, default `./log`); console resta colorata. Mai loggare chiavi API; testo OCR troncato con `safe_text`.
- Path assoluti contenenti utente di sistema mai esposti via API; sempre relativi a `DATA_ROOT`.
- Tracciabilità per libro: `pipeline_runs.request_id`, `source_sha256`, `pipeline_version`, timestamp.
- Tracciabilità per ricerca: `research_runs.request_id`, hash query, libri/pagine usati come contesto (audit).
- Nessun PII di default (i libri sono pubblicazioni); se in futuro si trattano contenuti personali, va aggiunta sezione GDPR.

### 4.7 Open Questions tecniche

- **OQ1**: thread-safety di scrittura `polyindex/*.json` quando 2 ingest finiscono contemporaneamente → adottato file lock (`fcntl` su Unix) + atomic replace; sufficiente per single-process FastAPI ma da rivedere se passiamo a multi-worker.
- **OQ2**: dimensione massima di `INDEX.json` prima di sharding (es. per soggetto inizia con A, B, ...) → posticipato a v2.0.
- **OQ3**: cache embeddings dei soggetti canonici → serve persistenza? Probabilmente sì in SQLite (`subject_embeddings`) per evitare rigenerazioni; vedi T24.

### 4.8 Logging (`src/core/log.py`)

Modulo unico di logging: **console colorata** (default) + opzioni audit su file/return JSON.

**Flusso obbligatorio**

1. All’avvio del processo, chiamare **`logInit`** una sola volta (livello globale + cartella log opzionale).
2. Ogni **`Log`** rispetta la gerarchia di livello, salvo **`override=True`**.

**Inizializzazione**

```text
logInit({ERROR|WARNING|INFO|DEBUG|RESULT}_LOG_LEVEL [, log_dir="./log"])
```

- **`log_dir`**: directory per i file giornalieri `{YYYY-MM-DD}.log` (creata on demand). Default `./log`.

**Chiamata**

```text
Log(level, "message" [, params: dict] [, override: bool] [, json: bool] [, to_file: bool]) -> str | None
```

- **`params`**: dizionario opzionale; in console appare in grigio accanto al messaggio. Con context attivo (vedi sotto) include automaticamente `request_id` e `source_sha256`.
- **`override`**: stampa la riga ignorando il filtro globale.
- **`json=True`**: restituisce una stringa JSON con gli stessi campi del record (`ts`, `level`, `file`, `line`, `caller`, `message`, + chiavi da `params`). La console resta **sempre** nel formato colorato attuale.
- **`to_file=True`**: append di una riga JSON (JSONL) su `{log_dir}/{YYYY-MM-DD}.log`. Scrittura thread-safe.

**Context di run (T18b)**

```python
bind_log_context(request_id=..., source_sha256=...)
# ... Log(...) durante la run ...
reset_log_context(request_token, sha_token)
```

- Invocato all’inizio di `run_pipeline`; ogni `Log` nella run eredita `request_id` / `source_sha256` senza ripassarli.
- **`log_stage_block_async(stage_name)`**: log start/end con `duration_ms` (usato sullo stage `pipeline` nell’orchestrator).

**Helper**

- **`safe_text(s, max_len=200)`**: troncamento testo OCR (o altro) nei log.

**Esempi**

```python
from src.core.log import logInit, Log, INFO_LOG_LEVEL, bind_log_context, reset_log_context

logInit(INFO_LOG_LEVEL, log_dir="./log")
Log(INFO_LOG_LEVEL, "pipeline avviata")
Log(INFO_LOG_LEVEL, "dettaglio pagina", {"page": 12}, json=True, to_file=True)
```

**Correlazione audit**: log (console/file) + tabella `pipeline_runs` (T14d) ricostruiscono una run via `request_id`.

## 5. Risks & Roadmap

### 5.1 Phased Rollout

**MVP (sblocca prodotto)**:
- PRE-A–PRE-C (✅): prerequisiti tecnici (parallelismo PDF, migrations SQLite, `pyproject.toml`).
- T1–T10 (✅ già completati).
- **T11 — OCR + ingest Stage 1 (✅ completato)**: T11(a–c) — `OCRPageEngine`/`EasyOCRPageEngine`, renderer PNG (`pypdfium2`), persistenza/cache Stage 1 (`stage1OCR`); **T11.5** — cablaggio HTTP sincrono su `POST /api/ingest/submit` fino a fine Stage 1 (`stage1` in risposta).
- **T12 — Vision Stage 2 (✅ completato)**: T12(a–c) — client OpenAI centralizzato (`src/core/openai_client.py`), `refine_with_vision` + `prompts/vision_prompt.md`, persistenza/cache Stage 2 (`stage2Vision`); **T12.5** — cablaggio HTTP (`stage2` in risposta, `_ACTIVE_PAGE_STAGES = 2`).
- **T13 — Editor Stage 3 (✅ completato)**: T13(a–b) — `refine_with_editor` + `prompts/editor_prompt.md`, persistenza/cache Stage 3 (`stage3Editor/`, sidecar idempotente); **T13.5** — cablaggio HTTP (`stage3` in risposta, `_ACTIVE_PAGE_STAGES = 3`, `STATUS_DONE` su `PHASE_STAGE3_EDITOR`).
- **T14 — Orchestrazione concorrente (✅ completato)**: T14(a) — `src/ingestion/orchestrator.py` con `PageJob`, `run_pipeline` batch-per-stage (render → stage1 → stage2 → stage3), `asyncio.Semaphore` per concorrenza intra-stage, swap Vision→Editor, eventi `IngestJobEvent`; T14(b) — `src/core/retry.py` + `src/core/errors.py`; T14(c) — `src/core/rate_limit.py`; T14(d) — migration 003 `pipeline_runs`, create/update in orchestrator, propagazione `request_id` in eventi.
- **T15–T17 (✅ completato)**: writer pagine + `manifest.json` (`output_writer.py`), builder `TOC.md` (`toc_builder.py`), builder `INDEX.md` (`index_builder.py`); integrati in orchestrator per T15, T16/T17 standalone (cablaggio orchestrator completo con T22/T30).
- **T18 — Logging + audit (✅ completato)**: T18(a) — estensione `src/core/log.py` (`json`, `to_file`, `log_dir`, `safe_text`); T18(b) — `bind_log_context` + `log_stage_block_async` in `run_pipeline`, correlazione con `pipeline_runs`; test `tests/test_logging.py`, `tests/test_logging_propagation.py`.
- **T22 (NUOVO)**: builder `<NomeLibro>.md` aggregato.
- T18.5(a–d): refactor HTTP async + job model.
- T19–T21: smoke/E2E e test HTTP.
- **T23 (NUOVO)**: builder `polyindex/TOC.json` (deterministico, idempotente).
- **T24 (NUOVO)**: parser `INDEX.md` → struttura `{subject_raw: [pages]}`.
- **T25 (NUOVO)**: AI Subject Matcher (normalizzazione + embeddings + LLM dirimitore + persistence dei canonical).
- **T26 (NUOVO)**: builder `polyindex/INDEX.json` con merge atomico + AI matching cross-book.
- **T27 (NUOVO)**: checkpoint daily/on-demand DB + polyindex.
- **T28 (NUOVO)**: cleanup `data/tmp/<sha>/` su successo (configurabile, default keep).
- **T29 (NUOVO)**: web UI singola pagina di upload (`web/index.html`) collegata al `POST /api/ingest/submit`.
- T30 (NUOVO): orchestrazione end-to-end Upload con tutti gli stadi cablati nel job registry.
- **F2 — Ricerca (passi manoscritto a–d)**: **F2-T1..F2-T10** come da §7 (schema, lookup, loader, LLM article/POH/timeline, HTTP, `research_runs`, E2E).

**v1.1**:
- UI ricerca (`web/search.html`).
- Gold set ampliata (20 query) + eval automatizzata + metriche formalizzate.

**Nota**: i passi Manoscritto `c` e `d` sono **in MVP**; ciò che qui restava come “v1.1 sul manoscritto” è stato assorbito sopra.

**v2.0**:
- Sharding `INDEX.json`.
- Multi-worker FastAPI con lock distribuito (Redis o equivalente leggero).
- Recovery di ingest interrotti senza riavvio manuale.

### 5.2 Technical Risks

- **R1**: matcher AI genera falsi merge di soggetti distinti (es. due "Marco Polo" diversi). Mitigazione: soglia conservativa, audit log dei merge, comando di rollback per soggetto.
- **R2**: articolo di ricerca cita pagine inventate. Mitigazione: post-validatore deterministico (citazione → pagina esistente o citazione scartata + warning nel log).
- **R3**: crescita lineare di `INDEX.json` rallenta lookup. Mitigazione: caricamento in memoria con cache LRU; sharding rimandato a v2.0.
- **R4**: divergenza endpoint locale vs remoto su Vision/Editor. Mitigazione: prompt in `src/ingestion/pipeline/prompts/`; temperature bassa; smoke test che gira con entrambi.
- **R5**: snapshot giornaliero rompe atomicità durante un ingest in corso. Mitigazione: snapshot acquisisce lo stesso lock di scrittura del polyindex.

## 6. Struttura del repository

La struttura cartelle (albero, principi, linee guida) è documentata in [`README.md`](README.md). Questo PRD non duplica il layout.

Differenze chiave rispetto al README attuale (richieste da questo PRD):

- Rinominare `data/polyndex/` → `data/polyindex/` (fix typo, allineato al manoscritto).
- Modulo `src/ingestion/pipeline/` con `engine.py`, `render.py`, `stage1.py`, `stage2.py` (Vision), `stage3.py` (Editor), cartella **`prompts/`** (`vision_prompt.md`, `editor_prompt.md`); prompt ricerca e matcher come da §3.2.
- **T14**: `src/ingestion/orchestrator.py` (batch-per-stage); `src/core/retry.py`, `src/core/errors.py`, `src/core/rate_limit.py`; `src/persistence/pipeline_runs.py` (migration 003 `pipeline_runs`).
- **T11.5**: ingest HTTP — `src/api/ingest_http_server.py` invoca lo Stage 1 dopo l’enumerazione; `src/api/ingest_form.py` per parsing multipart/payload form; `resolve_aligned_pdf_path_for_stage1` in `pdf_alignment.py` quando `pdf_alignment` è assente (es. skip per hash duplicato) ma serve il PDF allineato su disco.
- Aggiungere `src/ingestion/polyindex/` (T23–T26).
- Aggiungere `src/search/` con i prompt in §3.2, `lookup.py`, `article.py`, `api.py` (F2-T1+).
- Nome file DB runtime: `data/db/biblioteca.csv` (SQLite; vedi glossario e `Settings.sqlite_path`).
- Aggiungere `src/core/checkpoints.py` (T27).
- Aggiungere `web/index.html` con form di submit.

## 7. Backlog task atomiche e stato

Legenda: `[x]` completata, `[ ]` da fare, `[~]` in corso. Modello consigliato indicato per ogni task: **Opus** (logica complessa, prompt engineering, architettura cross-modulo), **Sonnet** (codice deterministico, stato, test), **Composer 2** (scaffolding, IO file, boilerplate).

### Fase 1 — Upload (completate)

- [x] **T1** — Definire contratto input ingestione.
- [x] **T2** — Validazione input.
- [x] **T3** — Loader configurazione `.env`.
- [x] **T4** — Calcolo `sha256` sorgente.
- [x] **T5** — SourceHashGate.
- [x] **T6** — Schema SQLite minimo.
- [x] **T7** — Upsert REICAT per hash.
- [x] **T8** — Skip path completo.
- [x] **T9** — PdfAlignment deterministico.
- [x] **T10** — Enumerazione pagine utili.
- [x] **PRE-A** — `ProcessPoolExecutor` in `pdf_alignment.py`. *(Sonnet)*
- [x] **PRE-B** — `_schema_migrations` + migrations in `book_sqlite.py`. *(Sonnet)*
- [x] **PRE-C** — `pyproject.toml` + dipendenze runtime complete. *(Composer 2)*
- [x] **T11(a)** — Wrapper OCR Protocol + EasyOCRPageEngine. *(Sonnet)*
- [x] **T11(b)** — PDF page renderer con pypdfium2. *(Sonnet)*
- [x] **T11(c)** — Persistenza Stage 1 + cache idempotente. *(Sonnet)*
- [x] **T11.5** — Cablaggio Stage 1 in `POST /api/ingest/submit`: dopo gate/allineamento/enumerazione esegue OCR su pagine utili, risponde con `stage1`; risoluzione path PDF allineato se lo skip duplicato non popola `pdf_alignment`. *(Sonnet)*
- [x] **T12(a)** — Client OpenAI-compatible centralizzato. *(Sonnet)*
- [x] **T12(b)** — refine_with_vision + `prompts/vision_prompt.md`. *(Sonnet)*
- [x] **T12(c)** — Persistenza Stage 2 + cache idempotente (`stage2Vision`). *(Sonnet)*
- [x] **T12.5** — Cablaggio Stage 2 Vision in `POST /api/ingest/submit` dopo il completamento dello Stage 1; `stage2` nel payload; `_ACTIVE_PAGE_STAGES = 2`. *(Sonnet)*
- [x] **T13(a)** — refine_with_editor + `prompts/editor_prompt.md`. *(Sonnet)*
- [x] **T13(b)** — Persistenza Stage 3 + diff per pagina (`stage3Editor/`, sidecar JSON idempotente). *(Sonnet)*
- [x] **T13.5** — Cablaggio Stage 3 Editor in `POST /api/ingest/submit` dopo Stage 2 Vision: chiama `run_stage3_editor` con stesso client OpenAI, emette eventi `PHASE_STAGE3_EDITOR` (STARTED/COMPLETED/DONE), aggiunge `stage3` al payload, `_ACTIVE_PAGE_STAGES = 3`, `STATUS_DONE` terminale su `PHASE_STAGE3_EDITOR`; skip se `pipeline_skipped`. *(Sonnet)*
- [x] **T14(a)** — Orchestrazione batch-per-stage in `src/ingestion/orchestrator.py`: `PageJob`, render di tutte le pagine, esecuzione sequenziale stage1 → stage2 → stage3 con concorrenza intra-stage via `asyncio.Semaphore` (`settings.max_parallel_request`), swap Vision→Editor, pubblicazione `IngestJobEvent` al registry; stage1 reso async (allineato a stage2/3). *(Opus)*
- [x] **T14(b)** — Retry centralizzato: `src/core/retry.py` (`retry_async`), `src/core/errors.py` (`TransientError`/`PermanentError`, `classify_openai_exception`); refactor `openai_client.py` e retry OCR in `stage1.py`; test `tests/test_retry.py`. *(Sonnet)*
- [x] **T14(c)** — Rate-limit token-bucket: `src/core/rate_limit.py` (`AsyncTokenBucket`, singleton lazy per client); sostituisce il limiter a intervallo fisso in `openai_client.py`; test `tests/test_rate_limit.py`. *(Sonnet)*
- [x] **T14(d)** — Telemetria run: migration 003 tabella `pipeline_runs` (`src/persistence/pipeline_runs.py`: `create_pipeline_run`, `mark_pipeline_run_finished`, `get_pipeline_run_by_request_id`); integrazione in orchestrator (create al T0, update finale succeeded/failed con contatori); `request_id` in tutti gli `IngestJobEvent` e nei log strutturati stage1/2/3; test `tests/test_pipeline_runs.py`. *(Sonnet)*
- [x] **T15** — Persistenza pagine `.md` + `manifest.json` (`src/ingestion/output_writer.py`, `materialize_book_pages`); integrazione post-stage3 in orchestrator; test `tests/test_output_writer.py`. *(Composer 2)*
- [x] **T16** — Builder `TOC.md` (`src/ingestion/toc_builder.py`, range `toc_range_aligned`); test `tests/test_toc_builder.py`. *(Composer 2)*
- [x] **T17** — Builder `INDEX.md` (`src/ingestion/index_builder.py`, range `index_range_aligned`); test `tests/test_index_builder.py`. *(Composer 2)*
- [x] **T18(a)** — Logging esteso in `src/core/log.py`: `json`, `to_file`, `log_dir` in `logInit`, `safe_text`; console invariata; test `tests/test_logging.py`. *(Sonnet)*
- [x] **T18(b)** — Propagazione audit: `bind_log_context` / `reset_log_context`, `log_stage_block_async` in `run_pipeline`, correlazione log ↔ `pipeline_runs`; test `tests/test_logging_propagation.py`. *(Sonnet)*

### Fase 1 — Upload (writer per libro)

- [ ] **T22 (NUOVO)** — Builder `<slug>.md` (Σ pages). *(Composer 2)*

### Fase 1 — Upload (HTTP refactor)

- [ ] **T18.5(a)** — Bootstrap FastAPI. *(Sonnet)*
- [ ] **T18.5(b)** — Submit con upload streaming. *(Sonnet)*
- [ ] **T18.5(c)** — Job model in-process. *(Opus)*
- [ ] **T18.5(d)** — Status + artifacts endpoints. *(Sonnet)*

### Fase 1 — Test E2E e HTTP

- [x] **T19** — Smoke test end-to-end (validazione/edge case).
- [x] **T20** — Smoke test duplicate hash.
- [ ] **T19'** — Smoke E2E nuovo hash (reale, no rete). *(Sonnet)*
- [ ] **T21(a)** — Test form mapping HTTP. *(Sonnet)*
- [ ] **T21(b)** — E2E HTTP submit→poll→artifacts. *(Sonnet)*

### Fase 1 — Upload (polyindex e biblioteca cross-book)

- [ ] **T23 (NUOVO)** — Polyindex TOC.json updater (deterministico, atomic + lock). *(Sonnet)*
- [ ] **T24 (NUOVO)** — Parser deterministico `INDEX.md` → soggetti grezzi + pagine. *(Sonnet)*
- [ ] **T25 (NUOVO)** — AI Subject Matcher (2-stadi: normalizzazione + embeddings + LLM dirimitore). *(Opus)*
- [ ] **T26 (NUOVO)** — Polyindex INDEX.json updater con merge atomico. *(Opus)*
- [ ] **T27 (NUOVO)** — Checkpoint daily + on-demand DB e polyindex. *(Composer 2)*
- [ ] **T28 (NUOVO)** — Cleanup `data/tmp/<sha>/` policy. *(Composer 2)*

### Fase 1 — Upload (UX e cablaggio finale)

- [ ] **T29 (NUOVO)** — `web/index.html` form unico di upload operatore. *(Composer 2)*
- [ ] **T30 (NUOVO)** — Orchestratore end-to-end Upload cablato (gate→align→render→OCR×3→writer×4→polyindex×2→snapshot). *(Opus)*

### Fase 1 — Test E2E cross-book

- [ ] **T31 (NUOVO)** — E2E cross-book: 2 libri ingestiti → `polyindex/*.json` aggregato correttamente. *(Sonnet)*

### Fase 2 — Ricerca (MVP: passi **a–d** del manoscritto)

- [ ] **F2-T1 (NUOVO)** — Schema input ricerca (`ResearchRequest` Pydantic) + validazione. *(Sonnet)*
- [ ] **F2-T2 (NUOVO)** — Subject Lookup deterministico su `polyindex/INDEX.json` (normalizzazione + match) + AI fallback su soggetti residui. *(Opus)*
- [ ] **F2-T3 (NUOVO)** — Chapter Expansion su `polyindex/TOC.json` (pagine candidate → capitolo → pagine vicine, con budget). *(Sonnet)*
- [ ] **F2-T4 (NUOVO)** — Pages Markdown Loader (carica `pages/p.NNNN.<slug>.md` per pagine candidate, taglia/normalizza). *(Composer 2)*
- [ ] **F2-T5 (NUOVO)** — Article Generation LLM (`article_prompt.md`): passi `a` + `b` con link `source:` come da §2.5.1. *(Opus)*
- [ ] **F2-T6 (NUOVO)** — POH link pass LLM (`poh_links_prompt.md`) o fusione in F2-T5: passo `c`. *(Opus)*
- [ ] **F2-T7 (NUOVO)** — Timeline pass LLM (`timeline_prompt.md`): passo `d`, sezione `## Cronologia` tabella GFM. *(Opus)*
- [ ] **F2-T8 (NUOVO)** — Aggregatore Markdown finale + post-validatore link/tabellare + endpoint HTTP (`POST /api/research/submit`, `GET /{id}`, `GET /{id}/article`) + job registry `research`. *(Sonnet)*
- [ ] **F2-T9 (NUOVO)** — Tabella `research_runs` + audit pagine/soggetti usati; propagazione `request_id` nei log. *(Sonnet)*
- [ ] **F2-T10 (NUOVO)** — E2E ricerca: 2 libri ingestiti + query che richiede POH secondario + verifica `poh:` + `## Cronologia` + `source:`. *(Sonnet)*

### Fase 2 — Ricerca (v1.1 e oltre)

- [ ] **F2-T11** — UI ricerca (`web/search.html`). *(Composer 2)*
- [ ] **F2-T12** — Gold set 20+ query + metriche automatiche (recall/precision). *(Sonnet)*

## 8. Glossario

- **POH**: entità di dominio (persona, luogo, evento, concetto storico), id stabile + label + time_range opzionale.
- **Polyindex**: insieme dei due file globali `polyindex/TOC.json` (struttura capitoli cross-book) e `polyindex/INDEX.json` (soggetti canonici cross-book).
- **REICAT**: standard di catalogazione bibliografica italiano usato per i metadati libro.
- **Slug libro**: forma normalizzata del titolo (max 32 char, `[a-z0-9-]`); usato nei nomi file delle pagine.
- **Pagina allineata**: numerazione 1-based del PDF dopo applicazione di `pages_to_remove`. Corrisponde a "pp.libro" del manoscritto.
- **Source SHA-256**: digest del PDF originale; chiave universale del libro nel sistema.
- **Subject canonical**: forma canonica di un soggetto INDEX dopo deduplica AI (es. "Marco Polo" canonical, "M. Polo" alias).
- **Link `source:`**: schema URI interno per citare una pagina libro, formato `source:<source_sha256>:aligned:<p>` (vedi §2.5.1); va validato contro `manifest.json`.
- **Link `poh:`**: schema URI interno per riferire un POH, formato `poh:<poh_id>`; risoluzione lato viewer/CLI; fallback `poh:unknown-<slug>` quando l’id non è noto.
- **`biblioteca.csv`**: file SQLite principale sotto `data/db/`; estensione richiesta dal prodotto, contenuto binario SQLite (non CSV testuale).
