.PHONY: docs-install docs-serve docs-build docs-clean

docs-install:
	venv/bin/pip install -e '.[docs]'

docs-serve:
	venv/bin/mkdocs serve

docs-build:
	venv/bin/mkdocs build --strict

docs-clean:
	rm -rf site
