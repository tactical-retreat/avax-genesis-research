"""
Download the Avalanche mainnet genesis and build the allocation table the tracers read.

    python -m avax_research.cli.fetch_genesis            # -> data/genesis/

The genesis file is Ava Labs' own, from the avalanchego repository (`genesis/genesis_mainnet.json`,
unchanged since 2022), and is checked against a pinned SHA-256. Nothing derived from it is committed:
`all_allocations.csv` is rebuilt here, one row per genesis allocation.

Categories describe only the unlock schedule written in the genesis file (every schedule is quarterly), so
they carry no claim about who received an allocation:

    No lockup, Single unlock, 4 / 6 / 16 / 40 quarterly unlocks
"""

import argparse
import csv
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS_URL = "https://raw.githubusercontent.com/ava-labs/avalanchego/v1.15.0/genesis/genesis_mainnet.json"
GENESIS_SHA256 = "2a049446b4b4dcc2c2d51125f5354bfd0f363baeed6a6389437f7048d09723a1"
NAVAX_PER_AVAX = 1_000_000_000

COLUMNS = [
    "x_address",
    "p_address",
    "eth_control_address",
    "initial_avax",
    "locked_avax",
    "total_avax",
    "category",
    "unlock_count",
    "first_unlock",
    "last_unlock",
    "is_initial_staker",
    "node_id",
]


def category_for(unlock_count: int) -> str:
    if unlock_count == 0:
        return "No lockup"
    if unlock_count == 1:
        return "Single unlock"
    return f"{unlock_count} quarterly unlocks"


def _date(locktime: int) -> str:
    return datetime.fromtimestamp(locktime, UTC).date().isoformat()


def allocation_rows(genesis: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per `allocations[]` entry. An address can appear more than once."""
    stakers = {s["rewardAddress"]: s["nodeID"] for s in genesis.get("initialStakers", [])}
    rows = []
    for alloc in genesis["allocations"]:
        x_address = alloc["avaxAddr"]
        schedule = alloc.get("unlockSchedule") or []
        initial = alloc["initialAmount"] / NAVAX_PER_AVAX
        locked = sum(u["amount"] for u in schedule) / NAVAX_PER_AVAX
        locktimes = sorted(u["locktime"] for u in schedule)
        rows.append(
            {
                "x_address": x_address,
                "p_address": "P-" + x_address.removeprefix("X-"),
                "eth_control_address": alloc.get("ethAddr", ""),
                "initial_avax": initial,
                "locked_avax": locked,
                "total_avax": initial + locked,
                "category": category_for(len(schedule)),
                "unlock_count": len(schedule),
                "first_unlock": _date(locktimes[0]) if locktimes else "",
                "last_unlock": _date(locktimes[-1]) if locktimes else "",
                "is_initial_staker": x_address in stakers,
                "node_id": stakers.get(x_address, ""),
            }
        )
    return rows


def download(url: str, expected_sha256: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        body = response.read()
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected_sha256:
        raise SystemExit(f"genesis checksum mismatch: expected {expected_sha256}, got {actual} ({url})")
    return body


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", "-o", type=Path, default=Path("data/genesis"))
    parser.add_argument("--force", action="store_true", help="download again even if the file is present")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    genesis_path = args.output_dir / "genesis_mainnet.json"
    if args.force or not genesis_path.exists():
        print(f"downloading {GENESIS_URL}")
        genesis_path.write_bytes(download(GENESIS_URL, GENESIS_SHA256))
    elif hashlib.sha256(genesis_path.read_bytes()).hexdigest() != GENESIS_SHA256:
        sys.exit(f"{genesis_path} does not match the pinned checksum; rerun with --force")

    rows = allocation_rows(json.loads(genesis_path.read_text()))
    csv_path = args.output_dir / "all_allocations.csv"
    write_csv(rows, csv_path)
    total = sum(r["total_avax"] for r in rows)
    print(f"wrote {csv_path}: {len(rows):,} allocations, {total:,.0f} AVAX")


if __name__ == "__main__":
    main()
