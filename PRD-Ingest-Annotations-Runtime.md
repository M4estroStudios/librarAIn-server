# PRD — Ingest: annotazioni come vincoli runtime (Opzione B)

> Scope: consumo **runtime** delle annotazioni e delle note operatore negli stage di estrazione (Stage 2 Vision, prompt notes per OCR/Editor, Affinamento TOC/Indice). **Nessuna modifica alla UX di annotazione** (`web/dashboard/page-guidance*`).
> Libro pilota: *Storia di Roma Antica* (`a5243521…`), 141 pagine annotate, 232 shape, 51 etichette.
> Data: 2026-09-05.
> **Agenti:** il test e il rollout non sono impliciti. Contratto QA = **§6**. Implementazione finita solo dopo T1–T3 + report; T4 solo su go operatore.

---

### 1. Executive Summary

- **Problem Statement**: Le annotazioni (bbox/point/trail) e le note operatore (`notes`, `index_notes`, `page_notes`) non raggiungono mai i modelli di estrazione: vengono compresse da un LLM in un unico testo `ai_page_guidance` (troncato a `max_tokens=2048` su Storia di Roma) e le bbox non vincolano le pagine su cui sono state disegnate. Risultato: 0/417 pagine con aside `{}` nonostante 60+ box annotati, box promossi a `#` capitolo, cronologie a 2 colonne lette riga-per-riga, indice senza distinzione tipografica persone/luoghi.
- **Proposed Solution**: (1) iniettare le **note grezze** negli stage senza passare dal riassunto LLM; (2) per le **pagine annotate**, passare a Stage 2 l'immagine con **overlay delle annotazioni** più un blocco testuale per-pagina compilato **deterministicamente** dalle etichette (registry etichetta→regola Markdown); (3) Affinamento Indice/TOC riceve `index_notes`/`notes` grezze; (4) il Consiglio AI resta come brief globale ma senza troncamento e con un sottoinsieme di immagini; (5) cache MD invalidata dall'hash delle note, non solo dal modello.
- **Success Criteria** (misurati dal validatore deterministico, v. §3, sul re-ingest del libro pilota):
  1. **≥ 90% delle pagine annotate** rispettano le regole implicate dalle proprie etichette (aside per Box, `#`/`##` per Capitolo/Sezione, nessun box come heading, didascalia formattata, ordine colonna-per-colonna nelle cronologie annotate).
  2. **0 pagine** in cui un titolo di Box Curiosità diventa heading `#`/`##` tra le pagine annotate con etichetta Box (oggi: quasi tutte).
  3. `INDEX.md` raffinato prodotto senza crash, con ordine di lettura colonna-per-colonna e luoghi in italics sulle pagine indice annotate.
  4. `ai_page_guidance` generato non troncato (fine di frase, copre tutte le sezioni delle note: pagina, TOC, indice, appendici).
  5. Rilancio con note modificate **non** riusa la cache MD degli stage (invalidazione automatica).

---

### 2. User Experience & Functionality

#### User Personas

- **Operatore ingest (lab)**: annota i PDF nel dashboard e si aspetta che un campione di annotazioni "completo e ridondante" produca output conformi almeno sulle pagine annotate.

#### Glossario

| Termine | Significato |
|---------|-------------|
| **Etichetta canonica** | Forma normalizzata del nome annotazione (case-insensitive, spazi/underscore equivalenti): `Box Curiosità` ≡ `Box_Curiosità` → `box_curiosita`. |
| **Registry etichetta→regola** | Tabella deterministica che mappa un'etichetta canonica a un'istruzione Markdown per lo stage (es. `box_curiosita` → "questo riquadro va tra `{` e `}`, titolo in `**ALL CAPS**`, mai heading"). |
| **Blocco per-pagina** | Testo compilato dal registry per una singola pagina annotata, iniettato nel messaggio user di Stage 2 accanto all'immagine. |
| **Overlay** | PNG della pagina con le shape disegnate sopra (riuso di `flatten_annotations_on_image`). |
| **Note grezze** | `notes`, `index_notes`, `page_notes` come scritte dall'operatore, senza riscrittura LLM. |

#### User Stories

**US-1 — Note grezze negli stage (fine del collo di bottiglia guidance)**

- `As an operatore, I want che le mie note pagina/indice arrivino testuali ai modelli di estrazione so that nessuna regola scompaia per compressione o troncamento.`

Acceptance Criteria:

- `PipelineContext.page_prompt_notes` = composizione deterministica di `page_notes` (grezze) + `ai_page_guidance` (se presente), con intestazioni distinte; **non** più solo il guidance.
- `PipelineContext.index_prompt_notes` = `index_notes` grezze (+ `notes` per la parte TOC), **non** il guidance pagina.
- `IngestRequest` continua a trasportare i tre campi note (già presenti); nessun campo viene scartato dopo la generazione del guidance.
- Il blocco `<operator_notes>` risultante non supera un budget configurabile (default 8.000 caratteri); oltre, tronca il guidance ma **mai** le note grezze, e logga un warning.
- Se `strip_operator_notes_leak` degrada l'output per note lunghe (falsi positivi di leak), le firme di leak si calcolano solo sulle prime N righe di ciascuna sezione note (TBD in implementazione, test dedicato).

**US-2 — Overlay + blocco per-pagina a Stage 2 sulle pagine annotate**

- `As an operatore, I want che le bbox vincolino l'estrazione della pagina su cui le ho disegnate so that "pagina annotata" implichi "pagina conforme".`

Acceptance Criteria:

- Le annotazioni per-pagina raggiungono la pipeline: nuovo campo `annotations` su `IngestRequest` (stessa struttura normalizzata di `normalize_annotations`), popolato dal server dallo stato `ingest_notes` al submit; il ri-ingest da Biblioteca lo recupera dallo stesso store.
- Per ogni pagina **originale** con ≥1 elemento annotato, `run_stage2_vision` (e il ramo GLM OCR combinato) riceve:
  1. l'immagine pulita della pagina (come oggi);
  2. l'**overlay** (riuso `flatten_annotations_on_image`, coords 0-999 → pixel), come seconda immagine, preceduto dal testo: "Questa seconda immagine mostra le regioni annotate dall'operatore. I riquadri colorati e le etichette NON sono contenuto della pagina: non trascriverli mai.";
  3. il **blocco per-pagina** compilato dal registry (etichetta canonica + descrizione operatore + regola Markdown + coords).
- Pagine non annotate: comportamento identico a oggi (una sola immagine, nessun blocco).
- Il mapping pagina originale → pagina allineata usa l'alignment esistente (`pages_to_remove`); annotazioni su pagine rimosse vengono ignorate con log.
- Nessuna etichetta o coordinate dell'overlay compare nell'output MD (estensione di `finalize_vision_page_output` con pattern delle etichette note).

**US-3 — Registry etichetta→regola (compiler deterministico)**

- `As an operatore, I want che i nomi delle mie etichette si traducano in regole fisse so that il modello esegua invece di reinterpretare.`

Acceptance Criteria:

- Modulo nuovo (es. `src/ingestion/annotation_rules.py`) con: normalizzazione a etichetta canonica; registry di pattern → template regola; fallback per etichette ignote = "regione annotata: {name} — {description}".
- Copertura minima del registry (dal libro pilota): `box_curiosita*` (tutte le varianti: doppia, tripla, sandwich, con tabella, con immagine connessa, multipagina top/bottom), `capitolo`, `sezione`, `header_pari|dispari` / `testata_*`, `immagine_*` (full width, part width, full page, multi-page sx/dx), `decorazione_di_fine_capitolo`, `cronologia_*`, `senso_di_marcia_multicolonna`, `titolo_toc|indice|appendice`, `toc_1_colonna`, `indice_analitico_a_2_colonne`, `2_righe_di_pagine_cit`, `introduzione_lista`, `grafico_genealogico`.
- Le 51 etichette esistenti del libro pilota si risolvono in ≤ 30 etichette canoniche; un test fissa il mapping.
- La `description` dell'operatore è sempre accodata alla regola (l'operatore può raffinare/override il template).
- Coerenza con `md_formatting`: la regola per i box riusa il testo di `DEFAULT_MD_ASIDES` (stessa sintassi `{}`), e la regola compilata dichiara esplicitamente la precedenza sul default `md_h1` ("un titolo ALL CAPS dentro una regione Box non è mai un heading").

**US-4 — Consiglio AI: niente troncamento, meno immagini**

- `As an operatore, I want un consiglio AI completo so that anche gli stage non coperti dall'overlay (aggregazioni, polyindex) abbiano il quadro intero.`

Acceptance Criteria:

- `max_tokens` della chiamata `suggest_page_guidance` portato a valore configurabile (default ≥ 8192); se il modello risponde con `finish_reason=length`, log warning + retry con istruzione di sintesi.
- Immagini inviate: al massimo **8 pagine annotate rappresentative** (selezione: massimizzare la copertura di etichette canoniche distinte) con overlay, + le 5 sample attuali. Niente più coppia annotata/originale per tutte le 141 pagine.
- Il JSON dei metadati annotazioni resta completo (tutte le pagine).
- Validazione post-generazione: il guidance deve menzionare ogni sezione note non vuota (pagina/TOC/indice); in caso contrario, log warning visibile in dashboard (non bloccante).

**US-5 — Affinamento TOC/Indice con le note giuste**

- `As an operatore, I want che l'affinamento indice conosca le mie regole indice so that colonne e tipografia siano rispettate.`

Acceptance Criteria:

- `refine_aggregate_markdown_file` per `kind=index` riceve `index_notes` grezze come `prompt_notes`; per `kind=toc` riceve `notes` grezze. Mai il guidance pagina.
- `index_aggregate_refine_prompt.md` esteso con: gestione righe di continuazione pagine (`2_righe_di_pagine_cit`), preservazione italics dei luoghi (`*Lemma*, 12, 15`), nessun re-flow tra colonne (l'ordine di lettura è responsabilità di Stage 2, il refine non riordina).
- Il crash dello stage "Affinamento Indice" sul run di Storia di Roma viene diagnosticato e coperto: lo stage riparte dai `pages/*.md` materializzati (i tmp recuperati a mano sono equivalenti), ricostruisce `INDEX.md`/`TOC.md` mancanti e completa. Root cause: `TBD` (log del run originale da recuperare in implementazione).

**US-6 — Cache invalidata dalle note**

- `As an operatore, I want che cambiare note/annotazioni e rilanciare rigeneri le pagine so that non riveda output vecchi spacciati per nuovi.`

Acceptance Criteria:

- Il marker di `md_cache` diventa `<!-- librarain:model=X notes=SHA8 -->` dove `SHA8` è l'hash del blocco note effettivo per quello stage (per Stage 2: note composte + blocco per-pagina + presenza overlay).
- Cache hit ⇔ modello **e** hash coincidono. File legacy (marker senza `notes=`): validi solo se l'hash corrente è vuoto; altrimenti ricomputati.
- Vale per stage2, stage3, glm_ocr e cache di sezione toc/index refine.

#### Non-Goals

- Nessuna modifica alla UX di annotazione (draw, COPILOT, pool chip, mentions).
- Nessun crop per-regione né vincolo geometrico hard (accettazione/rigetto dell'output in base alle coords): il vincolo resta prompt-side.
- Stage 3 resta senza immagine (v. Appendice C).
- Nessuna generalizzazione garantita alle pagine **non** annotate: il KPI MVP è sulle annotate; il resto beneficia solo del guidance migliorato.
- Nessuna canonicalizzazione retroattiva dei nomi in UI/DB: la normalizzazione avviene solo runtime.

---

### 3. AI System Requirements

- **Tool & Data Requirements**
  - Modelli: invariati (`VISION_MODEL` per Stage 2 e guidance, `EDITOR_MODEL` per Stage 3 e refine, `GLM_OCR_MODEL` per il ramo GLM). Nessun modello nuovo.
  - Dati: annotazioni da `ingest_notes.state_json` (SQLite) / draft `data/sync/drafts/<sha>.json`; nessuna nuova persistenza, solo trasporto in `IngestRequest.annotations`.
  - Costo incrementale stimato: +1 immagine per pagina annotata a Stage 2 (~141 immagini sul pilota, ~34% delle pagine), −270 immagini circa alla chiamata guidance. Netto ≈ neutro o in calo.

- **Evaluation Strategy**
  - Fonte di verità operativa: **§6 Playbook di test e rollout**. Un agente che implementa questa PRD **non** può dichiarare il lavoro finito prima di aver eseguito T0→T3 (unit + baseline + smoke) e aver scritto il report. T4 (run completo) richiede go esplicito dell'operatore dopo T3.
  - **Validatore deterministico** (`scripts/validate_annotated_pages.py`, nuovo): vedi §6.2. Soglia di accettazione = Success Criteria §1, misurata sul run completo (T4), non sullo smoke.
  - Guardrail: estensione dei test anti-leak (`markdown_artifacts`) per etichette overlay; test unit su registry, normalizzazione nomi, composizione note, invalidazione cache (T1).

---

### 4. Technical Specifications

- **Architecture Overview**
  - Flusso dati nuovo: `ingest_notes.state_json` → (submit) `IngestRequest.annotations` → `orchestrator._build_pipeline_context` → `PipelineContext.annotations_by_original_page` → Stage 2 / GLM OCR (`overlay + blocco per-pagina`), e note grezze → `page_prompt_notes` / `index_prompt_notes`.
  - Punti di modifica principali:
    - `src/models/request.py`: campo `annotations` (lista normalizzata, default vuota) + composizione note.
    - `src/api/ingest_form.py` / `ingest_http_server.py` / `page_guidance_http.py`: popolamento `annotations` dal payload/store al submit e al ri-ingest.
    - `src/ingestion/orchestrator.py` (righe ~259-284): composizione `page_prompt_notes`/`index_prompt_notes`; passaggio annotazioni e render overlay.
    - `src/ingestion/pipeline/stage2.py` (`refine_with_vision`, `run_stage2_vision`) e `glm_ocr_stage.py`: parametro `page_annotations` opzionale (blocco testo + path overlay), seconda immagine nel messaggio user.
    - `src/ingestion/annotation_rules.py` (nuovo): normalizzazione + registry + compiler per-pagina; riuso di `flatten_annotations_on_image` spostato/importato da `page_guidance_suggest`.
    - `src/ingestion/pipeline/md_cache.py`: marker esteso con hash note.
    - `src/api/page_guidance_suggest.py`: `max_tokens` configurabile, selezione 8 pagine rappresentative.
    - `src/ingestion/toc_index_refine.py` + `orchestrator` (riga ~768): `prompt_notes` differenziati per kind.
    - `src/ingestion/pipeline/prompts/index_aggregate_refine_prompt.md`: regole continuazione/italics.
    - `scripts/validate_annotated_pages.py` (nuovo): validatore KPI, v. §6.2.
    - `scripts/rerun_annotated_pages.py` (nuovo): re-run Stage 2+3 su un sottoinsieme di pagine originali con `force_recompute`, v. §6.3.
    - `tests/test_annotation_rules.py`, `tests/test_md_cache_notes_hash.py`, `tests/test_validate_annotated_pages.py` (fixture, no rete).
  - Overlay renderizzato in `data/tmp/<sha>/render/annotated/p.NNNN.png` (stessa DPI del render pagina), generato nella fase render solo per pagine annotate.

- **Integration Points**
  - Interni: store `ingest_notes` (lettura al submit), job registry/progress (nessun nuovo evento obbligatorio; opzionale conteggio "pagine con overlay").
  - Esterni: nessuno nuovo.
  - Compatibilità: richieste senza `annotations` (client vecchi, libri senza annotazioni) → pipeline identica a oggi.

- **Security & Privacy**
  - Nessun nuovo dato sensibile; le annotazioni restano nello store esistente. Non loggare `state_json` intero (già policy); loggare solo conteggi per pagina.

- **Open Questions**
  - Root cause del crash "Affinamento Indice" sul run originale (recuperare log; `TBD`).
  - Budget caratteri `<operator_notes>` ottimale per modelli piccoli locali (default proposto 8.000; `TBD` dopo smoke test).
  - Alcuni vision model potrebbero trascrivere le etichette overlay nonostante l'istruzione: se lo smoke test lo mostra, fallback = solo blocco testuale con coords 0-999 senza seconda immagine (flag `ANNOTATION_OVERLAY_ENABLED`).

---

### 5. Risks & Roadmap

- **Phased Rollout** (dettaglio eseguibile in **§6**; qui solo la sequenza):
  - **T0–T2** (nessun costo LLM sul libro): implementazione US-1…US-6 + unit test + baseline del validatore sull'output *attuale* di Storia di Roma.
  - **T3 Smoke** (costo LLM solo su 15 pagine): `scripts/rerun_annotated_pages.py` sul pacchetto fisso §6.4, poi validatore. Gate go/no-go prima del libro intero.
  - **T4 Run completo** (costo LLM sul libro): re-ingest pilota da Stage 2 in poi, validatore su tutte le 141 annotate, report KPI.
  - **T5 Decisione C**: confronto Stage 2 vs Stage 3 sul report; se i trigger dell'Appendice C scattano, aprire C-lite; altrimenti chiudere l'MVP.
  - **v1.1**: selezione "pagine rappresentative" per il guidance basata su clustering visivo; report conformità in dashboard; estensione registry da etichette di altri libri.
  - **v2.0**: Appendice C se T5 lo richiede.

- **Technical & Product Risks**
  - *Leak etichette overlay nell'output* → stripper dedicato + fallback testo-solo (flag).
  - *Note lunghe degradano modelli piccoli* → budget caratteri + ordine di priorità (blocco per-pagina > page_notes > guidance).
  - *Invalidazione cache rigenera tutto il libro al primo run post-deploy* → atteso e voluto sul pilota; documentare per libri già ingeriti (le pagine `output/` non vengono toccate finché non si rilancia).
  - *Stage 2 su 2 immagini raddoppia la latenza sulle pagine annotate* → ~34% delle pagine sul pilota; parallelismo esistente invariato.
  - *Il refine indice non può correggere un ordine colonne già rotto da Stage 2* → mitigato da US-2 (le pagine indice sono annotate) + prompt refine che vieta il riordino.
  - *Uno smoke "a occhio" su pagine a caso non copre le etichette rare* → il pacchetto §6.4 è **fisso** e non sostituibile senza aggiornare questa PRD.

---

## 6. Playbook di test e rollout (obbligatorio per gli agenti)

Questa sezione è il contratto di QA. Chi implementa o verifica questa PRD la segue in ordine. Non inventare un altro pacchetto di pagine, non saltare la baseline, non lanciare T4 se T3 è rosso.

### 6.0 Costanti del libro pilota

| Chiave | Valore |
|--------|--------|
| Titolo | Storia di Roma Antica |
| `SOURCE_SHA` | `a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd` |
| Draft annotazioni | `data/sync/drafts/<SOURCE_SHA>.json` |
| Output | `data/output/<SOURCE_SHA>/` |
| Tmp | `data/tmp/<SOURCE_SHA>/` |
| Pagine PDF originali | 420 |
| Pagine allineate in output | 417 (`pages_to_remove` = 3, 4, 5) |
| Pagine annotate | 141 (232 shape, 51 nomi grezzi) |
| Range TOC originale | 417–418 (allineate 414–415) |
| Range INDEX originale | 403–416 (allineate 400–413) |
| Baseline nota (2026-09-05, output recuperato dai tmp pre-B) | 0/417 pagine con aside `{`; box annotati spesso diventano `#`; cronologia orig. 394 letta riga-per-riga; `INDEX.md`/`TOC.md` assenti |

Mapping originale → allineato: `manifest.json` → `pages[].original` / `pages[].aligned` / `pages[].file`. Le annotazioni usano il numero di **pagina originale** del PDF.

### 6.1 Istruzioni per l'agente (leggere per prime)

1. Prima di qualsiasi chiamata LLM sul libro: T1 (unit) e T2 (baseline). Se T1 fallisce, stop.
2. T3 costa vision/editor su **15 pagine**, non su 417. È il gate. Se T3 è rosso, **non** lanciare T4: fix e ritestare T3.
3. T4 richiede conferma esplicita dell'operatore nel chat ("vai col run completo") dopo che l'agente ha incollato il report T3.
4. Ogni fase scrive un file sotto `data/tmp/<SOURCE_SHA>/annotation-qa/` (creare la cartella). Non lasciare i numeri solo nel transcript.
5. Se il PDF non è sul disco, non inventare un ingest: chiedere all'operatore di ri-uploadare / usare Biblioteca. Il draft JSON **non** contiene il PDF.
6. Non cancellare `data/output/<SOURCE_SHA>/` finché T3 non è verde: è la baseline e il recupero del run crashato.
7. `force_recompute` da solo su un re-ingest HTTP **rifà tutto il libro**. Per lo smoke usare **solo** `scripts/rerun_annotated_pages.py` (T3). Per T4, cancellare i marker cache di stage2/stage3/refine (o `force_recompute` sul job) **dopo** il go dell'operatore.

### 6.2 Validatore — `scripts/validate_annotated_pages.py`

Deliverable di implementazione, non un'idea. Firma CLI:

```text
python -m scripts.validate_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --pages-dir data/output/<SOURCE_SHA>/pages \
  --annotations data/sync/drafts/<SOURCE_SHA>.json \
  --manifest data/output/<SOURCE_SHA>/manifest.json \
  --only-original 10,12,15 \
  --stage2-dir data/tmp/<SOURCE_SHA>/stage2Vision \
  --stage3-dir data/tmp/<SOURCE_SHA>/stage3Editor \
  --out data/tmp/<SOURCE_SHA>/annotation-qa/report.json
```

Comportamento:

- Carica annotazioni dal draft (`state.annotations`) o da `--annotations` JSON con la stessa forma.
- Per ogni pagina originale con ≥1 shape, risolve il file MD allineato via manifest.
- Applica le regole del registry (stesso modulo `annotation_rules.py` della pipeline). Una pagina è **conforme** solo se **tutte** le regole implicate dalle sue etichette passano.
- Exit code `0` se la % conforme sul sottoinsieme valutato è ≥ soglia (`--min-pass`, default `0.90`); altrimenti `1`.
- Scrive JSON + stampa un riepilogo testuale.

Regole minime del validatore (testabili, niente giudizio LLM):

| Etichetta canonica (sottoinsieme) | Pass se |
|-----------------------------------|---------|
| `box_curiosita` e varianti (doppia, tripla, sandwich, wimg, tabella, multipage) | Esiste almeno un blocco aside: riga `{` e riga `}` successive. **Fail** se un heading `#`/`##`/`###` ha testo che coincide (normalizzato, senza punteggiatura) col titolo ALL CAPS immediatamente sotto una bbox Box, quando quel titolo è ricostruibile dalle prime righe del MD in ALL CAPS. |
| `capitolo` | Esiste almeno un heading `# ` (non `##`). |
| `sezione` | Esiste almeno un heading `## `. |
| `immagine_*` | Esiste almeno una riga didascalia: riga che inizia con `>` **oppure** riga intera in italics `*…*` / `_…_`. (Il validatore accetta entrambi finché `md_captions` e le note operatore non sono allineati a una sola convenzione.) |
| `cronologia_su_2_colonne` / `senso_di_marcia_multicolonna` su pagine di cronologia | Nessuna riga contiene due coppie `Nome (anni)` affiancate (pattern `)\s+[A-ZÀ-Ü]` dopo una chiusura `)` di anno). I nomi in colonna sinistra e destra non stanno sulla stessa riga. |
| `indice_analitico_a_2_colonne` | Almeno una riga lemma in italics `*…*` o `_…_` (luoghi). Nessuna riga con due lemmi distinti separati da 2+ spazi o tab (sintomo di lettura riga-per-riga). |
| `2_righe_di_pagine_cit` | Esiste almeno una riga che è solo numeri di pagina (`12, 15, 22` o `12-15`) senza lemma a sinistra. |
| `toc_1_colonna` / `titolo_toc` | Esiste una riga titolo TOC e le voci sono `N titolo` / `p. N titolo` (non due voci per riga). |

Il report JSON ha forma:

```json
{
  "sha": "...",
  "pages_dir": "...",
  "evaluated": 15,
  "passed": 12,
  "failed": 3,
  "pass_rate": 0.8,
  "pages": [
    {
      "original": 12,
      "aligned": 9,
      "file": "pages/p.0009.storia-di-roma-antica.md",
      "labels": ["box_curiosita", "header_dispari"],
      "ok": false,
      "checks": [
        {"rule": "box_aside", "ok": false, "detail": "nessun blocco { }"},
        {"rule": "box_not_heading", "ok": false, "detail": "H1 «LO SCAVO ALL'ISTITUTO TECNICO…»"}
      ]
    }
  ]
}
```

Se `--stage2-dir` e `--stage3-dir` sono passati, il report include anche `stage2_ok` / `stage3_ok` / `stage3_regressed` per pagina (serve T5 / Appendice C).

### 6.3 Re-run sottoinsieme — `scripts/rerun_annotated_pages.py`

Serve perché l'HTTP ingest **non** ha un "force_recompute su N pagine": `force_recompute` sul job rifà l'intero libro.

```text
python -m scripts.rerun_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --original-pages 10,12,15,27,41,67,107,150,168,219,335,336,394,403,417 \
  --from-stage 2 \
  --force-recompute \
  --write-output-pages
```

Comportamento:

- Carica Settings, PDF (da storage ingest / path in draft o `data/` — fallisce chiaro se manca), annotazioni, alignment da manifest.
- Per ogni pagina originale richiesta: invalida cache `stage2Vision` + `stage3Editor` di quella pagina (cancella il `.md` o bypass con `force_recompute=True` sul singolo page job).
- Riesegue Stage 2 (con overlay+blocco se US-2 è implementato) e Stage 3 (cieco, come da MVP) **solo** su quelle pagine.
- Con `--write-output-pages`: aggiorna i corrispondenti `data/output/<SHA>/pages/p.NNNN.*.md` (non tocca le altre 400 pagine).
- Non lancia polyindex, non lancia affinamento TOC/INDEX a meno di `--also-refine` (usare `--also-refine` solo in T4 o se lo smoke include orig. 403 e 417 e si vuole un frammento refine).
- Logga model + hash note (US-6) per ogni pagina scritta.

Se lo script non esiste ancora, T3 è bloccato: implementarlo **prima** di spendere LLM a mano.

### 6.4 Pacchetto smoke (15 pagine originali, fisso)

Non sostituire queste pagine senza emendare la PRD. Scelte per **massima diversità di etichette**, non per vicinanza nel libro.

| # | Orig. | Allineata | Etichette (grezze) | Perché è nel pacchetto | Fallimento noto in baseline |
|---|------:|----------:|--------------------|------------------------|-----------------------------|
| 1 | 10 | 7 | Capitolo (#), Sezione (##) | heading massimi | nessun `#` / `##`; testo piatto |
| 2 | 12 | 9 | Box Curiosità, Header dispari | box singolo | box → `# LO SCAVO…` |
| 3 | 15 | 12 | Box_Curiosità, immagine_part-page_part_width, Header_Pari | box + didascalia laterale | no `{}`, no didascalia MD |
| 4 | 27 | 24 | due Box_Curiosità, Header_Pari | due box, flusso spezzato | `# LA LUPA` + `# LA FONDAZIONE…` + corpo spezzato |
| 5 | 41 | 38 | Capitolo_(#), Sezione_(##), Box_Curiosità | tre famiglie sulla stessa pagina | box non aside |
| 6 | 67 | 64 | doppia Box Curiosità | variante stacked | `TBD` in baseline (validatore T2 lo misura) |
| 7 | 107 | 104 | Box_Curiosità_Sandwitch | sezione in mezzo a due box | rischio sezione trattata come box |
| 8 | 150 | 147 | Tripla Box curiosità | tre box stacked | `TBD` in baseline |
| 9 | 168 | 165 | Box Curiosità con Tabella, Decorazione | tabella + ornamento | `TBD` in baseline |
| 10 | 219 | 216 | Grafico Genealigico | contenuto non prosa | `TBD` in baseline |
| 11 | 335 | 332 | Multipage Box (part 1, top) | box a cavallo, metà superiore | `TBD` in baseline |
| 12 | 336 | 333 | Multipage Box (parte 2, bottom) | coppia obbligatoria con 335 | `TBD` in baseline |
| 13 | 394 | 391 | Titolo Appendice, Cronologia 2 col., Senso di Marcia | colonne + anni | `Romolo (…) Tarquinio (…)` sulla stessa riga; imperatori intercalati |
| 14 | 403 | 400 | Titolo Indice, Indice 2 colonne | inizio indice | luoghi non italics; possibile riga-per-riga |
| 15 | 417 | 414 | Titolo TOC, TOC 1 colonna | sommario | da verificare: due voci/riga? titolo? |

Lista CSV per i comandi: `10,12,15,27,41,67,107,150,168,219,335,336,394,403,417`.

### 6.5 Fasi (T0–T5)

#### T0 — Preflight (nessun LLM)

Checklist, in ordine. Stop al primo fail.

- [ ] Server: `make run-server` già in uso o riavviabile; non è obbligatorio per T1/T2.
- [ ] Esistono `data/sync/drafts/<SOURCE_SHA>.json` e `data/output/<SOURCE_SHA>/manifest.json` e ≥400 file in `pages/`.
- [ ] PDF recuperabile (path in draft `has_pdf` / storage ingest). Se manca: **chiedere all'operatore**, non proseguire a T3/T4.
- [ ] `tmp/<SOURCE_SHA>/` può esistere (stage1/2/3). Non cancellarlo in T0.
- [ ] Creare `data/tmp/<SOURCE_SHA>/annotation-qa/`.
- [ ] Annotare nel report T0 i modelli correnti da `.env`: `VISION_MODEL`, `EDITOR_MODEL`, `OCRVISION_MODEL` / `GLM_OCR_MODEL`.

Output: `annotation-qa/T0-preflight.md` (modelli, path PDF sì/no, conteggio pages).

#### T1 — Unit e lint (nessun LLM, obbligatorio)

```text
make lint
python -m unittest tests.test_annotation_rules tests.test_md_cache_notes_hash tests.test_validate_annotated_pages tests.test_stage2 tests.test_stage3 tests.test_page_guidance_suggest tests.test_toc_index_refine
```

Gate: tutti verdi. Copertura minima attesa:

- 51 nomi grezzi del draft → ≤ 30 canonici.
- Composizione note: `page_prompt_notes` contiene `page_notes` grezze; `index_prompt_notes` contiene `index_notes` e **non** è uguale al solo guidance.
- Cache: stesso modello + hash diverso → miss; stesso hash → hit.
- Validatore su fixture (pagina inventata con `{` / senza `{`) → pass/fail attesi.

Output: `annotation-qa/T1-unittest.txt` (stdout).

#### T2 — Baseline sul output attuale (nessun LLM)

Esegue il validatore **sull'output già in `data/output/`**, senza re-run. Serve il delta "prima/dopo".

```text
python -m scripts.validate_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --out data/tmp/<SOURCE_SHA>/annotation-qa/T2-baseline-all.json

python -m scripts.validate_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --only-original 10,12,15,27,41,67,107,150,168,219,335,336,394,403,417 \
  --out data/tmp/<SOURCE_SHA>/annotation-qa/T2-baseline-smoke.json
```

Gate: lo script gira (exit 1 è atteso: la baseline è cattiva). Se crasha, fix del validatore, non del libro.

L'agente **incolla nel chat** `pass_rate` globale, `pass_rate` smoke, e le 15 righe `original / ok / rules failed`. Non riassumere a parole senza i numeri.

Attese qualitative (se il validatore contraddice, vince il validatore e si aggiorna questa riga):

- Globale: aside `{` ≈ 0; `pass_rate` box vicino a 0.
- Smoke: orig. 10, 12, 27, 394 quasi certamente `ok: false`.

#### T3 — Smoke 15 pagine (LLM, `force_recompute` solo su quelle)

Precondizione: T1 verde, T2 eseguito, PDF presente, US-1/2/3/6 nel codice.

```text
python -m scripts.rerun_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --original-pages 10,12,15,27,41,67,107,150,168,219,335,336,394,403,417 \
  --from-stage 2 \
  --force-recompute \
  --write-output-pages

python -m scripts.validate_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --only-original 10,12,15,27,41,67,107,150,168,219,335,336,394,403,417 \
  --stage2-dir data/tmp/<SOURCE_SHA>/stage2Vision \
  --stage3-dir data/tmp/<SOURCE_SHA>/stage3Editor \
  --out data/tmp/<SOURCE_SHA>/annotation-qa/T3-smoke.json
```

**Gate go (tutti obbligatori) prima di chiedere T4:**

1. `pass_rate` smoke ≥ **0.80** (12/15). Soglia più bassa del KPI finale (0.90) perché 15 pagine includono i casi più duri (genealogia, multipage, 2 colonne).
2. Orig. **12** e **27**: `box_aside` pass **e** `box_not_heading` pass. Se anche uno dei due è fail → **no-go**, indipendentemente dal rate (è il bug che ha scatenato la PRD).
3. Orig. **394**: regola colonne pass (niente due regni sulla stessa riga).
4. Nessuna etichetta overlay (`Box_Curiosità`, `Header_Pari`, coords `0-999`) nel MD prodotto. Se c'è leak: attivare/verificare stripper; se persiste, ritestare con `ANNOTATION_OVERLAY_ENABLED=0` (solo blocco testuale) e annotare il risultato nel report.
5. Stage 3 non ha **regresso** più di 1 pagina rispetto a Stage 2 sulle 15 (`stage3_regressed` ≤ 1). Se ≥ 2, T3 resta go per B ma T5 segnala C-lite.

**No-go:** fix puntuale (prompt regola, overlay, leak), ritestare **solo le pagine fail** (`--original-pages` ristretto), poi rilanciare il validatore sulle 15. Massimo 2 cicli fix+T3 prima di fermarsi e chiedere all'operatore.

Output: `T3-smoke.json` + `T3-smoke.md` (tabella 15 righe prima/dopo vs T2). L'agente incolla la tabella nel chat e **chiede** il go a T4. Non partire da solo.

#### T4 — Run completo del pilota (LLM, solo dopo go operatore)

Obiettivo: Success Criteria §1 sul 141 annotate + US-5 (INDEX/TOC).

Procedura:

1. Snapshot di sicurezza (non distruttivo): copiare `data/output/<SOURCE_SHA>/pages` → `data/tmp/<SOURCE_SHA>/annotation-qa/pages-pre-T4/` se non esiste già (T3 ha già sovrascritto 15 file: va bene, la baseline T2 è nei JSON).
2. Invalidare cache stage2/stage3/refine: cancellare i `.md` in `data/tmp/<SOURCE_SHA>/stage2Vision/`, `stage3Editor/`, `stage4TocIndexRefine/` **oppure** lanciare il re-ingest HTTP con force recompute. Non cancellare `stage1OCR/` se l'OCR è ancora valido (B non cambia Stage 1).
3. Re-ingest da dashboard / API usando lo **stesso** draft (note + annotazioni). Verificare nei log di submit che `IngestRequest.annotations` non sia vuoto (conteggio 232) e che `page_prompt_notes` non sia solo il guidance troncato.
4. Attendere fine job. Se crasha all'Affinamento Indice: diagnosticare (US-5), non dichiarare T4 passato. Completare INDEX/TOC dai pages materializzati.
5. Validatore su tutte le annotate + check file aggregati:

```text
python -m scripts.validate_annotated_pages \
  --sha a524352156f0954a0d7c6ea17c427b8a6cbef8b170024f5917fef26c7c4b84bd \
  --stage2-dir data/tmp/<SOURCE_SHA>/stage2Vision \
  --stage3-dir data/tmp/<SOURCE_SHA>/stage3Editor \
  --min-pass 0.90 \
  --out data/tmp/<SOURCE_SHA>/annotation-qa/T4-full.json
```

Controlli extra (l'agente li esegue, non "si ricorda"):

- Esistono `data/output/<SOURCE_SHA>/INDEX.md` e `TOC.md`.
- Guidance persistito (store / draft / log): non termina a metà token; menziona indice e appendici.
- Cache: toccare una sola riga di `page_notes` in un dry-run di hash e verificare che lo SHA8 del marker stage2 cambi (test già in T1; in T4 basta un log spot su 1 pagina).

**Gate T4 (MVP chiuso):**

| Criterio | Soglia |
|----------|--------|
| `pass_rate` su 141 annotate | ≥ 0.90 |
| Pagine Box annotate con heading-che-è-un-box | 0 |
| `INDEX.md` / `TOC.md` | presenti, refine completato senza crash |
| Guidance | non troncato |
| Leak overlay | 0 pagine |

Se `pass_rate` è 0.80–0.89: MVP **non** chiuso; l'agente elenca le etichette canoniche con fail rate ≥ 30% e propone fix registry/prompt, **senza** lanciare un terzo full-run se non richiesto.

#### T5 — Decisione Appendice C (nessun LLM extra)

Dal `T4-full.json` (campi `stage2_ok` / `stage3_ok`):

- Sia `n_regressed` = pagine annotate con Stage 2 conforme e Stage 3 non conforme.
- Sia `n_fallback` = pagine in cui Stage 3 è stato scartato per output invalid / leak (da log job, campo se esposto).

Aprire C-lite se `n_regressed / 141 ≥ 0.05` **oppure** fallback > 2% delle pagine job. Altrimenti scrivere in `T5-decision.md`: "C chiusa, Stage 3 resta cieco".

### 6.6 Cosa l'agente scrive nel chat a ogni gate

Formato obbligatorio (così il prossimo agente o l'operatore può riprendere):

```text
QA <T#> <SOURCE_SHA[:12]>
pass_rate=… evaluated=… passed=… failed=…
go/no-go: …
pagine fail: orig N (rules…); …
artifact: data/tmp/…/annotation-qa/T#-….json
next: <T#+1 oppure fix>
```

### 6.7 Fuori scope del playbook

- Non misurare le 276 pagine **non** annotate in T3/T4 come KPI (possono comparire in un'appendice informativa del report T4, senza gate).
- Non cambiare modello "per vedere se migliora" durante T3/T4: un cambio modello invalida il delta vs baseline.
- Non ri-generare il Consiglio AI in T3 (le 15 pagine usano le note grezze + overlay). La verifica "guidance non troncato" è T4 (o una chiamata `suggest_page_guidance` isolata in T4.0 se l'operatore la chiede prima del full run).


## Appendice C — Stage 3 Editor con immagine (upgrade futuro)

**Perché oggi resta cieco.** Stage 3 è un cleanup di Markdown (`editor_prompt.md`): normalizza spaziature e sillabazioni. Non re-legge la pagina; se Stage 2 ha rotto la struttura, Stage 3 propaga. Con l'Opzione B la struttura viene decisa a Stage 2 con overlay+regole, quindi il valore marginale dell'immagine a Stage 3 è basso rispetto al costo (+1 chiamata vision per ognuna delle 417 pagine, e `EDITOR_MODEL` dovrebbe diventare un vision model, vincolando la scelta dei modelli locali).

**Quando riaprire il tema (trigger misurabili).** Dopo il run pilota con B:
1. Se il validatore mostra ≥ 5% di pagine annotate in cui Stage 2 produce la struttura giusta ma Stage 3 la degrada (confronto `stage2Vision/*.md` vs `stage3Editor/*.md` sulle regole del registry), oppure
2. se il fallback "output invalid → riusa Stage 2" scatta su > 2% delle pagine.

**Forma proposta (C-lite, non full re-OCR).**
- Stage 3 riceve l'immagine **solo per le pagine annotate** (o solo per quelle in cui il validatore inline rileva violazioni del registry), come verifica: "correggi il Markdown SOLO dove contraddice l'immagine e le regole per-pagina; non ritrascrivere".
- Richiede: `EDITOR_MODEL` vision-capable per quel sottoinsieme (o routing per-pagina al `VISION_MODEL` via `compute_plan`), estensione firma `run_stage3_editor` con `page_image_path`/`page_annotations` opzionali, cache marker già pronto (US-6).
- KPI proprio: riduzione ≥ 50% delle violazioni introdotte tra Stage 2 e Stage 3, costo aggiuntivo ≤ 35% di chiamate vision extra.

**Full C (compiler su tutte le pagine + Stage 3 vision ovunque)** resta fuori scope finché B + C-lite non dimostrano un plateau di qualità: è l'opzione più costosa e l'evidenza attuale (errori concentrati dove Stage 2 sbaglia struttura) non la giustifica.
