.PHONY: docs-install docs-serve docs-build docs-clean

docs-install:
	venv/bin/pip install -e '.[docs]'

docs-serve:
	venv/bin/mkdocs serve -f .mkdocs/mkdocs.yml

docs-build:
	venv/bin/mkdocs build --strict -f .mkdocs/mkdocs.yml

docs-clean:
	rm -rf site
