.PHONY: help install dev test buckets smoke deploy logs down

help:
	@echo "Usage targets:"
	@echo "  make install   - install Python deps into the active venv"
	@echo "  make dev       - run the app locally in OFFLINE_MODE (no creds, port 8000)"
	@echo "  make test      - run the offline end-to-end test suite"
	@echo "  make buckets   - create the 4 private Supabase Storage buckets (needs .env)"
	@echo "  make deploy    - docker compose up -d --build (on the VPS)"
	@echo "  make smoke URL=https://usage.90ten.life - post-deploy health check"
	@echo "  make logs      - tail the container logs"
	@echo "  make down      - stop the stack"

install:
	pip install -r requirements.txt

dev:
	OFFLINE_MODE=true uvicorn app.main:app --reload --port 8000

test:
	OFFLINE_MODE=true python -m pytest tests/ -q

buckets:
	python scripts/bootstrap_supabase.py

deploy:
	docker compose up -d --build

logs:
	docker compose logs -f labels-api

down:
	docker compose down

smoke:
	./scripts/smoke.sh $(URL)
