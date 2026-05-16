# Interprete usato per creare il venv (override: make PY=python3.11 setup-env).
PY ?= python3

# Preferisce ./venv se presente (dopo setup-env).
PYTHON ?= $(shell test -x ./venv/bin/python && echo ./venv/bin/python || echo $(PY))

.PHONY: check-python setup-env test clean-pycache run-server

check-python:
	$(PY) -c "import sys; sys.exit('Python 3.11+ required (see pyproject.toml requires-python)' if sys.version_info < (3, 11) else 0)"

setup-env: check-python
	rm -rf venv
	$(PY) -m venv venv
	./venv/bin/python -m pip install --upgrade pip
	./venv/bin/pip install -e ".[dev]"

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py"
	$(MAKE) clean-pycache

run-server:
	$(PYTHON) -m src.api.ingest_http_server

clean-pycache:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(path, ignore_errors=True) for path in pathlib.Path('.').rglob('__pycache__')]"
