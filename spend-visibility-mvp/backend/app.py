from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from agent.service import invoke_recommendation
from backend.data import load_spend_records
from backend.models import ChatRequest, GovernanceEvent, SpendRecord
from config.env_check import check_env
from observability.telemetry import configure_dynatrace

SAMPLE_SPEND = load_spend_records()

app = FastAPI(title="Spend Visibility API", version="0.1.0")
configure_dynatrace(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def validate_configuration() -> None:
    check_env()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "backend"}


@app.get("/api/spend", response_model=list[SpendRecord])
def spend(
    category: str | None = Query(default=None),
    supplier: str | None = Query(default=None),
    location: str | None = Query(default=None),
) -> list[SpendRecord]:
    records = SAMPLE_SPEND
    if category:
        records = [record for record in records if record.category == category]
    if supplier:
        records = [record for record in records if record.supplier == supplier]
    if location:
        records = [record for record in records if record.location == location]
    return records


@app.get("/api/invoices", response_model=list[SpendRecord])
def invoices() -> list[SpendRecord]:
    return SAMPLE_SPEND


@app.get("/api/suppliers")
def suppliers() -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    for record in SAMPLE_SPEND:
        item = grouped.setdefault(record.supplier, {"supplier": record.supplier, "spend": 0.0, "invoice_count": 0})
        item["spend"] = float(item["spend"]) + record.amount
        item["invoice_count"] = int(item["invoice_count"]) + 1
    return list(grouped.values())


@app.get("/api/opportunities")
def opportunities() -> list[dict[str, object]]:
    return [
        {"type": "unmanaged_spend", "supplier": record.supplier, "amount": record.amount, "reason": record.quality_flags or ["outside_active_contract"]}
        for record in SAMPLE_SPEND
        if not record.managed
    ]


@app.get("/api/chart-data")
def chart_data() -> dict[str, list[dict[str, object]]]:
    by_month: dict[str, float] = {}
    for record in SAMPLE_SPEND:
        month = record.invoice_date[:7]
        by_month[month] = by_month.get(month, 0) + record.amount
    return {"historical": [{"period": period, "amount": amount} for period, amount in sorted(by_month.items())], "forecast": []}


@app.post("/api/databricks/ingest")
def databricks_ingest() -> dict[str, object]:
    """Databricks MCP-shaped ingestion seam; local mode reloads the CSV source."""
    global SAMPLE_SPEND
    SAMPLE_SPEND = load_spend_records()
    return {"status": "accepted", "source": "synthetic_spend.csv", "records_loaded": len(SAMPLE_SPEND)}


@app.post("/api/governance/events")
def governance_event(event: GovernanceEvent) -> dict[str, object]:
    return {"status": "accepted", "event_id": f"gov-{len(event.records)}-{event.actor}", "decision": event.decision}


@app.get("/api/governance/ping")
def governance_ping() -> dict[str, str]:
    return {"status": "ok", "platform": "dummy-ai-governance-portal", "mode": "local-synthetic"}


@app.get("/api/summary")
def summary() -> dict[str, object]:
    return {
        "total_spend": sum(record.amount for record in SAMPLE_SPEND),
        "supplier_count": len({record.supplier for record in SAMPLE_SPEND}),
        "managed_spend": sum(record.amount for record in SAMPLE_SPEND if record.managed),
        "unmanaged_spend": sum(record.amount for record in SAMPLE_SPEND if not record.managed),
        "refreshed_at": datetime.now(UTC).isoformat(),
    }


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict[str, str]:
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    return {"response": invoke_recommendation(request.message)}