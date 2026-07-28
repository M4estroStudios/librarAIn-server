# Sicurezza

Modello attuale: **tool interno su localhost**, non servizio esposto.

## Controlli presenti

| Controllo | Comportamento |
|-----------|----------------|
| Bind host | Da `INGEST_HTTP_HOST` (default `127.0.0.1`; es. `192.168.1.200` o `0.0.0.0`) |
| CSRF su POST | Se `Sec-Fetch-Site` non è `same-origin`/`none`, oppure `Origin` assente-ma-`null`, oppure host di `Origin` ≠ `Host` → **403** |
| `source_sha256` | Deve essere hex a 64 char (`validate_source_sha256`); applicato nei sink path (preview, exclude, audit) |
| Upload PDF | Magic bytes `%PDF`, `Content-Length` obbligatorio, limite `INGEST_MAX_UPLOAD_BYTES` |
| Filename upload | Sanitizzato + nome random su disco |
| Static dashboard/mockup | Blocco `..` + `relative_to` |
| SQL | Query parametrizzate; f-string solo su identificatori interni costanti |
| Segreti | `.env` in `.gitignore`; nessuna API key hardcoded in `src/` |

## Controlli assenti (consapevoli)

- **Nessuna autenticazione** sulle API (Bearer/token/session). Variabili tipo `INGEST_API_TOKEN` in `.env` locali **non sono lette** dal codice.
- Nessun CORS aperto (non servono header `Access-Control-*`): i browser non espongono risposte cross-origin, ma senza il guard POST un sito poteva comunque *eseguire* mutazioni via simple request.
- Job registry in-memory senza TTL aggressivo documentato come hardening production-grade.

## Implicazioni operative

1. Non esporre la porta 8765 su LAN/VPN senza reverse proxy + auth.
2. Tenere LM Studio e le API key solo sulla macchina operatore.
3. Trattare `data/` come contenuto sensibile (testi OCR, articoli).
4. Prima di operazioni distruttive (`scripts.backup_data` restore) fare uno ZIP.

## Path traversal

Prima della validazione hex, valori tipo `../../…` potevano uscire da `DATA_ROOT` nei costruttori path admin. Ora i sink rifiutano digest non validi con errore 400.
