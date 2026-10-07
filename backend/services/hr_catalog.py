"""Shared form definitions and strict validation for local HR workflows."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from backend.services.common import ServiceError


def field(name, label, type="text", required=True, options=None):
    return {"name": name, "label": label, "type": type, "required": required, "options": options or []}


CATALOG = {
    "requisition": {
        "label": "Hiring requisitions",
        "area": "hiring",
        "fields": [
            field("job_title", "Job title"),
            field("jd_text", "Job description", "textarea"),
            field("headcount", "Number of openings", "number"),
            field("justification", "Business justification", "textarea"),
        ],
    },
    "offer_approval": {
        "label": "Offer approvals",
        "area": "hiring",
        "fields": [field("justification", "Reason for approval", "textarea")],
    },
    "offer_template": {
        "label": "Offer letter templates",
        "area": "hiring",
        "fields": [
            field("content", "Letter text", "textarea"),
        ],
    },
    "onboarding": {
        "label": "New-hire handover",
        "area": "hiring",
        "fields": [
            field("start_date", "Start date", "date"),
            field("checklist", "Checklist (one task per line)", "textarea"),
            field("notes", "Handover notes", "textarea", False),
        ],
    },
}


ATS_KINDS = {key for key, value in CATALOG.items() if value["area"] == "hiring"}
APPROVAL_KINDS = {"requisition", "offer_approval"}


def offer_matches(offer, approval):
    return (
        offer.details == approval.data["offer_snapshot"]
        and offer.title == approval.data.get("offer_title", offer.title)
        and offer.notes == approval.data.get("offer_notes", offer.notes)
    )


def validate_data(kind, data):
    if kind not in CATALOG:
        raise ServiceError("Choose a supported workflow", status=422)
    spec = CATALOG[kind]
    allowed = {f["name"] for f in spec["fields"]}
    if data.keys() - allowed:
        raise ServiceError("Unexpected fields in this workflow", status=422)
    out = {}
    for f in spec["fields"]:
        value = data.get(f["name"], "")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ServiceError(f"{f['label']} must be a single value", status=422)
        value = str(value).strip()
        if f["required"] and not value:
            raise ServiceError(f"{f['label']} is required", status=422)
        if len(value) > (30000 if f["type"] == "textarea" else 300):
            raise ServiceError(f"{f['label']} is too long", status=422)
        if value and f["type"] == "date":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ServiceError(f"{f['label']} must be a valid date", status=422) from exc
        if value and f["type"] == "select" and value not in f["options"]:
            raise ServiceError(f"{f['label']} has an invalid choice", status=422)
        if value and f["type"] == "number":
            try:
                number = Decimal(value)
                if not number.is_finite() or not 0 <= number <= 1_000_000_000:
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise ServiceError(f"{f['label']} must be a finite non-negative number", status=422) from exc
            if f["name"] == "headcount" and (number < 1 or number != int(number)):
                raise ServiceError("Openings must be a positive whole number", status=422)
        out[f["name"]] = value
    if kind == "offer_template":
        placeholders = re.findall(r"\{\{(.*?)\}\}", out["content"])
        if set(placeholders) - {"candidate", "job", "amount", "currency", "pay_period", "start_date", "company"}:
            raise ServiceError("Use only the supported letter placeholders", status=422)
    return out
