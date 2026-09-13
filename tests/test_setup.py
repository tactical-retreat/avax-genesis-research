"""Offline checks for the parts added when the toolkit moved here: key loading, the genesis table, the client."""

import importlib
import pkgutil
from pathlib import Path

import pytest

import avax_research
from avax_research.api_keys import load_api_keys
from avax_research.cli.fetch_genesis import COLUMNS, allocation_rows, category_for
from avax_research.clients.glacier_client import GlacierClient
from avax_research.tracer.genesis_matcher import GenesisMatcher

STAKER = "X-avax1cnrgn3p08xzj8cnm2xclafa53x6d637tfe9ead"
HOLDER = "X-avax1yu8wlxfdkj8mnca60jwwzvfyqzd6xlnf635tld"

GENESIS = {
    "initialStakers": [{"nodeID": "NodeID-A6onFGyJjA37EZ7kYHANMR1PFRT8NmXrF", "rewardAddress": STAKER}],
    "allocations": [
        {
            "ethAddr": "0xb3d82b1367d362de99ab59a658165aff520cbd4d",
            "avaxAddr": HOLDER,
            "initialAmount": 240_000_000_000,
            "unlockSchedule": [
                {"amount": 360_000_000_000, "locktime": 1607472000},
                {"amount": 360_000_000_000, "locktime": 1615248000},
            ],
        },
        {"ethAddr": "0x0000000000000000000000000000000000000001", "avaxAddr": STAKER, "initialAmount": 5_000_000_000},
    ],
}


def test_every_module_imports():
    for module in pkgutil.walk_packages(avax_research.__path__, "avax_research."):
        importlib.import_module(module.name)


def test_keys_come_from_env_then_file_then_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GLACIER_API_KEYS", raising=False)
    monkeypatch.delenv("GLACIER_API_KEY", raising=False)
    monkeypatch.delenv("GLACIER_API_KEYS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_api_keys() == []

    (tmp_path / "glacier_api_keys.txt").write_text("# comment\nkey-one   # alice\n\n  key-two\n# key-three\n")
    assert load_api_keys() == ["key-one", "key-two"]

    other = tmp_path / "other.txt"
    other.write_text("key-four\n")
    monkeypatch.setenv("GLACIER_API_KEYS_FILE", str(other))
    assert load_api_keys() == ["key-four"]

    monkeypatch.setenv("GLACIER_API_KEYS", "a, b")
    assert load_api_keys() == ["a", "b"]


def test_client_without_keys_sends_no_key_header(tmp_path: Path):
    client = GlacierClient(api_keys=[], cache_path=str(tmp_path / "cache.db"))
    assert client.current_api_key is None
    assert "x-glacier-api-key" not in client._make_client(None)._headers
    keyed = GlacierClient(api_keys=["k1", "k2"], cache_path=str(tmp_path / "cache2.db"))
    assert keyed._make_client("k1")._headers["x-glacier-api-key"] == "k1"
    client.close()
    keyed.close()


def test_allocation_rows_describe_only_the_schedule():
    holder, staker = allocation_rows(GENESIS)
    assert holder["category"] == "2 quarterly unlocks" and holder["unlock_count"] == 2
    assert (holder["initial_avax"], holder["locked_avax"], holder["total_avax"]) == (240.0, 720.0, 960.0)
    assert (holder["first_unlock"], holder["last_unlock"]) == ("2020-12-09", "2021-03-09")
    assert holder["p_address"] == "P-" + HOLDER[2:] and holder["is_initial_staker"] is False
    assert staker["category"] == "No lockup" and staker["is_initial_staker"] is True
    assert staker["node_id"] == "NodeID-A6onFGyJjA37EZ7kYHANMR1PFRT8NmXrF" and staker["first_unlock"] == ""
    assert category_for(1) == "Single unlock" and category_for(40) == "40 quarterly unlocks"
    assert set(holder) == set(COLUMNS)


def test_genesis_matcher_reads_the_built_table(tmp_path: Path):
    from avax_research.cli.fetch_genesis import write_csv

    path = tmp_path / "all_allocations.csv"
    write_csv(allocation_rows(GENESIS), path)
    matcher = GenesisMatcher(path)
    match = matcher.match("P-" + HOLDER[2:])
    assert match is not None and match.category == "2 quarterly unlocks" and match.total_avax == 960.0
    assert matcher.match(STAKER).is_validator
    assert sorted(matcher.get_all_categories()) == ["2 quarterly unlocks", "No lockup"]
