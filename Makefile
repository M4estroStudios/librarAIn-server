# Interprete usato per creare il venv (override: make PY=python3.11 setup-env).
ifeq ($(OS),Windows_NT)
PY ?= python3.12
else
PY ?= python3
endif
VENV_PYTHON := $(firstword $(wildcard venv/Scripts/python.exe venv/bin/python.exe venv/bin/python))

# Preferisce il Python del venv se presente (dopo setup-env).
PYTHON ?= $(if $(VENV_PYTHON),$(VENV_PYTHON),$(PY))

.PHONY: check-python setup-env install-torch test clean-pycache run-server

check-python:
	$(PY) -c "import sys; sys.exit('Python 3.11+ required (see pyproject.toml requires-python)' if sys.version_info < (3, 11) else 0)"

setup-env: check-python
	$(PY) -c "import shutil; shutil.rmtree('venv', ignore_errors=True)"
	$(PY) -m venv venv
	"$(VENV_PYTHON)" -m pip install --upgrade pip
	$(MAKE) install-torch
	"$(VENV_PYTHON)" -m pip install -e ".[dev]"

install-torch:
	"$(VENV_PYTHON)" -c "exec('''import platform, shutil, subprocess, sys, tomllib\nfrom pathlib import Path\ncfg = tomllib.loads(Path(\"pyproject.toml\").read_text(encoding=\"utf-8\"))[\"tool\"][\"librarain\"][\"torch\"]\ncuda_url = cfg[\"cuda_index_url\"]\ncpu_url = cfg[\"cpu_index_url\"]\npy = sys.executable\nsystem = platform.system()\nmachine = platform.machine().lower()\nhas_nvidia = bool(shutil.which(\"nvidia-smi\")) and subprocess.run([\"nvidia-smi\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0\npip_args = [py, \"-m\", \"pip\", \"install\", \"--upgrade\", \"--force-reinstall\", \"torch\", \"torchvision\"]\nif system == \"Darwin\" and machine in (\"arm64\", \"aarch64\"):\n    subprocess.check_call(pip_args)\nelif has_nvidia:\n    subprocess.check_call(pip_args + [\"--index-url\", cuda_url])\nelse:\n    subprocess.check_call(pip_args + [\"--index-url\", cpu_url])\n''')"

test:
	"$(PYTHON)" -m unittest discover -s tests -p "test_*.py"
	$(MAKE) clean-pycache

run-server:
	"$(PYTHON)" -m src.api.ingest_http_server

clean-pycache:
	"$(PYTHON)" -c "import pathlib, shutil; [shutil.rmtree(path, ignore_errors=True) for path in pathlib.Path('.').rglob('__pycache__')]"
