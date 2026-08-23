# Prompts — Ingest Note Compile (`2026_08_23`)

> **PRD:** [`PRD-Ingest-Note-Compile.md`](./PRD-Ingest-Note-Compile.md)  
> **Repo:** `librarAIn-server` (UI ingest `web/dashboard/page-guidance*`)  
> **Sub-agent:** ogni slice = **un** Task Cursor con model slug **`cursor-grok-4.5-high`** (Grok 4.5), prompt copy-paste unico. Esecuzione **in serie** sullo stesso branch.  
> **Checkpoint:** ogni slice → aggiorna Stato; commit solo su richiesta esplicita utente.

## Escalation (stesso item)

| Tier | Max prompt | Successivo |
|------|------------|------------|
| Grok 4.5 | 6 | Chiedi utente: riformula / spezza / manuale |

## Batch: 2026_08_23

### Stato sintetico

| ID | Titolo | Dipende da | Stato |
|----|--------|------------|-------|
| F-001 | Bbox esistenti: no move / no resize | — | done |
| F-002 | Titolo vuoto + delete on page change | — | done |
| F-003 | COPILOT top 15%: Testata Pari / Dispari | F-001 | done |
| F-004 | COPILOT corpo: tag ≥20% pagine | F-003 | done |

**Ordine rigido:** F-001 → F-002 → F-003 → F-004 (F-001 e F-002 sono indipendenti ma si eseguono in serie comunque).

---

### [x] F-001 — Bbox esistenti non trascinabili / non ridimensionabili
- **PRD:** US-1 — `PRD-Ingest-Note-Compile.md`
- **Prompt:**
  > Implementa **solo** F-001 del PRD `PRD-Ingest-Note-Compile.md` in `librarAIn-server`. Scope unico: disabilitare move/resize delle annotazioni già create.
  > 1. In `web/dashboard/page-guidance.js`, sul `mousedown` del `#annotate-canvas`: se `hitTest` trova un elemento **esistente** (bbox/point/trail), selezionalo (`state.selectedId`), su doppio click apri rename come oggi (`promptRename`), ma **non** impostare `drag` con `mode` `"move"` né `"resize"`.
  > 2. Lascia invariato il draw-to-create bbox (`drag.mode = "bbox"` + `state.draft`), point click-to-place, trail.
  > 3. Aggiungi cleanup sicuro: `mouseup` (e se utile `mouseleave`) anche su `window`/`document` che azzera `drag` se residuo, così un rilascio fuori canvas non lascia shape “attaccate” al cursore al rientro (solo per create-draw o drag residui).
  > 4. Nessun nuovo tool “muovi”. Non toccare COPILOT, titoli vuoti, mentions, CSS layout.
  > Acceptance: click/doppio-click su bbox esistente non sposta né ridimensiona; nuova bbox si disegna ancora con drag; niente regressioni point/trail create. Non toccare: backend, REICAT, page-zoom defaults, F-002+.
- **Stato:** done — endDrag + no move/resize su hit esistente
- **Note:** `web/dashboard/page-guidance.js` only.

---

### [x] F-002 — Titolo vuoto senza auto-fill; delete al cambio pagina
- **PRD:** US-5 — `PRD-Ingest-Note-Compile.md`
- **Prompt:**
  > Implementa **solo** F-002 del PRD `PRD-Ingest-Note-Compile.md` in `librarAIn-server`. Scope unico: titolo annotazione lasciabile vuoto e cancellazione al cambio pagina.
  > …
- **Stato:** done — empty name + purge on detail change
- **Note:** `page-guidance.js` + skip chip se nome vuoto in mentions.

---

### [x] F-003 — COPILOT top 15%: Testata Pari / Testata Dispari
- **PRD:** US-2 + US-4 — `PRD-Ingest-Note-Compile.md`
- **Dipende da:** F-001 (editor stabile senza drag)
- **Stato:** done — COPILOT SX con Testata Pari/Dispari in top 15%
- **Note:** `page-guidance.js` + `.css`; no seed pool.

---

### [x] F-004 — COPILOT corpo pagina: tag ≥ 20%
- **PRD:** US-3 — `PRD-Ingest-Note-Compile.md`
- **Dipende da:** F-003
- **Prompt:**
  > Implementa **solo** F-004 del PRD `PRD-Ingest-Note-Compile.md` in `librarAIn-server`. Scope: riempire COPILOT quando la bbox **non** è nel top 15%.
  > 1. Riusa il pannello COPILOT di F-003. Se top edge bbox > 15%: calcola frequenza token = (# pagine con ≥1 annotazione che ha quel `mentionToken(name)`) / (# pagine con ≥1 annotazione grafica) su `state.pages`.
  > 2. Includi token con frequenza ≥ 0.20; ordina per frequenza desc, tie-break `localeCompare("it")`; **escludi** token di Testata Pari e Testata Dispari.
  > 3. Click voce → applica nome come F-003. Se lista vuota: nascondi pannello o mostra hint “Nessun suggerimento ancora” (una sola scelta, minima).
  > 4. Non inventare tag; non usare LLM. Aggiorna lista su open editor / cambio annotazioni.
  > Acceptance: sotto top 15% COPILOT mostra solo tag frequenti non-testata; top 15% resta come F-003. Non toccare: no-drag, titolo vuoto, backend.
- **Stato:** done — COPILOT body: tag ≥20% pagine, escluso testate; lista vuota → pannello nascosto
- **Note:** `page-guidance.js` only; riusa pannello F-003.

---

## Blocchi post-slice

- Aggiorna riga *Stato* in questo file dopo ogni slice.
- Commit solo su richiesta esplicita utente.
- Review se ≥ 5 file toccati nella slice.
