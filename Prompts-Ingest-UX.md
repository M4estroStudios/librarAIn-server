# Prompts — Ingest UX layout (`2026_08_20`)

> **PRD:** [`PRD-Ingest-UX.md`](./PRD-Ingest-UX.md)  
> **Repo:** `librarAIn-server` (UI ingest in `web/`)  
> **Sub-agent:** ogni slice = **un** Task Cursor con model slug **`cursor-grok-4.5-high`** (Grok 4.5), prompt copy-paste unico. Esecuzione **in serie** sullo stesso branch.  
> **Checkpoint:** ogni slice → **STOP test umano** prima del commit; nessun commit senza richiesta esplicita.

## Escalation (stesso item)

| Tier | Max prompt | Successivo |
|------|------------|------------|
| Grok 4.5 | 6 | Chiedi utente: riformula / spezza / manuale |

## Batch: 2026_08_20

### Stato sintetico

| ID | Titolo | Dipende da | Stato |
|----|--------|------------|-------|
| F-001 | Chip NOTE-TAG citate: BLACK on WHITE | — | done — attesa test umano |
| F-002 | Layout 2 colonne: Preview SX \| Note/REICAT DX | — | superseded — 3 col full-width |
| F-003 | Splitter draggabile + scroll indipendenti | F-002 | superseded — dual splitter 3 col |
| F-004 | Zoom iniziale ~200% + wheel zoom + pan vs annotate | F-002 | done — attesa test umano |

**Ordine rigido:** F-001 → F-002 → F-003 → F-004.

---

### [x] F-001 — Chip NOTE-TAG già citate: BLACK on WHITE
- **PRD:** US-1 — `PRD-Ingest-UX.md`
- **Prompt:**
  > Implementa **solo** F-001 del PRD `PRD-Ingest-UX.md` in `librarAIn-server`. Scope unico: stile dei chip aggregati già citati nelle note.
  > 1. In `web/dashboard/page-guidance-mentions.js` + `web/dashboard/page-guidance.css`, per ogni chip nella riga aggregata (`[data-mention-chips]`), se il `@token` del chip **compare già** nel valore della textarea della stessa sezione (`notes` / `index_notes` / `page_notes`), marca il chip come citato (es. classe `is-cited`).
  > 2. Stile `is-cited`: testo **nero** su sfondo **bianco**, bordo scuro sufficiente per contrasto sul tema scuro. Non applicare questo stile all’highlight in-text (`.mention-in-text` / backdrop) né alle textarea.
  > 3. Chip non citati: restano nello stile attuale (white on grey). `is-current` (pagina corrente) può coesistere; se conflitto visivo, `is-cited` ha priorità sul fill, `is-current` può restare come bordo/accent minimo.
  > 4. Aggiorna i chip quando cambia il testo della textarea (già c’è sync: riusa/estendi senza nuovi sistemi).
  > Acceptance: chip citati = black on white solo in area aggregata; in-text invariato; chip non citati invariati; niente refactor layout. Non toccare: page-zoom, page-picker layout, REICAT, backend.
- **Stato:** done — attesa test umano
- **Note:** `is-cited` se substring `@`+token nella textarea della sezione; CSS black on white.

---

### [x] F-002 — Layout due colonne: Preview SX | Note/REICAT DX
- **PRD:** US-2 — `PRD-Ingest-UX.md`
- **Prompt:**
  > Implementa **solo** F-002 del PRD `PRD-Ingest-UX.md` in `librarAIn-server`. Scope unico: riposizionare preview e pannello DX (note **oppure** REICAT).
  > 1. Nella UI ingest (`web/index.html` + CSS correlato, JS solo se necessario), crea un workspace a **due colonne** a tutta altezza utile: **sinistra = PREVIEW** (blocco `page-picker-detail` / anteprima pagina + tool annotate esistenti), **destra = slot contestuale**.
  > 2. Slot DX mostra **NOTE** (`#model-notes-fieldset` / campi note + guidance del flusso annotate) **oppure** **REICAT** (`#reicat-metadata-fieldset`), a seconda della modalità viewer attiva. Non compaiono insieme: stesso contenitore DX, switch di visibilità come oggi (o equivalente più pulito). Non ridisegnare i campi REICAT — solo spostarli nello slot DX.
  > 3. Rimuovi padding superfluo tra le colonne; usa la larghezza disponibile. Ogni colonna deve poter scrollare in verticale **in modo indipendente** (overflow su ciascuna colonna, non un unico scroll di pagina per entrambi).
  > 4. Desktop-first. Non implementare splitter (F-003) né cambiare zoom/pan (F-004). Mantieni griglia miniature / toolbar modalità esistenti funzionanti.
  > Acceptance: preview a SX e note o REICAT a DX contemporaneamente visibili; mutua esclusione note/REICAT nello slot DX; scroll indipendenti; submit/annotate/mentions non regressi. Non toccare: stile chip F-001 oltre regressioni, page-zoom defaults, backend.
- **Stato:** done — attesa test umano
- **Note:** `#page-picker-workspace` + `#page-picker-context`; REICAT/note mutuamente esclusivi nello slot DX.

---

### [x] F-003 — Splitter draggabile sul confine centrale
- **PRD:** US-3 — `PRD-Ingest-UX.md`
- **Dipende da:** F-002
- **Prompt:**
  > Implementa **solo** F-003 del PRD `PRD-Ingest-UX.md` in `librarAIn-server`. Scope unico: resize colonne del workspace F-002.
  > 1. Sul lato comune Preview | DX aggiungi un handle verticale **draggabile** che cambia le width delle due colonne (flex-basis / CSS variables / grid-template-columns — scegli il pattern minimo coerente col CSS esistente).
  > 2. Ratio iniziale usable (~50/50 o ~55/45). Imposta **min-width** su entrambe le colonne così non collassano a 0.
  > 3. Persistenza `localStorage`: **non richiesta**. Se la aggiungi gratis ok, non spenderci tempo.
  > 4. Durante il drag non spezzare annotate canvas, page-zoom, né lo scroll indipendente delle colonne.
  > Acceptance: drag ridimensiona SX/DX; min-width rispettati; layout F-002 e funzionalità annotate/note/REICAT intatte. Non toccare: chip styling, default zoom 200%, backend.
- **Stato:** done — attesa test umano
- **Note:** `#page-picker-splitter`; CSS var `--page-picker-left` (55% init); min 12rem.

---

### [x] F-004 — Zoom ~200% iniziale + wheel zoom + pan vs annotate
- **PRD:** US-4 — `PRD-Ingest-UX.md`
- **Dipende da:** F-002
- **Prompt:**
  > Implementa **solo** F-004 del PRD `PRD-Ingest-UX.md` in `librarAIn-server`. Scope unico: interazione zoom/pan sulla preview ingest, riusando `web/page-zoom.js`.
  > 1. Per il viewport preview del page-picker (`.page-picker-detail-img-wrap` già in SELECTORS), all’apertura/cambio pagina imposta zoom iniziale circa **200%** rispetto al fit corrente (default ×2 sul fit, clamp a `MAX_ZOOM` esistente).
  > 2. Assicurati che **wheel** sulla PIC zoommi in/out e che **click-drag** pan-ni quando zoomata (page-zoom già ha pan — verifica che funzioni nel nuovo layout F-002 e ripara se rotto).
  > 3. Conflitto con annotate (bbox/point/trail): una sola regola chiara, documentata in un commento breve — **se tool annotate attivo → priorità draw; altrimenti pan**. Non inventare modifier multipli.
  > 4. Nessun secondo sistema zoom. Smoke: draw a zoom ≠ 100% resta allineato al canvas.
  > Acceptance: preview parte ~200%; wheel zoom; pan quando non si disegna; annotate non regresso; nessun tocco a REICAT fields oltre regressioni layout. Non toccare: splitter F-003 oltre regressioni, chip F-001, backend.
- **Stato:** done — attesa test umano
- **Note:** Regola pan vs draw: annotate attivo → draw; altrimenti left-drag pan. Wheel zoom sempre (capture). Zoom iniziale picker ×2 sul fit.

---

## Blocchi post-slice

- STOP test umano prima di ogni commit (`test ok`).
- Commit solo su richiesta esplicita utente.
- Aggiorna riga *Stato* in questo file, poi slice successiva senza nuovo «continua».
- Review se ≥ 5 file toccati nella slice.
