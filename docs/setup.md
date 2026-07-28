# Setup e avvio

## Requisiti

- Python **3.11+** (il Makefile su Windows punta a `python3.12` se disponibile).
- GPU NVIDIA opzionale ma consigliata per EasyOCR e modelli locali.
- [LM Studio](https://lmstudio.ai/) (o altro endpoint OpenAI-compatibile) se `OPENAI_PROVIDER=local`.
- `make` (su Windows: da Git Bash, WSL, o toolchain equivalente).

## Installazione

```bash
cp example.env .env
# modifica DATA_ROOT, modelli VISION/EDITOR/RESEARCH, OCR_USE_GPU, …

make setup-env
```

`setup-env` crea `venv/`, installa **torch** (CUDA se `nvidia-smi` è disponibile, altrimenti CPU; su Apple Silicon usa il wheel default) e poi `pip install -e ".[dev]"`.

Dipendenze dichiarate in `pyproject.toml`. `requirements.txt` punta solo a `-e .[dev]`.

## Comandi Makefile

| Target | Effetto |
|--------|---------|
| `make setup-env` | Ricrea venv + torch + install progetto |
| `make finish-env` | Pip upgrade + torch + editable install (dopo venv già creato) |
| `make install-torch` | Solo torch/torchvision con index corretto |
| `make test` | `unittest discover -s tests` + clean `__pycache__` |
| `make lint` | `ruff check src tests scripts` |
| `make run-server` | Server HTTP su `127.0.0.1:8765` |
| `make run-mock-server` | Mock UI/API su porta 8766 |
| `make clean-pycache` | Rimuove `__pycache__` |

Override interprete: `make PY=python3.11 setup-env`.

## Avvio tipico

1. Avvia LM Studio e carica i modelli referenziati in `.env`.
2. `make run-server`
3. Apri `http://127.0.0.1:8765/` (ingest) o `/dashboard` (lab unificato).
4. Health check: `GET http://127.0.0.1:8765/health` → `{"ok": true}`.

## Verifica installazione

```bash
make lint
make test
```

CI GitHub Actions (`.github/workflows/ci.yml`) esegue lint + test su Ubuntu / Python 3.12.

## Struttura root rilevante

```
librarAIn-server/
├── src/           # codice applicativo
├── web/           # UI operatore
├── tests/         # unittest
├── scripts/       # utility CLI
├── data/          # runtime (input, output, polyindex, db, research, tmp)
├── docs/          # questa documentazione
├── example.env    # template configurazione
├── pyproject.toml
└── Makefile
```
