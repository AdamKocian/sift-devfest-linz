.PHONY: dev server web test test-server test-web lint build demo demo-reset clean

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

test: test-server test-web

test-server:
	cd server && uv run ruff check server tests && uv run pytest -q

test-web:
	cd web && npm run lint && npm run build

lint:
	cd server && uv run ruff check server tests
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
