# PRD — Ingest UX: layout note + preview

> Ottimizzazioni UX sulla pagina di ingest (`web/index.html` + page-guidance / page-zoom) prima dell’uso in scala.
> Scope volutamente stretto: solo leggibilità tag e workspace annotazione. Data: 2026-08-20.

---

### 1. Executive Summary

- **Problem Statement**: Prima dell’ingest in scala, il workspace di annotazione è scomodo: le NOTE-TAG già citate sono poco distinguibili nella zona aggregata, la preview della pagina è troppo piccola/laterale e le note stanno sotto, quindi non si lavora bene in parallelo su immagine e testo.
- **Proposed Solution**: (1) stile BLACK on WHITE per le NOTE-TAG già citate nella sola sezione aggregata; (2) layout a due colonne — preview a sinistra, a destra lo slot contestuale **NOTE oppure REICAT** (mutuamente esclusivi) — senza padding superfluo, con splitter draggabile, scroll indipendenti, wheel-zoom e click-drag pan sulla PIC (riuso/estensione di `page-zoom.js` dove possibile).
- **Success Criteria**:
  1. Operatore distingue a colpo d’occhio le tag già citate nella riga aggregata senza confonderle con l’highlight in-text.
  2. Preview e pannello DX (note **o** REICAT) sono visibili contemporaneamente a tutta altezza utile del viewport.
  3. Wheel sulla PIC zoomma; click-drag pan-na; splitter ridimensiona le colonne senza rompere annotate canvas / mentions `@`.
  4. Nessuna regressione sul flusso ingest esistente (submit, page picker, annotate tools).

---

### 2. User Experience & Functionality

#### User Personas

- **Operatore ingest (lab)**: prepara guidance/annotazioni su pagine PDF prima di lanciare job in volume; lavora da desktop, tema scuro dashboard.

#### User Stories

**US-1 — Tag già citate leggibili**

- `As an operatore ingest, I want le NOTE-TAG già citate nella sezione aggregata in BLACK on WHITE so that le riconosco subito rispetto alle altre chip.`

Acceptance Criteria:

- Solo i chip nella sezione aggregata (`mention-chips` / equivalenti), non l’highlight `@…` dentro le textarea.
- Chip “già citate” (presenti nel testo della nota della sezione): testo nero su sfondo bianco; bordo sufficiente per contrasto sul tema scuro.
- Chip non ancora citate / altri stati: restano come oggi (o comunque distinti dal nuovo stile).
- Hover/focus: restano usabili; non obbligatorio redesign completo.

**US-2 — Layout due colonne Preview | Note/REICAT**

- `As an operatore ingest, I want la preview a SX e note o REICAT a DX so that lavoro sul form di destra guardando la pagina senza scrollare su e giù.`

Acceptance Criteria:

- Blocco PREVIEW (immagine pagina + tool annotate esistenti) in **centro**, pagina intera visibile (fit, non croppata).
- Blocco SX: **griglia miniature** (più piccole, scroll verticale).
- Blocco DX: **NOTE** oppure **REICAT**, mutuamente esclusivi.
- Layout a **tutta larghezza** viewport; tre colonne con **due** splitter draggabili.

**US-3 — Splitter ridimensionabile**

- `As an operatore ingest, I want un confine centrale draggabile so that scelgo quanto spazio dare a preview vs note.`

Acceptance Criteria:

- Handle verticale sul lato comune delle due colonne; drag orizzontale cambia le width.
- Ratio iniziale semplice (es. ~55/45 o 50/50 — implementazione libera purché usable).
- Larghezze minime per entrambe le colonne (non collassare a 0).
- Persistenza ratio in `localStorage`: **non richiesta** in MVP (ok se gratuita, non bloccare se manca).

**US-4 — Zoom e pan sulla PIC**

- `As an operatore ingest, I want wheel = zoom e click-drag = pan sulla preview so that leggo dettagli senza aprire overlay.`

Acceptance Criteria:

- Zoom iniziale circa **200%** rispetto allo zoom “fit” corrente (o default attuale ×2), con la pagina ancora navigabile via pan.
- Wheel sopra la PIC: zoom in/out (clamp ai limiti già usati da `page-zoom.js` se riusato).
- Click-drag sulla PIC: pan orizzontale/verticale quando zoomata.
- Compatibile con draw annotate (bbox/point/trail): pan non deve rubare i gesture di disegno; se conflitto, priorità annotate quando tool attivo, pan quando tool idle / modifier — scelta minima documentata in codice (una sola regola chiara).
- Preferire riuso di `web/page-zoom.js` (già include `.page-picker-detail-img-wrap`) invece di un secondo sistema zoom.

#### Non-Goals

- Redesign completo del form REICAT o della page picker griglia (solo riposizionamento REICAT in colonna DX).
- Touch-first / mobile layout.
- Persistenza avanzata di zoom/pan/splitter tra sessioni (opzionale, non MVP).
- Cambiare semantica delle mentions o del modello dati annotazioni.
- Nuove feature AI.

---

### 3. AI System Requirements

This feature does not introduce AI/ML components.

---

### 4. Technical Specifications

#### Architecture Overview

- Client-only UX su UI ingest esistente (`web/index.html`, CSS correlato, `web/dashboard/page-guidance*.js|css`, `web/page-zoom.js`).
- Nessun cambio API/backend previsto.
- Data flow invariato: annotazioni e note restano nei campi form / stato client già usati al submit.

#### Integration Points

- **CSS chip**: `page-guidance.css` (`.mention-chip` e varianti “già citate”).
- **Layout**: struttura `page-picker-detail` + area note del form ingest; eventualmente wrapper flex/grid con splitter.
- **Zoom/pan**: `page-zoom.js` (estendere se manca default 200% o regola pan vs annotate).
- Auth/tenancy: N/A (lab locale come oggi).

#### Security & Privacy

- Nessun nuovo dato sensibile; solo CSS/layout/interazione.
- Se si salva ratio in `localStorage`, solo preferenza UI locale (non PII).

#### Open Questions

- Esatta regola di priorità pan vs annotate quando tool disegno è attivo (decisione implementativa unica, documentata in commento breve).
- Quali campi note esattamente entrano nella colonna DX nel modo annotate — default: quelli già visibili nel flusso annotate corrente.
- REICAT: stesso slot DX della colonna note; visibile solo quando la modalità REICAT è attiva (mutuamente esclusivo con le note).

---

### 5. Risks & Roadmap

#### Phased Rollout

- **MVP (questo PRD)**: stile chip + layout SX/DX + splitter + zoom/pan usable su desktop.
- **v1.1 (opzionale)**: persistenza splitter/zoom; polish gesture annotate+pan; allineare lo stesso layout in `biblioteca` page-review se utile.

#### Technical & Product Risks

- **Conflitto gesture annotate vs pan**: mitigazione = una regola semplice (tool attivo → draw; altrimenti pan).
- **Canvas annotate fuori sync con zoom**: mitigazione = riuso path `page-zoom` già usato altrove; smoke test draw a zoom ≠ 100%.
- **Layout fragile su viewport stretti**: mitigazione = min-width colonne; sotto soglia accettabile lasciare overflow orizzontale piuttosto che reinventare responsive.

#### Out of Scope / Future Considerations

- Unificare preview ingest e biblioteca in un unico componente layout.
- Shortcut tastiera dedicate zoom/fit.
