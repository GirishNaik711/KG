from pydantic import BaseModel, Field


class SpendRecord(BaseModel):
    invoice_id: str
    supplier: str
    category: str
    location: str
    division: str
    amount: float
    currency: str
    source: str
    managed: bool
    contract_id: str | None = None
    po_id: str | None = None
    invoice_date: str
    quality_flags: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str


class GovernanceEvent(BaseModel):
    decision: str
    actor: str = "synthetic-governance-portal"
    records: list[str] = Field(default_factory=list)