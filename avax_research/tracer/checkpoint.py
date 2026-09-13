"""
Checkpoint serialization for restartable traces.

JSON-based format that preserves all state needed to resume a trace.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models.address import AddressKind, AvaxAddress

logger = logging.getLogger(__name__)


@dataclass
class GenesisAttribution:
    """Attribution of funds to a genesis source."""
    genesis_address: AvaxAddress
    category: str
    amount_avax: float
    path_length: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "genesis_address": self.genesis_address.raw_bytes.hex(),
            "category": self.category,
            "amount_avax": self.amount_avax,
            "path_length": self.path_length,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GenesisAttribution":
        """Deserialize from dict."""
        return cls(
            genesis_address=AvaxAddress(bytes.fromhex(d["genesis_address"]), AddressKind.PRIMARY),
            category=d["category"],
            amount_avax=d["amount_avax"],
            path_length=d["path_length"],
        )


@dataclass
class CChainDestination:
    """A C-chain address funded from genesis (always an EVM address, from an ImportTx output)."""
    address: AvaxAddress
    total_avax: float
    attributions: list[GenesisAttribution]
    import_tx_hashes: list[str]
    min_depth: int = 0
    max_depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "address": self.address.raw_bytes.hex(),
            "total_avax": self.total_avax,
            "attributions": [a.to_dict() for a in self.attributions],
            "import_tx_hashes": self.import_tx_hashes,
            "min_depth": self.min_depth,
            "max_depth": self.max_depth,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CChainDestination":
        """Deserialize from dict."""
        return cls(
            address=AvaxAddress(bytes.fromhex(d["address"]), AddressKind.EVM),
            total_avax=d["total_avax"],
            attributions=[GenesisAttribution.from_dict(a) for a in d["attributions"]],
            import_tx_hashes=d["import_tx_hashes"],
            min_depth=d.get("min_depth", 0),
            max_depth=d.get("max_depth", 0),
        )

    def categories_str(self) -> str:
        """Get comma-separated unique categories."""
        cats = sorted(set(a.category for a in self.attributions))
        return ", ".join(cats)


@dataclass
class QueueItem:
    """An item in the trace queue."""
    address: AvaxAddress
    amount_avax: float
    depth: int
    attributions: list[GenesisAttribution]
    chain: str = "P"  # Which chain the funds are on: "P" or "X"
    is_cached: bool = False  # Whether transactions are cached for this address

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "address": self.address.raw_bytes.hex(),
            "amount_avax": self.amount_avax,
            "depth": self.depth,
            "attributions": [a.to_dict() for a in self.attributions],
            "chain": self.chain,
            "is_cached": self.is_cached,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QueueItem":
        """Deserialize from dict."""
        return cls(
            address=AvaxAddress(bytes.fromhex(d["address"]), AddressKind.PRIMARY),  # queued on P or X
            amount_avax=d["amount_avax"],
            depth=d["depth"],
            attributions=[GenesisAttribution.from_dict(a) for a in d["attributions"]],
            chain=d.get("chain", "P"),
            is_cached=d.get("is_cached", False),
        )

    def sort_key(self) -> tuple[int, int, float, bytes]:
        """Key for deterministic sorting: (depth, not_cached, -amount, address_bytes).

        Cached items sort first (0 < 1), then by amount descending.
        """
        return (self.depth, 0 if self.is_cached else 1, -self.amount_avax, self.address.raw_bytes)


@dataclass
class GenesisTraceCheckpoint:
    """State for restartability."""
    strategy: str  # "bfs", "greedy", or "hybrid"
    current_depth: int
    queue: list[QueueItem]
    visited: set[bytes]
    destinations: dict[bytes, CChainDestination]
    timestamp: str
    # Additional metadata
    categories_filter: list[str] = field(default_factory=list)
    addresses_filter: list[str] = field(default_factory=list)
    max_depth: int = 20
    min_amount_avax: float = 0.0
    total_genesis_avax: float = 0.0
    total_exported_avax: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "strategy": self.strategy,
            "current_depth": self.current_depth,
            "queue": [item.to_dict() for item in self.queue],
            "visited": [b.hex() for b in self.visited],
            "destinations": {
                k.hex(): v.to_dict() for k, v in self.destinations.items()
            },
            "timestamp": self.timestamp,
            "categories_filter": self.categories_filter,
            "addresses_filter": self.addresses_filter,
            "max_depth": self.max_depth,
            "min_amount_avax": self.min_amount_avax,
            "total_genesis_avax": self.total_genesis_avax,
            "total_exported_avax": self.total_exported_avax,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GenesisTraceCheckpoint":
        """Deserialize from dict."""
        return cls(
            strategy=d["strategy"],
            current_depth=d["current_depth"],
            queue=[QueueItem.from_dict(item) for item in d["queue"]],
            visited={bytes.fromhex(h) for h in d["visited"]},
            destinations={
                bytes.fromhex(k): CChainDestination.from_dict(v)
                for k, v in d["destinations"].items()
            },
            timestamp=d["timestamp"],
            categories_filter=d.get("categories_filter", []),
            addresses_filter=d.get("addresses_filter", []),
            max_depth=d.get("max_depth", 20),
            min_amount_avax=d.get("min_amount_avax", 0.0),
            total_genesis_avax=d.get("total_genesis_avax", 0.0),
            total_exported_avax=d.get("total_exported_avax", 0.0),
        )

    def save(self, path: Path) -> None:
        """Save checkpoint to file."""
        self.timestamp = datetime.now().isoformat()
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info(f"Checkpoint saved to {path}: depth={self.current_depth}, "
                   f"visited={len(self.visited)}, destinations={len(self.destinations)}")

    @classmethod
    def load(cls, path: Path) -> "GenesisTraceCheckpoint":
        """Load checkpoint from file."""
        with open(path) as f:
            data = json.load(f)
        checkpoint = cls.from_dict(data)
        logger.info(f"Checkpoint loaded from {path}: depth={checkpoint.current_depth}, "
                   f"visited={len(checkpoint.visited)}, destinations={len(checkpoint.destinations)}")
        return checkpoint


def save_checkpoint(checkpoint: GenesisTraceCheckpoint, path: Path | str) -> None:
    """Save a checkpoint to a JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.save(path)


def load_checkpoint(path: Path | str) -> GenesisTraceCheckpoint | None:
    """Load a checkpoint from a JSON file, returns None if file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return None
    return GenesisTraceCheckpoint.load(path)
