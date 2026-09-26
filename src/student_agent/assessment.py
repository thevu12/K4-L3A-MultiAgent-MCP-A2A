"""Conservative adapters for the observed MCP evidence layouts.

The public contract defines the evidence envelope but not ``data``. These
adapters accept the authoritative Olist-shaped payloads returned by the
competition gateway and discard records outside the order's own timeline.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
CAPTURED = {"captured", "paid", "settled"}
ISSUE_PRIORITY = (
    "refund_failed",
    "refund_pending",
    "duplicate_charge",
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "payment_mismatch",
    "valid_split_payment",
    "unsupported_claim",
)
CASE_STATUSES = {"action_required", "no_action", "needs_investigation"}
PARTY_TYPES = {
    "seller",
    "platform",
    "logistics_provider",
    "payment_provider",
    "customer",
    "unknown",
}


@dataclass(frozen=True)
class Evidence:
    tool: str
    domain: str
    data: Any
    ref: str


@dataclass(frozen=True)
class Finding:
    issue: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class PaymentFacts:
    captured_count: int
    captured_total: Decimal
    duplicate: bool
    mismatch: bool
    references: tuple[str, ...]


@dataclass(frozen=True)
class RefundFacts:
    completed_total: Decimal
    latest_status: str | None


@dataclass(frozen=True)
class PolicyResolution:
    case_status: str
    refund: Decimal
    action: str
    parties: tuple[dict[str, str | None], ...]


def _object(data: Any, key: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    value = data.get(key, data)
    return value if isinstance(value, dict) else {}


def _rows(data: Any, key: str) -> list[dict[str, Any]] | None:
    if isinstance(data, list):
        value = data
    elif isinstance(data, dict):
        value = data.get(key)
    else:
        return None
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        return None
    return value


def _first(row: dict[str, Any], *keys: str) -> Any:
    return next((row[key] for key in keys if key in row), None)


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("1e12"):
            return None
        return amount.quantize(CENT)
    except InvalidOperation:
        return None


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and 1 <= len(value) <= 128 else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _order_times(order: dict[str, Any]) -> tuple[datetime | None, ...]:
    return tuple(
        _timestamp(order.get(key))
        for key in (
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        )
    )


def _in_window(
    value: datetime | None,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    if value is None:
        return start is None and end is None
    if start is not None and value < start:
        return False
    return end is None or value <= end


def _scoped_items(
    data: Any,
    order_id: str,
    order: dict[str, Any],
) -> list[dict[str, Any]] | None:
    rows = _rows(data, "items")
    if rows is None:
        return None
    purchase, _, _, estimated = _order_times(order)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row.get("order_id") not in {None, order_id}:
            return None
        item_id = _id(_first(row, "order_item_id", "item_id"))
        if item_id is None:
            return None
        limit = _timestamp(_first(row, "shipping_limit_date", "shipping_limit_at"))
        if (purchase is not None or estimated is not None) and not _in_window(
            limit, purchase, estimated
        ):
            continue
        if item_id not in seen:
            selected.append(row)
            seen.add(item_id)
    return selected


def _item_total(rows: list[dict[str, Any]] | None, order: dict[str, Any]) -> Decimal | None:
    direct = _money(_first(order, "total_brl", "total", "order_total_brl"))
    if direct is not None:
        return direct
    if not rows:
        return None
    total = ZERO
    for row in rows:
        price = _money(_first(row, "price", "price_brl"))
        freight = _money(_first(row, "freight_value", "freight_brl"))
        if price is None or freight is None:
            return None
        total += price + freight
    return total


def _payment_facts(
    payment_data: Any,
    timeline_data: Any,
    order_id: str,
    order: dict[str, Any],
) -> PaymentFacts | None:
    payment_rows = _rows(payment_data, "payments")
    timeline_events = _rows(timeline_data, "events")
    if payment_rows is None or timeline_events is None:
        return None

    purchase, approved, _, _ = _order_times(order)
    start = purchase - timedelta(hours=1) if purchase else None
    end_base = approved or purchase
    end = end_base + timedelta(days=1) if end_base else None
    captured_amounts: list[Decimal] = []
    seen_capture_events: set[tuple[Any, ...]] = set()
    mismatch = False
    for event in timeline_events:
        if event.get("order_id") not in {None, order_id}:
            return None
        occurred_at = _timestamp(_first(event, "event_at", "occurred_at"))
        if not _in_window(occurred_at, start, end):
            continue
        event_type = _first(event, "event_type", "type")
        status = event.get("status")
        if event_type in CAPTURED and status in {None, "confirmed", "captured", "settled"}:
            amount = _money(_first(event, "amount_brl", "amount", "payment_value"))
            if amount is None:
                return None
            signature = (event_type, occurred_at, status, amount)
            if signature in seen_capture_events:
                continue
            seen_capture_events.add(signature)
            captured_amounts.append(amount)
        elif event_type == "reconciliation_mismatch" and status in {None, "open", "confirmed"}:
            mismatch = True

    explicit_duplicate = False
    references: list[str] = []
    if not captured_amounts:
        for row in payment_rows:
            status = _first(row, "status", "payment_status")
            if status not in CAPTURED:
                continue
            amount = _money(_first(row, "amount_brl", "amount", "payment_value"))
            if amount is None:
                return None
            captured_amounts.append(amount)
            explicit_duplicate = explicit_duplicate or row.get("is_duplicate") is True
            if reference := _id(_first(row, "payment_reference", "payment_id")):
                references.append(reference)

    signatures: list[tuple[str, str, Decimal]] = []
    for row in payment_rows:
        if row.get("order_id") not in {None, order_id}:
            return None
        amount = _money(_first(row, "payment_value", "amount_brl", "amount"))
        sequential = _first(row, "payment_sequential", "sequence")
        payment_type = _first(row, "payment_type", "type")
        if amount is not None and sequential is not None and isinstance(payment_type, str):
            signatures.append((str(sequential), payment_type, amount))
        explicit_duplicate = explicit_duplicate or row.get("is_duplicate") is True
        if reference := _id(_first(row, "payment_reference", "payment_id")):
            references.append(reference)
    repeated_signature = any(count > 1 for count in Counter(signatures).values())
    duplicate = explicit_duplicate or (
        repeated_signature
        and len(captured_amounts) >= 2
        and len(set(captured_amounts)) == 1
    )
    return PaymentFacts(
        len(captured_amounts),
        sum(captured_amounts, ZERO),
        duplicate,
        mismatch,
        tuple(dict.fromkeys(references))[:20],
    )


def _refund_facts(
    data: Any,
    order_id: str,
    order: dict[str, Any],
) -> RefundFacts | None:
    rows = _rows(data, "events")
    if rows is None:
        return None
    purchase, approved, delivered, estimated = _order_times(order)
    end_candidates = [value for value in (approved, delivered, estimated, purchase) if value]
    end = max(end_candidates) + timedelta(days=14) if end_candidates else None
    latest: tuple[datetime, str, Decimal] | None = None
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        if row.get("order_id") not in {None, order_id}:
            return None
        occurred_at = _timestamp(_first(row, "event_at", "occurred_at", "updated_at"))
        if not _in_window(occurred_at, purchase, end):
            continue
        status = _first(row, "status", "refund_status")
        amount = _money(_first(row, "amount_brl", "amount"))
        if status not in {"pending", "failed", "completed"} or amount is None:
            return None
        signature = (occurred_at, status, amount)
        if signature in seen:
            continue
        seen.add(signature)
        if occurred_at is None:
            return None
        candidate = (occurred_at, status, amount)
        if latest is None or occurred_at > latest[0]:
            latest = candidate
    if latest is None:
        return RefundFacts(ZERO, None)
    _, status, amount = latest
    return RefundFacts(amount if status == "completed" else ZERO, status)


def _seller_ids(data: Any, order_id: str) -> set[str] | None:
    rows = _rows(data, "sellers")
    if rows is None:
        return None
    seller_ids: set[str] = set()
    for row in rows:
        if row.get("order_id") not in {None, order_id}:
            return None
        seller_id = _id(row.get("seller_id"))
        if seller_id is None:
            return None
        seller_ids.add(seller_id)
    return seller_ids


def _shipment_issue(data: Any, order_id: str, order: dict[str, Any]) -> str | None:
    shipment = _object(data, "shipment")
    events = _rows(shipment, "events")
    if not shipment:
        return None
    if events is None:
        status = shipment.get("status")
        owner = shipment.get("delay_source")
        if status in {"late", "delayed"} and owner in {"seller", "logistics"}:
            return f"late_delivery_{owner}"
        return None
    _, _, delivered, _ = _order_times(order)
    start = delivered - timedelta(days=1) if delivered else None
    end = delivered + timedelta(days=1) if delivered else None
    for event in events:
        if event.get("order_id") not in {None, order_id}:
            return None
        occurred_at = _timestamp(_first(event, "event_at", "occurred_at"))
        if delivered is None or not _in_window(occurred_at, start, end):
            continue
        if event.get("event_type") != "delivered_late" or event.get("status") != "confirmed":
            continue
        actor = event.get("actor")
        if actor == "seller":
            return "late_delivery_seller"
        if actor in {"logistics", "logistics_provider"}:
            return "late_delivery_logistics"
    return None


def _policy_resolution(
    policy: dict[str, Any],
    expected_version: Any,
    issue: str,
) -> PolicyResolution | None:
    if not isinstance(expected_version, str) or policy.get("policy_version") != expected_version:
        return None
    if policy.get("currency", "BRL") != "BRL":
        return None
    rules = policy.get("rules")
    rule = rules.get(issue) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        return None
    case_status = rule.get("case_status")
    refund = _money(rule.get("refund_brl"))
    action = rule.get("recommended_action")
    raw_parties = rule.get("responsible_parties")
    if (
        case_status not in CASE_STATUSES
        or refund is None
        or not isinstance(action, str)
        or not 1 <= len(action) <= 80
        or not isinstance(raw_parties, list)
        or len(raw_parties) > 5
    ):
        return None
    parties: list[dict[str, str | None]] = []
    for party in raw_parties:
        if not isinstance(party, dict) or party.get("party_type") not in PARTY_TYPES:
            return None
        party_id = party.get("party_id")
        if party_id is not None and _id(party_id) is None:
            return None
        parties.append({"party_type": party["party_type"], "party_id": party_id})
    return PolicyResolution(case_status, refund, action, tuple(parties))


def _conflict(field: str, sources: list[str]) -> dict[str, Any]:
    return {
        "field": field,
        "sources": sources,
        "selected_source": None,
        "resolution_code": "UNRESOLVED_AUTHORITATIVE_CONFLICT",
    }


def assess_case(case: dict[str, Any], evidence: dict[str, Evidence]) -> dict[str, Any]:
    request = case.get("customer_request", {})
    claimed_order_id = request.get("claimed_order_id")
    claims = request.get("claims", [])
    used: set[str] = set()
    conflicts: list[dict[str, Any]] = []

    def data(tool: str) -> Any:
        result = evidence.get(tool)
        return result.data if result else None

    def refs(tools: CollectionOfTools) -> list[str]:
        used.update(tool for tool in tools if tool in evidence)
        return list(dict.fromkeys(evidence[tool].ref for tool in tools if tool in evidence))

    order = _object(data("get_order"), "order")
    order_id = _id(order.get("order_id"))
    if order_id is not None:
        refs(("get_order",))
    if order_id is None or order_id != claimed_order_id:
        return _empty_output(case, refs(("get_order",)) if order_id else [])

    rejected: set[str] = set()
    for tool, result in evidence.items():
        containers: list[dict[str, Any]] = []
        if isinstance(result.data, dict):
            containers.append(result.data)
            for key in (
                "order",
                "shipment",
                "payments",
                "items",
                "sellers",
                "events",
                "shipping_limits",
            ):
                value = result.data.get(key)
                if isinstance(value, dict):
                    containers.append(value)
                elif isinstance(value, list):
                    containers.extend(row for row in value if isinstance(row, dict))
        elif isinstance(result.data, list):
            containers.extend(row for row in result.data if isinstance(row, dict))
        if tool != "get_order" and any(
            row.get("order_id") is not None and row["order_id"] != order_id
            for row in containers
        ):
            rejected.add(tool)
            conflicts.append(_conflict("order_id", ["get_order", tool]))
            refs(("get_order", tool))
    usable = {tool: result for tool, result in evidence.items() if tool not in rejected}

    def safe_data(tool: str) -> Any:
        result = usable.get(tool)
        return result.data if result else None

    items = _scoped_items(safe_data("get_order_items"), order_id, order)
    total = _item_total(items, order)
    payments = _payment_facts(
        safe_data("get_order_payments"),
        safe_data("get_payment_timeline"),
        order_id,
        order,
    )
    refunds = _refund_facts(safe_data("get_refund_timeline"), order_id, order)
    shipment_issue = _shipment_issue(safe_data("get_shipment_summary"), order_id, order)
    seller_ids = _seller_ids(safe_data("get_sellers"), order_id)
    policy = _object(safe_data("get_policy"), "policy")

    if payments and refunds and refunds.completed_total > payments.captured_total:
        conflicts.append(
            _conflict(
                "refunded_total_brl",
                ["get_payment_timeline", "get_refund_timeline"],
            )
        )
        refs(("get_payment_timeline", "get_refund_timeline"))

    findings: dict[str, Finding] = {}

    def finding(issue: str, *tools: str) -> None:
        findings[issue] = Finding(issue, tuple(dict.fromkeys(("get_order", *tools))))

    if refunds and refunds.latest_status in {"pending", "failed"}:
        finding(f"refund_{refunds.latest_status}", "get_refund_timeline")
    if payments and payments.duplicate:
        finding("duplicate_charge", "get_order_payments", "get_payment_timeline")
    if payments and payments.captured_count:
        status = order.get("status", order.get("order_status"))
        completed_refund = refunds.completed_total if refunds else ZERO
        if status in {"canceled", "unavailable"} and (
            payments.captured_total - completed_refund > CENT
        ):
            finding(
                f"{status}_order_paid",
                "get_order_payments",
                "get_payment_timeline",
            )
        elif status in {"canceled", "unavailable"} and refunds:
            refs(("get_payment_timeline", "get_refund_timeline"))
    if shipment_issue:
        finding(shipment_issue, "get_shipment_summary")
    if payments and total is not None:
        totals_differ = abs(payments.captured_total - total) > CENT
        if payments.mismatch or totals_differ:
            finding(
                "payment_mismatch",
                "get_order_items",
                "get_order_payments",
                "get_payment_timeline",
            )
        elif payments.captured_count >= 2:
            finding(
                "valid_split_payment",
                "get_order_items",
                "get_order_payments",
                "get_payment_timeline",
            )

    unsupported_resolution = _policy_resolution(
        policy, case.get("policy_version"), "unsupported_claim"
    )
    core_complete = (
        items is not None
        and total is not None
        and payments is not None
        and payments.captured_count > 0
        and bool(_object(safe_data("get_shipment_summary"), "shipment"))
    )
    if not findings and core_complete and unsupported_resolution:
        finding(
            "unsupported_claim",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_shipment_summary",
            "get_policy",
        )

    issue = next((name for name in ISSUE_PRIORITY if name in findings), "insufficient_evidence")
    if conflicts:
        issue = "insufficient_evidence"
    selected = findings.get(issue)
    issue_refs = refs(selected.tools) if selected else []
    resolution = (
        _policy_resolution(policy, case.get("policy_version"), issue) if selected else None
    )
    if resolution:
        refs(("get_policy",))

    refund = resolution.refund if resolution else ZERO
    case_status = resolution.case_status if resolution else "needs_investigation"
    confidence = 0.84 if selected and resolution and not conflicts else 0.2
    parties = list(resolution.parties) if resolution else []
    action = resolution.action if resolution else (
        "review_evidence_chain" if selected else "collect_missing_evidence"
    )

    assessments = []
    for claim in claims:
        topic = claim.get("topic")
        verdict, claim_refs = "insufficient_evidence", []
        if not conflicts and selected:
            if topic == "requested_full_refund" and resolution and payments:
                claim_refs = refs((*selected.tools, "get_policy", "get_payment_timeline"))
                if resolution.case_status != "needs_investigation":
                    if refund <= ZERO:
                        verdict = "unsupported"
                    elif refund + CENT >= payments.captured_total:
                        verdict = "supported"
                    else:
                        verdict = "partially_supported"
            elif topic == issue:
                verdict = "unsupported" if issue == "unsupported_claim" else "supported"
                claim_refs = issue_refs
            elif topic == "duplicate_charge" and issue == "valid_split_payment":
                verdict, claim_refs = "unsupported", issue_refs
        assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": 0.84 if verdict != "insufficient_evidence" else 0.2,
                "evidence_refs": claim_refs,
            }
        )

    entities = {
        "order_ids": [order_id],
        "item_ids": [],
        "seller_ids": [],
        "payment_references": list(payments.references) if payments else [],
        "shipment_ids": [],
    }
    if issue in {"late_delivery_seller", "late_delivery_logistics"} and items:
        entities["item_ids"] = list(
            dict.fromkeys(
                value
                for row in items
                if (value := _id(_first(row, "order_item_id", "item_id")))
            )
        )[:20]
        refs(("get_order_items",))
    seller_party_ids = {
        party["party_id"]
        for party in parties
        if party["party_type"] == "seller" and party["party_id"] is not None
    }
    verified_seller_ids = sorted(seller_party_ids & seller_ids) if seller_ids else []
    entities["seller_ids"] = verified_seller_ids[:20]
    if verified_seller_ids:
        refs(("get_sellers",))
    shipment = _object(safe_data("get_shipment_summary"), "shipment")
    if issue.startswith("late_delivery_") and (shipment_id := _id(shipment.get("shipment_id"))):
        entities["shipment_ids"] = [shipment_id]
        refs(("get_shipment_summary",))

    all_refs = list(dict.fromkeys(evidence[tool].ref for tool in evidence if tool in used))
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "claim_assessments": assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": all_refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": [
                {
                    "reason_code": issue.upper(),
                    "amount_brl": float(refund),
                    "entity_id": order_id,
                }
            ]
            if refund > ZERO
            else [],
        },
        "resolution_actions": [action],
    }


CollectionOfTools = tuple[str, ...] | list[str]


def _empty_output(case: dict[str, Any], refs: list[str]) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            key: []
            for key in (
                "order_ids",
                "item_ids",
                "seller_ids",
                "payment_references",
                "shipment_ids",
            )
        },
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0.2,
                "evidence_refs": [],
            }
            for claim in case.get("customer_request", {}).get("claims", [])
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "EVIDENCE_GAP", "rank": 1}],
            "responsible_parties": [],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["collect_missing_evidence"],
    }
