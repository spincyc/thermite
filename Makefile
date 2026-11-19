PYTHON ?= python3
CONFIG ?= $(THERMITE_SITE)
# Test worker processes: 0 is one per CPU; 1 runs the suite serially.
JOBS ?= 0
.PHONY: help check check-identity check-browser check-static generate docs upgrade
help:
	@$(PYTHON) bin/thermite --help
	@echo 'Source upgrade: make upgrade CONFIG=/path/to/private-site'
check:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m tests --jobs $(JOBS)
check-identity:
	@PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=lib DJANGO_SETTINGS_MODULE=oh_identity.test_settings $(PYTHON) -m django test oh_identity --noinput
# The example house's pages in the system's Chromium, at a phone's and a
# desktop's size: about half a minute, so not part of check. Skipped, with
# the reason, where there is no Chromium.
check-browser:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest tests.browser_smoke
# The linter and the type checker over the Python: pyproject.toml has the
# rules and the modules that are checked, requirements-static.txt the tools.
# A few seconds, and not part of check, which needs neither tool. Skipped,
# with the reason, where they are not installed, unless
# THERMITE_STATIC_REQUIRED=1.
RUFF ?= ruff
MYPY ?= mypy
check-static:
	@if command -v $(RUFF) >/dev/null 2>&1 && command -v $(MYPY) >/dev/null 2>&1; then \
		$(RUFF) check . && $(MYPY); \
	elif [ "$(THERMITE_STATIC_REQUIRED)" = 1 ]; then \
		echo "check-static: $(RUFF) and $(MYPY) are required and not both installed (requirements-static.txt)" >&2; exit 1; \
	else \
		echo "check-static: skipped: $(RUFF) and $(MYPY) are not both installed (python -m pip install --require-hashes -r requirements-static.txt)"; \
	fi
generate:
	@$(PYTHON) bin/thermite generate
docs:
	@$(PYTHON) bin/thermite docs build --out .scratch/site
upgrade:
	@$(PYTHON) lib/oh_source_upgrade.py --config "$(CONFIG)"
