# GRILL-ME: librarAIn → E-TALY Article Linking

Sessione di stress-test del piano di conversione/import degli articoli librarAIn come risorse asset in E-TALY (POH Browser / `MDHandler`).

**Data:** 2026-07-09  
**Scope:** mapping POH, formato markdown, metadati, UI di review, bundle export.

---

## Principio trasversale

librarAIn compila il **pacchetto più completo possibile** già in formato E-TALY parsabile (`MDHandler`). Ogni azione distruttiva (sovrascrittura), ogni nuova entità (POH, `poh_s`, righe CSV) e ogni dato inferito (geo, cover, timeline LLM) passa da **review + conferma manuale**. L'export è **bloccato** se restano link `poh:` non mappati o se fallisce la validazione lint.

---

## Domande e risposte

### 1 — Mapping ID (slug polyindex → `poh_xxx` E-TALY)

**Domanda:** Come mappiamo slug librarAIn/polyindex (es. `augusto`, `civita-castellana`) a ID E-TALY (`poh_p0042`, …)?

| Opzione | Descrizione |
|---------|-------------|
| A | Tabella manuale/curata |
| B | Match automatico su `wikidata_qid` |
| C | Match fuzzy su label/nome |
| D | Mix: auto + revisione manuale per i non risolti |

**Raccomandazione:** D  
**Risposta:** **D**

---

### 2 — Sovrascrittura file `.md` esistenti in E-TALY

**Domanda:** Cosa fare se esiste già un file in `assets/timeline/data/text/ITA/`?

| Opzione | Descrizione |
|---------|-------------|
| A | Sovrascrivere sempre |
| B | Importare solo se file mancante |
| C | Sovrascrivere solo stub/WIP |
| D | Merge selettivo body |

**Raccomandazione:** C  
**Risposta:** **Sovrascrittura possibile solo con conferma manuale** (diff per POH → approvazione esplicita). Nessun replace automatico.

---

### 3 — Articoli librarAIn senza mapping E-TALY

**Domanda:** Cosa fare con slug senza `poh_xxx` corrispondente?

| Opzione | Descrizione |
|---------|-------------|
| A | Scartare e loggare |
| B | Creare nuovi POH in E-TALY |
| C | Coda pending finché non mappato |
| D | Importare come `poh_s*` |

**Raccomandazione:** C  
**Risposta:** **Coda + conferma manuale** — stesso gate del punto 2; niente import automatico neanche per POH nuovi.

---

### 4 — Tipo POH (`poh_o` / `poh_p` / `poh_m`)

**Domanda:** Chi decide se organizzazione, persona o monumento?

| Opzione | Descrizione |
|---------|-------------|
| A | Solo E-TALY (prefisso ID target) |
| B | librarAIn propone, utente conferma |
| C | Tabella manuale slug → tipo + ID |

**Raccomandazione:** A  
**Risposta:** **REICAT-style (B con conferma)** — librarAIn propone tipo, ID target e metadati export (come "Compila metadati Vision"); l'utente conferma prima dell'import. Il tipo finale è quello dell'ID E-TALY approvato.

---

### 5 — Citazioni `source:` e sezione `## Fonti`

**Domanda:** E-TALY non renderizza link `source:sha:page`. Cosa fare?

| Opzione | Descrizione |
|---------|-------------|
| A | Rimuovere tutto |
| B | Testo semplice: `[I rioni di Roma, p.108]` |
| C | Tenere `## Fonti` con link rotti |
| D | Footnote plain in calce |

**Raccomandazione:** B  
**Risposta:** **B** — convertire i link in testo semplice; stessa logica per `## Fonti`.

---

### 6 — Timeline nel frontmatter vs `## Cronologia` nel body

**Domanda:** E-TALY vuole 5 eventi `anno: testo` nel frontmatter. librarAIn ha tabella `## Cronologia`.

| Opzione | Descrizione |
|---------|-------------|
| A | Estrarre fino a 5 righe dalla tabella |
| B | LLM riassume in 5 punti |
| C | Duplicare frontmatter + body |
| D | Solo frontmatter, rimuovere `## Cronologia` dal body |

**Raccomandazione:** D (estrazione prima; LLM se insufficiente)  
**Risposta:** **D**

---

### 7 — Link interni `poh:` senza mapping

**Domanda:** `[Augusto](poh:augusto)` → `[[poh_p????|Augusto]]`. Target non mappato?

| Opzione | Descrizione |
|---------|-------------|
| A | Solo testo, niente link |
| B | Link rotto |
| C | Bloccare export |
| D | Coda link pending, export parziale |

**Raccomandazione:** C  
**Risposta:** **C**

---

### 8 — Monumenti (`poh_m`) e coordinate

**Domanda:** Da dove prendere `poi_id`, `lat`, `lon`, `region`, `category`?

| Opzione | Descrizione |
|---------|-------------|
| A | Frontmatter `.md` E-TALY esistente |
| B | `POIs.csv` / dati POI via `poi_id` |
| C | librarAIn propone via web (Wikidata/geocoding) + conferma |
| D | Escludere monumenti per ora |

**Raccomandazione:** A+B, fallback C  
**Risposta:** **B+C** — di base da E-TALY/CSV; dove mancante, ricerca web con librarAIn + conferma manuale.

**Nota:** Persone (`poh_p`) e organizzazioni (`poh_o`) **non** richiedono coordinate (solo monumenti e, opzionalmente, `poh_s` con match geo).

---

### 9 — Cover image

**Domanda:** `assets/timeline/resources/POH/covers/{pohId}.webp` — librarAIn non le genera.

| Opzione | Descrizione |
|---------|-------------|
| A | Ignorare (placeholder) |
| B | Copiare cover E-TALY esistente |
| C | Scaricare da Wikipedia/Wikidata + conferma |
| D | Obbligatoria per export |

**Raccomandazione:** B+C  
**Risposta:** **B+C**

---

### 10 — Sezione `## Annotazioni`

**Domanda:** Alcuni articoli librarAIn hanno `## Annotazioni`.

| Opzione | Descrizione |
|---------|-------------|
| A | Rimuovere |
| B | Tenere in fondo al body |
| C | Convertire in note inline |
| D | File separato |

**Raccomandazione:** B  
**Risposta:** **B**

---

### 11 — UI di conferma

**Domanda:** Dove avviene proposta → diff → conferma → export?

| Opzione | Descrizione |
|---------|-------------|
| A | Admin librarAIn |
| B | Script CLI + CSV/JSON |
| C | Tool E-TALY |
| D | Admin librarAIn + bundle `.zip` per asset E-TALY |

**Raccomandazione:** A+D  
**Risposta:** **A+D**

---

### 12 — Titolo articolo (H1)

**Domanda:** librarAIn apre con `# [Titolo](poh:slug)`. E-TALY usa `name:` in frontmatter.

| Opzione | Descrizione |
|---------|-------------|
| A | Rimuovere H1; `name:` = titolo librarAIn |
| B | `name:` = label CSV E-TALY |
| C | H1 → intro bold nel body |
| D | librarAIn propone `name:` in review |

**Raccomandazione:** D  
**Risposta:** **D**

---

### 13 — Lingua

**Domanda:** Export multilingua?

| Opzione | Descrizione |
|---------|-------------|
| A | Solo `ITA/` |
| B | ITA ora, altre lingue backlog |
| C | Traduzione LLM per lingua |
| D | Stub ITA in tutte le cartelle |

**Raccomandazione:** A  
**Risposta:** **A**

---

### 14 — POH nuovi in CSV E-TALY (`poh_o/p/m`)

**Domanda:** ID nuovo approvato ma riga CSV assente?

| Opzione | Descrizione |
|---------|-------------|
| A | Bundle include riga CSV proposta |
| B | Solo `.md`; CSV manuale |
| C | librarAIn propone riga da `time_range` polyindex + conferma |
| D | Mai POH nuovi |

**Raccomandazione:** C  
**Risposta:** **C** — bundle con `.md` + patch CSV proposta.

---

### 15 — Timeline incompleta (< 5 eventi o date fuori range)

**Domanda:** Come riempire il frontmatter timeline?

| Opzione | Descrizione |
|---------|-------------|
| A | Esportare 1–4 eventi disponibili |
| B | LLM genera mancanti fino a 5 + review |
| C | Bloccare finché non ci sono 5 eventi |
| D | Stub generici in review |

**Raccomandazione:** B  
**Risposta:** **B**

---

### 16 — Validazione pre-export

**Domanda:** Quali controlli automatici bloccano l'export?

| Opzione | Descrizione |
|---------|-------------|
| A | Solo link `poh:` unresolved |
| B | A + frontmatter minimo |
| C | B + lint syntax non supportata |
| D | C + dry-run parser MDHandler |

**Raccomandazione:** C  
**Risposta:** **C** — link `poh:`, frontmatter (`id`, `name`, ≥1 evento timeline), niente `[[File:…]]`, link `source:`, H1.

---

### 17 — `poh_s` (siti/luoghi)

**Domanda:** Includere `poh_s*` nel mapping/import?

| Opzione | Descrizione |
|---------|-------------|
| A | Solo `poh_o/p/m` |
| B | Solo `poh_s` già esistenti |
| C | Anche nuovi `poh_s` proposti da librarAIn |
| D | Caso per caso |

**Raccomandazione:** B  
**Risposta:** **C** — librarAIn può trovare luoghi non presenti in E-TALY.

---

### 18 — Formato bundle per `poh_s` nuovi

**Domanda:** Cosa mettere nel bundle per un `poh_s` nuovo?

**Principio risposta utente:** librarAIn deve fornire **quanti più dati possibili** in formato E-TALY corretto e parsabile.

**Risposta:** Bundle completo — `.md` con frontmatter pieno (`id`, `name`, `wiki_*` se disponibili, geo/`poi_id` se trovati, timeline se applicabile), patch CSV/registry dove serve, cover se trovabile. Nessun export minimale.

---

### 19 — Timeline su `poh_s` nuovi

**Domanda:** Obbligatori 5 eventi timeline anche su `poh_s`?

| Opzione | Descrizione |
|---------|-------------|
| A | Obbligatoria (5 eventi, LLM + review) |
| B | Opzionale se ci sono date reali |
| C | Mai timeline su `poh_s` |

**Raccomandazione:** B  
**Risposta:** **B** — timeline opzionale.

---

### 20 — Re-export dopo rigenerazione articolo

**Domanda:** Articolo librarAIn rigenerato; `.md` già importato in E-TALY?

| Opzione | Descrizione |
|---------|-------------|
| A | Nuova proposta in coda review (diff) |
| B | Auto-update se precedente era librarAIn |
| C | Sempre diff + conferma |
| D | Ignorare finché non richiesto manualmente |

**Raccomandazione:** C  
**Risposta:** **A** — nuova proposta in coda; valutare se vale la pena sostituire.

---

## Riepilogo decisioni

| Area | Decisione |
|------|-----------|
| Mapping | Auto + manuale (D) |
| Conferma | Obbligatoria per overwrite, import, inferenze |
| Tipo POH | Proposta REICAT-style + conferma |
| Citazioni | → testo semplice |
| Cronologia body | Rimossa; solo frontmatter |
| Link unmapped | Blocca export |
| Geo monumenti | E-TALY first, web fallback + conferma |
| Cover | E-TALY first, web fallback + conferma |
| Annotazioni | Mantenute |
| UI | Admin librarAIn + bundle `.zip` |
| Titolo | `name:` proposto in review |
| Lingua | Solo ITA |
| POH/CSV nuovi | Proposta + patch CSV nel bundle |
| Timeline | 5 eventi (LLM se mancano); opzionale su `poh_s` |
| Validazione | Lint C |
| `poh_s` | Nuovi ammessi, formato completo |
| Rigenerazione | Nuova coda review con diff |

---

## Prossimo passo implementativo

Primo deliverable: flusso **"Proposta export E-TALY"** nell'admin librarAIn (lista POH, diff, mapping pending, validazione, download bundle).
