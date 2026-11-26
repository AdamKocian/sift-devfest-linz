from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="Sift API",
    description="Agent-first lightweight telemetry optimizer for Dynatrace",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str
    version: str
    mode: str


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(status="ok", version="0.1.0", mode="fixture")


@app.get("/api/overview")
def get_overview():
    return {
        "status": "idle",
        "last_run": "12 min ago",
        "kpis": {
            "ingest_7d_gib": 412,
            "ingest_trend": "+8 % vs previous week",
            "est_cost_month_eur": 3940,
            "waste_found_pct": 38,
            "waste_gib": 157,
            "proposals_count": 24,
            "saved_so_far_eur": 1120,
            "verified_rules_count": 9,
        },
        "signals_count": 1284,
        "tokens_today": 182000,
    }
