"""C-Chain and P/X addresses are different hashes of a key: the model must never convert between them."""

import pytest

from avax_research.clients.glacier_client import GlacierClient
from avax_research.models.address import AddressKind, AvaxAddress, Chain
from avax_research.tracer.genesis_matcher import GenesisMatcher

PX = "avax10q9exhgsl8kfdqcnse4t9c3ar7y8wmw9guy8xe"
EVM = "0xdf391e05a90e62bae97db3b5c5bbe3bb416c6fbe"
# the 0x string the old model printed for PX: same bytes, but an unrelated (empty) C-Chain address
PX_BYTES_AS_0X = "0x780b935d10f9ec968313866ab2e23d1f88776dc5"


def test_format_decides_the_family_and_p_x_are_the_same_address():
    p, x, bare = (AvaxAddress.from_any(s) for s in (f"P-{PX}", f"X-{PX}", PX))
    assert p == x == bare and p.kind is AddressKind.PRIMARY
    assert AvaxAddress.from_any(EVM).kind is AddressKind.EVM
    assert AvaxAddress.from_any(EVM[2:]).is_evm


def test_no_cross_family_forms():
    px, evm = AvaxAddress.from_any(PX), AvaxAddress.from_any(EVM)
    assert px.c_address == "" and str(px) == PX and px.x_address == f"X-{PX}"
    assert evm.p_address == evm.x_address == evm.bech32 == "" and str(evm) == EVM
    assert px.match_strings == {PX, f"x-{PX}", f"p-{PX}"} and evm.match_strings == {EVM}
    with pytest.raises(ValueError):
        px.for_chain(Chain.C)
    with pytest.raises(ValueError):
        evm.for_chain(Chain.P)


def test_same_bytes_in_different_families_are_different_addresses():
    px = AvaxAddress.from_any(PX)
    evm_same_bytes = AvaxAddress.from_any(PX_BYTES_AS_0X)
    assert px.raw_bytes == evm_same_bytes.raw_bytes
    assert px != evm_same_bytes and len({px, evm_same_bytes}) == 2


def test_genesis_matching_ignores_evm_addresses_with_matching_bytes(tmp_path):
    csv_path = tmp_path / "all_allocations.csv"
    csv_path.write_text(
        "x_address,p_address,eth_control_address,initial_avax,locked_avax,total_avax,category,unlock_count,"
        "first_unlock,last_unlock,is_initial_staker,node_id\n"
        f"X-{PX},P-{PX},,0,1,1,Single unlock,1,2021-01-01,2021-01-01,False,\n"
    )
    matcher = GenesisMatcher(csv_path)
    assert matcher.match(f"P-{PX}") is not None
    assert matcher.match(PX_BYTES_AS_0X) is None


class _NoNetwork(GlacierClient):
    def _call_with_retry(self, func, *args, operation: str = "request", **kwargs):
        raise AssertionError(f"unexpected API call: {operation} {kwargs}")


def test_client_never_queries_an_address_on_a_chain_it_cannot_exist_on(tmp_path):
    client = _NoNetwork(api_keys=[], cache_path=str(tmp_path / "cache.db"))
    px, evm = AvaxAddress.from_any(PX), AvaxAddress.from_any(EVM)
    assert list(client.get_p_chain_transactions(evm)) == []
    assert list(client.get_x_chain_transactions(evm)) == []
    assert list(client.get_c_chain_native_transactions(px)) == []
    assert client.get_balances(evm) == {} and client.get_chain_ids_for_address(evm) == []
    assert client.is_cached(evm, "P") is False
    client.close()


def test_atomic_lookup_uses_the_real_form_of_each_family(tmp_path):
    seen: list[str] = []

    class Recording(GlacierClient):
        def _call_with_retry(self, func, *args, operation: str = "request", **kwargs):
            seen.append(kwargs["addresses"])
            return None  # no transactions

    client = Recording(api_keys=[], cache_path=str(tmp_path / "cache.db"))
    list(client.get_c_chain_atomic_transactions(AvaxAddress.from_any(PX)))
    list(client.get_c_chain_atomic_transactions(AvaxAddress.from_any(EVM)))
    assert seen == [PX, "0xdF391E05A90e62baE97db3b5C5BBe3Bb416c6fbe"]
    client.close()
