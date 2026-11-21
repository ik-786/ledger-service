.PHONY: install lint test model load run docker

install:
	pip install -e ".[dev]"

lint:
	ruff format --check .
	ruff check .
	mypy

test:
	pytest

model:  ## 500 random sequences instead of the default 30
	LEDGER_MODEL_SEEDS=500 pytest tests/test_model.py

load:
	python scripts/loadtest.py --clients 32 --seconds 20

run:
	python -m ledger serve --db ledger.db

docker:
	docker build -t ledger-service .
	docker run --rm -p 8000:8000 -v ledger-data:/data ledger-service
