PYTHON ?= python3
REPORTS_DIR ?= reports

.PHONY: install-dev lint type test test-unit test-hardware test-junit ci

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e .

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
