import pytest

from bot.execution.dreamdex_readonly_rpc import (
    ALLOWED_RPC_METHODS,
    DreamDexRpcError,
    DreamDexReadOnlyRpcTransport,
    FixtureDreamDexReadOnlyRpcTransport,
    parse_rpc_quantity,
    validate_rpc_response,
)


ADDRESS = "0x2222222222222222222222222222222222222222"


def test_allowlist_is_exact_and_typed_transport_has_no_generic_or_mutation_api():
    assert ALLOWED_RPC_METHODS == {
        "eth_chainId", "eth_getCode", "eth_getTransactionCount", "eth_estimateGas",
        "eth_getBlockByNumber", "eth_gasPrice", "eth_maxPriorityFeePerGas",
        "eth_feeHistory", "eth_getBalance", "eth_getTransactionReceipt",
        "eth_blockNumber", "eth_getBlockByHash",
    }
    transport = FixtureDreamDexReadOnlyRpcTransport({"eth_chainId": "0x13a7"})
    assert not hasattr(transport, "call")
    assert not hasattr(transport, "send_transaction")
    assert not hasattr(transport, "send_raw_transaction")


@pytest.mark.parametrize("value", [True, False, "1", "0x", "0x00", "1", "0x" + "1" * 65])
def test_quantity_parser_rejects_noncanonical_or_unsafe_values(value):
    with pytest.raises(ValueError):
        parse_rpc_quantity(value)


def test_quantity_parser_accepts_canonical_zero_and_bounds():
    assert parse_rpc_quantity("0x0") == 0
    assert parse_rpc_quantity("0x13a7") == 5031
    with pytest.raises(ValueError):
        parse_rpc_quantity("0x100", maximum=10)


def test_public_response_validator_returns_only_result_or_rejects_error():
    assert validate_rpc_response({"jsonrpc": "2.0", "id": 1, "result": "0x1"}, 1) == "0x1"
    with pytest.raises(DreamDexRpcError, match="rpc_error"):
        validate_rpc_response({"jsonrpc": "2.0", "id": 1, "error": {"data": "secret"}}, 1)


class _Response:
    status_code = 200
    content = b'{"jsonrpc":"2.0","id":1,"result":"0x13a7"}'

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": "0x13a7"}


class _Http:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def test_http_transport_validates_jsonrpc_envelope_and_does_not_follow_redirects():
    client = _Http(_Response())
    transport = DreamDexReadOnlyRpcTransport("https://rpc.example.invalid", http_client=client)
    assert transport.get_chain_id() == 5031
    assert client.calls[0][1]["follow_redirects"] is False
    assert client.calls[0][1]["timeout"] == 10.0


def test_http_transport_rejects_request_id_mismatch_and_result_error_conflict():
    class Bad(_Response):
        def json(self):
            return {"jsonrpc": "2.0", "id": 99, "result": "0x1"}

    with pytest.raises(DreamDexRpcError, match="invalid_jsonrpc_envelope"):
        DreamDexReadOnlyRpcTransport("https://rpc.example.invalid", http_client=_Http(Bad())).get_chain_id()

    class Conflict(_Response):
        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": "0x1", "error": {"code": -1}}

    with pytest.raises(DreamDexRpcError, match="result_error_conflict"):
        DreamDexReadOnlyRpcTransport("https://rpc.example.invalid", http_client=_Http(Conflict())).get_chain_id()


def test_http_transport_sanitizes_network_exception_without_url_or_payload():
    class Failing:
        def post(self, *args, **kwargs):
            raise RuntimeError("timeout https://secret.example/?token=private")

    with pytest.raises(DreamDexRpcError, match="timeout") as caught:
        DreamDexReadOnlyRpcTransport("https://rpc.example.invalid", http_client=Failing()).get_chain_id()
    assert "secret.example" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_typed_methods_use_pending_and_latest_tags_and_exact_estimate_fields():
    transport = FixtureDreamDexReadOnlyRpcTransport({
        "eth_chainId": "0x13a7", "eth_getCode": "0x6000", "eth_getTransactionCount": "0x2",
        "eth_estimateGas": "0x5208", "eth_getBlockByNumber": {"number": "0x10", "baseFeePerGas": "0x64"},
        "eth_maxPriorityFeePerGas": "0x2", "eth_getBalance": "0x100",
    })
    assert transport.get_chain_id() == 5031
    assert transport.get_contract_code(ADDRESS) == "0x6000"
    assert transport.get_pending_nonce(ADDRESS) == 2
    assert transport.estimate_gas({"from": ADDRESS, "to": ADDRESS, "value": "0x0", "data": "0x1234"}) == 21000
    assert transport.get_latest_block_fee_evidence().base_fee_per_gas_wei == 100
    assert transport.get_native_balance(ADDRESS) == 256
    assert dict(transport.calls)["eth_getTransactionCount"] == (ADDRESS, "pending")
    assert dict(transport.calls)["eth_getBalance"] == (ADDRESS, "latest")
    with pytest.raises(ValueError):
        transport.estimate_gas({"from": ADDRESS, "to": ADDRESS, "value": "0x0", "data": "0x1234", "nonce": "0x1"})


def test_transport_invalid_address_is_rejected_before_fixture_lookup():
    transport = FixtureDreamDexReadOnlyRpcTransport({"eth_getCode": "0x6000"})
    with pytest.raises(ValueError):
        transport.get_contract_code("not-an-address")
