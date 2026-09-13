"""
Forward tracer from genesis addresses to C-chain exports.

Traces funds forward from genesis addresses through P/X chain to find
C-chain export destinations, tracking attribution to genesis sources.
"""

import csv
import heapq
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from ..clients.glacier_client import GlacierClient, TransactionRecord
from ..models.address import AvaxAddress
from .checkpoint import (
    CChainDestination,
    GenesisAttribution,
    GenesisTraceCheckpoint,
    QueueItem,
    load_checkpoint,
    save_checkpoint,
)
from .genesis_matcher import GenesisMatch, GenesisMatcher

logger = logging.getLogger(__name__)

# Blockchain IDs for detecting cross-chain exports
C_CHAIN_ID = "2q9e4r6Mu3U68nU1fYjgbR6JvwrRx36CohpAX5UQxse55x1Q5"
X_CHAIN_ID = "2oYMBNV4eNHyqk2fjjV5nVQLDbtmNJzq5s3qs3Lo6ftnC6FByM"
P_CHAIN_ID = "11111111111111111111111111111111LpoYY"

# Default transaction types for value transfers
DEFAULT_VALUE_TRANSFER_TYPES = ["BaseTx", "ExportTx", "ImportTx"]


class TraceStrategy(Enum):
    """
    Traversal order for forward tracing from genesis. All three stop at max_depth and count each ImportTx once.

    BFS finishes every address at depth d before depth d+1: the most complete result for a given depth,
    at the cost of spending the API budget on many small flows. GREEDY follows the largest flows first
    (cached addresses first), so the big C-Chain exports show up early and a run cut short by rate limits
    or max_total_addresses still has the flows that matter; it can miss small branches. HYBRID runs BFS to
    hybrid_switch_depth, where fan-out is still small, then hands the unfinished frontier to GREEDY.
    """
    BFS = "bfs"
    GREEDY = "greedy"
    HYBRID = "hybrid"


@dataclass
class GenesisTraceConfig:
    """Configuration for genesis forward tracing."""
    max_depth: int = 20
    min_amount_avax: float = 0.0
    strategy: TraceStrategy = TraceStrategy.BFS
    hybrid_switch_depth: int = 5  # When to switch from BFS to greedy
    include_staking: bool = False
    checkpoint_path: Path | None = None
    output_path: Path | None = None
    checkpoint_interval: int = 1  # Save checkpoint every N depths (BFS) or N addresses (greedy)
    max_addresses_per_depth: int = 10000
    max_total_addresses: int = 100000


@dataclass
class GenesisTraceResult:
    """Result of a genesis forward trace."""
    destinations: list[CChainDestination]
    starting_addresses: list[AvaxAddress]
    categories_traced: list[str]
    total_genesis_avax: float
    total_exported_avax: float
    addresses_visited: int
    max_depth_reached: int
    errors: list[str]

    def summary(self) -> dict:
        """Get a summary of the trace result."""
        # Group destinations by category
        by_category: dict[str, list[CChainDestination]] = {}
        for dest in self.destinations:
            cats = set(a.category for a in dest.attributions)
            if len(cats) > 1:
                key = "Mixed"
            elif cats:
                key = list(cats)[0]
            else:
                key = "Unknown"
            if key not in by_category:
                by_category[key] = []
            by_category[key].append(dest)

        return {
            "destinations": len(self.destinations),
            "starting_addresses": len(self.starting_addresses),
            "categories_traced": self.categories_traced,
            "total_genesis_avax": self.total_genesis_avax,
            "total_exported_avax": self.total_exported_avax,
            "addresses_visited": self.addresses_visited,
            "max_depth_reached": self.max_depth_reached,
            "destinations_by_category": {
                cat: len(dests) for cat, dests in by_category.items()
            },
            "errors": len(self.errors),
        }


class GenesisForwardTracer:
    """
    Traces funds forward from genesis addresses to C-chain exports.

    Supports three traversal strategies:
    - BFS: Process by depth level, deterministic
    - Greedy: Process largest amounts first globally
    - Hybrid: BFS to a certain depth, then switch to greedy

    All strategies maintain determinism via secondary sort on address bytes.
    """

    def __init__(
        self,
        glacier_client: GlacierClient | None = None,
        genesis_matcher: GenesisMatcher | None = None,
    ):
        """Initialize the tracer."""
        self.glacier = glacier_client or GlacierClient()
        self.genesis = genesis_matcher or GenesisMatcher()

    def trace_from_genesis(
        self,
        categories: list[str] | None = None,
        addresses: list[str] | None = None,
        config: GenesisTraceConfig | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> GenesisTraceResult:
        """
        Trace forward from genesis addresses to find C-chain exports.

        Args:
            categories: Filter genesis addresses by these categories (OR logic)
            addresses: Specific genesis addresses to trace from
            config: Trace configuration
            progress_callback: Optional callback for progress messages

        Returns:
            GenesisTraceResult with all C-chain destinations found
        """
        config = config or GenesisTraceConfig()
        errors: list[str] = []

        def log(msg: str) -> None:
            if progress_callback:
                progress_callback(msg)
            else:
                print(msg)

        # Try to load existing checkpoint
        checkpoint: GenesisTraceCheckpoint | None = None
        if config.checkpoint_path and config.checkpoint_path.exists():
            checkpoint = load_checkpoint(config.checkpoint_path)
            if checkpoint:
                log(f"Resuming from checkpoint: depth={checkpoint.current_depth}, "
                    f"visited={len(checkpoint.visited)}")

        # Get starting genesis addresses
        starting_addrs = self._get_genesis_addresses(categories, addresses)
        if not starting_addrs:
            return GenesisTraceResult(
                destinations=[],
                starting_addresses=[],
                categories_traced=categories or [],
                total_genesis_avax=0.0,
                total_exported_avax=0.0,
                addresses_visited=0,
                max_depth_reached=0,
                errors=["No genesis addresses found for the given filter"],
            )

        # Calculate total genesis AVAX
        total_genesis_avax = sum(
            m.total_avax for _, m in starting_addrs
        )

        log("\nGenesis Forward Trace")
        log("=" * 50)
        log(f"Categories: {', '.join(categories) if categories else 'All'}")
        log(f"Genesis addresses: {len(starting_addrs)}")
        log(f"Total genesis AVAX: {total_genesis_avax:,.2f}")
        log(f"Strategy: {config.strategy.value}")
        log(f"Max depth: {config.max_depth}")
        log("")

        # Initialize state from checkpoint or fresh
        # Track processed ImportTx hashes to avoid double-counting
        processed_import_txs: set[str] = set()

        if checkpoint:
            visited = checkpoint.visited
            destinations = checkpoint.destinations
            queue_items = checkpoint.queue
            current_depth = checkpoint.current_depth
            # Rebuild processed_import_txs from destinations
            for dest in destinations.values():
                processed_import_txs.update(dest.import_tx_hashes)
        else:
            visited: set[bytes] = set()
            destinations: dict[bytes, CChainDestination] = {}
            current_depth = 0

            # Initialize queue with genesis addresses (genesis funds are on P-chain)
            queue_items: list[QueueItem] = []
            tx_types = None if config.include_staking else DEFAULT_VALUE_TRANSFER_TYPES
            for addr, match in starting_addrs:
                attr = GenesisAttribution(
                    genesis_address=addr,
                    category=match.category,
                    amount_avax=match.total_avax,
                    path_length=0,
                )
                # Check if this address has cached transactions
                is_cached = self.glacier.is_cached(addr, "P", tx_types)
                queue_items.append(QueueItem(
                    address=addr,
                    amount_avax=match.total_avax,
                    depth=0,
                    attributions=[attr],
                    chain="P",  # Genesis funds start on P-chain
                    is_cached=is_cached,
                ))

            # Log cache status
            cached_count = sum(1 for item in queue_items if item.is_cached)
            log(f"Cache status: {cached_count}/{len(queue_items)} genesis addresses cached")

        # Open output CSV if specified
        csv_writer = None
        csv_file = None
        if config.output_path:
            config.output_path.parent.mkdir(parents=True, exist_ok=True)
            csv_file = open(config.output_path, "a", newline="")
            csv_writer = csv.writer(csv_file)
            # Write header if file is empty
            if config.output_path.stat().st_size == 0:
                csv_writer.writerow([
                    "c_address", "total_avax", "categories", "num_sources",
                    "min_depth", "max_depth", "genesis_sources"
                ])
                csv_file.flush()

        try:
            # Run the appropriate strategy
            if config.strategy == TraceStrategy.BFS:
                result = self._trace_bfs(
                    queue_items, visited, destinations, processed_import_txs, config,
                    current_depth, starting_addrs, total_genesis_avax,
                    log, csv_file,
                )
            elif config.strategy == TraceStrategy.GREEDY:
                result = self._trace_greedy(
                    queue_items, visited, destinations, processed_import_txs, config,
                    starting_addrs, total_genesis_avax, log, csv_file,
                )
            else:  # HYBRID
                result = self._trace_hybrid(
                    queue_items, visited, destinations, processed_import_txs, config,
                    current_depth, starting_addrs, total_genesis_avax,
                    log, csv_file,
                )

            result.categories_traced = categories or []
            result.errors = errors + result.errors

            # Write final CSV with accumulated totals
            if config.output_path:
                self._write_final_csv(result.destinations, config.output_path)
                log(f"\nFinal results written to {config.output_path}")

            return result

        finally:
            if csv_file:
                csv_file.close()

    def _get_genesis_addresses(
        self,
        categories: list[str] | None,
        addresses: list[str] | None,
    ) -> list[tuple[AvaxAddress, GenesisMatch]]:
        """Get genesis addresses matching the filter criteria."""
        result: list[tuple[AvaxAddress, GenesisMatch]] = []
        seen: set[bytes] = set()

        if addresses:
            # Specific addresses requested
            for addr_str in addresses:
                try:
                    addr = AvaxAddress.from_any(addr_str)
                    match = self.genesis.match(addr)
                    if match and addr.raw_bytes not in seen:
                        result.append((addr, match))
                        seen.add(addr.raw_bytes)
                except ValueError:
                    logger.warning(f"Invalid address: {addr_str}")

        if categories:
            # Filter by categories
            for category in categories:
                matches = self.genesis.get_addresses_by_category(category)
                for match in matches:
                    if match.address.raw_bytes not in seen:
                        result.append((match.address, match))
                        seen.add(match.address.raw_bytes)

        if not addresses and not categories:
            # All genesis addresses
            all_cats = self.genesis.get_all_categories()
            for cat in all_cats:
                for match in self.genesis.get_addresses_by_category(cat):
                    if match.address.raw_bytes not in seen:
                        result.append((match.address, match))
                        seen.add(match.address.raw_bytes)

        # Sort deterministically: by total_avax desc, then address bytes
        result.sort(key=lambda x: (-x[1].total_avax, x[0].raw_bytes))
        return result

    def _trace_bfs(
        self,
        queue_items: list[QueueItem],
        visited: set[bytes],
        destinations: dict[bytes, CChainDestination],
        processed_import_txs: set[str],
        config: GenesisTraceConfig,
        start_depth: int,
        starting_addrs: list[tuple[AvaxAddress, GenesisMatch]],
        total_genesis_avax: float,
        log: Callable[[str], None],
        csv_file,
        frontier: list[QueueItem] | None = None,
    ) -> GenesisTraceResult:
        """BFS traversal strategy - process by depth level.

        If `frontier` is given, it receives the queued items that were not processed because they are
        deeper than config.max_depth (the hybrid strategy continues from them).
        """
        errors: list[str] = []
        max_depth_reached = start_depth

        # Organize queue by depth
        by_depth: dict[int, list[QueueItem]] = {}
        for item in queue_items:
            if item.depth not in by_depth:
                by_depth[item.depth] = []
            by_depth[item.depth].append(item)

        # Sort each depth level deterministically
        for depth in by_depth:
            by_depth[depth].sort(key=lambda x: x.sort_key())

        current_depth = start_depth
        while current_depth <= config.max_depth:
            if current_depth not in by_depth:
                break

            items_at_depth = by_depth[current_depth]
            if not items_at_depth:
                current_depth += 1
                continue

            total_avax = sum(item.amount_avax for item in items_at_depth)
            log(f"Depth {current_depth}: {len(items_at_depth)} addresses, "
                f"{total_avax:,.2f} AVAX")

            # Process all addresses at this depth
            next_depth_items: list[QueueItem] = []
            addresses_processed = 0

            for item in items_at_depth:
                if item.address.raw_bytes in visited:
                    continue
                visited.add(item.address.raw_bytes)
                addresses_processed += 1

                # Check limits
                if len(visited) >= config.max_total_addresses:
                    errors.append(f"Hit max total address limit: {config.max_total_addresses}")
                    break

                # Skip if below minimum amount (same as greedy)
                if item.amount_avax < config.min_amount_avax:
                    continue

                # Process this address
                try:
                    new_items, new_exports = self._process_address(
                        item, config, destinations, processed_import_txs, log, csv_file
                    )
                    next_depth_items.extend(new_items)
                except Exception as e:
                    logger.warning(f"Error processing {item.address}: {e}")
                    errors.append(f"Error at {item.address}: {str(e)}")

            max_depth_reached = max(max_depth_reached, current_depth)

            # Prepare next depth
            if next_depth_items:
                next_depth = current_depth + 1
                if next_depth not in by_depth:
                    by_depth[next_depth] = []

                # Deduplicate and sort
                seen_at_next: set[bytes] = {item.address.raw_bytes for item in by_depth[next_depth]}
                for item in next_depth_items:
                    if item.address.raw_bytes not in seen_at_next and item.address.raw_bytes not in visited:
                        by_depth[next_depth].append(item)
                        seen_at_next.add(item.address.raw_bytes)

                by_depth[next_depth].sort(key=lambda x: x.sort_key())

                # Truncate if too many addresses
                if len(by_depth[next_depth]) > config.max_addresses_per_depth:
                    by_depth[next_depth] = by_depth[next_depth][:config.max_addresses_per_depth]
                    log(f"  [Truncated depth {next_depth} to {config.max_addresses_per_depth} addresses]")

            # Save checkpoint at depth intervals
            if config.checkpoint_path and current_depth % config.checkpoint_interval == 0:
                self._save_checkpoint(
                    config, "bfs", current_depth + 1,
                    by_depth.get(current_depth + 1, []),
                    visited, destinations, total_genesis_avax,
                )
                log(f"[Checkpoint saved: depth {current_depth}, {len(visited)} visited]")

            current_depth += 1

            if len(visited) >= config.max_total_addresses:
                break

        if frontier is not None:
            frontier.extend(
                item
                for depth, items in sorted(by_depth.items())
                if depth > config.max_depth
                for item in items
                if item.address.raw_bytes not in visited
            )

        # Build result
        total_exported = sum(d.total_avax for d in destinations.values())
        self._print_summary(destinations, log)

        return GenesisTraceResult(
            destinations=list(destinations.values()),
            starting_addresses=[addr for addr, _ in starting_addrs],
            categories_traced=[],
            total_genesis_avax=total_genesis_avax,
            total_exported_avax=total_exported,
            addresses_visited=len(visited),
            max_depth_reached=max_depth_reached,
            errors=errors,
        )

    def _trace_greedy(
        self,
        queue_items: list[QueueItem],
        visited: set[bytes],
        destinations: dict[bytes, CChainDestination],
        processed_import_txs: set[str],
        config: GenesisTraceConfig,
        starting_addrs: list[tuple[AvaxAddress, GenesisMatch]],
        total_genesis_avax: float,
        log: Callable[[str], None],
        csv_file,
    ) -> GenesisTraceResult:
        """Greedy traversal strategy - process largest amounts first, cached addresses first."""
        errors: list[str] = []
        max_depth_reached = 0
        counter = 0  # For tie-breaking in heap

        # Build priority queue: (not_cached, -amount, depth, counter, item)
        # not_cached=0 for cached (process first), 1 for uncached
        heap: list[tuple[int, float, int, int, QueueItem]] = []
        for item in queue_items:
            not_cached = 0 if item.is_cached else 1
            heapq.heappush(heap, (not_cached, -item.amount_avax, item.depth, counter, item))
            counter += 1

        addresses_since_checkpoint = 0

        while heap:
            not_cached, neg_amount, depth, _, item = heapq.heappop(heap)

            if item.address.raw_bytes in visited:
                continue
            # Check these before marking the address visited: the heap is ordered by amount, not depth, so
            # an address can be popped first via a path that is too deep or too small and later via one
            # that is in range.
            if item.amount_avax < config.min_amount_avax or depth > config.max_depth:
                continue
            visited.add(item.address.raw_bytes)
            addresses_since_checkpoint += 1

            max_depth_reached = max(max_depth_reached, depth)

            if len(visited) >= config.max_total_addresses:
                errors.append(f"Hit max total address limit: {config.max_total_addresses}")
                break

            # Progress logging
            if len(visited) % 100 == 0:
                log(f"  Processed {len(visited)} addresses, depth {depth}, "
                    f"{len(destinations)} exports found")

            # Process this address
            try:
                new_items, _ = self._process_address(
                    item, config, destinations, processed_import_txs, log, csv_file
                )
                for new_item in new_items:
                    if new_item.address.raw_bytes not in visited:
                        not_cached = 0 if new_item.is_cached else 1
                        heapq.heappush(heap, (not_cached, -new_item.amount_avax, new_item.depth, counter, new_item))
                        counter += 1
            except Exception as e:
                logger.warning(f"Error processing {item.address}: {e}")
                errors.append(f"Error at {item.address}: {str(e)}")

            # Checkpoint at intervals
            if config.checkpoint_path and addresses_since_checkpoint >= config.checkpoint_interval * 100:
                # Convert heap back to queue items
                queue_list = [item for _, _, _, _, item in heap]
                self._save_checkpoint(
                    config, "greedy", max_depth_reached,
                    queue_list, visited, destinations, total_genesis_avax,
                )
                log(f"[Checkpoint saved: {len(visited)} visited, {len(destinations)} exports]")
                addresses_since_checkpoint = 0

        # Build result
        total_exported = sum(d.total_avax for d in destinations.values())
        self._print_summary(destinations, log)

        return GenesisTraceResult(
            destinations=list(destinations.values()),
            starting_addresses=[addr for addr, _ in starting_addrs],
            categories_traced=[],
            total_genesis_avax=total_genesis_avax,
            total_exported_avax=total_exported,
            addresses_visited=len(visited),
            max_depth_reached=max_depth_reached,
            errors=errors,
        )

    def _trace_hybrid(
        self,
        queue_items: list[QueueItem],
        visited: set[bytes],
        destinations: dict[bytes, CChainDestination],
        processed_import_txs: set[str],
        config: GenesisTraceConfig,
        start_depth: int,
        starting_addrs: list[tuple[AvaxAddress, GenesisMatch]],
        total_genesis_avax: float,
        log: Callable[[str], None],
        csv_file,
    ) -> GenesisTraceResult:
        """Hybrid traversal - BFS first, then switch to greedy."""
        # Phase 1: BFS until switch depth
        log(f"Phase 1: BFS to depth {config.hybrid_switch_depth}")
        bfs_config = GenesisTraceConfig(
            max_depth=config.hybrid_switch_depth,
            min_amount_avax=config.min_amount_avax,
            strategy=TraceStrategy.BFS,
            include_staking=config.include_staking,
            checkpoint_path=None,  # Don't checkpoint during BFS phase
            max_addresses_per_depth=config.max_addresses_per_depth,
            max_total_addresses=config.max_total_addresses,
        )

        frontier: list[QueueItem] = []
        bfs_result = self._trace_bfs(
            queue_items, visited, destinations, processed_import_txs, bfs_config,
            start_depth, starting_addrs, total_genesis_avax,
            log, csv_file, frontier=frontier,
        )

        # Phase 2: greedy from the addresses BFS queued but did not process
        if not frontier or len(visited) >= config.max_total_addresses:
            return bfs_result

        log(f"\nPhase 2: Greedy over {len(frontier)} frontier addresses, to depth {config.max_depth}")
        greedy_config = GenesisTraceConfig(
            max_depth=config.max_depth,
            min_amount_avax=config.min_amount_avax,
            strategy=TraceStrategy.GREEDY,
            include_staking=config.include_staking,
            checkpoint_path=config.checkpoint_path,
            checkpoint_interval=config.checkpoint_interval,
            max_addresses_per_depth=config.max_addresses_per_depth,
            max_total_addresses=config.max_total_addresses,  # compared against the shared visited set
        )
        greedy_result = self._trace_greedy(
            frontier, visited, destinations, processed_import_txs, greedy_config,
            starting_addrs, total_genesis_avax, log, csv_file,
        )
        greedy_result.max_depth_reached = max(greedy_result.max_depth_reached, bfs_result.max_depth_reached)
        greedy_result.errors = bfs_result.errors + greedy_result.errors
        return greedy_result

    def _process_address(
        self,
        item: QueueItem,
        config: GenesisTraceConfig,
        destinations: dict[bytes, CChainDestination],
        processed_import_txs: set[str],
        log: Callable[[str], None],
        csv_file,
    ) -> tuple[list[QueueItem], list[CChainDestination]]:
        """
        Process an address: query transactions on the chain where funds are.

        Returns:
            (new_queue_items, new_c_chain_exports)
        """
        new_items: list[QueueItem] = []
        new_exports: list[CChainDestination] = []

        tx_types = None if config.include_staking else DEFAULT_VALUE_TRANSFER_TYPES

        # Only query the chain where funds currently are
        for tx in self._get_outgoing_transactions(item.address, item.chain, tx_types):
            if tx.amount_avax < config.min_amount_avax:
                continue

            # Check if this is an export to C-chain
            is_c_export = (
                tx.tx_type == "ExportTx" and
                tx.destination_chain == C_CHAIN_ID
            )

            # Check if this is a cross-chain to X or P
            is_x_export = (
                tx.tx_type == "ExportTx" and
                tx.destination_chain == X_CHAIN_ID
            )
            is_p_export = (
                tx.tx_type == "ExportTx" and
                tx.destination_chain == P_CHAIN_ID
            )

            if is_c_export:
                # Find the actual C-chain destinations via ImportTx
                c_dests = self._find_c_chain_destinations(tx, item)
                for c_dest in c_dests:
                    # Skip if we've already processed this ImportTx (each c_dest has one tx_hash)
                    tx_hash = c_dest.import_tx_hashes[0] if c_dest.import_tx_hashes else None
                    if tx_hash and tx_hash in processed_import_txs:
                        continue  # Already counted this ImportTx
                    if tx_hash:
                        processed_import_txs.add(tx_hash)

                    # Record the export
                    dest_bytes = c_dest.address.raw_bytes
                    if dest_bytes in destinations:
                        # Merge with existing
                        existing = destinations[dest_bytes]
                        existing.total_avax += c_dest.total_avax
                        existing.attributions.extend(c_dest.attributions)
                        if tx_hash:
                            existing.import_tx_hashes.append(tx_hash)
                        existing.max_depth = max(existing.max_depth, c_dest.max_depth)
                    else:
                        destinations[dest_bytes] = c_dest
                        new_exports.append(c_dest)

                        # Log the export
                        log(f"  [EXPORT] {c_dest.address.c_address} received "
                            f"{c_dest.total_avax:,.2f} AVAX")
                        for attr in c_dest.attributions:
                            log(f"    - {attr.genesis_address.p_address} "
                                f"({attr.category}): {attr.amount_avax:,.2f} AVAX "
                                f"@ depth {attr.path_length}")

                        # Write to CSV and flush immediately
                        if csv_file:
                            # Get unique genesis source addresses
                            source_addrs = sorted(set(
                                a.genesis_address.p_address for a in c_dest.attributions
                            ))
                            csv.writer(csv_file).writerow([
                                c_dest.address.c_address,
                                c_dest.total_avax,
                                c_dest.categories_str(),
                                len(c_dest.attributions),
                                c_dest.min_depth,
                                c_dest.max_depth,
                                "|".join(source_addrs),
                            ])
                            csv_file.flush()
            else:
                # Determine destination chain for the new queue items
                if is_x_export:
                    dest_chain = "X"
                elif is_p_export:
                    dest_chain = "P"
                else:
                    dest_chain = item.chain  # Same chain transfer

                # Regular P/X chain transfer - propagate attributions
                for to_addr_str in tx.to_addresses:
                    try:
                        to_addr = AvaxAddress.from_any(to_addr_str)
                        # Skip self-transfers
                        if to_addr.raw_bytes == item.address.raw_bytes:
                            continue

                        # Propagate attribution (scaled by ratio)
                        new_attrs = self._propagate_attributions(
                            item.attributions, tx.amount_avax, item.amount_avax,
                            item.depth + 1
                        )
                        # Check if destination has cached transactions
                        is_cached = self.glacier.is_cached(to_addr, dest_chain, tx_types)
                        new_items.append(QueueItem(
                            address=to_addr,
                            amount_avax=tx.amount_avax,
                            depth=item.depth + 1,
                            attributions=new_attrs,
                            chain=dest_chain,
                            is_cached=is_cached,
                        ))
                    except ValueError:
                        continue

        return new_items, new_exports

    def _get_outgoing_transactions(
        self,
        address: AvaxAddress,
        chain: str,
        tx_types: list[str] | None,
    ) -> Iterator[TransactionRecord]:
        """Get outgoing transactions from an address."""
        try:
            if chain == "P":
                for tx in self.glacier.get_p_chain_transactions(
                    address, tx_types=tx_types, max_pages=10
                ):
                    # Only outgoing (address is in from_addresses)
                    addr_strs = address.match_strings
                    from_strs = {a.lower() for a in tx.from_addresses}
                    if addr_strs & from_strs:
                        yield tx
            elif chain == "X":
                for tx in self.glacier.get_x_chain_transactions(
                    address, tx_types=tx_types, max_pages=10
                ):
                    addr_strs = address.match_strings
                    from_strs = {a.lower() for a in tx.from_addresses}
                    if addr_strs & from_strs:
                        yield tx
        except Exception as e:
            logger.warning(f"Error getting {chain}-chain txs for {address}: {e}")

    def _find_c_chain_destinations(
        self,
        export_tx: TransactionRecord,
        source_item: QueueItem,
    ) -> list[CChainDestination]:
        """Find actual C-chain destinations for an ExportTx."""
        result: list[CChainDestination] = []

        # Get intermediate targets from ExportTx
        for to_addr_str in export_tx.to_addresses:
            try:
                intermediate_addr = AvaxAddress.from_any(to_addr_str)

                # Query C-chain atomic transactions for ImportTx
                bech32_addr = intermediate_addr.bech32
                for atomic_tx in self.glacier.get_c_chain_atomic_transactions(
                    bech32_addr, tx_types=["ImportTx"], max_pages=5
                ):
                    # Check if source matches
                    target_strs = intermediate_addr.match_strings
                    from_strs = {a.lower() for a in atomic_tx.from_addresses}

                    if target_strs & from_strs:
                        # Found matching ImportTx - get C-chain destinations
                        for c_addr_str in atomic_tx.to_addresses:
                            try:
                                if c_addr_str.startswith("0x"):
                                    c_addr = AvaxAddress.from_c_address(c_addr_str)

                                    # Create attributions for this destination
                                    attrs = self._propagate_attributions(
                                        source_item.attributions,
                                        atomic_tx.amount_avax,
                                        source_item.amount_avax,
                                        source_item.depth + 1,
                                    )

                                    result.append(CChainDestination(
                                        address=c_addr,
                                        total_avax=atomic_tx.amount_avax,
                                        attributions=attrs,
                                        import_tx_hashes=[atomic_tx.tx_hash],
                                        min_depth=source_item.depth + 1,
                                        max_depth=source_item.depth + 1,
                                    ))
                            except ValueError:
                                continue

            except ValueError:
                continue

        return result

    def _propagate_attributions(
        self,
        source_attrs: list[GenesisAttribution],
        tx_amount: float,
        source_total: float,
        new_depth: int,
    ) -> list[GenesisAttribution]:
        """Propagate attributions from source to destination, scaling by ratio."""
        if source_total <= 0:
            return []

        ratio = min(1.0, tx_amount / source_total)
        result: list[GenesisAttribution] = []

        for attr in source_attrs:
            scaled_amount = attr.amount_avax * ratio
            if scaled_amount > 0:
                result.append(GenesisAttribution(
                    genesis_address=attr.genesis_address,
                    category=attr.category,
                    amount_avax=scaled_amount,
                    path_length=new_depth,
                ))

        return result

    def _save_checkpoint(
        self,
        config: GenesisTraceConfig,
        strategy: str,
        current_depth: int,
        queue: list[QueueItem],
        visited: set[bytes],
        destinations: dict[bytes, CChainDestination],
        total_genesis_avax: float,
    ) -> None:
        """Save a checkpoint."""
        if not config.checkpoint_path:
            return

        checkpoint = GenesisTraceCheckpoint(
            strategy=strategy,
            current_depth=current_depth,
            queue=queue,
            visited=visited,
            destinations=destinations,
            timestamp=datetime.now().isoformat(),
            max_depth=config.max_depth,
            min_amount_avax=config.min_amount_avax,
            total_genesis_avax=total_genesis_avax,
            total_exported_avax=sum(d.total_avax for d in destinations.values()),
        )
        save_checkpoint(checkpoint, config.checkpoint_path)

    def _print_summary(
        self,
        destinations: dict[bytes, CChainDestination],
        log: Callable[[str], None],
    ) -> None:
        """Print summary grouped by category."""
        if not destinations:
            log("\nNo C-chain destinations found.")
            return

        # Group by primary category
        by_category: dict[str, list[CChainDestination]] = {}
        for dest in destinations.values():
            cats = set(a.category for a in dest.attributions)
            if len(cats) > 1:
                key = "Mixed (multiple categories)"
            elif cats:
                key = list(cats)[0]
            else:
                key = "Unknown"

            if key not in by_category:
                by_category[key] = []
            by_category[key].append(dest)

        log("\n" + "=" * 60)
        log("C-Chain Destinations by Genesis Category")
        log("=" * 60)

        for category in sorted(by_category.keys()):
            dests = by_category[category]
            dests.sort(key=lambda x: -x.total_avax)
            total_cat_avax = sum(d.total_avax for d in dests)

            log(f"\n{category}: {len(dests)} destinations, {total_cat_avax:,.2f} AVAX")

            for dest in dests[:10]:
                depths = f"depth {dest.min_depth}-{dest.max_depth}" if dest.min_depth != dest.max_depth else f"depth {dest.min_depth}"
                log(f"  {dest.address.c_address}  {dest.total_avax:>14,.2f} AVAX "
                    f"({len(dest.attributions)} sources, {depths})")

            if len(dests) > 10:
                log(f"  ... and {len(dests) - 10} more")

            # For mixed category, show breakdown
            if category == "Mixed (multiple categories)":
                for dest in dests[:5]:
                    for attr in dest.attributions:
                        log(f"      - {attr.category}: {attr.amount_avax:,.2f} AVAX")

        # Summary totals
        total_exported = sum(d.total_avax for d in destinations.values())
        log(f"\nTotal: {len(destinations)} C-chain destinations, {total_exported:,.2f} AVAX exported")

    def _write_final_csv(
        self,
        destinations: list[CChainDestination],
        output_path: Path,
    ) -> None:
        """Write final CSV with accumulated totals, overwriting the incremental file."""
        # Sort by total_avax descending for nicer output
        sorted_dests = sorted(destinations, key=lambda d: -d.total_avax)

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "c_address", "total_avax", "categories", "num_sources",
                "min_depth", "max_depth", "genesis_sources"
            ])
            for dest in sorted_dests:
                # Get unique genesis source addresses
                source_addrs = sorted(set(
                    a.genesis_address.p_address for a in dest.attributions
                ))
                writer.writerow([
                    dest.address.c_address,
                    dest.total_avax,
                    dest.categories_str(),
                    len(dest.attributions),
                    dest.min_depth,
                    dest.max_depth,
                    "|".join(source_addrs),
                ])
