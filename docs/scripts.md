# Script operativi

Utility CLI in `scripts/`. Non fanno parte del runtime HTTP; si lanciano a mano (o da docs/README).

Esegui dal root del repo con il venv attivo:

```bash
python -m scripts.<nome> [opzioni]
```

## Inventario

### `backup_data`

Crea uno ZIP di `data/` (di default esclude `tmp/`) in `backup/`, oppure ripristina da ZIP.

**Attenzione:** `restore` fa `rmtree` di `data_root` senza conferma interattiva.

### `backfill_time_index`

Rigenera `TIME_INDEX.json` per tutti i libri con manifest in `output/`, usando le stesse regole LLM/regex dell'ingest.

Opzioni tipiche: `--data-root data`.

### `backfill_index_book_meta`

Completa `title` / `slug` nelle entry libro dentro `INDEX.json` (utile dopo migrazioni di schema).

### `backfill_books_from_manifest`

Upsert in SQLite dei metadati REICAT letti da `output/*/manifest.json`. Supporta `--dry-run`.

### `sort_index_files`

Ordina chiavi/voci in `INDEX.json` e negli `INDEX.md` per libro. Supporta `--dry-run`.

### `merge_pdf_pages`

CLI interattiva per unire range di pagine da PDF diversi in un unico file (utility offline, non collegata all'API).

## Quando usarli

| Situazione | Script |
|------------|--------|
| Nuovo schema TIME_INDEX su libri vecchi | `backfill_time_index` |
| INDEX.json senza title/slug | `backfill_index_book_meta` |
| SQLite non allineato ai manifest | `backfill_books_from_manifest` |
| Indici disordinati dopo merge manuali | `sort_index_files` |
| Backup prima di operazioni distruttive | `backup_data` |
