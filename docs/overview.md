# Panoramica

## Cos'è librarAIn

librarAIn è un sistema interno per trasformare **PDF scansionati di libri storici** in una biblioteca digitale interrogabile. Il server (`librarAIn-server`) esegue tutta la pipeline locale: OCR, raffinamento LLM, indici analitici, ricerca e generazione articoli.

Target tipico: libri su Roma / Italia (monografie, guide, storiografia) con indice analitico e tavola dei contenuti. L'operatore lavora da browser su `http://127.0.0.1:8765` con LM Studio (o API OpenAI-compatibili) per i modelli.

## Due fasi del prodotto

### Fase 1 — Ingestione

1. L'operatore carica un PDF e i metadati REICAT (form web).
2. Il server allinea il PDF (rimuove pagine inutili), esegue OCR/Vision/Editor.
3. Produce Markdown per pagina, TOC e INDEX del libro.
4. Aggiorna gli indici globali (**polyindex**) e registra il libro in SQLite.

Risultato: ogni libro è un albero sotto `data/output/<sha256>/` più aggiornamenti in `data/polyindex/`.

### Fase 2 — Ricerca e articoli

1. Una query (o un POH) viene risolta contro `INDEX.json` e `TIME_INDEX.json`.
2. Si raccolgono le pagine rilevanti dai Markdown già prodotti.
3. Un LLM genera un articolo stile enciclopedia con citazioni `source:…`, link `poh:…` e cronologia.
4. L'articolo viene pubblicato come MD+HTML sotto `data/research/` e indicizzato in `catalog.json`.

Sono disponibili anche: generazione batch dei POH mancanti, merge di materiale nuovo in un articolo esistente, chat tool-calling stile Perplexity, export bundle E-TALY.

## Concetti di dominio

| Termine | Significato |
|---------|-------------|
| **source_sha256** | Digest SHA-256 del PDF sorgente; chiave primaria del libro |
| **aligned_page** | Pagina 1-based sul PDF già ripulito (`input/processed/<sha>.pdf`) |
| **original_page** | Pagina corrispondente sul PDF originale (prima del cut) |
| **POH / subject** | Voce dell'indice analitico, unificata cross-libro in `INDEX.json` |
| **canonical_id** | Identificativo stabile del POH (slug, es. `marco-polo`) |
| **manifest.json** | Metadati e mappa pagine del singolo libro in output |
| **Job** | Esecuzione asincrona (ingest, research, repair, dedup, …) con SSE |

## Cosa non è

- Non è un servizio multi-tenant esposto in Internet: bind su localhost, nessuna autenticazione API.
- Non sostituisce un CMS: gli articoli sono file Markdown/HTML su disco.
- Non usa un vector DB esterno per l'ingest: embeddings POH vivono in SQLite; la ricerca articoli passa dal catalogo + INDEX.
