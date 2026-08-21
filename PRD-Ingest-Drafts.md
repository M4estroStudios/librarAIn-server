# PRD — Sessioni / bozze ingest

> Persistenza esplicita delle compilazioni ingest (metadati + annotazioni) riprendibili su più sedute, senza tenere il PDF sul server.
> Scope: lista bozze all’apertura pagina Ingest, “Salva Bozza”, ripresa con ri-upload PDF.
> Data batch: **2026_08_21**. Repo: `librarAIn-server`.

---

### 1. Executive Summary

- **Problem Statement**: L’operatore lavora su libri lunghi (500+ pagine) in più sessioni; oggi la compilazione (REICAT, range, note, annotazioni) si perde alla chiusura del browser se non si è ancora lanciato l’ingest, e non esiste una lista di bozze riprendibili.
- **Proposed Solution**: Bozze **attive** salvate esplicitamente sul server (solo stato form, keyed per `source_sha256`); all’apertura di Ingest si vede l’elenco; per aprire una bozza (o crearne una nuova) si **ri-uploada sempre** il PDF; bottone **Salva Bozza** accanto a Invio; al submit OK la bozza esce dalla lista attiva ma metadati/annotazioni restano disponibili in Biblioteca / ri-ingest.
- **Success Criteria**:
  1. Operatore salva una bozza a metà compilazione, chiude il browser, riapre Ingest, vede la bozza in lista, ri-uploada lo stesso PDF e recupera REICAT/range/note/annotazioni.
  2. “Nuova bozza” parte da upload PDF non già in lista (o nuovo file); flusso form invariato dopo l’ingresso.
  3. Submit ingest riuscito: bozza non più in lista attiva; stesso contenuto riusabile da Biblioteca / conferma ri-caricamento come oggi.
  4. Nessuna regressione su coda multi-PDF in-sessione, submit, page guidance, Biblioteca.

---

### 2. User Experience & Functionality

#### User Personas

- **Operatore ingest (lab)**: desktop, server spesso spento e riacceso solo quando serve; lavora a più riprese sullo stesso PDF.

#### User Stories

**US-1 — Schermata iniziale: elenco bozze + nuova**

- `As an operatore ingest, I want all’apertura della pagina Ingest vedere le bozze attive e un tasto per crearne un’altra so that scelgo cosa riprendere o parto da un nuovo PDF.`

Acceptance Criteria:

- All’apertura di `web/index.html` (e `index2.html` se stesso form), **prima** del dropzone/workspace pieno: pannello **Bozze attive**.
- Ogni riga mostra almeno: titolo (o filename se titolo vuoto), `source_sha256` abbreviato, `updated_at` leggibile, azione **Apri**.
- Azione **Nuova bozza** / equivalente: mostra upload PDF (stesso meccanismo dropzone); se l’hash **non** è già una bozza attiva → nuova sessione form vuota (o solo restore server post-ingest se già in DB libri — comportamento allineato all’attuale confirm “metadati nel database”).
- Se l’hash **è** già bozza attiva → apri quella bozza (non duplicare).
- Lista vuota: empty state chiaro + CTA nuova bozza.
- Errore rete/API: messaggio non bloccante; si può comunque creare nuova bozza via upload.

**US-2 — Aprire bozza = sempre ri-upload PDF**

- `As an operatore, I want che per aprire una bozza debba ri-caricare il PDF so that funziona anche se il server non conserva i file tra sessioni.`

Acceptance Criteria:

- Click **Apri** su una bozza: chiede upload del PDF (file picker / drop).
- Calcolo SHA-256 del file caricato; se **≠** `source_sha256` della bozza → errore chiaro, bozza non aperta, lista resta.
- Se match → carica PDF nel workspace e applica lo `state` salvato (stessi campi di `ingest_notes` / form attuale: REICAT, range, notes, annotations, `ai_page_guidance`).
- Nessuna copia PDF “bozza” obbligatoria oltre al flusso upload già usato dall’ingest (server spesso offline / no PDF persistito per bozza).

**US-3 — Salva Bozza**

- `As an operatore, I want un tasto Salva Bozza vicino a Invio validazione so that congelo il lavoro senza lanciare la pipeline.`

Acceptance Criteria:

- Bottone **Salva Bozza** accanto a `#submit-btn` (“Invia validazione”), visibile quando c’è un PDF caricato e SHA noto.
- Click: POST/PUT stato corrente al server; feedback UI (es. “Bozza salvata” / errore).
- Payload = stesso insieme campi già persistito in `ingest_notes` (REICAT + range + note + annotations + guidance).
- Solo salvataggio **esplicito** (no autosave server in MVP). La bozza `sessionStorage` esistente può restare come fallback tab; non sostituisce il salvataggio server.
- Dopo salvataggio: la bozza compare / si aggiorna nella lista attiva (anche se si torna alla home bozze).

**US-4 — Submit OK rimuove bozza attiva; dati restano per Biblioteca**

- `As an operatore, I want che dopo ingest riuscito la bozza sparisca dalla lista ma metadati e annotazioni restino sul libro so that li ritrovo in Biblioteca e posso ri-ingestare.`

Acceptance Criteria:

- Al submit che completa con successo (stesso momento in cui oggi si chiama `persist_ingest_notes_for_pdf`): bozza marcata **non attiva** / rimossa dalla lista “bozze attive”.
- Lo `state` (note, annotazioni, REICAT, range) **non** viene cancellato dal DB: resta disponibile per Biblioteca e per il flusso “metadati già nel database” al ri-upload.
- Cancellazione manuale bozza attiva (opzionale MVP): se gratuita, “Elimina bozza” dalla lista rimuove solo lo stato di bozza attiva; se i dati sono solo-bozza e mai ingestiti, ok cancellare lo state; se già libro in Biblioteca, non distruggere REICAT libro.

#### Non-Goals

- Autosave server periodico.
- Conservare il PDF della bozza sul server per ripresa senza upload.
- Multi-bozza per lo stesso SHA (una bozza attiva per hash).
- Sync multi-operatore / auth.
- Redesign Biblioteca o pipeline OCR.
- Cambiare semantica annotazioni / page guidance.

---

### 3. AI System Requirements

This feature does not introduce AI/ML components.

---

### 4. Technical Specifications

#### Architecture Overview

- Estendere il modello già presente `ingest_notes` (SQLite, keyed `source_sha256` + `state_json` + `updated_at`) con flag **`is_draft`** (o tabella/status equivalente): `true` = in lista bozze attive; al submit OK → `false` (state resta).
- API:
  - `GET /api/ingest/drafts` — elenco bozze con `is_draft=1` (titolo da REICAT se presente, filename opzionale se salvato nello state, sha, updated_at).
  - `PUT` o `POST /api/ingest/notes-state` (estensione) — salva state + `is_draft=true` (Salva Bozza); richiede `source_sha256` (+ state JSON o multipart campi form). PDF **non** obbligatorio per il solo save se SHA già noto dal client.
  - Apertura bozza: client calcola SHA del PDF uploadato, `GET` state esistente, applica se match.
  - Submit esistente: dopo persist notes, set `is_draft=false`.
- UI: `web/index.html` (+ `index2.html` se condivide lo stesso shell): vista lista vs vista workspace; bottone Salva Bozza.

#### Integration Points

- Riuso `build_ingest_notes_state` / `save_ingest_notes_state` / `resolve_ingest_ui_state` in `src/api/page_guidance_http.py`.
- Client: `collectIngestNotesState` / `applyIngestNotesState` / restore-on-sha già in `web/index.html`.
- Biblioteca: nessun cambio obbligatorio se i dati restano in `ingest_notes` + books REICAT come oggi.

#### Security & Privacy

- Lab locale senza auth (invariato). Bozze = dati operativi locali sul SQLite del lab.
- Non loggare intero `state_json` (annotazioni possono essere grandi); loggare solo sha abbreviato + ok/error.

#### Open Questions

- Salvare anche `file_name` display nello state al primo upload (consigliato: sì, per lista leggibile).
- `index2.html` (GLM): stesso UI bozze nel MVP? **Default: sì**, stesso pattern se condivide JS/form; altrimenti solo `index.html` e allineare index2 in follow-up.
- Elimina bozza manuale: **Default MVP: sì**, azione secondaria in lista.

---

### 5. Risks & Roadmap

#### Phased Rollout

- **MVP (questo PRD)**: lista bozze, nuova via upload, apri con re-upload+hash match, Salva Bozza, submit → `is_draft=false`, state conservato per Biblioteca.
- **v1.1 (opzionale)**: autosave server; nome bozza editabile; badge “ha annotazioni”; export/import bozza JSON offline.

#### Technical & Product Risks

- **Hash mismatch** su ri-upload (PDF “simile” ma diverso): mitigazione = messaggio esplicito, nessun merge silenzioso.
- **Conflitto** con confirm attuale “metadati nel database”: mitigazione = se `is_draft`, ripristino diretto senza dialog confuso; se solo post-ingest, tenere dialog attuale.
- **Coda multi-PDF in-sessione**: snapshot in-memory resta; Salva Bozza salva l’item corrente. Non obiettivo salvare l’intera coda come un’unica bozza.

#### Out of Scope / Future Considerations

- Bozze senza server (solo file JSON locale).
- Ripresa PDF da `input/raw` se già presente (utile ma contraddice la scelta prodotto “sempre re-upload” per questo MVP).
