"""Forward tracing from genesis over a fake Glacier client: the three traversal strategies and their bookkeeping."""

from datetime import datetime
from pathlib import Path

import pytest

from avax_research.clients.glacier_client import TransactionRecord
from avax_research.models.address import AvaxAddress, Chain
from avax_research.tracer.genesis_forward import (
    C_CHAIN_ID,
    GenesisForwardTracer,
    GenesisTraceConfig,
    TraceStrategy,
)
from avax_research.tracer.genesis_matcher import GenesisMatcher

NAVAX = 1_000_000_000


def px(n: int) -> str:
    """A distinct valid bech32 P/X address."""
    return AvaxAddress(bytes([n]) * 20, AvaxAddress.from_any("avax10q9exhgsl8kfdqcnse4t9c3ar7y8wmw9guy8xe").kind).bech32


def evm(n: int) -> str:
    return "0x" + f"{n:02x}" * 20


def transfer(tx_hash: str, src: str, dst: str, avax: float, tx_type: str = "BaseTx", dest_chain: str | None = None):
    return TransactionRecord(
        tx_hash=tx_hash,
        chain=Chain.P,
        tx_type=tx_type,
        timestamp=datetime(2021, 1, 1),
        block_number=1,
        from_addresses=[src],
        to_addresses=[dst],
        amount_navax=int(avax * NAVAX),
        is_cross_chain=dest_chain is not None,
        destination_chain=dest_chain,
    )


class FakeGlacier:
    """P-Chain transactions by sender, and C-Chain ImportTxs by the bech32 owner they consume."""

    def __init__(self, p_txs: list[TransactionRecord], imports: dict[str, list[TransactionRecord]]):
        self.p_txs = p_txs
        self.imports = imports
        self.queried: list[str] = []

    def is_cached(self, address, chain, tx_types=None) -> bool:
        return False

    def get_p_chain_transactions(self, address, tx_types=None, max_pages=10):
        self.queried.append(address.bech32)
        return [tx for tx in self.p_txs if address.bech32 in tx.from_addresses or address.bech32 in tx.to_addresses]

    def get_x_chain_transactions(self, address, tx_types=None, max_pages=10):
        return []

    def get_c_chain_atomic_transactions(self, address, tx_types=None, max_pages=5):
        return self.imports.get(address, [])


def chain_fixture(length: int, final_avax: float = 1000.0):
    """genesis -> a1 -> ... -> a{length} -(ExportTx)-> intermediate -(ImportTx)-> 0x.."""
    genesis = px(1)
    hops = [genesis] + [px(10 + i) for i in range(length)]
    p_txs = [transfer(f"t{i}", hops[i], hops[i + 1], final_avax) for i in range(length)]
    intermediate = px(99)
    p_txs.append(transfer("export", hops[-1], intermediate, final_avax, "ExportTx", C_CHAIN_ID))
    imp = transfer("import", intermediate, evm(7), final_avax, "ImportTx")
    return genesis, p_txs, {intermediate: [imp]}


def genesis_csv(tmp_path: Path, *addresses: str) -> GenesisMatcher:
    path = tmp_path / "all_allocations.csv"
    rows = [
        "x_address,p_address,eth_control_address,initial_avax,locked_avax,total_avax,category,unlock_count,"
        "first_unlock,last_unlock,is_initial_staker,node_id"
    ]
    rows += [f"X-{a},P-{a},,5000,0,5000,No lockup,0,,,False," for a in addresses]
    path.write_text("\n".join(rows) + "\n")
    return GenesisMatcher(path)


def run(tmp_path, glacier, genesis, **config):
    tracer = GenesisForwardTracer(glacier_client=glacier, genesis_matcher=genesis_csv(tmp_path, genesis))
    return tracer.trace_from_genesis(
        addresses=[genesis], config=GenesisTraceConfig(**config), progress_callback=lambda _: None
    )


@pytest.mark.parametrize("strategy", list(TraceStrategy))
def test_every_strategy_reaches_an_export_past_the_hybrid_switch_depth(tmp_path, strategy):
    genesis, p_txs, imports = chain_fixture(length=6)
    result = run(tmp_path, FakeGlacier(p_txs, imports), genesis, strategy=strategy, max_depth=10, hybrid_switch_depth=2)
    assert [str(d.address) for d in result.destinations] == [evm(7)]
    assert result.total_exported_avax == 1000.0


def test_an_import_reached_twice_is_counted_once(tmp_path):
    genesis = px(1)
    a, b, intermediate = px(10), px(11), px(99)
    p_txs = [
        transfer("g-a", genesis, a, 600),
        transfer("g-b", genesis, b, 400),
        # both export into the same UTXO owner, and one ImportTx consumes it
        transfer("a-x", a, intermediate, 600, "ExportTx", C_CHAIN_ID),
        transfer("b-x", b, intermediate, 400, "ExportTx", C_CHAIN_ID),
    ]
    imports = {intermediate: [transfer("import", intermediate, evm(7), 1000, "ImportTx")]}
    for strategy in TraceStrategy:
        result = run(tmp_path, FakeGlacier(p_txs, imports), genesis, strategy=strategy)
        [dest] = result.destinations
        assert dest.import_tx_hashes == ["import"] and dest.total_avax == 1000.0, strategy


@pytest.mark.parametrize("strategy", list(TraceStrategy))
def test_an_address_first_reached_too_deep_is_still_traced_from_a_shallower_path(tmp_path, strategy):
    """Greedy pops the 900 AVAX path first and reaches `hub` beyond max_depth; the 100 AVAX path reaches it in range."""
    genesis, short, b1, b2, b3, hub, intermediate = px(1), px(10), px(11), px(12), px(13), px(20), px(99)
    p_txs = [
        transfer("g-short", genesis, short, 100),
        transfer("short-hub", short, hub, 100),
        transfer("g-b1", genesis, b1, 900),
        transfer("b1-b2", b1, b2, 900),
        transfer("b2-b3", b2, b3, 900),
        transfer("b3-hub", b3, hub, 900),
        transfer("hub-x", hub, intermediate, 100, "ExportTx", C_CHAIN_ID),
    ]
    imports = {intermediate: [transfer("import", intermediate, evm(7), 100, "ImportTx")]}
    result = run(tmp_path, FakeGlacier(p_txs, imports), genesis, strategy=strategy, max_depth=3, hybrid_switch_depth=1)
    assert [str(d.address) for d in result.destinations] == [evm(7)]
