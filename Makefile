PYTHON ?= python3
.PHONY: help check generate docs
help:
	@$(PYTHON) bin/thermite --help
check:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest tests.test_public -q
generate:
	@$(PYTHON) bin/thermite generate
docs:
	@$(PYTHON) bin/thermite docs build --out .scratch/site
