"""
CLI for tracing AVAX fund flows.

Usage:
    python -m avax_research.cli.trace --addresses addresses.txt --max-depth 10 --output-dir data/results
    python -m avax_research.cli.trace --address 0x1234... --max-depth 5

    # Find related destinations (addresses funded by same sources)
    python -m avax_research.cli.trace --address 0x1234... --mode related
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from ..clients.glacier_client import GlacierClient
from ..export.csv_export import CSVExporter
from ..export.graph_viz import GraphVizExporter
from ..export.markdown_report import MarkdownReporter
from ..models.address import Chain
from ..tracer.bfs_tracer import BFSTracer, TraceConfig
from ..tracer.genesis_forward import GenesisForwardTracer, GenesisTraceConfig, TraceStrategy
from ..tracer.genesis_matcher import GenesisMatcher


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    )


def load_addresses(addresses_arg: list[str], addresses_file: str | None) -> list[str]:
    """Load addresses from arguments and/or file."""
    addresses = list(addresses_arg) if addresses_arg else []

    if addresses_file:
        path = Path(addresses_file)
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        # Handle CSV format (take first column)
                        if "," in line:
                            line = line.split(",")[0].strip()
                        addresses.append(line)

    return addresses


def progress_callback(depth: int, count: int, message: str) -> None:
    """Print progress updates."""
    print(f"  [Depth {depth}] {count} addresses - {message}", end="\r", flush=True)


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Trace AVAX fund flows across P/X/C chains to genesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Trace a single address
  python -m avax_research.cli.trace --address 0x1234...

  # Trace from a file of addresses
  python -m avax_research.cli.trace --addresses addresses.txt --output-dir data/results

  # Trace with custom depth and minimum amount
  python -m avax_research.cli.trace --address 0x1234... --max-depth 15 --min-amount 100
        """,
    )

    # Address input
    addr_group = parser.add_mutually_exclusive_group()
    addr_group.add_argument(
        "--address", "-a",
        action="append",
        dest="addresses",
        help="Address to trace (can specify multiple times)",
    )
    addr_group.add_argument(
        "--addresses-file", "-f",
        help="File containing addresses (one per line, or CSV)",
    )

    # Tracing mode
    parser.add_argument(
        "--mode", "-m",
        choices=["source", "related", "destinations", "genesis", "exports", "stream", "from-genesis"],
        default="source",
        help="Tracing mode: "
             "'from-genesis' traces forward from genesis addresses to C-chain exports, "
             "'stream' follows largest flows from P/X chain, prints C-chain exports live, "
             "'genesis' traces from C-chain backward through P/X to genesis (recommended), "
             "'exports' traces from P/X chain forward to find C-chain destinations, "
             "'source' traces backward on all chains, "
             "'related' finds addresses funded by same sources, "
             "'destinations' traces forward on all chains",
    )
    parser.add_argument(
        "--no-balances",
        action="store_true",
        help="Skip fetching current C-chain balances (faster, for destinations mode)",
    )

    # Tracing options
    parser.add_argument(
        "--max-depth", "-d",
        type=int,
        default=10,
        help="Maximum depth to trace (default: 10)",
    )
    parser.add_argument(
        "--min-amount",
        type=float,
        default=0.0,
        help="Minimum transfer amount in AVAX to follow (default: 0)",
    )
    parser.add_argument(
        "--chains",
        nargs="+",
        choices=["C", "P", "X"],
        default=["C", "P", "X"],
        help="Chains to trace (default: all)",
    )
    parser.add_argument(
        "--include-staking",
        action="store_true",
        help="Include staking transactions (excluded by default for value transfers only)",
    )
    parser.add_argument(
        "--no-cross-chain",
        action="store_true",
        help="Don't follow cross-chain transfers",
    )
    parser.add_argument(
        "--continue-past-genesis",
        action="store_true",
        help="Continue tracing past genesis addresses",
    )

    # From-genesis mode options
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help="Genesis category to trace from (can specify multiple times, for from-genesis mode)",
    )
    parser.add_argument(
        "--strategy",
        choices=["bfs", "greedy", "hybrid"],
        default="bfs",
        help="Traversal strategy for from-genesis mode: 'bfs' (default), 'greedy', or 'hybrid'",
    )
    parser.add_argument(
        "--hybrid-depth",
        type=int,
        default=5,
        help="Depth to switch from BFS to greedy in hybrid strategy (default: 5)",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        help="Path to checkpoint file for resumable tracing (default: {output-dir}/{prefix}_checkpoint.json)",
    )

    # Output options
    parser.add_argument(
        "--output-dir", "-o",
        default="data/results",
        help="Output directory (default: data/results)",
    )
    parser.add_argument(
        "--format",
        nargs="+",
        choices=["csv", "json", "markdown", "mermaid", "graphviz", "all"],
        default=["all"],
        help="Output formats (default: all)",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Prefix for output filenames",
    )

    # Other options
    parser.add_argument(
        "--genesis-csv",
        default="data/genesis/all_allocations.csv",
        help="Path to genesis allocations CSV",
    )
    parser.add_argument(
        "--api-key",
        help="Glacier API key (overrides GLACIER_API_KEYS and glacier_api_keys.txt)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=2.0,
        help="API rate limit (requests/second, default: 2)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Minimal output",
    )

    args = parser.parse_args()

    # Setup logging
    if args.quiet:
        logging.basicConfig(level=logging.ERROR)
    else:
        setup_logging(args.verbose)

    logger = logging.getLogger(__name__)

    # Load addresses
    addresses = load_addresses(args.addresses or [], args.addresses_file)
    if not addresses and args.mode != "from-genesis":
        parser.error("No addresses provided. Use --address or --addresses-file")
    # from-genesis mode: if no filter specified, trace all genesis addresses

    if not args.quiet:
        print("\nAVAX Fund Tracing")
        print("=" * 40)
        print(f"Addresses to trace: {len(addresses)}")
        print(f"Max depth: {args.max_depth}")
        print(f"Chains: {', '.join(args.chains)}")
        print(f"Output directory: {args.output_dir}")
        print()

    # Initialize components
    try:
        glacier_kwargs = {"rate_limit": args.rate_limit}
        if args.api_key:
            glacier_kwargs["api_key"] = args.api_key

        client = GlacierClient(**glacier_kwargs)
        genesis = GenesisMatcher(args.genesis_csv)
        tracer = BFSTracer(client, genesis)

        # Configure trace
        config = TraceConfig(
            max_depth=args.max_depth,
            min_amount_avax=args.min_amount,
            include_staking=args.include_staking,  # Default False (value transfers only)
            include_cross_chain=not args.no_cross_chain,
            stop_at_genesis=not args.continue_past_genesis,
            chains=[Chain[c] for c in args.chains],
        )

        # Run trace based on mode
        if args.mode == "from-genesis":
            # Trace forward from genesis addresses to C-chain exports
            if not args.quiet:
                print("Tracing forward from genesis to C-chain exports...")

            # Create forward tracer
            forward_tracer = GenesisForwardTracer(client, genesis)

            # Build config
            strategy_map = {
                "bfs": TraceStrategy.BFS,
                "greedy": TraceStrategy.GREEDY,
                "hybrid": TraceStrategy.HYBRID,
            }
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            prefix = args.prefix or datetime.now().strftime("%Y%m%d_%H%M%S")

            # Default checkpoint path if not specified
            checkpoint_path = Path(args.checkpoint) if args.checkpoint else output_dir / f"{prefix}_checkpoint.json"

            forward_config = GenesisTraceConfig(
                max_depth=args.max_depth,
                min_amount_avax=args.min_amount,
                strategy=strategy_map[args.strategy],
                hybrid_switch_depth=args.hybrid_depth,
                include_staking=args.include_staking,
                checkpoint_path=checkpoint_path,
                output_path=output_dir / f"{prefix}_from_genesis.csv",
            )

            if not args.quiet:
                print(f"Checkpoint: {checkpoint_path}")

            # Run the trace
            forward_result = forward_tracer.trace_from_genesis(
                categories=args.categories,
                addresses=addresses if addresses else None,
                config=forward_config,
            )

            if not args.quiet:
                print("\nFrom-genesis trace complete!")
                print(f"  Genesis addresses traced: {len(forward_result.starting_addresses)}")
                print(f"  Total genesis AVAX: {forward_result.total_genesis_avax:,.2f}")
                print(f"  C-chain destinations found: {len(forward_result.destinations)}")
                print(f"  Total exported AVAX: {forward_result.total_exported_avax:,.2f}")
                print(f"  Addresses visited: {forward_result.addresses_visited}")
                print(f"  Max depth reached: {forward_result.max_depth_reached}")
                if forward_result.errors:
                    print(f"  Errors: {len(forward_result.errors)}")
                print()

            return 0

        elif args.mode == "stream":
            # Streaming mode - print exports as found
            if len(addresses) > 1:
                logger.warning("Stream mode only uses the first address")

            stream_config = TraceConfig(
                max_depth=args.max_depth,
                min_amount_avax=args.min_amount,
                include_staking=args.include_staking,
                include_cross_chain=True,
                stop_at_genesis=False,
                chains=[Chain.P, Chain.X],
            )

            exports = tracer.stream_exports(addresses[0], stream_config)

            # Save exports to CSV
            if exports:
                output_dir = Path(args.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                prefix = args.prefix or datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_path = output_dir / f"{prefix}_stream_exports.csv"

                import csv
                with open(csv_path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["tx_hash", "source_chain", "source_address", "dest_address", "amount_avax", "timestamp"])
                    for e in exports:
                        writer.writerow([e.tx_hash, e.source_chain, e.source_address, e.dest_address, e.amount_avax, e.timestamp.isoformat()])
                print(f"\nExported to {csv_path}")

            return 0

        elif args.mode == "genesis":
            # Trace from C-chain backward through P/X only to genesis
            if len(addresses) > 1:
                logger.warning("Genesis mode only uses the first address")

            if not args.quiet:
                print("Tracing to genesis (P/X chain only)...")

            # Include C-chain for atomic imports, plus P/X for the trace
            genesis_config = TraceConfig(
                max_depth=args.max_depth,
                min_amount_avax=args.min_amount,
                include_staking=args.include_staking,
                include_cross_chain=True,  # Need this to follow cross-chain
                stop_at_genesis=True,
                chains=[Chain.C, Chain.P, Chain.X],  # All chains - C for atomic imports
            )

            result = tracer.trace(
                addresses,
                genesis_config,
                progress_callback=None if args.quiet else progress_callback,
            )

            if not args.quiet:
                print("\n")  # Clear progress line
                print("Genesis trace complete!")
                print(f"  Addresses visited: {result.addresses_visited}")
                print(f"  Transactions processed: {result.transactions_processed}")
                print(f"  Genesis matches: {len(result.genesis_matches)}")
                print(f"  Max depth reached: {result.max_depth_reached}")
                if result.errors:
                    print(f"  Errors: {len(result.errors)}")
                print()

                # Print genesis findings
                if result.genesis_matches:
                    print("Genesis Sources Found:")
                    categories: dict[str, list] = {}
                    for addr, match in result.genesis_matches:
                        if match.category not in categories:
                            categories[match.category] = []
                        categories[match.category].append((addr, match))
                    for cat, matches in sorted(categories.items(), key=lambda x: -len(x[1])):
                        print(f"\n  {cat}: {len(matches)} addresses")
                        for addr, match in matches[:5]:
                            print(f"    {addr.p_address}: {match.total_avax:,.0f} AVAX")
                        if len(matches) > 5:
                            print(f"    ... and {len(matches) - 5} more")

        elif args.mode == "exports":
            # Trace from P/X chain forward to find C-chain destinations
            if len(addresses) > 1:
                logger.warning("Exports mode only uses the first address")

            if not args.quiet:
                print("Tracing exports to C-chain (P/X chain only)...")

            # Override chains to P/X only
            exports_config = TraceConfig(
                max_depth=args.max_depth,
                min_amount_avax=args.min_amount,
                include_staking=args.include_staking,
                include_cross_chain=True,
                stop_at_genesis=False,  # We're going forward, not back
                chains=[Chain.P, Chain.X],  # Only P/X chain
            )

            dest_result = tracer.trace_destinations(
                addresses[0],
                exports_config,
                fetch_balances=not args.no_balances,
                progress_callback=None if args.quiet else progress_callback,
            )

            if not args.quiet:
                print("\n")  # Clear progress line
                print("Export trace complete!")
                print(f"  Addresses visited: {dest_result.addresses_visited}")
                print(f"  Transactions processed: {dest_result.transactions_processed}")
                print(f"  Total outflow: {dest_result.total_outflow_avax:,.2f} AVAX")
                print(f"  Destinations found: {len(dest_result.destinations)}")
                if dest_result.errors:
                    print(f"  Errors: {len(dest_result.errors)}")
                print()

                # Print destinations with C-chain format
                if dest_result.c_chain_destinations:
                    print("C-chain destinations (via P/X chain exports):")
                    print(f"{'Address':<44} {'Received':>14} {'Sent Back':>12} {'Net':>14} {'Balance':>14}")
                    print("-" * 100)
                    for dest in dest_result.c_chain_destinations[:30]:
                        balance_str = f"{dest.current_balance_avax:,.2f}" if dest.current_balance_avax is not None else "N/A"
                        print(
                            f"{str(dest.address):<44} "
                            f"{dest.total_received_avax:>14,.2f} "
                            f"{dest.total_sent_back_avax:>12,.2f} "
                            f"{dest.net_received_avax:>14,.2f} "
                            f"{balance_str:>14}"
                        )
                    if len(dest_result.c_chain_destinations) > 30:
                        print(f"  ... and {len(dest_result.c_chain_destinations) - 30} more")

            # Create compatible result for export
            result = type('Result', (), {
                'graph': dest_result.graph,
                'starting_addresses': [dest_result.starting_address],
                'genesis_matches': [],
                'max_depth_reached': exports_config.max_depth,
                'addresses_visited': dest_result.addresses_visited,
                'transactions_processed': dest_result.transactions_processed,
                'errors': dest_result.errors,
                'summary': lambda self: dest_result.summary(),
            })()

        elif args.mode == "related":
            # Find related destinations mode
            if len(addresses) > 1:
                logger.warning("Related mode only uses the first address")

            if not args.quiet:
                print("Finding related destinations...")

            related_result = tracer.find_related_destinations(
                addresses[0],
                config,
                progress_callback=None if args.quiet else progress_callback,
            )

            if not args.quiet:
                print("\n")  # Clear progress line
                print("Related destinations found!")
                print(f"  Funding sources: {len(related_result.funding_sources)}")
                print(f"  Related destinations: {len(related_result.related_destinations)}")
                if related_result.errors:
                    print(f"  Errors: {len(related_result.errors)}")
                print()

                # Print top related destinations
                if related_result.related_destinations:
                    print("Top related destinations (funded by same sources):")
                    for i, dest in enumerate(related_result.related_destinations[:20]):
                        print(f"  {i+1}. {dest.address}: {dest.total_amount_avax:.2f} AVAX")
                    if len(related_result.related_destinations) > 20:
                        print(f"  ... and {len(related_result.related_destinations) - 20} more")

            # For related mode, use the source graph as the main result for export
            # Create a simple result object for compatibility
            result = type('Result', (), {
                'graph': related_result.source_graph,
                'starting_addresses': [related_result.starting_address],
                'genesis_matches': [],
                'max_depth_reached': config.max_depth,
                'addresses_visited': len(related_result.source_graph.nodes),
                'transactions_processed': len(related_result.source_graph.all_edges),
                'errors': related_result.errors,
                'summary': lambda self: related_result.summary(),
            })()

        elif args.mode == "destinations":
            # Trace forward to find destinations
            if len(addresses) > 1:
                logger.warning("Destinations mode only uses the first address")

            if not args.quiet:
                print("Tracing forward to destinations...")

            dest_result = tracer.trace_destinations(
                addresses[0],
                config,
                fetch_balances=not args.no_balances,
                progress_callback=None if args.quiet else progress_callback,
            )

            if not args.quiet:
                print("\n")  # Clear progress line
                print("Forward trace complete!")
                print(f"  Addresses visited: {dest_result.addresses_visited}")
                print(f"  Transactions processed: {dest_result.transactions_processed}")
                print(f"  Total outflow: {dest_result.total_outflow_avax:,.2f} AVAX")
                print(f"  Destinations found: {len(dest_result.destinations)}")
                if dest_result.errors:
                    print(f"  Errors: {len(dest_result.errors)}")
                print()

                # Print top destinations with balances
                if dest_result.c_chain_destinations:
                    print("Top C-chain destinations:")
                    print(f"{'Address':<44} {'Received':>14} {'Sent Back':>12} {'Net':>14} {'Balance':>14}")
                    print("-" * 100)
                    for dest in dest_result.c_chain_destinations[:30]:
                        balance_str = f"{dest.current_balance_avax:,.2f}" if dest.current_balance_avax is not None else "N/A"
                        print(
                            f"{str(dest.address):<44} "
                            f"{dest.total_received_avax:>14,.2f} "
                            f"{dest.total_sent_back_avax:>12,.2f} "
                            f"{dest.net_received_avax:>14,.2f} "
                            f"{balance_str:>14}"
                        )
                    if len(dest_result.c_chain_destinations) > 30:
                        print(f"  ... and {len(dest_result.c_chain_destinations) - 30} more")

            # Create a compatible result object for export
            result = type('Result', (), {
                'graph': dest_result.graph,
                'starting_addresses': [dest_result.starting_address],
                'genesis_matches': [],
                'max_depth_reached': config.max_depth,
                'addresses_visited': dest_result.addresses_visited,
                'transactions_processed': dest_result.transactions_processed,
                'errors': dest_result.errors,
                'summary': lambda self: dest_result.summary(),
            })()

        else:
            # Source tracing mode (default)
            if not args.quiet:
                print("Tracing fund flows...")

            result = tracer.trace(
                addresses,
                config,
                progress_callback=None if args.quiet else progress_callback,
            )

            if not args.quiet:
                print("\n")  # Clear progress line
                print("Trace complete!")
                print(f"  Addresses visited: {result.addresses_visited}")
                print(f"  Transactions processed: {result.transactions_processed}")
                print(f"  Genesis matches: {len(result.genesis_matches)}")
                print(f"  Max depth reached: {result.max_depth_reached}")
                if result.errors:
                    print(f"  Errors: {len(result.errors)}")
                print()

        # Export results
        output_dir = Path(args.output_dir)
        formats = set(args.format)

        if "all" in formats:
            formats = {"csv", "json", "markdown", "mermaid", "graphviz"}

        prefix = args.prefix or datetime.now().strftime("%Y%m%d_%H%M%S")
        exported_files: list[str] = []

        if "csv" in formats or "json" in formats:
            csv_exporter = CSVExporter(output_dir)
            if "csv" in formats:
                csv_exporter.export_edges(result, f"{prefix}_edges.csv")
                csv_exporter.export_nodes(result, f"{prefix}_nodes.csv")
                csv_exporter.export_genesis_matches(result, f"{prefix}_genesis.csv")
                exported_files.extend(["edges.csv", "nodes.csv", "genesis.csv"])
                # Export cross-chain transactions if available (exports/destinations mode)
                if args.mode in ("exports", "destinations") and hasattr(dest_result, "cross_chain_txs"):
                    csv_exporter.export_cross_chain_txs(dest_result.cross_chain_txs, f"{prefix}_cross_chain.csv")
                    exported_files.append("cross_chain.csv")
            if "json" in formats:
                csv_exporter.export_json(result, f"{prefix}_result.json")
                exported_files.append("result.json")

        if "markdown" in formats:
            md_reporter = MarkdownReporter(output_dir)
            md_reporter.generate_report(result, f"{prefix}_report.md")
            exported_files.append("report.md")

        if "mermaid" in formats or "graphviz" in formats:
            viz_exporter = GraphVizExporter(output_dir)
            if "mermaid" in formats:
                viz_exporter.export_mermaid_markdown(result, f"{prefix}_graph.md")
                exported_files.append("graph.md")
            if "graphviz" in formats:
                viz_exporter.export_graphviz(result, f"{prefix}_graph.dot")
                exported_files.append("graph.dot")

        if not args.quiet:
            print(f"Exported to {output_dir}/:")
            for f in exported_files:
                print(f"  - {prefix}_{f}")
            print()

            # Print summary of genesis findings
            if result.genesis_matches:
                print("Genesis Sources Found:")
                categories: dict[str, int] = {}
                for _, match in result.genesis_matches:
                    categories[match.category] = categories.get(match.category, 0) + 1
                for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
                    print(f"  {cat}: {count}")
            else:
                print("No genesis addresses found in trace.")

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    except Exception as e:
        logger.exception(f"Error: {e}")
        if not args.quiet:
            print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
