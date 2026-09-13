"""
Cross-chain transaction linker.

Detects and links ExportTx/ImportTx pairs that move funds between chains.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from ..clients.glacier_client import TransactionRecord
from ..models.address import AvaxAddress, Chain

logger = logging.getLogger(__name__)


@dataclass
class CrossChainTransfer:
    """A linked pair of Export/Import transactions."""
    export_tx: TransactionRecord
    import_tx: TransactionRecord | None  # None if not yet found
    source_chain: Chain
    destination_chain: Chain
    addresses: list[AvaxAddress]
    amount_navax: int
    export_timestamp: datetime
    import_timestamp: datetime | None

    @property
    def amount_avax(self) -> float:
        return self.amount_navax / 1e9

    @property
    def is_complete(self) -> bool:
        return self.import_tx is not None


# Chain ID mappings from Glacier API
CHAIN_ID_MAP = {
    "11111111111111111111111111111111LpoYY": Chain.P,  # P-chain
    "2oYMBNV4eNHyqk2fjjV5nVQLDbtmNJzq5s3qs3Lo6ftnC6FByM": Chain.X,  # X-chain
    "2q9e4r6Mu3U68nU1fYjgbR6JvwrRx36CohpAX5UQxse55x1Q5": Chain.C,  # C-chain
    "p-chain": Chain.P,
    "x-chain": Chain.X,
    "c-chain": Chain.C,
}


def _parse_chain(chain_str: str | None) -> Chain | None:
    """Parse a chain string to Chain enum."""
    if not chain_str:
        return None
    chain_str_lower = chain_str.lower()
    if chain_str in CHAIN_ID_MAP:
        return CHAIN_ID_MAP[chain_str]
    if "p-chain" in chain_str_lower or chain_str_lower == "p":
        return Chain.P
    if "x-chain" in chain_str_lower or chain_str_lower == "x":
        return Chain.X
    if "c-chain" in chain_str_lower or chain_str_lower == "c":
        return Chain.C
    return None


class CrossChainLinker:
    """
    Links cross-chain transactions.

    Matches ExportTx on source chain with ImportTx on destination chain.
    """

    def __init__(self):
        # Pending exports waiting for their import match
        self._pending_exports: dict[str, TransactionRecord] = {}  # key = hash of identifying info
        self._linked: list[CrossChainTransfer] = []

    def is_export_tx(self, tx: TransactionRecord) -> bool:
        """Check if a transaction is an ExportTx."""
        return tx.tx_type in ("ExportTx", "AtomicExportTx")

    def is_import_tx(self, tx: TransactionRecord) -> bool:
        """Check if a transaction is an ImportTx."""
        return tx.tx_type in ("ImportTx", "AtomicImportTx")

    def is_cross_chain_tx(self, tx: TransactionRecord) -> bool:
        """Check if a transaction is a cross-chain transfer."""
        return tx.is_cross_chain or self.is_export_tx(tx) or self.is_import_tx(tx)

    def get_source_chain(self, tx: TransactionRecord) -> Chain | None:
        """Get the source chain of a cross-chain transaction."""
        if tx.source_chain:
            return _parse_chain(tx.source_chain)
        if self.is_export_tx(tx):
            return tx.chain
        return None

    def get_destination_chain(self, tx: TransactionRecord) -> Chain | None:
        """Get the destination chain of a cross-chain transaction."""
        if tx.destination_chain:
            return _parse_chain(tx.destination_chain)
        if self.is_import_tx(tx):
            return tx.chain
        return None

    def _make_link_key(
        self,
        addresses: list[str],
        amount: int,
        source: Chain,
        dest: Chain,
    ) -> str:
        """Create a key for matching export/import pairs."""
        # Sort addresses for consistent matching
        sorted_addrs = sorted(set(addresses))
        return f"{','.join(sorted_addrs)}:{amount}:{source.value}->{dest.value}"

    def process_transaction(self, tx: TransactionRecord) -> CrossChainTransfer | None:
        """
        Process a transaction, linking it if it's part of a cross-chain transfer.

        Args:
            tx: Transaction to process

        Returns:
            CrossChainTransfer if a link was completed, None otherwise
        """
        if not self.is_cross_chain_tx(tx):
            return None

        if self.is_export_tx(tx):
            return self._process_export(tx)
        elif self.is_import_tx(tx):
            return self._process_import(tx)

        return None

    def _process_export(self, tx: TransactionRecord) -> CrossChainTransfer | None:
        """Process an ExportTx."""
        dest_chain = self.get_destination_chain(tx)
        if not dest_chain:
            logger.warning(f"Export tx {tx.tx_hash} has no destination chain")
            return None

        # Store for later matching
        addresses = tx.from_addresses + tx.to_addresses
        key = self._make_link_key(addresses, tx.amount_navax, tx.chain, dest_chain)
        self._pending_exports[key] = tx

        logger.debug(f"Stored pending export: {tx.tx_hash} ({tx.chain.value} -> {dest_chain.value})")
        return None

    def _process_import(self, tx: TransactionRecord) -> CrossChainTransfer | None:
        """Process an ImportTx and try to match with a pending export."""
        source_chain = self.get_source_chain(tx)
        if not source_chain:
            logger.warning(f"Import tx {tx.tx_hash} has no source chain")
            return None

        # Try to find matching export
        addresses = tx.from_addresses + tx.to_addresses
        key = self._make_link_key(addresses, tx.amount_navax, source_chain, tx.chain)

        export_tx = self._pending_exports.pop(key, None)

        # Convert addresses to AvaxAddress
        avax_addresses = []
        for addr in set(addresses):
            try:
                avax_addresses.append(AvaxAddress.from_any(addr))
            except ValueError:
                continue

        if export_tx:
            # Found a match
            transfer = CrossChainTransfer(
                export_tx=export_tx,
                import_tx=tx,
                source_chain=source_chain,
                destination_chain=tx.chain,
                addresses=avax_addresses,
                amount_navax=tx.amount_navax,
                export_timestamp=export_tx.timestamp,
                import_timestamp=tx.timestamp,
            )
            self._linked.append(transfer)
            logger.debug(f"Linked cross-chain transfer: {export_tx.tx_hash} -> {tx.tx_hash}")
            return transfer

        # No match found - create incomplete transfer
        transfer = CrossChainTransfer(
            export_tx=tx,  # Use import as placeholder
            import_tx=tx,
            source_chain=source_chain,
            destination_chain=tx.chain,
            addresses=avax_addresses,
            amount_navax=tx.amount_navax,
            export_timestamp=tx.timestamp,  # Approximate
            import_timestamp=tx.timestamp,
        )
        self._linked.append(transfer)
        return transfer

    def get_linked_transfers(self) -> list[CrossChainTransfer]:
        """Get all linked cross-chain transfers."""
        return self._linked

    def get_pending_exports(self) -> list[TransactionRecord]:
        """Get exports that haven't been matched with imports."""
        return list(self._pending_exports.values())

    def find_bridge_path(
        self,
        address: AvaxAddress,
        transactions: list[TransactionRecord],
    ) -> list[CrossChainTransfer]:
        """
        Find all cross-chain transfers involving an address.

        Args:
            address: Address to trace
            transactions: List of transactions to analyze

        Returns:
            List of cross-chain transfers involving the address
        """
        address_strs = address.match_strings

        transfers = []
        for tx in transactions:
            if not self.is_cross_chain_tx(tx):
                continue

            # Check if address is involved
            tx_addresses = set(a.lower() for a in tx.from_addresses + tx.to_addresses)
            if tx_addresses & address_strs:
                transfer = self.process_transaction(tx)
                if transfer:
                    transfers.append(transfer)

        return transfers

    def clear(self) -> None:
        """Clear all state."""
        self._pending_exports.clear()
        self._linked.clear()
