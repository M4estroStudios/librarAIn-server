# Layout dei dati (`data/`)

Tutto il runtime vive sotto `DATA_ROOT` (default `data/`). Il codice non scrive fuori da questa root (salvo path espliciti negli script).

## Albero

```
data/
├── db/
│   └── biblioteca.db              # SQLite: books, runs, embeddings
├── input/
│   ├── raw/                       # PDF originali caricati dall'operatore
│   ├── raw_appendix/              # PDF appendici estratte (appendix_<nome>.pdf)
│   └── processed/
│       └── <source_sha256>.pdf    # PDF allineato (pagine rimosse)
├── output/
│   └── <source_sha256>/
│       ├── manifest.json
│       ├── TOC.md
│       ├── INDEX.md
│       ├── INDEX.skipped.md       # righe INDEX non parsate (se presenti)
│       ├── <slug>.md              # libro concatenato
│       └── pages/
│           └── p.<NNNN>.<slug>.md
├── polyindex/
│   ├── TOC.json
│   ├── INDEX.json
│   ├── TIME_INDEX.json
│   └── checkpoints/               # snapshot opzionali
├── research/
│   ├── catalog.json
│   ├── articles/
│   │   ├── <poh_id>.md
│   │   └── <poh_id>.html
│   └── …                          # stato batch / query log
├── etaly/
│   └── proposals/                 # mapping export
└── tmp/
    └── <source_sha256>/
        ├── render/                # PNG pagine
        ├── stage1OCR/
        ├── stage2Vision/
        ├── stage3Editor/
        ├── stage4TocIndexRefine/
        ├── exclude_config.json
        └── review_pending.json
```

## Identificativo libro

Il libro è sempre identificato da **`source_sha256`**: 64 caratteri esadecimali lowercase, digest del PDF sorgente al momento della validazione. Tutti i path sotto `output/`, `tmp/`, `input/processed/` usano questo digest.

Validazione centralizzata: `src.core.hashing.validate_source_sha256`.

## Artefatti per libro (`output/<sha>/`)

| File | Contenuto |
|------|-----------|
| `manifest.json` | REICAT, slug, mappa aligned↔original, pagine escluse, opzioni |
| `pages/p.NNNN.<slug>.md` | Markdown finale per pagina allineata |
| `<slug>.md` | Concatenazione libro |
| `TOC.md` | Tavola dei contenuti del libro |
| `INDEX.md` | Indice analitico del libro |
| `INDEX.skipped.md` | Righe scartate dal parser INDEX (revisione umana) |

## Cache intermedie (`tmp/<sha>/`)

Gli stage scrivono output incrementali. Un re-ingest con stesso hash di pagina/modello può saltare il lavoro già fatto (marker modello nei file MD).

`TMP_KEEP_AFTER_SUCCESS=true` (default) lascia le cache su disco dopo un ingest riuscito.

## Polyindex e research

Vedi [polyindex.md](polyindex.md) e [research.md](research.md).

## Cosa è in git

Storicamente molti output sotto `data/output/` e `data/research/` sono tracciati. `.gitignore` esclude tipicamente `data/input/`, `data/tmp/`, `data/db/`, `data/polyindex/`, `backup/`. Verificare `.gitignore` prima di aggiungere nuovi alberi.

## Backup

Utility: `python -m scripts.backup_data` — vedi [scripts.md](scripts.md). I restore cancellano `data_root`: usare con cautela.
