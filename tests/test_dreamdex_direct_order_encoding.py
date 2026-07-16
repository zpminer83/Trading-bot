from decimal import Decimal

from bot.execution.dreamdex_direct_order_encoding import (
    CANCEL_SELECTOR,
    PLACE_SELECTOR,
    REDUCE_SELECTOR,
    DreamDexDirectOrderSpecification,
    audit_direct_owner_vendor,
    audit_direct_selectors,
    build_cancel_order_call_preview,
    build_direct_owner_identity,
    build_place_order_call_preview,
    build_reduce_order_call_preview,
    compute_safe_calldata_fingerprint,
    parse_order_cancelled_event,
    parse_order_placed_event,
    validate_direct_order_specification,
)


POOL = "0x1111111111111111111111111111111111111111"
OWNER = "0x2222222222222222222222222222222222222222"
SIGNER = "0x3333333333333333333333333333333333333333"


def make_spec(**overrides):
    values = {
        "symbol": "SOMI:USDso",
        "side": "buy",
        "order_type": "limit",
        "price": Decimal("10"),
        "quantity": Decimal("1"),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": False,
        "deadline": 999999999999999999,
        "owner_subject": OWNER,
        "signer_subject": OWNER,
        "target_contract": POOL,
        "native_value": None,
        "tick_size": Decimal("0.0001"),
        "quantity_step": Decimal("0.01"),
        "minimum_quantity": Decimal("1"),
        "minimum_notional": Decimal("1"),
    }
    values.update(overrides)
    return DreamDexDirectOrderSpecification(**values)


def test_direct_owner_selected_and_operator_inactive_by_default():
    audit = audit_direct_owner_vendor()
    assert audit.selected_mode == "direct_owner"
    assert audit.operator_mode_active is False
    assert audit.execution_authority == "unavailable"
    assert audit.authoritative is False
    assert audit.vendor_files
    assert audit.vendor_file_fingerprints


def test_identity_does_not_auto_bind_login_smart_wallet_or_signer():
    identity = build_direct_owner_identity(contest_login_address=OWNER, configured_owner_address=OWNER, platform_trading_address=POOL)
    assert identity.mapping_status == "unresolved"
    assert identity.transaction_signer_role == "unresolved"
    assert identity.authoritative is False
    assert OWNER not in repr(identity)
    assert POOL not in repr(identity)


def test_exact_place_encoding_preview_is_deterministic_and_redacted():
    spec = make_spec()
    first = build_place_order_call_preview(spec)
    second = build_place_order_call_preview(spec)
    assert first.selector == PLACE_SELECTOR
    assert first.validation_status == "valid"
    assert first.calldata_length == 292
    assert first.calldata_fingerprint == second.calldata_fingerprint
    assert first.target_masked != POOL
    assert "calldata" not in first.safe_dict()
    assert "raw_calldata" not in repr(first)


def test_place_encoding_keeps_native_value_conditional_and_does_not_send():
    preview = build_place_order_call_preview(make_spec(native_value=None))
    assert preview.native_value_category == "conditional_getAutoPullRequirement"
    assert not hasattr(preview, "send")
    assert not hasattr(preview, "submit")


def test_invalid_price_quantity_and_order_type_are_blocked():
    assert "invalid_price_tick" in validate_direct_order_specification(make_spec(price=Decimal("10.00005"))).reasons
    assert "invalid_quantity_step" in validate_direct_order_specification(make_spec(quantity=Decimal("1.001"))).reasons
    assert "quantity_below_minimum" in validate_direct_order_specification(make_spec(quantity=Decimal("0.99"), minimum_quantity=Decimal("1"))).reasons
    assert "unsupported_order_type" in validate_direct_order_specification(make_spec(order_type="market")).reasons


def test_unavailable_minimum_notional_remains_a_trading_blocker():
    validation = validate_direct_order_specification(make_spec(minimum_notional=None))
    assert validation.valid is False
    assert "minimum_notional_unavailable" in validation.reasons
    preview = build_place_order_call_preview(make_spec(minimum_notional=None))
    assert preview.calldata_fingerprint is not None
    assert preview.validation_status == "blocked"


def test_owner_signer_mismatch_is_fail_closed():
    validation = validate_direct_order_specification(make_spec(owner_subject=OWNER, signer_subject=SIGNER))
    assert "owner_signer_mismatch" in validation.reasons
    target_conflict = validate_direct_order_specification(make_spec(owner_subject=OWNER, signer_subject=OWNER), expected_target_contract="0x4444444444444444444444444444444444444444")
    assert "target_contract_conflict" in target_conflict.reasons


def test_cancel_encoding_has_exact_selector_and_owner_mismatch_fails_closed():
    preview = build_cancel_order_call_preview(target_contract=POOL, order_id=7, signer_subject=OWNER)
    assert preview.selector == CANCEL_SELECTOR
    assert preview.validation_status == "valid"
    mismatch = build_cancel_order_call_preview(target_contract=POOL, order_id=7, owner_subject=OWNER, signer_subject=SIGNER)
    assert mismatch.validation_status == "blocked"
    assert "cancel_owner_mismatch" in mismatch.unresolved_reasons


def test_reduce_is_direct_abi_and_not_operator_reduce_selector():
    preview = build_reduce_order_call_preview(target_contract=POOL, order_id=7, new_quantity_remaining=3, signer_subject=OWNER)
    assert preview.selector == REDUCE_SELECTOR
    assert preview.validation_status == "valid"
    assert preview.selector != "0x364c2587"


def test_order_id_lifecycle_requires_receipt_event_and_rejects_absence():
    receipt = {"logs": [{"address": POOL, "topics": ["0xd90f62f61ee2f606b132cfdfd883ddd079228b6fd6bffd9d7cf848daf824639d", "0x" + "0" * 63 + "7"]}]}
    event = parse_order_placed_event(receipt, expected_pool=POOL)
    assert event.status == "confirmed"
    assert event.order_id == 7
    absent = parse_order_placed_event({"logs": []})
    assert absent.status == "absent"
    assert absent.authoritative is False


def test_cancel_event_and_fingerprints_are_offline():
    receipt = {"logs": [{"topics": ["0x06ff08ed6b6987bb7df963009d8b54dc03988f4e465c009924929bb010fe03e7", "0x" + "0" * 63 + "7"]}]}
    event = parse_order_cancelled_event(receipt)
    assert event.status == "confirmed"
    assert compute_safe_calldata_fingerprint(b"fixture") == compute_safe_calldata_fingerprint(b"fixture")
    assert event.authoritative is False


def test_audit_marks_smart_wallet_and_signer_semantics_non_authoritative():
    audit = audit_direct_owner_vendor()
    assert audit.smart_wallet_semantics == "observed_non_authoritative"
    assert "transaction_signer_unavailable" in audit.unresolved_reasons
    assert any(item["name"] == "placeOrder" and item["status"] == "source_confirmed" for item in audit.function_evidence)
    assert any(item["name"] == "OrderPlaced" for item in audit.event_evidence)


def test_selector_conflict_blocks_direct_owner_execution_evidence():
    selectors = audit_direct_selectors(declared_selectors={"placeOrder": "0xdeadbeef"})
    place = next(item for item in selectors if item["name"] == "placeOrder")
    assert place["status"] == "conflicting"
    audit = audit_direct_owner_vendor(declared_selectors={"placeOrder": "0xdeadbeef"})
    assert audit.selector_consistency == "conflicting"
    assert "direct_order_selector_conflicting" in audit.unresolved_reasons
