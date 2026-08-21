# Prompts — Sessioni / bozze ingest (`2026_08_21`)

> **PRD:** [`PRD-Ingest-Drafts.md`](./PRD-Ingest-Drafts.md)  
> **Repo:** `librarAIn-server` (UI ingest in `web/`, API in `src/api/`)  
> **Sub-agent:** ogni slice = **un** Task Cursor con model slug **`cursor-grok-4.5-high`** (Grok 4.5), prompt copy-paste unico. Esecuzione **in serie** sullo stesso branch.  
> **Checkpoint:** ogni slice → **STOP test umano** prima del commit; nessun commit senza richiesta esplicita.

## Escalation (stesso item)

| Tier | Max prompt | Successivo |
|------|------------|------------|
| Grok 4.5 | 6 | Chiedi utente: riformula / spezza / manuale |

## Batch: 2026_08_21

### Stato sintetico

| ID | Titolo | Dipende da | Stato |
|----|--------|------------|-------|
| F-001 | API + schema `is_draft` (list/save/clear/delete) | — | done — test ok |
| F-002 | UI home: lista bozze + Nuova bozza | F-001 | done — attesa test umano |
| F-003 | UI Apri bozza: re-upload + hash match + restore | F-002 | done — attesa test umano |
| F-004 | UI Salva Bozza + Elimina + submit ↔ lista | F-001, F-003 | done — attesa test umano |

**Ordine rigido:** F-001 → F-002 → F-003 → F-004.

---

### [x] F-001 — API + schema `is_draft`
- **PRD:** US-3 / US-4 (backend) — `PRD-Ingest-Drafts.md`
- **Prompt:**
  > Implementa **solo** F-001 del PRD `PRD-Ingest-Drafts.md` in `librarAIn-server`. Scope unico: persistenza bozze lato server (niente UI).
  > 1. In `src/api/page_guidance_http.py`, estendi la tabella `ingest_notes` (`source_sha256` PK, `state_json`, `updated_at`) con colonna **`is_draft` INTEGER NOT NULL DEFAULT 0**. Migration idempotente su DB esistenti (`ALTER TABLE` se la colonna manca).
  > 2. `GET /api/ingest/drafts` → `{ ok, drafts: [{ source_sha256, title, file_name, updated_at }] }` solo righe `is_draft=1`, ordinate per `updated_at` desc. `title` da REICAT nello `state_json`; `file_name` da campo opzionale nello state (default `""`).
  > 3. Endpoint **Salva Bozza** (estendi `PUT`/`POST` su `/api/ingest/notes-state` o path dedicato) che accetta `source_sha256` + state (JSON body o form fields allineati a `build_ingest_notes_state`), setta `is_draft=1`, aggiorna `updated_at`, opzionale `file_name` nello state. PDF **non** richiesto. Riusa `save_ingest_notes_state` / `build_ingest_notes_state` / `resolve_ingest_ui_state`.
  > 4. Su submit ingest riuscito (dove già chiami `persist_ingest_notes_for_pdf`): dopo persist, setta **`is_draft=0`** (lo `state_json` resta). Aggiungi `DELETE /api/ingest/drafts?source_sha256=…` (o path equivalente) che: se solo bozza mai ingestita → rimuove la riga; se libro già in Biblioteca → solo `is_draft=0` senza distruggere REICAT libro / state utile. Regola in commento breve.
  > 5. Test pytest mirati (list/save/clear/delete + migration). Wiring in `src/api/ingest_http_server.py`.
  > Acceptance: API bozze funzionanti; submit non cancella state; list solo draft attive. Non toccare: UI `web/index.html` / `web/index2.html`, annotate page-guidance, pipeline OCR.
- **Stato:** done — test ok

---

### [x] F-002 — UI home: lista bozze + Nuova bozza
- **PRD:** US-1 — `PRD-Ingest-Drafts.md`
- **Dipende da:** F-001
- **Prompt:**
  > Implementa **solo** F-002 del PRD `PRD-Ingest-Drafts.md` in `librarAIn-server`. Scope unico: schermata iniziale bozze (niente Apri completo né Salva Bozza).
  > 1. In `web/index.html` (+ `web/index2.html` se stesso shell/form), all’apertura mostra pannello **Bozze attive** (fetch `GET /api/ingest/drafts`) **prima** del workspace pieno; nascondi/mostra dropzone+form finché non si entra in una sessione.
  > 2. Righe: titolo (o `file_name` / “Senza titolo”), sha abbreviato, `updated_at` leggibile; bottone **Apri** (stub ok: può solo preparare il flusso upload — wiring restore in F-003). Bottone **Nuova bozza** che attiva l’upload PDF esistente.
  > 3. Upload “nuova”: se SHA già in lista draft → tratta come apri quella (non duplicare); altrimenti sessione form vuota / restore post-ingest come oggi (`restoreIngestNotesForSha` + confirm “metadati nel database” se applicabile).
  > 4. Empty state + errore fetch non bloccante (si può comunque fare Nuova bozza).
  > Acceptance: lista visibile all’open; nuova via upload; no duplicati per stesso SHA. Non implementare ancora Salva Bozza né hash-check Apri (F-003/F-004). Non toccare backend oltre consumo API.
- **Stato:** done — attesa test umano

---

### [x] F-003 — UI Apri bozza: re-upload + hash match + restore
- **PRD:** US-2 — `PRD-Ingest-Drafts.md`
- **Dipende da:** F-002
- **Prompt:**
  > Implementa **solo** F-003 del PRD `PRD-Ingest-Drafts.md` in `librarAIn-server`. Scope unico: aprire una bozza esistente con ri-upload PDF.
  > 1. Click **Apri** su una riga bozza → file picker / drop del PDF (riusa dropzone o dialog dedicato minimo).
  > 2. Calcola SHA-256 del file; se ≠ `source_sha256` della bozza → messaggio errore chiaro, resta sulla lista, non applicare state.
  > 3. Se match → carica PDF nel workspace (come upload normale) e applica lo state server (`GET /api/ingest/notes-state?source_sha256=…` già esistente, o state già in lista se completo): REICAT, range, notes, annotations, guidance via `applyIngestNotesState` / `restoreIngestNotesForSha` in `web/index.html`.
  > 4. Se bozza attiva (`is_draft`), ripristino **diretto** senza dialog confuso “metadati nel database”; se non draft ma solo post-ingest, tieni il confirm attuale.
  > Acceptance: solo PDF con hash corretto apre la bozza; mismatch bloccato; form popolato. Non toccare: Salva Bozza button (F-004), schema API (F-001).
- **Stato:** done — attesa test umano

---

### [x] F-004 — UI Salva Bozza + Elimina + allineamento submit
- **PRD:** US-3 / US-4 (UI) — `PRD-Ingest-Drafts.md`
- **Dipende da:** F-001, F-003
- **Prompt:**
  > Implementa **solo** F-004 del PRD `PRD-Ingest-Drafts.md` in `librarAIn-server`. Scope unico: azioni Salva/Elimina e coerenza lista post-submit.
  > 1. Accanto a `#submit-btn` (“Invia validazione”) aggiungi bottone **Salva Bozza**; abilitato solo con PDF caricato e SHA noto.
  > 2. Click → chiama API save draft (F-001) con `collectIngestNotesState()` + `file_name` del PDF; feedback “Bozza salvata” / errore. No autosave server. `sessionStorage` draft tab può restare come oggi.
  > 3. In lista bozze: azione **Elimina** → `DELETE` API; aggiorna lista. Conferma breve ok.
  > 4. Dopo submit ingest **riuscito**: aggiorna UI così la bozza non resta “attiva” in lista (refetch o rimozione locale); state resta sul server come da F-001.
  > 5. Opzionale minimo: tasto “Torna alle bozze” dal workspace che mostra di nuovo la lista (senza perdere sessionStorage tab se già c’è).
  > Acceptance: Salva Bozza persiste e ricompare in lista dopo reload pagina; Elimina funziona; submit OK toglie dalla lista attiva. Non toccare: pipeline OCR, Biblioteca page, layout annotate UX del batch `2026_08_20`.
- **Stato:** done — attesa test umano

---

## Blocchi post-slice

- STOP test umano prima di ogni commit (`test ok`).
- Commit solo su richiesta esplicita utente.
- Aggiorna riga *Stato* in questo file, poi slice successiva senza nuovo «continua».
- Review se ≥ 5 file toccati nella slice.
