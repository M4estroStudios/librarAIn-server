# Operazioni quotidiane

## Checklist avvio giornata

1. LM Studio acceso con i modelli di `.env` disponibili.
2. `make run-server`
3. Aprire `/dashboard` o `/` — verificare `GET /health` e, se serve, `GET /api/system/status`.
4. Prima di ingest/research pesanti: preflight (`operation=ingest` / `research`).

## Flusso ingest tipico

1. Compilare REICAT + range TOC/INDEX + pagine da rimuovere su `/` o `/index2.html`.
2. Aggiungere note di formattazione se il libro ha convenzioni particolari.
3. Submit → seguire SSE (o pagina Jobs).
4. A fine job: controllare `data/output/<sha>/` (pagine, INDEX, TOC) e polyindex.
5. In admin: merge soggetti duplicati se compaiono cluster ovvi; audit pagine se ci sono gap.

## Flusso research tipico

1. Cercare un POH da dashboard/ricerca.
2. Generare articolo singolo (`submit`) oppure batch dei mancanti (`generate`).
3. Rivedere HTML in `/articolo/<id>.html`.
4. Se arriva materiale da un nuovo libro: `merge-article` dall'admin/research.

## Job e SSE

- Lista attiva: `/jobs` o `GET /api/system/jobs`
- Storico: `GET /api/system/jobs/history`
- Replay SSE: header `Last-Event-ID` = ultimo `seq` ricevuto
- Semafori: se un job resta `queued`, un altro job pesante sta usando lo slot

## Manutenzione indici

```bash
python -m scripts.backup_data …          # prima di operazioni rischiose
python -m scripts.backfill_time_index
python -m scripts.backfill_index_book_meta
python -m scripts.sort_index_files --dry-run
python -m scripts.backfill_books_from_manifest --dry-run
```

## Troubleshooting

| Sintomo | Cosa controllare |
|---------|------------------|
| 403 su POST da browser/tool | Origin / Sec-Fetch-Site (curl senza Origin è ok) |
| Ingest skip immediato | Hash già in `books` (gate); usare opzioni force metadata se serve solo aggiornare REICAT |
| Errori VRAM / preflight fail | Chiudere modelli LM Studio, abbassare `GPU_VRAM_MAX_USED_GB`, un job alla volta |
| Unicode / log strani su Windows | Logger sostituisce char non rappresentabili su console; i file log sono UTF-8 |
| Warning `http request returned error` su `/nav.css` | Asset statico non servito (path/status nei params) |
| Articolo senza link POH | Embeddings mancanti / soglia matcher; lanciare backfill embeddings |
| Gap pagine in admin | Repair pagina o repair-all; verificare exclude |
| `database is locked` | Ridurre concorrenza; evitare script lunghi in parallelo al server |
| Merge-article 500 | Verificare che il server sia aggiornato (handler richiede import corretto) |

## Logging

La libreria (`src/core/log.py`) è volutamente sottile: livello, messaggio, params opzionali, file/line/caller automatici.

**Regola obbligatoria:** ogni `Log(...)` usa un messaggio **statico, specifico e autoreferenziale**. Il testo identifica l'evento e non cambia a runtime; i valori variabili (method, path, status, job_id, model, errore, …) stanno **solo** in `params`. Non interpolare f-string nel messaggio.

Esempi:

- male: `"http"` (troppo generico)
- male: `f"ingest http server listening on http://{host}:{port}"` (valori nel messaggio)
- bene: `"ingest http server listening"` + `{"url": "http://…"}`
- bene: `"http request returned error"` + `{"method": "GET", "path": "/nav.css", "status": 404}`

Se un punto critico fallisce senza log, o con un messaggio generico/dinamico, va corretto nello stesso PR del comportamento.

## Test e qualità

```bash
make lint
make test
```

Suite: `unittest` sotto `tests/` (~600+ casi). CI su push/PR a `main`.

## Limiti operativi noti

- Un solo host locale; no auth.
- File UI monolitici (`index.html`, `admin.html`) — modifiche UI richiedono attenzione.
- `data/output` può diventare molto grande se versionato in git.
