import asyncio

from httpx import ASGITransport, AsyncClient

from agent.service import invoke_recommendation
from backend.app import app


async def _check_spend_api_and_summary() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/spend", params={"category": "Packaging"})
        assert response.status_code == 200
        assert len(response.json()) == 2
        summary = await client.get("/api/summary")
        assert summary.json()["total_spend"] == 896750

        governance = await client.get("/api/governance/ping")
        assert governance.json()["status"] == "ok"

        ingest = await client.post("/api/databricks/ingest")
        assert ingest.json()["records_loaded"] == 10

        chat = await client.post("/api/chat", json={"message": "Prioritize unmanaged spend"})
        assert chat.status_code == 200
        assert "unmanaged logistics" in chat.json()["response"]


def test_spend_api_and_summary() -> None:
    asyncio.run(_check_spend_api_and_summary())


def test_recommendation_graph() -> None:
    result = invoke_recommendation("prioritize unmanaged spend")
    assert "unmanaged logistics" in result