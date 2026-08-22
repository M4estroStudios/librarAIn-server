# Sync bozze (laptop ↔ PC ingest)

Metadati delle bozze attive (REICAT, range, note, annotazioni, tipologies appendice).

## Flusso consigliato

### Sul laptop (annotazioni / compilazione)

1. Compila e **Salva Bozza** (esporta automaticamente in `data/sync/drafts/`).
2. Crea il pacchetto completo (JSON + PDF):

```bash
make drafts-pack
```

Output: `data/sync/bundles/librarain-drafts.zip`

3. Opzionale: `git add data/sync && git commit` per i soli JSON (senza PDF).

### Sul PC potente (ingest AI locale)

1. Copia lo ZIP in `data/sync/bundles/librarain-drafts.zip` (o passalo come argomento).
2. Importa:

```bash
make drafts-unpack
```

3. Avvia il server (`make run-server`): all’avvio re-importa anche i JSON da `data/sync/`.

Le bozze compaiono in **Bozze attive** con PDF già pronto (Apri senza ri-upload).
