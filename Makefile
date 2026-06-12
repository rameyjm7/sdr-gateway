PYTHON ?= python3
CC ?= cc
REPORTS_DIR ?= reports
NATIVE_DIR ?= app/sdr/native
NATIVE_BIN_DIR ?= $(NATIVE_DIR)/bin

.PHONY: install-dev lint type test test-unit test-hardware test-junit ci native clean-native

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e .

native: $(NATIVE_BIN_DIR)/hackrf_iq_sweep

$(NATIVE_BIN_DIR)/hackrf_iq_sweep: $(NATIVE_DIR)/hackrf_iq_sweep.c
	mkdir -p $(NATIVE_BIN_DIR)
	$(CC) -O2 -Wall -Wextra -o $@ $< $$(pkg-config --cflags --libs libhackrf)

clean-native:
	rm -rf $(NATIVE_BIN_DIR)

lint:
	$(PYTHON) -m ruff check app tests

type:
	$(PYTHON) -m mypy app tests

test:
	$(PYTHON) -m pytest -q -m "not hardware"

test-unit:
	$(PYTHON) -m pytest -q -m unit

test-hardware:
	$(PYTHON) -m pytest -q -m hardware -s -rs

test-junit:
	mkdir -p $(REPORTS_DIR)
	$(PYTHON) -m pytest -q -m "not hardware" \
		--junitxml=$(REPORTS_DIR)/junit-unit.xml \
		--cov=app \
		--cov-report=xml:$(REPORTS_DIR)/coverage.xml \
		--cov-fail-under=75

ci: lint type test-junit
