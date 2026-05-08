.PHONY: check-python312 setup-env test clean-pycache

check-python312:
	python3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 'Python 3.12 is required')"

setup-env: check-python312
	rm -rf venv
	python3.12 -m venv venv
	./venv/bin/python -m pip install --upgrade pip
	./venv/bin/pip install -r requirements.txt

test:
	python3 -m unittest discover -s tests -p "test_*.py"
	$(MAKE) clean-pycache

clean-pycache:
	python3 -c "import pathlib, shutil; [shutil.rmtree(path, ignore_errors=True) for path in pathlib.Path('.').rglob('__pycache__')]"
