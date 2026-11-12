PYTHON ?= python3
CONFIG ?= $(THERMITE_SITE)
# Test worker processes: 0 is one per CPU; 1 runs the suite serially.
JOBS ?= 0
.PHONY: help check check-identity generate docs upgrade
help:
	@$(PYTHON) bin/thermite --help
	@echo 'Source upgrade: make upgrade CONFIG=/path/to/private-site'
check:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m tests --jobs $(JOBS)
check-identity:
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=lib DJANGO_SETTINGS_MODULE=oh_identity.test_settings $(PYTHON) -m django test oh_identity --noinput
generate:
	@$(PYTHON) bin/thermite generate
docs:
	@$(PYTHON) bin/thermite docs build --out .scratch/site
upgrade:
	@$(PYTHON) lib/oh_source_upgrade.py --config "$(CONFIG)"
