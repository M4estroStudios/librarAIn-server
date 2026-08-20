# Piano LibrarAIn — Plugin E-TALY v2

> **Stato:** bozza operativa (2026-08-18)  
> **Owner prodotto:** PM E-TALY / RoMaps  
> **Repo implementazione:** `librarAIn-server`  
> **Contratto schema:** `E-TALY/wiki/RoMaps/assets/Content Contract.md`  
> **Requirements trasmessi:** `E-TALY/wiki/RoMaps/Backlog/Requirements LibrarAIn.md`

---

## 1. Contesto prodotto

E-TALY (prima istanza: **RoMaps**) è una scocca interattiva Flutter intorno a un **database enciclopedico markdown**.

**LibrarAIn** deve funzionare come **creatore di enciclopedie in via generica** (corpus editoriale → articoli + grafo).

Tutto ciò che è legato esclusivamente a E-TALY / RoMaps (ID `poh_*`, CSV city pack, path assets Flutter, lint Content Contract, generatori di gioco) va gestito nello **specifico plugin E-TALY** di LibrarAIn — non nel core.

```
[CORE LibrarAIn]                      [PLUGIN E-TALY]
libri → articoli enciclopedici        articoli → city pack RoMaps
slug, citazioni, cronologia           poh_id, CSV, [[link]], lint, ZIP
city-agnostic                         city_id, regions, path assets
```

---

## 2. Inventario city-specific RoMaps (cosa l’app consuma)

City pack canonico: `E-TALY/RoMaps/assets/` (`city_id`: `romaps_roma`).

### 2.1 Cuore enciclopedico (target primario del plugin v2)

| Dataset | Path | Qty tipiche | Schema / ruolo |
|--------|------|-------------|----------------|
| POH monumenti | `timeline/data/csv/poh_m.csv` + `text/ITA/poh_m*.md` | ~1894 | `name,id_code,beginning,end,shelf` + MD frontmatter |
| POH persone | `poh_p.csv` + `poh_p*.md` | ~4606 | idem |
| POH organizzazioni | `poh_o.csv` + `poh_o*.md` | ~402 | idem |
| POH eventi | `poh_e.csv` + `poh_e*.md` | ~97 | App già consuma MD; catalogo factory incompleto |
| POH siti | solo `poh_s*.md` | ~905 | Target hyperlink; nessun CSV |
| POI catalog | `map/data/csv/POIs.csv` | ~1895 | `id,unlocked,lat,lon,region,address,name,category` |
| POI testi | `map/data/text/ITA/poiXXXX.md` | ~1895 | Infobox mappa |

**Convenzione:** `poiXXXX` ↔ `poh_mXXXX` (stesso suffisso numerico, dove semanticamente stesso luogo).

**Frontmatter MD tipico (monumento):**

```yaml
---
id: poh_m0001
name: …
poi_id: poi0001
lat: …
lon: …
region: r01
category: museo
1877: evento timeline   # chiavi anno → Vertical Bar
---
Body con [[poh_xNNNN|label]] e FONTI
```

### 2.2 Feature dichiarate ma quasi vuote (fase 2 plugin)

| Dataset | Stato |
|---------|--------|
| Quiz `timeline/data/text/quiz/<poh_id>.md` | Quasi assenti |
| Audio `map|timeline/data/audio/{LANG}/` | Cartelle vuote |
| Traduzioni `ENG/CHI/RUS` | Directory presenti, 0 MD |
| Cover WebP | ~4218 già in pack (pipeline cover) |

### 2.3 Pack strutturale (non output tipico LibrarAIn)

`city.json`, `Regions.csv`, `CategoriesSVG.csv`, GeoJSON rioni/anello/confini, GTFS/transit, `calendar_events.json`, `board.json`, musica, mascotte, overlay GIS.

Checklist multi-città: `E-TALY/wiki/RoMaps/assets/Second City Checklist.md`.

### 2.4 Come RoMaps carica i dati

```
city.json
  → CsvSeeder (POI/Regions) → Drift locale (progress utente)
  → PohCatalog (poh_m/p/o.csv)
  → MdHandler + LocalizedAssetResolver (*.md / audio)
```

I testi enciclopedici restano **asset**; Drift non è il DB dei contenuti.

---

## 3. Stato attuale LibrarAIn

### 3.1 Core (generico — già presente)

| Layer | Path | Ruolo |
|-------|------|--------|
| HTTP | `src/api/ingest_http_server.py` | Entry server |
| Ingest | `src/ingestion/` | PDF → MD → INDEX/TOC → polyindex |
| Research | `src/search/` | Articoli enciclopedici |
| Persistenza | `src/persistence/` | SQLite `biblioteca.db` |
| Output libro | `data/output/<sha>/` | pages, INDEX, TOC, manifest |
| Polyindex | `data/polyindex/` | TOC/INDEX/TIME_INDEX.json |
| Articoli | `data/research/articles/` | MD interno pre-consumer |

**Formato interno articoli (core):**  
`[label](poh:<slug>)`, `[…](source:<sha>:aligned:<n>)`, `## Cronologia` (tabella GFM).  
**Nessun CSV RoMaps** sotto `data/output/`.

### 3.2 Export E-TALY (oggi: modulo hard-wired, non plugin)

| Pezzo | Path |
|-------|------|
| Adapter MD | `src/export/etaly_adapter.py` |
| Bundle ZIP | `src/export/bundle.py` |
| Lint | `src/export/lint.py` |
| Registry | `src/export/registry.py` (`PohType` = `p,o,m` only) |
| HTTP + UI | `src/api/etaly_export_handler.py`, `web/etaly_export.html` |
| Registry data | `data/etaly/registry.json` |

**ZIP attuale:**

```
text/ITA/{poh_id}.md
sources/<sha>/p{page}.webp
covers/{poh_id}.webp          # opzionale
patch/poh_{p,o,m}.csv         # solo action=new
patch/registry.json
MANIFEST.json
```

**Default assets:** ancora `../E-TALY/e_taly/assets` (legacy) — canonico = `../E-TALY/RoMaps/assets`.

### 3.3 Matrice gap vs Content Contract

| Asset / requisito | Core | Plugin oggi | Gap |
|-------------------|------|-------------|-----|
| Articoli research generici | ✅ | — | Qualità editoriale |
| `poh_{p,o,m}.md` + frontmatter | — | ✅ parziale | Cap cronologia fisso 5; campi wiki magri |
| `patch/poh_{p,o,m}.csv` | — | ✅ | `shelf` spesso vuoto; no merge in-place |
| `poh_e*` | — | ❌ | Estendere `PohType` |
| Path `RoMaps/assets` | — | ❌ | Default legacy |
| `city_id` in MANIFEST | — | ⚠️ | Completare |
| Lint `CC-*` | — | ⚠️ subset | Allineare a Content Contract §6 |
| Multi-city `data/<city_id>/` | ❌ | ❌ | Namespaced roots |
| POIs / quiz / audio / i18n | ❌ | ❌ | **Non-goal v2** (fase 2) |
| Sistema plugin caricabile | ❌ | modulo `export/` | Isolare confine |

---

## 4. Confine architetturale target

### 4.1 Resta nel CORE

- Ingest, polyindex, research, catalogo articoli
- Formato interno stabile (slug, `source:`, Cronologia)
- API generiche, SQLite, job/SSE
- Contratto “articolo finalizzato” come input per qualunque consumer

**Interfaccia suggerita:**

```text
FinalizedEncyclopedia = {
  subjects: [{slug, type?, labels, time_range}],
  articles: [{slug, markdown_internal, citations}],
  sources:  [{sha, aligned_pages}]
}
```

### 4.2 Va nel PLUGIN E-TALY

- Mapping slug → `poh_{p|o|m|e}NNNN` + registry per `city_id`
- Rewrite link, frontmatter YAML RoMaps, sanitizzazione MDHandler
- Patch CSV, ZIP layout, lint Content Contract, path → `RoMaps/assets`
- UI operatore propose / confirm / build
- (Futuro fase 2) generatori game-content dal DB: POI, quiz, i18n — **non** nel core ingest

**CityProfile:**

```text
CityProfile = {
  city_id: "romaps_roma",
  assets_root: ".../RoMaps/assets",
  languages: ["ITA"],
  id_counters / registry,
  chronology_cap,
  region_model
}
→ CityPackZip (MANIFEST + text/ + patch/)
```

### 4.3 Scope v2 vs fase 2 (vincolante)

**LibrarAIn v2 (plugin):** solo **database hyperlinkato** (articoli POH + rete `[[id|label]]` + export pack conforme).

**Non-goal v2:** tile, SVG/emblemi, mascotte, branding, quiz, generatori POI, audio, fasti/challenges.

**Fase 2:** contenuti di gioco **a partire dal DB hyperlinkato** (non più dai libri come sorgente primaria dei generatori), sempre nel plugin.

---

## 5. Piano lavori (issues / PR ordinate)

### Fase 0 — Igiene e allineamento

| ID | Issue | Deliverable | Done when |
|----|-------|-------------|-----------|
| **L0.1** | Hygiene QR-6.1 + docs drift | `.gitignore` su `data/output` e `data/research`; README aggiornato (research già esiste); token API obbligatorio oltre localhost | Repo pulito; README coerente |

Opzionale: 1 pagina in `docs/` che fissa il confine core vs plugin.

---

### Fase 1 — Isolamento plugin (foundation)

| ID | Issue / PR | Cosa fa | Non fa |
|----|------------|---------|--------|
| **L1.1** | Package boundary `plugins/etaly/` (o `src/plugins/etaly/`) | Sposta `export/*`, handler, UI export sotto namespace plugin; API core espone solo articoli finalizzati; import unidirezionali (plugin → core) | Nessuna nuova feature prodotto |
| **L1.2** | CityProfile + path canonico | Setting: `city_id`, `assets_root` → default `../E-TALY/RoMaps/assets`; deprecare `e_taly/assets`; MANIFEST con `city_id` + `schema_version` | Multi-city pieno |
| **L1.3** | Smoke propose→confirm→build | Test/doc: ZIP layout contratto; target verificato contro `RoMaps/assets/timeline/…` | Write-through automatico obbligatorio |

**Ordine:** L1.1 → L1.2 → L1.3 (L1.1+L1.2 possono essere un PR unico).

**Prima issue consigliata:**  
`[plugin] Isolate E-TALY export + retarget RoMaps/assets` (= L1.1 + L1.2, gate merge = L1.3).

---

### Fase 2 — Completare Content Contract v2 (DB hyperlinkato)

| ID | Issue / PR | Priorità | Done when |
|----|------------|----------|-----------|
| **L2.1** | Cap cronologia parametrico (niente hardcode 5) | P0 | Setting condiviso adapter + lint |
| **L2.2** | Lint = gate `CC-*` (ID, LINK, YAML, CHRONO, BODY, LEN); no `CC-CIT` finché OQ-3 | P0 | Build fallisce su link irrisolti / YAML incompleto |
| **L2.3** | `PohType += e` (CSV + MD + registry + lint) | P0 | `patch/poh_e.csv` + `poh_e*.md` nel bundle |
| **L2.4** | CSV patch: `shelf` + merge policy (append/replace per `id_code`) | P1 | Patch conforme Content Contract §5 |
| **L2.5** | JSON Schema mirror del Content Contract (opz.) | P2 | Schema machine-readable |

**Ordine:** L2.1 → L2.2 → L2.3 → L2.4. L2.5 quando serve tooling.

---

### Fase 3 — Multi-city

| ID | Issue / PR | Done when |
|----|------------|-----------|
| **L3.1** | Data root namespaced `data/<city_id>/{db,output,polyindex,research,etaly}/` | Nessuna collisione cross-city su disco |
| **L3.2** | Registry + contatori ID per `city_id` | Stessi numeri in città diverse = pack diversi |
| **L3.3** | Export target switchabile da UI/API (`city_id`) | Operatore sceglie pack senza ritoccare codice |

**Dipendenza:** dopo Fase 1 (CityProfile). Può partire in parallelo a L2.x se L1.2 è stabile.

---

### Fase 4 — Backlog esplicito (non toccare in v2)

| ID | Titolo | Quando |
|----|--------|--------|
| **L4.1** | Generatori POI da DB hyperlinkato | Post-v2, solo plugin |
| **L4.2** | Quiz / audio / i18n pack | Post-v2, plugin + language packs OTA |
| **L4.3** | Citazioni `source:` ↔ MDHandler app (OQ-3) | Coordinato app + plugin, deferred |

Creare subito come milestone “fase 2” / wontfix-v2, così non finiscono nel core.

---

## 6. Milestone e Definition of Done

```
M0  Hygiene                         ████
M1  Plugin boundary + RoMaps            ████████
M2  Contract gates + tipo e                 ████████████
M3  Multi-city                                    ████████
M4  (freeze) POI/quiz/audio                         …… backlog
```

**DoD v2 (milestone chiusa):**

1. Core non conosce path Flutter / `poh_m` / shelf.
2. Plugin produce ZIP conforme: `text/ITA/poh_{p,o,m,e}*.md` + `patch/poh_*.csv` + MANIFEST con `city_id`.
3. Default assets = `RoMaps/assets`.
4. Lint CC blocca export sporco.
5. Nessun lavoro su POI/quiz/audio in questo ciclo.

---

## 7. Ownership

| Chi | Cosa |
|-----|------|
| PM E-TALY (big picture) | Accettazione Content Contract, priorità L2 vs L3, freeze L4, CityProfile Roma |
| Capo tecnico | L1 isolation + L2 lint / tipo `e` |
| PM su LibrarAIn (operativo) | Smoke su corpus reale, review qualità articoli core vs formato export, checklist acceptance |

---

## 8. Titoli issue pronti (GitHub)

1. `[hygiene] QR-6.1 data/ gitignore + README + token` — **L0.1**
2. `[plugin] Isolate E-TALY export + retarget RoMaps/assets` — **L1.1 + L1.2**
3. `[plugin] Smoke propose→confirm→build vs RoMaps pack` — **L1.3**
4. `[adapter] Cap cronologia parametrico` — **L2.1**
5. `[export] lint.py ↔ Content Contract v1 (CC-*)` — **L2.2**
6. `[schema] PohType += e (eventi)` — **L2.3**
7. `[export] CSV patch shelf + merge policy` — **L2.4**
8. `[multi-city] DATA_ROOT namespaced + city profile` — **L3.1–L3.3**
9. `[scope] Lock v2 = solo DB hyperlinkato` (tracking freeze L4) — **L4.***
10. `[deferred] Citazioni source: ↔ MDHandler (OQ-3)` — **L4.3**

---

## 9. Riferimenti

| Documento | Path |
|-----------|------|
| Content Contract | `E-TALY/wiki/RoMaps/assets/Content Contract.md` |
| Requirements LibrarAIn | `E-TALY/wiki/RoMaps/Backlog/Requirements LibrarAIn.md` |
| Second City Checklist | `E-TALY/wiki/RoMaps/assets/Second City Checklist.md` |
| Architettura server | `docs/architecture.md` |
| Export attuale | `src/export/` |
| City pack canonico | `E-TALY/RoMaps/assets/` |
