"""
Greedy tracer for fund flow analysis.

Traces funds backward from target addresses to their sources,
using a largest-first (greedy) algorithm to follow the biggest
money trails first.

Supports two modes:
- trace_source: Trace backward to find funding sources (default)
- find_related_destinations: Find other addresses funded by the same sources
"""

import heapq
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ..clients.glacier_client import GlacierClient, TransactionRecord
from ..models.address import AvaxAddress, Chain
from ..models.graph import GraphEdge, GraphNode, TraceGraph, TransactionType
from .cross_chain_linker import CrossChainLinker
from .genesis_matcher import GenesisMatch, GenesisMatcher

logger = logging.getLogger(__name__)

# Default transaction types for value transfers only (excluding staking)
DEFAULT_VALUE_TRANSFER_TYPES = ["BaseTx", "ExportTx", "ImportTx"]

# All staking-related transaction types
STAKING_TX_TYPES = [
    "AddValidatorTx",
    "AddDelegatorTx",
    "AddSubnetValidatorTx",
    "AddPermissionlessDelegatorTx",
    "AddPermissionlessValidatorTx",
    "RewardValidatorTx",
    "CreateChainTx",
    "CreateSubnetTx",
]


@dataclass
class CrossChainTx:
    """Record of an import/export transaction seen during trace."""
    tx_hash: str
    tx_type: str  # "ImportTx" or "ExportTx"
    source_chain: str  # P, X, or C
    dest_chain: str  # P, X, or C
    source_address: str  # Sender address (chain-appropriate format)
    dest_address: str  # Receiver address (chain-appropriate format)
    amount_avax: float
    timestamp: datetime
    block_height: int | None = None


@dataclass
class TraceConfig:
    """Configuration for greedy tracing."""
    max_depth: int = 10
    min_amount_avax: float = 0.0  # Minimum amount to follow
    include_staking: bool = False  # Include staking transactions (default: False for value transfers only)
    include_cross_chain: bool = True  # Follow cross-chain transfers
    stop_at_genesis: bool = True  # Stop tracing when genesis is reached
    chains: list[Chain] = field(default_factory=lambda: [Chain.C, Chain.P, Chain.X])

    # Transaction type filtering - defaults to value transfers only
    tx_types: list[str] | None = None  # If None, uses DEFAULT_VALUE_TRANSFER_TYPES when include_staking=False

    # Rate limiting
    max_addresses_per_depth: int = 1000  # Prevent explosion
    max_total_addresses: int = 10000

    def get_tx_types(self) -> list[str] | None:
        """Get the transaction types to filter by."""
        if self.tx_types is not None:
            return self.tx_types
        if not self.include_staking:
            return DEFAULT_VALUE_TRANSFER_TYPES
        return None  # All types


@dataclass
class TraceResult:
    """Result of a greedy trace."""
    graph: TraceGraph
    starting_addresses: list[AvaxAddress]
    genesis_matches: list[tuple[AvaxAddress, GenesisMatch]]
    max_depth_reached: int
    addresses_visited: int
    transactions_processed: int
    errors: list[str]

    def summary(self) -> dict:
        """Get a summary of the trace result."""
        return {
            "starting_addresses": len(self.starting_addresses),
            "addresses_visited": self.addresses_visited,
            "transactions_processed": self.transactions_processed,
            "max_depth_reached": self.max_depth_reached,
            "genesis_matches": len(self.genesis_matches),
            "genesis_categories": {
                cat: sum(1 for _, m in self.genesis_matches if m.category == cat)
                for cat in set(m.category for _, m in self.genesis_matches)
            },
            "errors": len(self.errors),
            **self.graph.summary(),
        }


@dataclass
class RelatedDestination:
    """A destination address that shares funding sources with the target."""
    address: AvaxAddress
    total_amount_avax: float
    shared_sources: list[AvaxAddress]  # Source addresses in common
    path_length: int  # Hops from source to this destination


@dataclass
class RelatedDestinationsResult:
    """Result of finding related destinations."""
    starting_address: AvaxAddress
    funding_sources: list[tuple[AvaxAddress, float]]  # (source, amount)
    related_destinations: list[RelatedDestination]
    source_graph: TraceGraph  # Upward trace to sources
    destination_graph: TraceGraph  # Downward trace from sources
    errors: list[str]

    def summary(self) -> dict:
        """Get a summary of the related destinations result."""
        return {
            "starting_address": self.starting_address.c_address,
            "starting_addresses": 1,  # For compatibility with report templates
            "funding_sources_found": len(self.funding_sources),
            "related_destinations_found": len(self.related_destinations),
            "source_graph_nodes": len(self.source_graph.nodes),
            "destination_graph_nodes": len(self.destination_graph.nodes),
            "addresses_visited": len(self.source_graph.nodes) + len(self.destination_graph.nodes),
            "transactions_processed": len(self.source_graph.all_edges) + len(self.destination_graph.all_edges),
            "max_depth_reached": 0,  # Not tracked for related mode
            "genesis_matches": 0,
            "total_nodes": len(self.source_graph.nodes),
            "total_edges": len(self.source_graph.all_edges),
            "errors": len(self.errors),
        }


@dataclass
class DestinationInfo:
    """Information about a destination address in forward tracing."""
    address: AvaxAddress
    total_received_avax: float  # Total received from the source (directly or transitively)
    total_sent_back_avax: float  # Total sent back toward the source
    net_received_avax: float  # Net = received - sent_back
    current_balance_avax: float | None  # Current C-chain balance (None if not fetched)
    depth: int  # Hops from the source
    chains_used: set[str]  # Which chains were used to reach this address


@dataclass
class TraceDestinationsResult:
    """Result of tracing forward from a source to destinations."""
    starting_address: AvaxAddress
    destinations: list[DestinationInfo]
    graph: TraceGraph
    total_outflow_avax: float  # Total sent out from starting address
    c_chain_destinations: list[DestinationInfo]  # Filtered to C-chain only
    addresses_visited: int
    transactions_processed: int
    errors: list[str]
    cross_chain_txs: list[CrossChainTx] = field(default_factory=list)  # All import/export txs seen

    def summary(self) -> dict:
        """Get a summary of the destinations result."""
        return {
            "starting_address": self.starting_address.c_address,
            "starting_addresses": 1,  # For compatibility with report templates
            "total_destinations": len(self.destinations),
            "c_chain_destinations": len(self.c_chain_destinations),
            "total_outflow_avax": self.total_outflow_avax,
            "addresses_visited": self.addresses_visited,
            "transactions_processed": self.transactions_processed,
            "max_depth_reached": max((d.depth for d in self.destinations), default=0),
            "genesis_matches": 0,  # Forward trace doesn't find genesis
            "total_nodes": len(self.graph.nodes),
            "total_edges": len(self.graph.all_edges),
            "errors": len(self.errors),
        }


def _tx_type_to_enum(tx_type: str) -> TransactionType:
    """Convert transaction type string to enum."""
    type_map = {
        "AddValidatorTx": TransactionType.ADD_VALIDATOR,
        "AddDelegatorTx": TransactionType.ADD_DELEGATOR,
        "AddSubnetValidatorTx": TransactionType.ADD_SUBNET_VALIDATOR,
        "CreateChainTx": TransactionType.CREATE_CHAIN,
        "CreateSubnetTx": TransactionType.CREATE_SUBNET,
        "RewardValidatorTx": TransactionType.REWARD_VALIDATOR,
        "BaseTx": TransactionType.BASE,
        "CreateAssetTx": TransactionType.CREATE_ASSET,
        "OperationTx": TransactionType.OPERATION,
        "ImportTx": TransactionType.IMPORT,
        "ExportTx": TransactionType.EXPORT,
        "EvmTransfer": TransactionType.EVM_TRANSFER,
        "AtomicImportTx": TransactionType.ATOMIC_IMPORT,
        "AtomicExportTx": TransactionType.ATOMIC_EXPORT,
    }
    return type_map.get(tx_type, TransactionType.UNKNOWN)


class BFSTracer:
    """
    Greedy largest-first tracer for fund flows.

    Uses a priority queue to always process the largest transfers first,
    regardless of depth. This ensures we follow the main money trail
    rather than getting lost in small change.

    Traces funds backward from target addresses through:
    - Native transfers on C-chain
    - UTXO transfers on P/X chains (with change address filtering)
    - Cross-chain bridges (Export/Import)
    - Staking rewards (optional, disabled by default)

    Supports two modes:
    - trace(): Trace backward to find funding sources
    - find_related_destinations(): Find other addresses funded by the same sources
    """

    def __init__(
        self,
        glacier_client: GlacierClient | None = None,
        genesis_matcher: GenesisMatcher | None = None,
    ):
        """
        Initialize the tracer.

        Args:
            glacier_client: Glacier API client (creates new one if None)
            genesis_matcher: Genesis matcher (creates new one if None)
        """
        self.glacier = glacier_client or GlacierClient()
        self.genesis = genesis_matcher or GenesisMatcher()
        self.linker = CrossChainLinker()

    def trace(
        self,
        addresses: list[str | AvaxAddress],
        config: TraceConfig | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> TraceResult:
        """
        Trace funds backward from the given addresses using greedy largest-first algorithm.

        This uses a priority queue to always process the largest transfers first,
        ensuring we follow the main money trail rather than small change.

        Args:
            addresses: Starting addresses to trace from
            config: Trace configuration
            progress_callback: Optional callback(depth, count, message)

        Returns:
            TraceResult with the complete graph and findings
        """
        config = config or TraceConfig()

        # Normalize addresses
        start_addrs = []
        for addr in addresses:
            if isinstance(addr, str):
                try:
                    start_addrs.append(AvaxAddress.from_any(addr))
                except ValueError as e:
                    logger.warning(f"Invalid address: {addr} - {e}")
            else:
                start_addrs.append(addr)

        if not start_addrs:
            return TraceResult(
                graph=TraceGraph(),
                starting_addresses=[],
                genesis_matches=[],
                max_depth_reached=0,
                addresses_visited=0,
                transactions_processed=0,
                errors=["No valid addresses provided"],
            )

        graph = TraceGraph()
        visited: set[bytes] = set()  # Track by raw bytes for efficiency
        genesis_matches: list[tuple[AvaxAddress, GenesisMatch]] = []
        errors: list[str] = []
        tx_count = 0
        max_depth = 0

        # Priority queue: (-amount_avax, depth, counter, address)
        # Using negative amount for max-heap behavior (largest first)
        # Counter breaks ties to ensure consistent ordering
        counter = 0
        queue: list[tuple[float, int, int, AvaxAddress]] = []

        for addr in start_addrs:
            # Start with infinite priority (will be processed first)
            heapq.heappush(queue, (float('-inf'), 0, counter, addr))
            counter += 1
            node = GraphNode(address=addr, depth=0)
            graph.add_node(node)

            # Check if starting address is genesis
            match = self.genesis.match(addr)
            if match:
                node.genesis_category = match.category
                node.genesis_amount_avax = match.total_avax
                node.is_initial_validator = match.is_validator
                node.node_id = match.node_id
                genesis_matches.append((addr, match))

        # Greedy loop - always process largest amount first
        while queue:
            neg_amount, depth, _, current_addr = heapq.heappop(queue)
            amount = -neg_amount if neg_amount != float('-inf') else 0

            # Skip if already visited
            if current_addr.raw_bytes in visited:
                continue
            visited.add(current_addr.raw_bytes)

            if progress_callback:
                amt_str = f"{amount:.2f} AVAX" if amount > 0 else "start"
                progress_callback(depth, len(visited), f"Processing {current_addr.c_address[:10]}... ({amt_str})")

            # Check depth limit
            if depth >= config.max_depth:
                continue
            max_depth = max(max_depth, depth)

            # Check address limit
            if len(visited) >= config.max_total_addresses:
                errors.append(f"Hit max address limit ({config.max_total_addresses})")
                break

            # Check if genesis - don't trace further
            if config.stop_at_genesis and self.genesis.is_genesis(current_addr):
                continue

            # Get incoming transactions for this address (with amounts)
            try:
                incoming_with_amounts = self._get_incoming_addresses_with_amounts(
                    current_addr, config, graph
                )
                tx_count += len(graph.get_incoming_edges(current_addr))

                # Check depth limit per level
                if len(incoming_with_amounts) > config.max_addresses_per_depth:
                    # Sort by amount and take largest
                    incoming_with_amounts.sort(key=lambda x: x[1], reverse=True)
                    incoming_with_amounts = incoming_with_amounts[:config.max_addresses_per_depth]
                    errors.append(
                        f"Truncated incoming addresses at depth {depth} "
                        f"for {current_addr.c_address}"
                    )

                # Add new addresses to priority queue (sorted by amount)
                for addr, addr_amount in incoming_with_amounts:
                    if addr.raw_bytes not in visited:
                        heapq.heappush(queue, (-addr_amount, depth + 1, counter, addr))
                        counter += 1

                        # Check for genesis
                        match = self.genesis.match(addr)
                        if match:
                            node = graph.get_or_create_node(addr)
                            node.genesis_category = match.category
                            node.genesis_amount_avax = match.total_avax
                            node.is_initial_validator = match.is_validator
                            node.node_id = match.node_id
                            genesis_matches.append((addr, match))

            except Exception as e:
                logger.error(f"Error tracing {current_addr.c_address}: {e}")
                errors.append(f"Error at {current_addr.c_address}: {str(e)}")

        # Update node depths
        for addr, d in self._calculate_depths(graph, start_addrs):
            if addr in graph.nodes:
                graph.nodes[addr].depth = d

        return TraceResult(
            graph=graph,
            starting_addresses=start_addrs,
            genesis_matches=genesis_matches,
            max_depth_reached=max_depth,
            addresses_visited=len(visited),
            transactions_processed=tx_count,
            errors=errors,
        )

    def _get_incoming_addresses_with_amounts(
        self,
        address: AvaxAddress,
        config: TraceConfig,
        graph: TraceGraph,
    ) -> list[tuple[AvaxAddress, float]]:
        """
        Get addresses that sent funds to this address, with amounts.

        Returns list of (address, total_amount_avax) tuples, aggregated by sender.
        """
        # Aggregate amounts by sender address
        incoming_amounts: dict[bytes, tuple[AvaxAddress, float]] = {}

        def add_incoming(addr: AvaxAddress, amount: float) -> None:
            """Add or aggregate incoming amount for an address."""
            if addr.raw_bytes in incoming_amounts:
                existing_addr, existing_amount = incoming_amounts[addr.raw_bytes]
                incoming_amounts[addr.raw_bytes] = (existing_addr, existing_amount + amount)
            else:
                incoming_amounts[addr.raw_bytes] = (addr, amount)

        tx_types = config.get_tx_types()

        # Query each configured chain
        if Chain.C in config.chains:
            try:
                # Native EVM transfers
                for tx in self.glacier.get_c_chain_native_transactions(address, max_pages=10):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    sources = self._add_tx_to_graph_with_amounts(tx, address, graph)
                    for src, amt in sources:
                        add_incoming(src, amt)

                # Atomic transactions (ImportTx/ExportTx to/from C-chain)
                for tx in self.glacier.get_c_chain_atomic_transactions(address, max_pages=10):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    sources = self._add_tx_to_graph_with_amounts(tx, address, graph)
                    for src, amt in sources:
                        add_incoming(src, amt)
            except Exception as e:
                logger.warning(f"Error getting C-chain txs for {address.c_address}: {e}")

        if Chain.P in config.chains:
            try:
                for tx in self.glacier.get_p_chain_transactions(
                    address, tx_types=tx_types, max_pages=10
                ):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    sources = self._add_tx_to_graph_with_amounts(tx, address, graph)
                    for src, amt in sources:
                        add_incoming(src, amt)

                    # Handle cross-chain
                    if config.include_cross_chain and self.linker.is_cross_chain_tx(tx):
                        self.linker.process_transaction(tx)

            except Exception as e:
                logger.warning(f"Error getting P-chain txs for {address.p_address}: {e}")

        if Chain.X in config.chains:
            try:
                for tx in self.glacier.get_x_chain_transactions(
                    address, tx_types=tx_types, max_pages=10
                ):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    sources = self._add_tx_to_graph_with_amounts(tx, address, graph)
                    for src, amt in sources:
                        add_incoming(src, amt)

                    if config.include_cross_chain and self.linker.is_cross_chain_tx(tx):
                        self.linker.process_transaction(tx)

            except Exception as e:
                logger.warning(f"Error getting X-chain txs for {address.x_address}: {e}")

        return list(incoming_amounts.values())

    def _add_tx_to_graph_with_amounts(
        self,
        tx: TransactionRecord,
        target_addr: AvaxAddress,
        graph: TraceGraph,
    ) -> list[tuple[AvaxAddress, float]]:
        """
        Add a transaction to the graph and return incoming addresses with amounts.

        Returns list of (source_address, amount_avax) tuples.
        """
        result: list[tuple[AvaxAddress, float]] = []

        # Include all address formats for matching, including non-prefixed bech32
        # (API returns addresses without X-/P- prefix like "avax1...")
        bech32_no_prefix = target_addr.x_address.lower().replace("x-", "")
        target_strs = {
            target_addr.c_address.lower(),
            target_addr.x_address.lower(),
            target_addr.p_address.lower(),
            bech32_no_prefix,  # avax1... format without chain prefix
        }

        # Determine if this transaction sends TO our target
        # (meaning funds came FROM the from_addresses)
        to_addrs_lower = {a.lower() for a in tx.to_addresses}

        if target_strs & to_addrs_lower:
            # Target received funds - add senders
            for from_addr in tx.from_addresses:
                try:
                    source = AvaxAddress.from_any(from_addr)

                    # Don't add self-transfers
                    if source.raw_bytes == target_addr.raw_bytes:
                        continue

                    edge = GraphEdge(
                        source=source,
                        target=target_addr,
                        chain=tx.chain,
                        tx_hash=tx.tx_hash,
                        amount_navax=tx.amount_navax,
                        timestamp=tx.timestamp,
                        tx_type=_tx_type_to_enum(tx.tx_type),
                        block_height=tx.block_number,
                    )

                    # Set cross-chain info
                    if tx.is_cross_chain:
                        edge.linked_chain = self.linker.get_source_chain(tx)

                    graph.add_edge(edge)
                    result.append((source, tx.amount_avax))

                except ValueError:
                    continue

        return result

    def _calculate_depths(
        self,
        graph: TraceGraph,
        start_addrs: list[AvaxAddress],
    ) -> list[tuple[AvaxAddress, int]]:
        """Calculate BFS depth for each node from starting addresses."""
        depths: dict[AvaxAddress, int] = {}
        queue = deque((addr, 0) for addr in start_addrs)

        while queue:
            addr, depth = queue.popleft()
            if addr in depths:
                continue
            depths[addr] = depth

            # Add sources (going backward)
            for source in graph.get_sources(addr):
                if source not in depths:
                    queue.append((source, depth + 1))

        return list(depths.items())

    def trace_single(
        self,
        address: str | AvaxAddress,
        max_depth: int = 10,
    ) -> TraceResult:
        """
        Convenience method to trace a single address.

        Args:
            address: Address to trace
            max_depth: Maximum depth to trace

        Returns:
            TraceResult
        """
        return self.trace([address], TraceConfig(max_depth=max_depth))

    def find_related_destinations(
        self,
        address: str | AvaxAddress,
        config: TraceConfig | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> RelatedDestinationsResult:
        """
        Find other C-chain addresses funded by the same sources.

        This method:
        1. Traces upward from the target address to find major P/X chain funding sources
        2. From those sources, traces forward/downward to find other C-chain destinations
        3. Returns a list of related addresses that share funding sources

        Args:
            address: Target address to find relatives of
            config: Trace configuration (uses defaults if None)
            progress_callback: Optional callback(depth, count, message)

        Returns:
            RelatedDestinationsResult with funding sources and related destinations
        """
        config = config or TraceConfig()
        errors: list[str] = []

        # Normalize address
        if isinstance(address, str):
            try:
                target = AvaxAddress.from_any(address)
            except ValueError as e:
                return RelatedDestinationsResult(
                    starting_address=AvaxAddress.from_any("0x" + "0" * 40),  # Dummy
                    funding_sources=[],
                    related_destinations=[],
                    source_graph=TraceGraph(),
                    destination_graph=TraceGraph(),
                    errors=[f"Invalid address: {address} - {e}"],
                )
        else:
            target = address

        # Phase 1: Trace upward to find funding sources
        if progress_callback:
            progress_callback(0, 0, "Phase 1: Tracing to funding sources...")

        source_result = self.trace([target], config, progress_callback)
        errors.extend(source_result.errors)

        # Identify major funding sources (P/X chain addresses with significant outflow)
        # We look for addresses that sent significant funds toward our target
        funding_sources: list[tuple[AvaxAddress, float]] = []
        source_addrs_seen: set[bytes] = set()

        # Walk the graph to find sources with large transfers
        for edge in source_result.graph.all_edges:
            # Only consider P/X chain sources (where cross-chain usually originates)
            if edge.chain in (Chain.P, Chain.X):
                if edge.source.raw_bytes not in source_addrs_seen:
                    # Calculate total outflow from this source in our graph
                    total_out = sum(
                        e.amount_avax
                        for e in source_result.graph.get_outgoing_edges(edge.source)
                    )
                    if total_out >= config.min_amount_avax:
                        funding_sources.append((edge.source, total_out))
                        source_addrs_seen.add(edge.source.raw_bytes)

        # Sort by amount (largest first)
        funding_sources.sort(key=lambda x: x[1], reverse=True)

        # Limit to top sources to avoid explosion
        max_sources = min(50, len(funding_sources))
        funding_sources = funding_sources[:max_sources]

        if not funding_sources:
            return RelatedDestinationsResult(
                starting_address=target,
                funding_sources=[],
                related_destinations=[],
                source_graph=source_result.graph,
                destination_graph=TraceGraph(),
                errors=errors + ["No P/X chain funding sources found"],
            )

        # Phase 2: Trace forward from funding sources to find destinations
        if progress_callback:
            progress_callback(0, 0, f"Phase 2: Tracing from {len(funding_sources)} sources...")

        destination_graph = TraceGraph()
        related_destinations: list[RelatedDestination] = []
        dest_addrs_seen: set[bytes] = {target.raw_bytes}  # Exclude the target itself

        # For each funding source, find other destinations
        for source_addr, source_amount in funding_sources:
            try:
                # Get outgoing transactions from this source
                outgoing = self._get_outgoing_addresses_with_amounts(
                    source_addr, config, destination_graph
                )

                for dest_addr, dest_amount in outgoing:
                    if dest_addr.raw_bytes not in dest_addrs_seen:
                        dest_addrs_seen.add(dest_addr.raw_bytes)

                        # Find all shared sources for this destination
                        shared = [source_addr]  # At least this one

                        related_destinations.append(RelatedDestination(
                            address=dest_addr,
                            total_amount_avax=dest_amount,
                            shared_sources=shared,
                            path_length=1,  # Direct from source
                        ))

            except Exception as e:
                logger.warning(f"Error getting destinations from {source_addr.c_address}: {e}")
                errors.append(f"Error tracing from source {source_addr.c_address}: {str(e)}")

        # Sort related destinations by amount
        related_destinations.sort(key=lambda x: x.total_amount_avax, reverse=True)

        return RelatedDestinationsResult(
            starting_address=target,
            funding_sources=funding_sources,
            related_destinations=related_destinations,
            source_graph=source_result.graph,
            destination_graph=destination_graph,
            errors=errors,
        )

    def _get_outgoing_addresses_with_amounts(
        self,
        address: AvaxAddress,
        config: TraceConfig,
        graph: TraceGraph,
        c_chain_export_amounts: dict[bytes, float] | None = None,
        cross_chain_txs: list[CrossChainTx] | None = None,
        max_export_amount: float | None = None,
    ) -> list[tuple[AvaxAddress, float]]:
        """
        Get addresses that received funds from this address, with amounts.

        Args:
            address: Source address
            config: Trace configuration
            graph: Graph to add edges to
            c_chain_export_amounts: Optional dict to track amounts received via C-chain exports (addr_bytes -> amount)
            cross_chain_txs: Optional list to collect import/export transactions
            max_export_amount: Cap on how much to attribute to C-chain exports (limits "leaked" amounts)

        Returns list of (address, total_amount_avax) tuples, aggregated by recipient.
        """
        outgoing_amounts: dict[bytes, tuple[AvaxAddress, float]] = {}

        # C-chain blockchain ID
        C_CHAIN_ID = "2q9e4r6Mu3U68nU1fYjgbR6JvwrRx36CohpAX5UQxse55x1Q5"

        # Track total attributed to C-chain exports from this address
        total_c_chain_exported: float = 0.0

        def add_outgoing(addr: AvaxAddress, amount: float, c_chain_export_amount: float = 0.0) -> None:
            nonlocal total_c_chain_exported
            if addr.raw_bytes in outgoing_amounts:
                existing_addr, existing_amount = outgoing_amounts[addr.raw_bytes]
                outgoing_amounts[addr.raw_bytes] = (existing_addr, existing_amount + amount)
            else:
                outgoing_amounts[addr.raw_bytes] = (addr, amount)
            # Track C-chain export amounts (accumulate actual cross-chain received amounts)
            # But cap based on max_export_amount to avoid attributing more than flowed through trace
            if c_chain_export_amount > 0 and c_chain_export_amounts is not None:
                # Cap the amount we can attribute
                if max_export_amount is not None:
                    remaining_budget = max(0.0, max_export_amount - total_c_chain_exported)
                    capped_amount = min(c_chain_export_amount, remaining_budget)
                    if capped_amount > 0:
                        c_chain_export_amounts[addr.raw_bytes] = (
                            c_chain_export_amounts.get(addr.raw_bytes, 0.0) + capped_amount
                        )
                        total_c_chain_exported += capped_amount
                else:
                    c_chain_export_amounts[addr.raw_bytes] = (
                        c_chain_export_amounts.get(addr.raw_bytes, 0.0) + c_chain_export_amount
                    )

        tx_types = config.get_tx_types()

        # Query each configured chain
        if Chain.C in config.chains:
            try:
                for tx in self.glacier.get_c_chain_native_transactions(address, max_pages=10):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    targets = self._add_outgoing_tx_to_graph(tx, address, graph)
                    for tgt, amt in targets:
                        add_outgoing(tgt, amt)
            except Exception as e:
                logger.warning(f"Error getting C-chain txs for {address.c_address}: {e}")

        if Chain.P in config.chains:
            try:
                for tx in self.glacier.get_p_chain_transactions(
                    address, tx_types=tx_types, max_pages=10
                ):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    targets = self._add_outgoing_tx_to_graph(tx, address, graph)

                    # Check if this is an ExportTx to C-chain
                    is_c_export = (
                        tx.tx_type == "ExportTx" and
                        tx.destination_chain == C_CHAIN_ID
                    )

                    # Collect cross-chain transactions
                    if tx.tx_type in ("ExportTx", "ImportTx") and cross_chain_txs is not None:
                        for tgt, amt in targets:
                            cross_chain_txs.append(CrossChainTx(
                                tx_hash=tx.tx_hash,
                                tx_type=tx.tx_type,
                                source_chain="P",
                                dest_chain=tx.destination_chain[:1] if tx.destination_chain else "P",
                                source_address=address.p_address,
                                dest_address=tgt.p_address if tx.tx_type == "ExportTx" else tgt.c_address,
                                amount_avax=amt,
                                timestamp=tx.timestamp,
                                block_height=tx.block_number,
                            ))

                    if is_c_export and c_chain_export_amounts is not None:
                        # For C-chain exports, find the actual C-chain destinations
                        # by looking up the ImportTx on C-chain
                        actual_c_destinations = self._find_c_chain_import_destinations(
                            tx, targets, cross_chain_txs
                        )
                        for c_dest, amt in actual_c_destinations:
                            # Pass the actual cross-chain amount as c_chain_export_amount
                            add_outgoing(c_dest, amt, c_chain_export_amount=amt)
                        # Don't add the intermediate P-chain targets for C-chain exports
                    else:
                        for tgt, amt in targets:
                            add_outgoing(tgt, amt)

            except Exception as e:
                logger.warning(f"Error getting P-chain txs for {address.p_address}: {e}")

        if Chain.X in config.chains:
            try:
                for tx in self.glacier.get_x_chain_transactions(
                    address, tx_types=tx_types, max_pages=10
                ):
                    if tx.amount_avax < config.min_amount_avax:
                        continue
                    targets = self._add_outgoing_tx_to_graph(tx, address, graph)

                    # Check if this is an ExportTx to C-chain
                    is_c_export = (
                        tx.tx_type == "ExportTx" and
                        tx.destination_chain == C_CHAIN_ID
                    )

                    # Collect cross-chain transactions
                    if tx.tx_type in ("ExportTx", "ImportTx") and cross_chain_txs is not None:
                        for tgt, amt in targets:
                            cross_chain_txs.append(CrossChainTx(
                                tx_hash=tx.tx_hash,
                                tx_type=tx.tx_type,
                                source_chain="X",
                                dest_chain=tx.destination_chain[:1] if tx.destination_chain else "X",
                                source_address=address.x_address,
                                dest_address=tgt.x_address if tx.tx_type == "ExportTx" else tgt.c_address,
                                amount_avax=amt,
                                timestamp=tx.timestamp,
                                block_height=tx.block_number,
                            ))

                    if is_c_export and c_chain_export_amounts is not None:
                        # For C-chain exports, find the actual C-chain destinations
                        # by looking up the ImportTx on C-chain
                        actual_c_destinations = self._find_c_chain_import_destinations(
                            tx, targets, cross_chain_txs
                        )
                        for c_dest, amt in actual_c_destinations:
                            # Pass the actual cross-chain amount as c_chain_export_amount
                            add_outgoing(c_dest, amt, c_chain_export_amount=amt)
                        # Don't add the intermediate X-chain targets for C-chain exports
                    else:
                        for tgt, amt in targets:
                            add_outgoing(tgt, amt)

            except Exception as e:
                logger.warning(f"Error getting X-chain txs for {address.x_address}: {e}")

        return list(outgoing_amounts.values())

    def _find_c_chain_import_destinations(
        self,
        export_tx: TransactionRecord,
        intermediate_targets: list[tuple[AvaxAddress, float]],
        cross_chain_txs: list[CrossChainTx] | None = None,
    ) -> list[tuple[AvaxAddress, float]]:
        """
        Find actual C-chain destinations for an ExportTx by looking up ImportTx.

        When funds are exported from X/P chain to C-chain:
        1. ExportTx creates UTXOs on X/P chain
        2. ImportTx on C-chain consumes those UTXOs
        3. ImportTx specifies actual C-chain destination in evmOutputs

        Args:
            export_tx: The ExportTx transaction
            intermediate_targets: Targets from the ExportTx (not actual C-chain destinations)

        Returns:
            List of (actual_c_chain_address, amount) tuples
        """
        result: list[tuple[AvaxAddress, float]] = []

        # For each intermediate target (X/P chain address that received the export UTXO),
        # query C-chain atomic transactions to find the ImportTx
        # NOTE: We must query using the bech32 format (avax1...) not the C-chain hex format
        for target_addr, amount in intermediate_targets:
            try:
                # Query C-chain atomic transactions using bech32 format
                # The API indexes by the bech32 addresses in consumedUtxos
                bech32_addr = target_addr.x_address.replace("X-", "")  # avax1...
                for atomic_tx in self.glacier.get_c_chain_atomic_transactions(
                    bech32_addr, tx_types=["ImportTx"], max_pages=5
                ):
                    # Check if this ImportTx consumed UTXOs from our ExportTx
                    # by checking if the source address matches
                    if not atomic_tx.from_addresses:
                        continue

                    # Check if the ImportTx source matches our intermediate target
                    target_strs = {
                        target_addr.c_address.lower(),
                        target_addr.x_address.lower(),
                        target_addr.p_address.lower(),
                        target_addr.x_address.lower().replace("x-", ""),
                    }
                    from_strs = {a.lower() for a in atomic_tx.from_addresses}

                    if target_strs & from_strs:
                        # Verify timing: ImportTx should be after ExportTx but within a reasonable window
                        # (typically imports happen within hours/days of exports, not years later)
                        export_ts = export_tx.timestamp.timestamp() if export_tx.timestamp else 0
                        import_ts = atomic_tx.timestamp.timestamp() if atomic_tx.timestamp else 0

                        # Skip if ImportTx is before ExportTx (wrong direction)
                        if import_ts < export_ts - 3600:  # Allow 1 hour margin for clock skew
                            continue

                        # Skip if ImportTx is much later (probably unrelated - more than 30 days)
                        if import_ts > export_ts + 30 * 24 * 3600:
                            continue

                        # Found matching ImportTx - get actual C-chain destinations
                        for to_addr in atomic_tx.to_addresses:
                            try:
                                if to_addr.startswith("0x"):
                                    actual_dest = AvaxAddress.from_c_address(to_addr)
                                    result.append((actual_dest, atomic_tx.amount_avax))

                                    # Record ImportTx in cross_chain_txs
                                    if cross_chain_txs is not None:
                                        cross_chain_txs.append(CrossChainTx(
                                            tx_hash=atomic_tx.tx_hash,
                                            tx_type="ImportTx",
                                            source_chain=export_tx.chain.value if hasattr(export_tx.chain, 'value') else str(export_tx.chain),
                                            dest_chain="C",
                                            source_address=target_addr.x_address,
                                            dest_address=to_addr,
                                            amount_avax=atomic_tx.amount_avax,
                                            timestamp=atomic_tx.timestamp,
                                            block_height=atomic_tx.block_number,
                                        ))
                            except ValueError:
                                continue
            except Exception as e:
                logger.warning(f"Error finding C-chain import for {target_addr.c_address}: {e}")

        return result

    def _add_outgoing_tx_to_graph(
        self,
        tx: TransactionRecord,
        source_addr: AvaxAddress,
        graph: TraceGraph,
    ) -> list[tuple[AvaxAddress, float]]:
        """
        Add a transaction to the graph for outgoing flow and return target addresses.

        Returns list of (target_address, amount_avax) tuples.
        """
        result: list[tuple[AvaxAddress, float]] = []

        # Include all address formats for matching, including non-prefixed bech32
        bech32_no_prefix = source_addr.x_address.lower().replace("x-", "")
        source_strs = {
            source_addr.c_address.lower(),
            source_addr.x_address.lower(),
            source_addr.p_address.lower(),
            bech32_no_prefix,  # avax1... format without chain prefix
        }

        # Determine if this transaction is FROM our source
        from_addrs_lower = {a.lower() for a in tx.from_addresses}

        if source_strs & from_addrs_lower:
            # Source sent funds - add recipients
            for to_addr in tx.to_addresses:
                try:
                    target = AvaxAddress.from_any(to_addr)

                    # Don't add self-transfers
                    if target.raw_bytes == source_addr.raw_bytes:
                        continue

                    edge = GraphEdge(
                        source=source_addr,
                        target=target,
                        chain=tx.chain,
                        tx_hash=tx.tx_hash,
                        amount_navax=tx.amount_navax,
                        timestamp=tx.timestamp,
                        tx_type=_tx_type_to_enum(tx.tx_type),
                        block_height=tx.block_number,
                    )

                    if tx.is_cross_chain:
                        edge.linked_chain = self.linker.get_destination_chain(tx)

                    graph.add_edge(edge)
                    result.append((target, tx.amount_avax))

                except ValueError:
                    continue

        return result

    def trace_destinations(
        self,
        address: str | AvaxAddress,
        config: TraceConfig | None = None,
        fetch_balances: bool = True,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> TraceDestinationsResult:
        """
        Trace forward from a source address to find all destinations.

        This traces outward/downward from a source to find where funds went,
        using a greedy largest-first approach.

        Args:
            address: Source address to trace from
            config: Trace configuration
            fetch_balances: Whether to fetch current C-chain balances
            progress_callback: Optional callback(depth, count, message)

        Returns:
            TraceDestinationsResult with all destinations and their flow info
        """
        config = config or TraceConfig()
        errors: list[str] = []

        # Normalize address
        if isinstance(address, str):
            try:
                source = AvaxAddress.from_any(address)
            except ValueError as e:
                return TraceDestinationsResult(
                    starting_address=AvaxAddress.from_any("0x" + "0" * 40),
                    destinations=[],
                    graph=TraceGraph(),
                    total_outflow_avax=0.0,
                    c_chain_destinations=[],
                    addresses_visited=0,
                    transactions_processed=0,
                    errors=[f"Invalid address: {address} - {e}"],
                )
        else:
            source = address

        graph = TraceGraph()
        visited: set[bytes] = set()
        tx_count = 0

        # Track amounts received and sent for each destination
        received_amounts: dict[bytes, float] = {}  # addr -> total received
        sent_amounts: dict[bytes, float] = {}  # addr -> total sent (back)
        addr_depths: dict[bytes, int] = {}
        addr_chains: dict[bytes, set[str]] = {}
        addr_objects: dict[bytes, AvaxAddress] = {}
        # Track amounts received via C-chain exports (addr_bytes -> cross-chain amount)
        c_chain_export_amounts: dict[bytes, float] = {}
        # Collect all import/export transactions seen during trace
        cross_chain_txs: list[CrossChainTx] = []

        # Priority queue: (-amount_avax, depth, counter, address)
        counter = 0
        queue: list[tuple[float, int, int, AvaxAddress]] = []

        # Start with the source
        heapq.heappush(queue, (float('-inf'), 0, counter, source))
        counter += 1
        graph.add_node(GraphNode(address=source, depth=0))
        addr_objects[source.raw_bytes] = source
        addr_depths[source.raw_bytes] = 0
        addr_chains[source.raw_bytes] = set()

        # Greedy forward traversal
        while queue:
            neg_amount, depth, _, current_addr = heapq.heappop(queue)
            amount = -neg_amount if neg_amount != float('-inf') else 0

            if current_addr.raw_bytes in visited:
                continue
            visited.add(current_addr.raw_bytes)

            if progress_callback:
                amt_str = f"{amount:.2f} AVAX" if amount > 0 else "start"
                progress_callback(depth, len(visited), f"Forward: {current_addr.c_address[:10]}... ({amt_str})")

            if depth >= config.max_depth:
                continue

            if len(visited) >= config.max_total_addresses:
                errors.append(f"Hit max address limit ({config.max_total_addresses})")
                break

            # Get outgoing transactions
            # Cap C-chain export attribution by how much this address received in the trace
            # (for the source address, no cap applies)
            if current_addr.raw_bytes == source.raw_bytes:
                max_export = None  # No cap for source - it's the origin of funds
            else:
                max_export = received_amounts.get(current_addr.raw_bytes, 0.0)

            try:
                outgoing = self._get_outgoing_addresses_with_amounts(
                    current_addr, config, graph, c_chain_export_amounts, cross_chain_txs,
                    max_export_amount=max_export
                )
                tx_count += len(graph.get_outgoing_edges(current_addr))

                # Limit per depth
                if len(outgoing) > config.max_addresses_per_depth:
                    outgoing.sort(key=lambda x: x[1], reverse=True)
                    outgoing = outgoing[:config.max_addresses_per_depth]

                for dest_addr, dest_amount in outgoing:
                    # Track the address
                    addr_objects[dest_addr.raw_bytes] = dest_addr

                    # Accumulate received amount
                    received_amounts[dest_addr.raw_bytes] = (
                        received_amounts.get(dest_addr.raw_bytes, 0.0) + dest_amount
                    )

                    # Track depth (use minimum)
                    if dest_addr.raw_bytes not in addr_depths:
                        addr_depths[dest_addr.raw_bytes] = depth + 1
                    else:
                        addr_depths[dest_addr.raw_bytes] = min(
                            addr_depths[dest_addr.raw_bytes], depth + 1
                        )

                    # Track chains used
                    if dest_addr.raw_bytes not in addr_chains:
                        addr_chains[dest_addr.raw_bytes] = set()

                    # Add to queue if not visited
                    if dest_addr.raw_bytes not in visited:
                        heapq.heappush(queue, (-dest_amount, depth + 1, counter, dest_addr))
                        counter += 1

            except Exception as e:
                logger.error(f"Error tracing forward from {current_addr.c_address}: {e}")
                errors.append(f"Error at {current_addr.c_address}: {str(e)}")

        # Now trace incoming to calculate sent_back amounts
        # (what each destination sent back toward the source)
        for addr_bytes, addr_obj in addr_objects.items():
            if addr_bytes == source.raw_bytes:
                continue
            # Check edges in graph where this addr is the source
            for edge in graph.get_outgoing_edges(addr_obj):
                target_bytes = edge.target.raw_bytes
                # If target is closer to source (lower depth), count as sent back
                target_depth = addr_depths.get(target_bytes, float('inf'))
                this_depth = addr_depths.get(addr_bytes, float('inf'))
                if target_depth < this_depth:
                    sent_amounts[addr_bytes] = sent_amounts.get(addr_bytes, 0.0) + edge.amount_avax

        # Calculate total outflow from source
        total_outflow = sum(
            edge.amount_avax for edge in graph.get_outgoing_edges(source)
        )

        # Build destination info list
        destinations: list[DestinationInfo] = []
        c_chain_destinations: list[DestinationInfo] = []

        for addr_bytes, addr_obj in addr_objects.items():
            if addr_bytes == source.raw_bytes:
                continue

            received = received_amounts.get(addr_bytes, 0.0)
            sent_back = sent_amounts.get(addr_bytes, 0.0)
            net = received - sent_back

            dest_info = DestinationInfo(
                address=addr_obj,
                total_received_avax=received,
                total_sent_back_avax=sent_back,
                net_received_avax=net,
                current_balance_avax=None,
                depth=addr_depths.get(addr_bytes, 0),
                chains_used=addr_chains.get(addr_bytes, set()),
            )
            destinations.append(dest_info)

        # Sort by net received (largest first)
        destinations.sort(key=lambda x: x.net_received_avax, reverse=True)

        # Filter to only addresses that actually received funds via C-chain exports
        # and use the actual cross-chain amount (not total received)
        for dest in destinations:
            cross_chain_amt = c_chain_export_amounts.get(dest.address.raw_bytes, 0.0)
            if cross_chain_amt > 0:
                # Create a copy with the actual cross-chain amount
                c_chain_dest = DestinationInfo(
                    address=dest.address,
                    total_received_avax=cross_chain_amt,  # Only cross-chain amount
                    total_sent_back_avax=0.0,  # Don't track sent-back for cross-chain
                    net_received_avax=cross_chain_amt,
                    current_balance_avax=None,
                    depth=dest.depth,
                    chains_used=dest.chains_used,
                )
                c_chain_destinations.append(c_chain_dest)

        # Sort by amount (largest first)
        c_chain_destinations.sort(key=lambda x: x.net_received_avax, reverse=True)

        # Fetch C-chain balances if requested
        if fetch_balances and c_chain_destinations:
            if progress_callback:
                progress_callback(0, 0, f"Fetching balances for {len(c_chain_destinations)} addresses...")

            for dest in c_chain_destinations[:100]:  # Limit to avoid too many API calls
                try:
                    balance = self._get_c_chain_balance(dest.address)
                    dest.current_balance_avax = balance
                except Exception as e:
                    logger.warning(f"Error fetching balance for {dest.address.c_address}: {e}")

        return TraceDestinationsResult(
            starting_address=source,
            destinations=destinations,
            graph=graph,
            total_outflow_avax=total_outflow,
            c_chain_destinations=c_chain_destinations,
            addresses_visited=len(visited),
            transactions_processed=tx_count,
            errors=errors,
            cross_chain_txs=cross_chain_txs,
        )

    # Balance cache: {address: (balance, timestamp)}
    _balance_cache: dict[str, tuple[float, float]] = {}
    _balance_cache_ttl: float = 3600.0  # 1 hour TTL

    def stream_exports(
        self,
        address: str | AvaxAddress,
        config: TraceConfig | None = None,
        on_export: Callable[[CrossChainTx, int, int], None] | None = None,
    ) -> list[CrossChainTx]:
        """
        Stream C-chain exports as they're found, following largest flows first.

        This is a simpler, streaming version that prints exports in real-time
        without building the full graph or tracking amounts per destination.

        Args:
            address: Starting P/X chain address
            config: Trace configuration
            on_export: Callback(export_tx, depth, total_exports) called when export found

        Returns:
            List of all CrossChainTx found
        """
        config = config or TraceConfig()

        # Normalize address
        if isinstance(address, str):
            try:
                source = AvaxAddress.from_any(address)
            except ValueError as e:
                logger.error(f"Invalid address: {address} - {e}")
                return []
        else:
            source = address

        # C-chain blockchain ID
        C_CHAIN_ID = "2q9e4r6Mu3U68nU1fYjgbR6JvwrRx36CohpAX5UQxse55x1Q5"

        visited: set[bytes] = set()
        all_exports: list[CrossChainTx] = []
        tx_types = config.get_tx_types()
        # Track totals by C-chain destination: {addr: (traced_amount, total_exported)}
        by_dest: dict[str, tuple[float, float]] = {}
        # Track ImportTx hashes we've already counted (to avoid duplicates)
        seen_import_txs: set[str] = set()
        # Track how much each address received from our trace (to cap attribution)
        received_from_trace: dict[bytes, float] = {source.raw_bytes: float('inf')}

        # Priority queue: (-amount, depth, counter, address)
        counter = 0
        queue: list[tuple[float, int, int, AvaxAddress]] = []
        heapq.heappush(queue, (float('-inf'), 0, counter, source))
        counter += 1

        print(f"\nStreaming C-chain exports from {source.p_address}")
        print(f"Max depth {config.max_depth}")
        print()

        while queue:
            neg_amount, depth, _, current_addr = heapq.heappop(queue)
            amount = -neg_amount if neg_amount != float('-inf') else 0

            if current_addr.raw_bytes in visited:
                continue
            visited.add(current_addr.raw_bytes)

            if depth >= config.max_depth:
                continue

            if len(visited) >= config.max_total_addresses:
                print(f"\n[Stopped: hit max address limit {config.max_total_addresses}]")
                break

            # Query P-chain transactions
            try:
                for tx in self.glacier.get_p_chain_transactions(
                    current_addr, tx_types=tx_types, max_pages=10
                ):
                    if tx.amount_avax < config.min_amount_avax:
                        continue

                    # Check if this is an ExportTx to C-chain
                    is_c_export = (
                        tx.tx_type == "ExportTx" and
                        tx.destination_chain == C_CHAIN_ID
                    )

                    if is_c_export:
                        # Find actual C-chain destinations by looking up ImportTx
                        intermediate_targets = []
                        for to_addr in tx.to_addresses:
                            try:
                                dest = AvaxAddress.from_any(to_addr)
                                intermediate_targets.append((dest, tx.amount_avax))
                            except ValueError:
                                continue

                        # How much did current_addr receive from our trace?
                        trace_budget = received_from_trace.get(current_addr.raw_bytes, 0)

                        # Look up actual C-chain destinations
                        c_dests = self._find_c_chain_import_destinations_dedup(
                            tx, intermediate_targets, seen_import_txs
                        )
                        for dest, amt, import_tx_hash in c_dests:
                            export_tx = CrossChainTx(
                                tx_hash=import_tx_hash,
                                tx_type="ImportTx",
                                source_chain="P",
                                dest_chain="C",
                                source_address=current_addr.p_address,
                                dest_address=dest.c_address,
                                amount_avax=amt,
                                timestamp=tx.timestamp,
                                block_height=tx.block_number,
                            )
                            all_exports.append(export_tx)

                            # Calculate traced amount (capped by what flowed from source)
                            traced_amt = min(amt, trace_budget)
                            trace_budget = max(0, trace_budget - traced_amt)

                            # Update totals: (traced_amount, total_exported)
                            prev = by_dest.get(dest.c_address, (0.0, 0.0))
                            by_dest[dest.c_address] = (prev[0] + traced_amt, prev[1] + amt)

                            # Print running summary
                            self._print_dest_summary_dual(by_dest)

                            if on_export:
                                on_export(export_tx, depth, len(all_exports))

                    # Add recipients to queue (for non-export or export to other chains)
                    from_strs = {a.lower() for a in tx.from_addresses}
                    addr_strs = {
                        current_addr.p_address.lower(),
                        current_addr.x_address.lower(),
                        current_addr.x_address.lower().replace("x-", ""),
                    }

                    if addr_strs & from_strs:
                        for to_addr in tx.to_addresses:
                            try:
                                dest = AvaxAddress.from_any(to_addr)
                                if dest.raw_bytes != current_addr.raw_bytes and dest.raw_bytes not in visited:
                                    heapq.heappush(queue, (-tx.amount_avax, depth + 1, counter, dest))
                                    counter += 1
                                    # Track how much this address received from the trace
                                    # Cap by what current_addr received
                                    current_budget = received_from_trace.get(current_addr.raw_bytes, 0)
                                    flow_amt = min(tx.amount_avax, current_budget)
                                    received_from_trace[dest.raw_bytes] = (
                                        received_from_trace.get(dest.raw_bytes, 0) + flow_amt
                                    )
                            except ValueError:
                                continue

            except Exception as e:
                logger.warning(f"Error querying P-chain for {current_addr.p_address}: {e}")

            # Query X-chain transactions
            try:
                for tx in self.glacier.get_x_chain_transactions(
                    current_addr, tx_types=tx_types, max_pages=10
                ):
                    if tx.amount_avax < config.min_amount_avax:
                        continue

                    # Check if this is an ExportTx to C-chain
                    is_c_export = (
                        tx.tx_type == "ExportTx" and
                        tx.destination_chain == C_CHAIN_ID
                    )

                    if is_c_export:
                        # Find actual C-chain destinations by looking up ImportTx
                        intermediate_targets = []
                        for to_addr in tx.to_addresses:
                            try:
                                dest = AvaxAddress.from_any(to_addr)
                                intermediate_targets.append((dest, tx.amount_avax))
                            except ValueError:
                                continue

                        # How much did current_addr receive from our trace?
                        trace_budget = received_from_trace.get(current_addr.raw_bytes, 0)

                        # Look up actual C-chain destinations
                        c_dests = self._find_c_chain_import_destinations_dedup(
                            tx, intermediate_targets, seen_import_txs
                        )
                        for dest, amt, import_tx_hash in c_dests:
                            export_tx = CrossChainTx(
                                tx_hash=import_tx_hash,  # Use ImportTx hash
                                tx_type="ImportTx",
                                source_chain="X",
                                dest_chain="C",
                                source_address=current_addr.x_address,
                                dest_address=dest.c_address,
                                amount_avax=amt,
                                timestamp=tx.timestamp,
                                block_height=tx.block_number,
                            )
                            all_exports.append(export_tx)

                            # Calculate traced amount (capped by what flowed from source)
                            traced_amt = min(amt, trace_budget)
                            trace_budget = max(0, trace_budget - traced_amt)

                            # Update totals: (traced_amount, total_exported)
                            prev = by_dest.get(dest.c_address, (0.0, 0.0))
                            by_dest[dest.c_address] = (prev[0] + traced_amt, prev[1] + amt)

                            # Print running summary
                            self._print_dest_summary_dual(by_dest)

                            if on_export:
                                on_export(export_tx, depth, len(all_exports))

                    # Add recipients to queue and track received amounts
                    from_strs = {a.lower() for a in tx.from_addresses}
                    addr_strs = {
                        current_addr.p_address.lower(),
                        current_addr.x_address.lower(),
                        current_addr.x_address.lower().replace("x-", ""),
                    }

                    if addr_strs & from_strs:
                        for to_addr in tx.to_addresses:
                            try:
                                dest = AvaxAddress.from_any(to_addr)
                                if dest.raw_bytes != current_addr.raw_bytes and dest.raw_bytes not in visited:
                                    heapq.heappush(queue, (-tx.amount_avax, depth + 1, counter, dest))
                                    counter += 1
                                    # Track how much this address received from the trace
                                    current_budget = received_from_trace.get(current_addr.raw_bytes, 0)
                                    flow_amt = min(tx.amount_avax, current_budget)
                                    received_from_trace[dest.raw_bytes] = (
                                        received_from_trace.get(dest.raw_bytes, 0) + flow_amt
                                    )
                            except ValueError:
                                continue

            except Exception as e:
                logger.warning(f"Error querying X-chain for {current_addr.x_address}: {e}")

        # Final summary
        print(f"\n\nComplete! Visited {len(visited)} addresses")
        if by_dest:
            total_traced = sum(v[0] for v in by_dest.values())
            total_exported = sum(v[1] for v in by_dest.values())
            print(f"Total traced from source: {total_traced:,.2f} AVAX")
            print(f"Total exported to C-chain: {total_exported:,.2f} AVAX")
            print("\nFinal C-chain destinations (sorted by traced amount):")
            print(f"{'Address':<44} {'Traced':>14} {'Total':>14}")
            print("-" * 74)
            for addr, (traced, total) in sorted(by_dest.items(), key=lambda x: -x[1][0]):
                print(f"{addr}  {traced:>12,.2f}  {total:>12,.2f}")

        return all_exports

    def _find_c_chain_import_destinations_dedup(
        self,
        export_tx: TransactionRecord,
        intermediate_targets: list[tuple[AvaxAddress, float]],
        seen_import_txs: set[str],
    ) -> list[tuple[AvaxAddress, float, str]]:
        """
        Find actual C-chain destinations, deduplicating by ImportTx hash.

        Returns list of (c_chain_address, amount, import_tx_hash) tuples.
        Only returns ImportTx that haven't been seen before.
        """
        result: list[tuple[AvaxAddress, float, str]] = []

        for target_addr, amount in intermediate_targets:
            try:
                bech32_addr = target_addr.x_address.replace("X-", "")
                for atomic_tx in self.glacier.get_c_chain_atomic_transactions(
                    bech32_addr, tx_types=["ImportTx"], max_pages=5
                ):
                    # Skip if we've already counted this ImportTx
                    if atomic_tx.tx_hash in seen_import_txs:
                        continue

                    if not atomic_tx.from_addresses:
                        continue

                    target_strs = {
                        target_addr.c_address.lower(),
                        target_addr.x_address.lower(),
                        target_addr.p_address.lower(),
                        target_addr.x_address.lower().replace("x-", ""),
                    }
                    from_strs = {a.lower() for a in atomic_tx.from_addresses}

                    if target_strs & from_strs:
                        # Mark as seen
                        seen_import_txs.add(atomic_tx.tx_hash)

                        # Get actual C-chain destinations
                        for to_addr in atomic_tx.to_addresses:
                            try:
                                if to_addr.startswith("0x"):
                                    actual_dest = AvaxAddress.from_c_address(to_addr)
                                    result.append((actual_dest, atomic_tx.amount_avax, atomic_tx.tx_hash))
                            except ValueError:
                                continue
            except Exception as e:
                logger.warning(f"Error finding C-chain import for {target_addr.c_address}: {e}")

        return result

    def _print_dest_summary(self, by_dest: dict[str, float]) -> None:
        """Print current destination summary."""
        import sys
        # Clear screen and print summary
        print("\033[2J\033[H", end="")  # Clear screen, move cursor to top
        print(f"C-chain destinations ({len(by_dest)} found):")
        print("-" * 60)
        total = 0.0
        for addr, amt in sorted(by_dest.items(), key=lambda x: -x[1]):
            print(f"{addr}  {amt:>14,.2f} AVAX")
            total += amt
        print("-" * 60)
        print(f"{'Total:':<44} {total:>14,.2f} AVAX")
        print()
        sys.stdout.flush()

    def _print_dest_summary_dual(self, by_dest: dict[str, tuple[float, float]]) -> None:
        """Print current destination summary with both traced and total amounts."""
        import sys
        # Clear screen and print summary
        print("\033[2J\033[H", end="")  # Clear screen, move cursor to top
        print(f"C-chain destinations ({len(by_dest)} found):")
        print(f"{'Address':<44} {'Traced':>14} {'Total':>14}")
        print("-" * 74)
        total_traced = 0.0
        total_exported = 0.0
        # Sort by traced amount (first element of tuple) descending
        for addr, (traced, total) in sorted(by_dest.items(), key=lambda x: -x[1][0]):
            print(f"{addr}  {traced:>12,.2f}  {total:>12,.2f}")
            total_traced += traced
            total_exported += total
        print("-" * 74)
        print(f"{'Total:':<44} {total_traced:>12,.2f}  {total_exported:>12,.2f}")
        print()
        sys.stdout.flush()

    def _get_c_chain_balance(self, address: AvaxAddress) -> float:
        """Get current C-chain AVAX balance for an address (cached for 1 hour)."""
        import time

        cache_key = address.c_address.lower()
        now = time.time()

        # Check cache
        if cache_key in self._balance_cache:
            balance, cached_at = self._balance_cache[cache_key]
            if now - cached_at < self._balance_cache_ttl:
                return balance

        try:
            import httpx
            # Use public Avalanche C-chain RPC
            rpc_url = "https://api.avax.network/ext/bc/C/rpc"
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_getBalance",
                "params": [address.c_address, "latest"],
                "id": 1,
            }
            response = httpx.post(rpc_url, json=payload, timeout=10.0)
            result = response.json()
            if "result" in result:
                balance_wei = int(result["result"], 16)
                balance = balance_wei / 1e18  # Convert to AVAX
                # Cache the result
                self._balance_cache[cache_key] = (balance, now)
                return balance
        except Exception as e:
            logger.warning(f"Failed to fetch balance: {e}")
        return 0.0
