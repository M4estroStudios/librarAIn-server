# librarAIn documentation

Documentazione operativa e tecnica del server **librarAIn-server**: ingestion di libri storici scansionati, indici POH cross-libro, ricerca e generazione articoli.

Questa cartella è la fonte di verità aggiornata al codice. I PRD in root (`PRD-Fase1.md`, `PRD_research.md`, `PRD-DASHBOARD.md`) restano requisiti di prodotto; qui trovi come il sistema funziona oggi.

## Indice

| Documento | Contenuto |
|-----------|-----------|
| [overview.md](overview.md) | Cos'è librarAIn, fasi, concetto di dominio |
| [architecture.md](architecture.md) | Package `src/`, layering, flusso end-to-end |
| [setup.md](setup.md) | Installazione, Makefile, avvio server |
| [configuration.md](configuration.md) | Variabili `.env` e settings |
| [data-layout.md](data-layout.md) | Albero `data/`, artefatti per libro e globali |
| [ingestion.md](ingestion.md) | Pipeline PDF → Markdown (EasyOCR e GLM) |
| [polyindex.md](polyindex.md) | `TOC.json`, `INDEX.json`, `TIME_INDEX.json` |
| [research.md](research.md) | Ricerca, articoli POH, batch, chat |
| [api.md](api.md) | Catalogo endpoint HTTP |
| [web-ui.md](web-ui.md) | Pagine operatore |
| [persistence.md](persistence.md) | SQLite e tabelle |
| [scripts.md](scripts.md) | Utility CLI in `scripts/` |
| [security.md](security.md) | Modello di sicurezza attuale |
| [operations.md](operations.md) | Job, SSE, preflight, backup, troubleshooting |

## Avvio rapido

```bash
cp example.env .env   # poi adatta modelli e DATA_ROOT
make setup-env
make run-server       # http://127.0.0.1:8765
```

Dettagli in [setup.md](setup.md) e [configuration.md](configuration.md).

## Glossario minimo

- **REICAT** — metadati bibliografici del libro (titolo, autori, ISBN, …).
- **POH** — soggetto dell'indice analitico (persona / opera / luogo / …) con `canonical_id`.
- **Aligned page** — numero pagina dopo rimozione delle pagine spurie dal PDF.
- **Polyindex** — indici aggregati cross-libro in `data/polyindex/`.
- **Gate hash** — se lo SHA-256 del PDF è già in SQLite, l'ingest viene saltato.
