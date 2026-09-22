ROOT := $(abspath ../../..)
PYTHON := $(ROOT)/.venv/bin/python
ENV := DEBUG=true LNBITS_BACKEND_WALLET_CLASS=FakeWallet LNBITS_DATABASE_URL= LNBITS_DATA_FOLDER=$(CURDIR)/dev/test-data PYTHONPATH=$(ROOT)
PYFILES := $(wildcard *.py) $(wildcard tests/*.py) engine/package.py engine/update-maps.py engine/logo_maps.py
JSFILES := static/admin.js static/public.js static/client.js static/lobby.js static/arena/loader.js static/arena/transport.js

.PHONY: check test lint smoke sandbox-check package
check: lint test

lint:
	$(ROOT)/.venv/bin/ruff check $(PYFILES)
	$(ROOT)/.venv/bin/black --check $(PYFILES)
	@for file in $(JSFILES); do node --check "$$file" || exit 1; done

test:
	$(ENV) $(PYTHON) -m pytest tests -o addopts='' -q
	node --test tests/test_assets.mjs tests/test_public_ui.mjs

smoke:
	$(ENV) $(PYTHON) tests/smoke.py

sandbox-check:
	mkdir -p dev/test-data
	nix-shell engine/native-shell.nix --run 'x86_64-unknown-linux-musl-gcc -O2 -static tests/sandbox_probe.c -o dev/sandbox-probe && QUAKEJS_ASSETS="$(CURDIR)/static/arena" QUAKEJS_HOME="$(CURDIR)/dev/test-data" ./dev/sandbox-probe'

package:
	$(PYTHON) engine/package.py
