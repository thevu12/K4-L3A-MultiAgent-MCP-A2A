from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.assessment import assess_case
from student_agent.cli import _persist_output
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

TOOLS = {
    "get_order",
    "get_order_items",
    "get_sellers",
    "get_order_payments",
    "get_payment_timeline",
    "get_refund_timeline",
    "get_shipment_summary",
    "get_policy",
}
ORDER_ID = "ORDER_001"
PURCHASED = "2026-01-01T09:00:00-03:00"
APPROVED = "2026-01-01T10:00:00-03:00"
DELIVERED = "2026-01-05T09:00:00-03:00"
ESTIMATED = "2026-01-06T09:00:00-03:00"


def _ref(name: str) -> str:
    return f"ev_{name}_{'x' * 24}"


def _evidence(domain: str, data: Any, ref_name: str) -> dict[str, Any]:
    return {"domain": domain, "evidence_ref": _ref(ref_name), "data": data}


class FakeGateway:
    def __init__(
        self,
        responses: dict[str, dict[str, Any]],
        failures: set[str] | None = None,
    ) -> None:
        self.responses = responses
        self.failures = failures or set()
        self.calls: list[str] = []

    async def list_tools(self) -> list[str]:
        return sorted(self.responses)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        del case_id, arguments
        self.calls.append(tool_name)
        if tool_name in self.failures:
            raise EvidenceError("TOOL_ERROR")
        return self.responses[tool_name]


def _payment(sequence: int, payment_type: str, amount: float) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "payment_sequential": str(sequence),
        "payment_type": payment_type,
        "payment_installments": "1",
        "payment_value": str(amount),
    }


def _event(
    event_type: str,
    amount: float,
    event_at: str,
    status: str = "confirmed",
) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "event_type": event_type,
        "amount_brl": str(amount),
        "event_at": event_at,
        "status": status,
    }


def _rule(
    case_status: str,
    refund: float,
    action: str,
    party_type: str,
    party_id: str | None = None,
) -> dict[str, Any]:
    return {
        "case_status": case_status,
        "refund_brl": refund,
        "recommended_action": action,
        "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
    }


def _responses(
    *,
    order_status: str = "delivered",
    items: list[dict[str, Any]] | None = None,
    payments: list[dict[str, Any]] | None = None,
    payment_events: list[dict[str, Any]] | None = None,
    refund_events: list[dict[str, Any]] | None = None,
    shipment_events: list[dict[str, Any]] | None = None,
    policy_rules: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    payment_rows = payments if payments is not None else [_payment(1, "credit_card", 100)]
    timeline = payment_events if payment_events is not None else [
        _event("captured", 100, APPROVED)
    ]
    item_rows = items if items is not None else [
        {
            "order_id": ORDER_ID,
            "order_item_id": "ITEM_001",
            "seller_id": "SELLER_001",
            "price": "80.00",
            "freight_value": "20.00",
            "shipping_limit_date": "2026-01-03T09:00:00-03:00",
        }
    ]
    return {
        "get_order": _evidence(
            "order",
            {
                "order_id": ORDER_ID,
                "order_status": order_status,
                "order_purchase_timestamp": PURCHASED,
                "order_approved_at": APPROVED,
                "order_delivered_customer_date": (
                    DELIVERED if order_status == "delivered" else None
                ),
                "order_estimated_delivery_date": ESTIMATED,
            },
            "order",
        ),
        "get_order_items": _evidence("item", item_rows, "items"),
        "get_sellers": _evidence(
            "seller", [{"seller_id": "SELLER_001"}], "sellers"
        ),
        "get_order_payments": _evidence("payment", payment_rows, "payments"),
        "get_payment_timeline": _evidence(
            "payment",
            {"order_id": ORDER_ID, "payments": payment_rows, "events": timeline},
            "payment_timeline",
        ),
        "get_refund_timeline": _evidence(
            "refund",
            {"order_id": ORDER_ID, "events": refund_events or []},
            "refund",
        ),
        "get_shipment_summary": _evidence(
            "shipment",
            {
                "order_id": ORDER_ID,
                "order_status": order_status,
                "delivered_customer_at": DELIVERED if order_status == "delivered" else None,
                "estimated_delivery_at": ESTIMATED,
                "events": shipment_events or [],
                "shipping_limits": [],
            },
            "shipment",
        ),
        "get_policy": _evidence(
            "policy",
            {
                "policy_version": "policy-v1",
                "currency": "BRL",
                "rules": policy_rules or {},
            },
            "policy",
        ),
    }


def _case(topic: str, *, include_refund_claim: bool = True) -> dict[str, Any]:
    claims = [{"claim_id": "CLAIM_ISSUE", "topic": topic}]
    if include_refund_claim:
        claims.append({"claim_id": "CLAIM_REFUND", "topic": "requested_full_refund"})
    return {
        "case_id": "CASE_001",
        "policy_version": "policy-v1",
        "customer_request": {"claimed_order_id": ORDER_ID, "claims": claims},
    }


def _run(
    tmp_path: Path,
    gateway: FakeGateway,
    *,
    case: dict[str, Any],
    tools: set[str] = TOOLS,
) -> tuple[dict[str, Any], list[dict[str, Any]], Contracts]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(trace_path, contracts), tools)
    )
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    return output, events, contracts


def test_duplicate_charge_uses_repeated_payment_signature_and_policy(tmp_path: Path) -> None:
    base = [_payment(1, "credit_card", 60), _payment(2, "voucher", 60)]
    responses = _responses(
        payments=[*base, *base],
        payment_events=[
            _event("captured", 60, APPROVED),
            _event("captured", 60, "2026-01-01T11:00:00-03:00"),
        ],
        policy_rules={
            "duplicate_charge": _rule(
                "action_required", 60, "refund_duplicate_charge", "payment_provider"
            )
        },
    )
    output, events, contracts = _run(
        tmp_path, FakeGateway(responses), case=_case("duplicate_charge")
    )

    contracts.validate_output(output, "test output")
    assert output["assessment"] == {
        "primary_issue": "duplicate_charge",
        "case_status": "action_required",
        "confidence": 0.84,
    }
    assert output["financial_resolution"]["recommended_refund_brl"] == 60.0
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
    event_types = [event["event_type"] for event in events]
    assert event_types[0] == "case_received"
    assert event_types[-1] == "verification_completed"
    assert {"task_assigned", "tool_result_consumed", "handoff"} <= set(event_types)


def test_valid_split_payment_ignores_out_of_window_noise(tmp_path: Path) -> None:
    noise_item = {
        "order_id": ORDER_ID,
        "order_item_id": "ITEM_001",
        "seller_id": "SELLER_001",
        "price": "80.00",
        "freight_value": "20.00",
        "shipping_limit_date": "2025-08-01T09:00:00-03:00",
    }
    responses = _responses(
        items=[_responses()["get_order_items"]["data"][0], noise_item],
        payments=[
            _payment(1, "credit_card", 40),
            _payment(2, "voucher", 60),
            _payment(1, "credit_card", 52),
        ],
        payment_events=[
            _event("captured", 40, APPROVED),
            _event("captured", 60, "2026-01-01T11:00:00-03:00"),
            _event("captured", 52, "2025-08-01T10:00:00-03:00"),
        ],
        refund_events=[
            _event("refund_requested", 52, "2025-08-10T09:00:00-03:00", "failed")
        ],
        shipment_events=[
            {
                "order_id": ORDER_ID,
                "event_type": "delivered_late",
                "event_at": "2026-05-01T09:00:00-03:00",
                "status": "confirmed",
                "actor": "seller",
            }
        ],
        policy_rules={
            "valid_split_payment": _rule(
                "no_action", 0, "document_no_action", "customer"
            )
        },
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("valid_split_payment")
    )

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["claim_assessments"][1]["verdict"] == "unsupported"


@pytest.mark.parametrize("status", ["canceled", "unavailable"])
def test_non_fulfilled_paid_order_uses_scoped_policy(tmp_path: Path, status: str) -> None:
    issue = f"{status}_order_paid"
    responses = _responses(
        order_status=status,
        items=[
            {
                "order_id": ORDER_ID,
                "order_item_id": "ITEM_001",
                "seller_id": "SELLER_001",
                "price": "60.00",
                "freight_value": "20.00",
                "shipping_limit_date": "2026-01-03T09:00:00-03:00",
            }
        ],
        payments=[_payment(1, "credit_card", 80)],
        payment_events=[_event("captured", 80, APPROVED)],
        policy_rules={
            issue: _rule("action_required", 80, "issue_refund", "platform")
        },
    )
    output, _, _ = _run(tmp_path, FakeGateway(responses), case=_case(issue))

    assert output["assessment"]["primary_issue"] == issue
    assert output["financial_resolution"]["recommended_refund_brl"] == 80.0
    assert output["claim_assessments"][1]["verdict"] == "supported"


def test_exact_duplicate_capture_record_is_not_a_second_charge(tmp_path: Path) -> None:
    payment = _payment(1, "credit_card", 100)
    capture = _event("captured", 100, APPROVED)
    responses = _responses(
        order_status="unavailable",
        payments=[payment, payment.copy()],
        payment_events=[capture, capture.copy()],
        policy_rules={
            "unavailable_order_paid": _rule(
                "action_required", 100, "issue_refund", "platform"
            ),
            "duplicate_charge": _rule(
                "action_required", 60, "refund_duplicate_charge", "payment_provider"
            ),
        },
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("unavailable_order_paid")
    )

    assert output["assessment"]["primary_issue"] == "unavailable_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100.0
    assert [claim["verdict"] for claim in output["claim_assessments"]] == [
        "supported",
        "supported",
    ]


@pytest.mark.parametrize(
    ("refund_status", "case_status", "refund", "action", "refund_verdict"),
    [
        ("pending", "needs_investigation", 0, "monitor_refund", "insufficient_evidence"),
        ("failed", "action_required", 100, "retry_refund", "supported"),
    ],
)
def test_refund_lifecycle_uses_events_and_policy(
    tmp_path: Path,
    refund_status: str,
    case_status: str,
    refund: float,
    action: str,
    refund_verdict: str,
) -> None:
    issue = f"refund_{refund_status}"
    responses = _responses(
        refund_events=[
            _event("refund_requested", 100, "2026-01-07T09:00:00-03:00", refund_status)
        ],
        policy_rules={
            issue: _rule(case_status, refund, action, "payment_provider")
        },
    )
    output, _, _ = _run(tmp_path, FakeGateway(responses), case=_case(issue))

    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == case_status
    assert output["resolution_actions"] == [action]
    assert output["claim_assessments"][1]["verdict"] == refund_verdict


@pytest.mark.parametrize("first_status", ["pending", "failed"])
def test_completed_refund_supersedes_earlier_state(
    tmp_path: Path, first_status: str
) -> None:
    responses = _responses(
        refund_events=[
            _event("refund_requested", 100, "2026-01-06T09:00:00-03:00", first_status),
            _event("refund_completed", 100, "2026-01-07T09:00:00-03:00", "completed"),
        ],
        policy_rules={
            f"refund_{first_status}": _rule(
                "action_required", 100, "retry_refund", "payment_provider"
            ),
            "unsupported_claim": _rule(
                "no_action", 0, "document_no_action", "customer"
            ),
        },
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case(f"refund_{first_status}")
    )

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0


def test_latest_refund_state_wins_over_static_issue_priority(tmp_path: Path) -> None:
    responses = _responses(
        refund_events=[
            _event("refund_failed", 100, "2026-01-06T09:00:00-03:00", "failed"),
            _event("refund_requested", 100, "2026-01-07T09:00:00-03:00", "pending"),
        ],
        policy_rules={
            "refund_pending": _rule(
                "needs_investigation", 0, "monitor_refund", "payment_provider"
            )
        },
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("refund_pending")
    )

    assert output["assessment"]["primary_issue"] == "refund_pending"


@pytest.mark.parametrize(
    ("actor", "issue", "party_type"),
    [
        ("seller", "late_delivery_seller", "seller"),
        ("logistics_provider", "late_delivery_logistics", "logistics_provider"),
    ],
)
def test_late_delivery_uses_confirmed_delivery_event(
    tmp_path: Path,
    actor: str,
    issue: str,
    party_type: str,
) -> None:
    party_id = "SELLER_001" if actor == "seller" else None
    responses = _responses(
        payments=[_payment(1, "credit_card", 20)],
        payment_events=[_event("captured", 20, APPROVED)],
        shipment_events=[
            {
                "order_id": ORDER_ID,
                "event_type": "delivered_late",
                "event_at": DELIVERED,
                "status": "confirmed",
                "actor": actor,
            }
        ],
        policy_rules={
            issue: _rule("action_required", 20, "refund_freight", party_type, party_id)
        },
    )
    output, _, _ = _run(tmp_path, FakeGateway(responses), case=_case(issue))

    assert output["assessment"]["primary_issue"] == issue
    assert output["financial_resolution"]["recommended_refund_brl"] == 20.0
    assert output["affected_entities"]["item_ids"] == ["ITEM_001"]
    assert output["affected_entities"]["seller_ids"] == (
        ["SELLER_001"] if party_id else []
    )
    assert (_ref("sellers") in output["evidence_refs"]) is bool(party_id)


def test_unverified_policy_seller_is_not_emitted(tmp_path: Path) -> None:
    responses = _responses(
        payments=[_payment(1, "credit_card", 20)],
        payment_events=[_event("captured", 20, APPROVED)],
        shipment_events=[
            {
                "order_id": ORDER_ID,
                "event_type": "delivered_late",
                "event_at": DELIVERED,
                "status": "confirmed",
                "actor": "seller",
            }
        ],
        policy_rules={
            "late_delivery_seller": _rule(
                "action_required",
                20,
                "refund_freight",
                "seller",
                "UNVERIFIED_SELLER",
            )
        },
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("late_delivery_seller")
    )

    assert output["affected_entities"]["seller_ids"] == []
    assert _ref("sellers") not in output["evidence_refs"]


def test_payment_mismatch_uses_reconciliation_event(tmp_path: Path) -> None:
    responses = _responses(
        payments=[_payment(1, "credit_card", 35)],
        payment_events=[
            _event("captured", 35, APPROVED),
            _event("reconciliation_mismatch", 35, "2026-01-01T12:00:00-03:00", "open"),
        ],
        policy_rules={
            "payment_mismatch": _rule(
                "action_required", 35, "reconcile_payment", "payment_provider"
            )
        },
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("payment_mismatch")
    )

    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["financial_resolution"]["recommended_refund_brl"] == 35.0


def test_clean_evidence_is_unsupported_claim(tmp_path: Path) -> None:
    responses = _responses(
        policy_rules={
            "unsupported_claim": _rule(
                "no_action", 0, "document_no_action", "customer"
            )
        }
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("unsupported_claim")
    )

    assert output["assessment"] == {
        "primary_issue": "unsupported_claim",
        "case_status": "no_action",
        "confidence": 0.84,
    }
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["claim_assessments"][1]["verdict"] == "unsupported"
    assert _ref("refund") not in output["evidence_refs"]


def test_missing_refund_record_does_not_block_non_refund_issue(tmp_path: Path) -> None:
    responses = _responses(
        order_status="canceled",
        payments=[_payment(1, "credit_card", 80)],
        payment_events=[_event("captured", 80, APPROVED)],
        policy_rules={
            "canceled_order_paid": _rule(
                "action_required", 80, "issue_refund", "platform"
            )
        },
    )
    gateway = FakeGateway(responses, failures={"get_refund_timeline"})
    output, events, _ = _run(
        tmp_path, gateway, case=_case("canceled_order_paid")
    )

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert _ref("refund") not in output["evidence_refs"]
    assert any(
        event["event_type"] == "handoff"
        and event.get("tool_name") == "get_refund_timeline"
        and event["attributes"]["error_code"] == "TOOL_ERROR"
        for event in events
    )


def test_missing_refund_record_allows_clean_unsupported_claim(tmp_path: Path) -> None:
    responses = _responses(
        policy_rules={
            "unsupported_claim": _rule(
                "no_action", 0, "document_no_action", "customer"
            )
        }
    )
    output, events, _ = _run(
        tmp_path,
        FakeGateway(responses, failures={"get_refund_timeline"}),
        case=_case("unsupported_claim"),
    )

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert _ref("refund") not in output["evidence_refs"]
    assert any(
        event["event_type"] == "handoff"
        and event.get("tool_name") == "get_refund_timeline"
        and event["attributes"]["error_code"] == "TOOL_ERROR"
        for event in events
    )


@pytest.mark.parametrize("status", ["canceled", "unavailable"])
def test_completed_full_refund_prevents_second_refund(
    tmp_path: Path, status: str
) -> None:
    issue = f"{status}_order_paid"
    responses = _responses(
        order_status=status,
        items=[
            {
                "order_id": ORDER_ID,
                "order_item_id": "ITEM_001",
                "seller_id": "SELLER_001",
                "price": "60.00",
                "freight_value": "20.00",
                "shipping_limit_date": "2026-01-03T09:00:00-03:00",
            }
        ],
        payments=[_payment(1, "credit_card", 80)],
        payment_events=[_event("captured", 80, APPROVED)],
        refund_events=[
            _event("refund_completed", 80, "2026-01-07T09:00:00-03:00", "completed")
        ],
        policy_rules={issue: _rule("action_required", 80, "issue_refund", "platform")},
    )
    output, _, _ = _run(tmp_path, FakeGateway(responses), case=_case(issue))

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert {_ref("payment_timeline"), _ref("refund")} <= set(output["evidence_refs"])


def test_completed_refund_above_capture_is_conflict(tmp_path: Path) -> None:
    responses = _responses(
        refund_events=[
            _event("refund_completed", 120, "2026-01-07T09:00:00-03:00", "completed")
        ]
    )
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("refund_failed")
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["data_conflicts"][0]["field"] == "refunded_total_brl"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert {_ref("payment_timeline"), _ref("refund")} <= set(output["evidence_refs"])


def test_cross_order_root_list_is_rejected(tmp_path: Path) -> None:
    responses = _responses()
    responses["get_order_items"]["data"][0]["order_id"] = "OTHER_ORDER"
    output, _, _ = _run(
        tmp_path, FakeGateway(responses), case=_case("unsupported_claim")
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["data_conflicts"][0]["field"] == "order_id"


def test_undiscovered_tools_are_not_called_and_no_claim_is_invented(tmp_path: Path) -> None:
    case = _case("unsupported_claim", include_refund_claim=False)
    case["customer_request"]["claims"] = []
    gateway = FakeGateway(_responses())
    output, _, contracts = _run(tmp_path, gateway, case=case, tools=set())

    contracts.validate_output(output, "test output")
    assert gateway.calls == []
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["claim_assessments"] == []


def test_persist_output_emits_final_event_after_atomic_write(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    output = assess_case(_case("unsupported_claim"), {})
    output_root = tmp_path / "outputs"
    target = output_root / "CASE_001.json"

    class PersistenceSpy:
        def emit(self, **event: Any) -> None:
            assert target.is_file()
            assert event["event_type"] == "case_finalized"

    _persist_output(output_root, "CASE_001", output, contracts, PersistenceSpy())  # type: ignore[arg-type]
    assert json.loads(target.read_text(encoding="utf-8")) == output
