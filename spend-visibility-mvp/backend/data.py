from __future__ import annotations

import csv
from pathlib import Path

from backend.models import SpendRecord

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic_spend.csv"


def load_spend_records(path: Path = DATA_PATH) -> list[SpendRecord]:
    with path.open(newline="", encoding="utf-8") as csv_file:
        return [
            SpendRecord(
                invoice_id=row["invoice_id"],
                supplier=row["supplier"],
                category=row["category"],
                location=row["location"],
                division=row["division"],
                amount=float(row["amount"]),
                currency=row["currency"],
                source=row["source"],
                managed=row["managed"].lower() == "true",
                contract_id=row["contract_id"].strip() or None,
                po_id=row["po_id"].strip() or None,
                invoice_date=row["invoice_date"],
                quality_flags=[flag for flag in row["quality_flags"].split("|") if flag],
            )
            for row in csv.DictReader(csv_file)
        ]