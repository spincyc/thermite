PYTHON ?= python3
.PHONY: help check generate docs
help:
	@$(PYTHON) bin/thermite --help
check:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -t . -q
generate:
	@$(PYTHON) bin/thermite generate
docs:
	@$(PYTHON) bin/thermite docs build --out .scratch/site
