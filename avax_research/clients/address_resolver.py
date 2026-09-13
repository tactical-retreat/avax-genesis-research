"""
Address resolver for Avalanche multi-chain addresses.

Handles conversion between X/P/C chain address formats and provides
utilities for working with addresses across chains.
"""

import csv
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..models.address import AvaxAddress, Chain

logger = logging.getLogger(__name__)


@dataclass
class GenesisAllocation:
    """A genesis allocation record from the CSV."""
    address: AvaxAddress
    category: str
    initial_avax: float
    locked_avax: float
    total_avax: float
    is_initial_validator: bool
    node_id: str | None
    unlock_count: int
    first_unlock: str | None
    last_unlock: str | None
    eth_control_address: str | None


class AddressResolver:
    """
    Resolves and converts Avalanche addresses across chains.

    Also provides genesis allocation lookup.
    """

    def __init__(self, genesis_csv_path: str | Path | None = None):
        """
        Initialize the resolver.

        Args:
            genesis_csv_path: Path to all_allocations.csv for genesis lookup
        """
        self._genesis_by_address: dict[AvaxAddress, GenesisAllocation] = {}
        self._genesis_by_c_address: dict[str, GenesisAllocation] = {}
        self._genesis_by_category: dict[str, list[GenesisAllocation]] = {}

        if genesis_csv_path:
            self._load_genesis(Path(genesis_csv_path))

    def _load_genesis(self, csv_path: Path) -> None:
        """Load genesis allocations from CSV."""
        if not csv_path.exists():
            logger.warning(f"Genesis CSV not found: {csv_path}")
            return

        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    x_addr = row["x_address"].strip()
                    if not x_addr:
                        continue

                    addr = AvaxAddress.from_x_address(x_addr)
                    c_addr = addr.c_address

                    allocation = GenesisAllocation(
                        address=addr,
                        category=row.get("category", "").strip(),
                        initial_avax=float(row.get("initial_avax", 0) or 0),
                        locked_avax=float(row.get("locked_avax", 0) or 0),
                        total_avax=float(row.get("total_avax", 0) or 0),
                        is_initial_validator=row.get("is_initial_staker", "").lower() == "true",
                        node_id=row.get("node_id") or None,
                        unlock_count=int(row.get("unlock_count", 0) or 0),
                        first_unlock=row.get("first_unlock") or None,
                        last_unlock=row.get("last_unlock") or None,
                        eth_control_address=row.get("eth_control_address") or None,
                    )

                    self._genesis_by_address[addr] = allocation
                    self._genesis_by_c_address[c_addr] = allocation

                    if allocation.category:
                        if allocation.category not in self._genesis_by_category:
                            self._genesis_by_category[allocation.category] = []
                        self._genesis_by_category[allocation.category].append(allocation)

                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping invalid genesis row: {e}")
                    continue

        logger.info(f"Loaded {len(self._genesis_by_address)} genesis allocations")

    def get_genesis_allocation(self, address: AvaxAddress | str) -> GenesisAllocation | None:
        """
        Look up genesis allocation for an address.

        Args:
            address: Address in any format (AvaxAddress, C, X, or P format string)

        Returns:
            GenesisAllocation if found, None otherwise
        """
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        return self._genesis_by_address.get(address)

    def is_genesis_address(self, address: AvaxAddress | str) -> bool:
        """Check if an address received funds at genesis."""
        return self.get_genesis_allocation(address) is not None

    def get_genesis_category(self, address: AvaxAddress | str) -> str | None:
        """Get the genesis category for an address."""
        allocation = self.get_genesis_allocation(address)
        return allocation.category if allocation else None

    def get_addresses_by_category(self, category: str) -> list[GenesisAllocation]:
        """Get all genesis allocations for a category."""
        return self._genesis_by_category.get(category, [])

    def get_all_categories(self) -> list[str]:
        """Get all genesis allocation categories."""
        return list(self._genesis_by_category.keys())

    def iter_genesis_allocations(self) -> Iterator[GenesisAllocation]:
        """Iterate over all genesis allocations."""
        yield from self._genesis_by_address.values()

    @staticmethod
    def normalize_address(address: str) -> AvaxAddress:
        """
        Normalize any address format to AvaxAddress.

        Args:
            address: Address in any format

        Returns:
            Normalized AvaxAddress
        """
        return AvaxAddress.from_any(address)

    @staticmethod
    def are_same_address(addr1: str, addr2: str) -> bool:
        """
        Check if two addresses (in any format) represent the same underlying address.

        Args:
            addr1: First address
            addr2: Second address

        Returns:
            True if both represent the same address
        """
        try:
            a1 = AvaxAddress.from_any(addr1)
            a2 = AvaxAddress.from_any(addr2)
            return a1 == a2
        except ValueError:
            return False

    @staticmethod
    def convert_address(address: str, to_chain: Chain) -> str:
        """
        Convert an address to the format for a specific chain.

        Args:
            address: Address in any format
            to_chain: Target chain

        Returns:
            Address in the target chain's format
        """
        avax_addr = AvaxAddress.from_any(address)
        return avax_addr.for_chain(to_chain)

    @staticmethod
    def get_all_formats(address: str) -> dict[str, str]:
        """
        Get an address in all three formats.

        Args:
            address: Address in any format

        Returns:
            Dict with keys "C", "X", "P" mapping to formatted addresses
        """
        avax_addr = AvaxAddress.from_any(address)
        return {
            "C": avax_addr.c_address,
            "X": avax_addr.x_address,
            "P": avax_addr.p_address,
        }

    def genesis_summary(self) -> dict:
        """Get a summary of loaded genesis data."""
        category_totals: dict[str, dict] = {}
        for category, allocations in self._genesis_by_category.items():
            category_totals[category] = {
                "count": len(allocations),
                "total_avax": sum(a.total_avax for a in allocations),
                "validators": sum(1 for a in allocations if a.is_initial_validator),
            }

        return {
            "total_addresses": len(self._genesis_by_address),
            "categories": category_totals,
            "total_validators": sum(
                1 for a in self._genesis_by_address.values() if a.is_initial_validator
            ),
        }
