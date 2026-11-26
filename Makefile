.PHONY: dev server web test test-server test-noisegen test-web lint build demo demo-reset clean \
	fixtures fixtures-synth discover discover-live up up-debug down noisegen noisegen-setup

export SIFT_MODE ?= fixture

# API (FastAPI, :8000) + web app (Vite, :5173). Open http://localhost:5173.
dev:
	@trap 'kill 0' EXIT; \
	(cd server && uv run uvicorn server.main:app --reload --port 8000) & \
	(cd web && npm run dev) & \
	wait

server:
	cd server && uv run uvicorn server.main:app --reload --port 8000

web:
	cd web && npm run dev

test: test-server test-noisegen test-web

test-server:
	cd server && uv run ruff check server tests && uv run pytest -q

test-noisegen:
	cd noisegen && uv run ruff check noisegen tests && uv run pytest -q

test-web:
	cd web && npm run lint && npm run build

lint:
	cd server && uv run ruff check server tests
	cd noisegen && uv run ruff check noisegen tests
	cd web && npm run lint

build:
	cd web && npm run build

# One process serves the built web app and the API on http://localhost:8000.
demo: build
	cd server && SIFT_MODE=fixture uv run uvicorn server.main:app --port 8000

# Rebuild the demo database (also available in the app: Settings > Demo).
demo-reset:
	cd server && uv run python -m server.cli reset

clean:
	rm -rf server/.venv web/node_modules web/dist data/sift.db

# --- Dynatrace ---------------------------------------------------------------------------
# Live commands read DT_PLATFORM_URL / DT_PLATFORM_TOKEN (and DT_ENV_URL / DT_API_TOKEN for
# ingest) from the environment, e.g. `set -a; . ./.env; set +a` first.

# Record live responses into fixtures/dynatrace (redacted). Needs a tenant. Review the diff.
fixtures:
	cd server && uv run python -m server.cli record

# Rebuild fixtures/dynatrace from fixtures/demo_world.json (synthetic recordings).
fixtures-synth:
	cd server && uv run python -m server.synth

# Run discovery and print the top 10 signals by cost (fixtures / live tenant).
discover:
	cd server && uv run python -m server.cli discover

discover-live:
	cd server && uv run python -m server.cli discover --live --sampling 1

# noisegen -> OTel Collector -> Dynatrace (Docker). up-debug needs no tenant.
up:
	docker compose -f infra/docker-compose.yml --env-file .env --profile dynatrace up --build

up-debug:
	docker compose -f infra/docker-compose.yml --profile debug up --build

down:
	docker compose -f infra/docker-compose.yml --profile dynatrace --profile debug down

# noisegen without Docker, to a collector on localhost:4318.
noisegen:
	cd noisegen && uv run noisegen run --seconds 900

# Create the demo dashboards/notebooks on the tenant and run their queries once.
noisegen-setup:
	cd noisegen && uv run noisegen setup --fixtures ../fixtures
