# Interprete usato per creare il venv (override: make PY=python3.11 setup-env).
ifeq ($(OS),Windows_NT)
PY ?= python3.12
else
PY ?= python3
endif
VENV_PYTHON = $(firstword $(wildcard venv/Scripts/python.exe venv/bin/python.exe venv/bin/python))

# Preferisce il Python del venv se presente (dopo setup-env).
PYTHON ?= $(if $(VENV_PYTHON),$(VENV_PYTHON),$(PY))
INGEST_HTTP_PORT ?= 8765

.PHONY: check-python setup-env finish-env install-torch test lint clean-pycache stop-existing-server run-server run-mock-server drafts-export drafts-import drafts-pack drafts-unpack

check-python:
	$(PY) -c "import sys; sys.exit('Python 3.11+ required (see pyproject.toml requires-python)' if sys.version_info < (3, 11) else 0)"

# Il venv viene creato qui; i passi successivi girano in una invocazione
# ricorsiva di make cosi' che VENV_PYTHON venga rivalutato a venv esistente.
setup-env: check-python
	$(PY) -c "import shutil; shutil.rmtree('venv', ignore_errors=True)"
	$(PY) -m venv venv
	$(MAKE) finish-env

finish-env:
	"$(VENV_PYTHON)" -m pip install --upgrade pip
	$(MAKE) install-torch
	"$(VENV_PYTHON)" -m pip install -e ".[dev]"

install-torch:
	"$(VENV_PYTHON)" -c "exec('''import platform, shutil, subprocess, sys, tomllib\nfrom pathlib import Path\ncfg = tomllib.loads(Path(\"pyproject.toml\").read_text(encoding=\"utf-8\"))[\"tool\"][\"librarain\"][\"torch\"]\ncuda_url = cfg[\"cuda_index_url\"]\ncpu_url = cfg[\"cpu_index_url\"]\npy = sys.executable\nsystem = platform.system()\nmachine = platform.machine().lower()\nhas_nvidia = bool(shutil.which(\"nvidia-smi\")) and subprocess.run([\"nvidia-smi\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0\npip_args = [py, \"-m\", \"pip\", \"install\", \"--upgrade\", \"--force-reinstall\", \"torch\", \"torchvision\"]\nif system == \"Darwin\" and machine in (\"arm64\", \"aarch64\"):\n    subprocess.check_call(pip_args)\nelif has_nvidia:\n    subprocess.check_call(pip_args + [\"--index-url\", cuda_url])\nelse:\n    subprocess.check_call(pip_args + [\"--index-url\", cpu_url])\n''')"

test:
	"$(PYTHON)" -m unittest discover -s tests -p "test_*.py"
	$(MAKE) clean-pycache

lint:
	"$(PYTHON)" -m ruff check src tests scripts

stop-existing-server:
ifeq ($(OS),Windows_NT)
	"$(PYTHON)" -c "import os,subprocess,time;port=$(INGEST_HTTP_PORT);me=str(os.getpid());found={p[-1] for line in subprocess.run(['netstat','-ano'],capture_output=True,text=True,errors='ignore',check=False).stdout.splitlines() if (p:=line.split()) and len(p)>=5 and p[1].endswith(':'+str(port)) and p[3].upper()=='LISTENING'};found.discard('0');found.discard(me);found and (print('Stopping existing ingest server process(es):', ', '.join(sorted(found)), flush=True),[subprocess.run(['taskkill','/PID',pid,'/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False) for pid in found],time.sleep(0.5))"
else
	@echo "Checking for existing ingest servers on port $(INGEST_HTTP_PORT)..."
	@pids="$$( { command -v pgrep >/dev/null 2>&1 && pgrep -f '[s]rc\.api\.ingest_http_server' 2>/dev/null || true; command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:$(INGEST_HTTP_PORT) -sTCP:LISTEN -t 2>/dev/null || true; } | tr ' ' '\n' | awk 'NF && $$1 ~ /^[0-9]+$$/' | sort -u | tr '\n' ' ' )"; \
	if [ -n "$$pids" ]; then \
		echo "Stopping existing ingest server process(es): $$pids"; \
		kill $$pids 2>/dev/null || true; \
		i=0; \
		while [ $$i -lt 30 ]; do \
			left="$$( { command -v pgrep >/dev/null 2>&1 && pgrep -f '[s]rc\.api\.ingest_http_server' 2>/dev/null || true; command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:$(INGEST_HTTP_PORT) -sTCP:LISTEN -t 2>/dev/null || true; } | tr ' ' '\n' | awk 'NF && $$1 ~ /^[0-9]+$$/' | sort -u | tr '\n' ' ' )"; \
			[ -z "$$left" ] && break; \
			sleep 0.1; \
			i=$$((i + 1)); \
		done; \
		left="$$( { command -v pgrep >/dev/null 2>&1 && pgrep -f '[s]rc\.api\.ingest_http_server' 2>/dev/null || true; command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:$(INGEST_HTTP_PORT) -sTCP:LISTEN -t 2>/dev/null || true; } | tr ' ' '\n' | awk 'NF && $$1 ~ /^[0-9]+$$/' | sort -u | tr '\n' ' ' )"; \
		if [ -n "$$left" ]; then \
			echo "Force-killing remaining ingest server process(es): $$left"; \
			kill -9 $$left 2>/dev/null || true; \
			sleep 0.3; \
		fi; \
	fi
endif

run-server: stop-existing-server
	"$(PYTHON)" -m src.api.ingest_http_server

run-mock-server:
	"$(PYTHON)" web/mockup/server.py

# Sync bozze laptop ↔ workstation (metadati JSON + ZIP con PDF)
drafts-export:
	"$(PYTHON)" -m scripts.draft_sync export

drafts-import:
	"$(PYTHON)" -m scripts.draft_sync import

drafts-pack:
	"$(PYTHON)" -m scripts.draft_sync pack

drafts-unpack:
	"$(PYTHON)" -m scripts.draft_sync unpack

clean-pycache:
	"$(PYTHON)" -c "import pathlib, shutil; [shutil.rmtree(path, ignore_errors=True) for path in pathlib.Path('.').rglob('__pycache__')]"
