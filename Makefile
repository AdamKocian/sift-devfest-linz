.PHONY: dev server web test test-server test-web build demo demo-reset clean

# Backend (FastAPI, fixture mode by default) + frontend (Vite) together.
dev:
	@trap 'kill 0' EXIT; \
	(cd server && SIFT_MODE=$${SIFT_MODE:-fixture} uv run uvicorn server.main:app --reload --port 8000) & \
	(cd web && npm run dev) & \
	wait

server:
	cd server && SIFT_MODE=$${SIFT_MODE:-fixture} uv run uvicorn server.main:app --reload --port 8000

web:
	cd web && npm run dev

test: test-server test-web

test-server:
	cd server && uv run pytest -q

test-web:
	cd web && npm run build

build:
	cd web && npm run build

demo:
	cd server && SIFT_MODE=fixture uv run uvicorn server.main:app --port 8000

demo-reset:
	rm -f data/sift.db

clean:
	rm -rf server/.venv web/node_modules web/dist data/sift.db
