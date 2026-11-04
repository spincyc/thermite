PYTHON ?= python3
.PHONY: help check check-identity generate docs
help:
	@$(PYTHON) bin/thermite --help
check:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -t . -q
check-identity:
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=lib DJANGO_SETTINGS_MODULE=oh_identity.test_settings $(PYTHON) -m django test oh_identity --noinput
generate:
	@$(PYTHON) bin/thermite generate
docs:
	@$(PYTHON) bin/thermite docs build --out .scratch/site
