"""
Genesis address matcher.

Loads genesis allocation data and matches addresses to their original categories.
"""

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from ..models.address import AvaxAddress

logger = logging.getLogger(__name__)


@dataclass
class GenesisMatch:
    """Result of a genesis address lookup."""
    address: AvaxAddress
    category: str
    initial_avax: float
    locked_avax: float
    total_avax: float
    is_validator: bool
    node_id: str | None
    eth_control_address: str | None


class GenesisMatcher:
    """
    Matches addresses to genesis allocations.

    Loads data from all_allocations.csv and provides fast lookups.
    """

    def __init__(self, genesis_csv_path: str | Path | None = None):
        """
        Initialize the genesis matcher.

        Args:
            genesis_csv_path: Path to all_allocations.csv
                             Defaults to data/genesis/all_allocations.csv
                             (built by scripts/fetch_genesis.py)
        """
        if genesis_csv_path is None:
            # Default path relative to project root
            genesis_csv_path = Path("data/genesis/all_allocations.csv")

        self._by_raw_bytes: dict[bytes, GenesisMatch] = {}
        self._by_category: dict[str, list[GenesisMatch]] = {}
        self._categories: set[str] = set()

        self._load(Path(genesis_csv_path))

    def _load(self, csv_path: Path) -> None:
        """Load genesis data from CSV."""
        if not csv_path.exists():
            logger.warning(f"Genesis CSV not found: {csv_path}")
            return

        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    x_addr = row.get("x_address", "").strip()
                    if not x_addr:
                        continue

                    addr = AvaxAddress.from_x_address(x_addr)
                    category = row.get("category", "").strip()

                    match = GenesisMatch(
                        address=addr,
                        category=category,
                        initial_avax=float(row.get("initial_avax", 0) or 0),
                        locked_avax=float(row.get("locked_avax", 0) or 0),
                        total_avax=float(row.get("total_avax", 0) or 0),
                        is_validator=row.get("is_initial_staker", "").lower() == "true",
                        node_id=row.get("node_id") or None,
                        eth_control_address=row.get("eth_control_address") or None,
                    )

                    self._by_raw_bytes[addr.raw_bytes] = match
                    self._categories.add(category)

                    if category not in self._by_category:
                        self._by_category[category] = []
                    self._by_category[category].append(match)

                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping invalid genesis row: {e}")
                    continue

        logger.info(
            f"Loaded {len(self._by_raw_bytes)} genesis addresses "
            f"across {len(self._categories)} categories"
        )

    def match(self, address: AvaxAddress | str) -> GenesisMatch | None:
        """
        Look up an address in genesis data.

        Args:
            address: Address in any format

        Returns:
            GenesisMatch if found, None otherwise
        """
        if isinstance(address, str):
            try:
                address = AvaxAddress.from_any(address)
            except ValueError:
                return None

        if not address.is_primary:
            return None  # genesis allocations are P/X addresses
        return self._by_raw_bytes.get(address.raw_bytes)

    def is_genesis(self, address: AvaxAddress | str) -> bool:
        """Check if an address received funds at genesis."""
        return self.match(address) is not None

    def get_category(self, address: AvaxAddress | str) -> str | None:
        """Get the genesis category for an address."""
        match = self.match(address)
        return match.category if match else None

    def get_addresses_by_category(self, category: str) -> list[GenesisMatch]:
        """Get all genesis addresses in a category."""
        return self._by_category.get(category, [])

    def get_all_categories(self) -> list[str]:
        """Get all category names."""
        return list(self._categories)

    def get_validators(self) -> list[GenesisMatch]:
        """Get all initial validators."""
        return [m for m in self._by_raw_bytes.values() if m.is_validator]

    def summary(self) -> dict:
        """Get a summary of genesis allocations."""
        category_stats: dict[str, dict] = {}

        for category, matches in self._by_category.items():
            category_stats[category] = {
                "count": len(matches),
                "total_avax": sum(m.total_avax for m in matches),
                "validators": sum(1 for m in matches if m.is_validator),
            }

        total_avax = sum(m.total_avax for m in self._by_raw_bytes.values())
        total_validators = sum(1 for m in self._by_raw_bytes.values() if m.is_validator)

        return {
            "total_addresses": len(self._by_raw_bytes),
            "total_avax": total_avax,
            "total_validators": total_validators,
            "categories": category_stats,
        }

    def find_matching_addresses(
        self,
        addresses: list[str | AvaxAddress],
    ) -> list[tuple[AvaxAddress, GenesisMatch]]:
        """
        Find which addresses from a list are genesis addresses.

        Args:
            addresses: List of addresses to check

        Returns:
            List of (address, match) tuples for addresses that matched
        """
        matches = []
        for addr in addresses:
            if isinstance(addr, str):
                try:
                    addr = AvaxAddress.from_any(addr)
                except ValueError:
                    continue

            match = self.match(addr)
            if match:
                matches.append((addr, match))

        return matches
