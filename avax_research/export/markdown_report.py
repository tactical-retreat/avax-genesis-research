"""
Markdown report generator for trace results.
"""

from datetime import datetime
from pathlib import Path
from typing import TextIO

from ..models.address import AvaxAddress
from ..tracer.bfs_tracer import TraceResult


class MarkdownReporter:
    """
    Generate Markdown reports from trace results.
    """

    def __init__(self, output_dir: str | Path = "data/results"):
        """
        Initialize the reporter.

        Args:
            output_dir: Directory to write output files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_report(
        self,
        result: TraceResult,
        filename: str = "report.md",
        title: str = "AVAX Fund Tracing Report",
    ) -> Path:
        """
        Generate a Markdown report.

        Args:
            result: Trace result
            filename: Output filename
            title: Report title

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w") as f:
            self._write_header(f, title)
            self._write_summary(f, result)
            self._write_starting_addresses(f, result)
            self._write_genesis_findings(f, result)
            self._write_top_nodes(f, result)
            self._write_errors(f, result)

        return output_path

    def _write_header(self, f: TextIO, title: str) -> None:
        """Write report header."""
        f.write(f"# {title}\n\n")
        f.write(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n")

    def _write_summary(self, f: TextIO, result: TraceResult) -> None:
        """Write summary section."""
        summary = result.summary()

        f.write("## Summary\n\n")
        f.write("| Metric | Value |\n")
        f.write("|--------|-------|\n")
        f.write(f"| Starting addresses | {summary['starting_addresses']} |\n")
        f.write(f"| Total addresses traced | {summary['addresses_visited']} |\n")
        f.write(f"| Transactions processed | {summary['transactions_processed']} |\n")
        f.write(f"| Max depth reached | {summary['max_depth_reached']} |\n")
        f.write(f"| Genesis matches | {summary['genesis_matches']} |\n")
        f.write(f"| Total nodes | {summary['total_nodes']} |\n")
        f.write(f"| Total edges | {summary['total_edges']} |\n")
        f.write(f"| Errors | {summary['errors']} |\n")
        f.write("\n")

        # Genesis categories breakdown
        if summary.get('genesis_categories'):
            f.write("### Genesis Categories Found\n\n")
            f.write("| Category | Count |\n")
            f.write("|----------|-------|\n")
            for cat, count in sorted(
                summary['genesis_categories'].items(),
                key=lambda x: x[1],
                reverse=True
            ):
                f.write(f"| {cat} | {count} |\n")
            f.write("\n")

        # Edge breakdown by chain
        if summary.get('edges_by_chain'):
            f.write("### Transactions by Chain\n\n")
            f.write("| Chain | Count |\n")
            f.write("|-------|-------|\n")
            for chain, count in sorted(summary['edges_by_chain'].items()):
                f.write(f"| {chain}-chain | {count} |\n")
            f.write("\n")

    def _write_starting_addresses(self, f: TextIO, result: TraceResult) -> None:
        """Write starting addresses section."""
        f.write("## Starting Addresses\n\n")

        for addr in result.starting_addresses:
            f.write(f"### `{addr}`\n\n")
            f.write("| Format | Address |\n")
            f.write("|--------|----------|\n")
            if addr.is_evm:
                f.write(f"| C-Chain | `{addr.c_address}` |\n")
            else:
                f.write(f"| P-Chain | `{addr.p_address}` |\n")
                f.write(f"| X-Chain | `{addr.x_address}` |\n")

            # Check if this is a genesis address
            for ga, match in result.genesis_matches:
                if ga.raw_bytes == addr.raw_bytes:
                    f.write(f"\n**Genesis Allocation:** {match.category} ({match.total_avax:,.2f} AVAX)\n")
                    if match.is_validator:
                        f.write(f"\n**Initial Validator:** {match.node_id}\n")
                    break

            f.write("\n")

    def _write_genesis_findings(self, f: TextIO, result: TraceResult) -> None:
        """Write genesis findings section."""
        if not result.genesis_matches:
            f.write("## Genesis Findings\n\n")
            f.write("*No genesis addresses found in trace.*\n\n")
            return

        f.write("## Genesis Findings\n\n")
        f.write(f"Found **{len(result.genesis_matches)}** genesis addresses in the trace.\n\n")

        # Group by category
        by_category: dict[str, list[tuple[AvaxAddress, any]]] = {}
        for addr, match in result.genesis_matches:
            cat = match.category
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append((addr, match))

        for category in sorted(by_category.keys()):
            matches = by_category[category]
            total_avax = sum(m.total_avax for _, m in matches)

            f.write(f"### {category}\n\n")
            f.write(f"**{len(matches)} addresses** | **{total_avax:,.2f} AVAX** total allocation\n\n")

            # Show top 10 by allocation
            top_matches = sorted(matches, key=lambda x: x[1].total_avax, reverse=True)[:10]

            f.write("| Address | Total AVAX | Validator |\n")
            f.write("|-------------------|------------|----------|\n")
            for addr, match in top_matches:
                validator = match.node_id[:20] + "..." if match.node_id else "No"
                f.write(f"| `{str(addr)[:14]}...` | {match.total_avax:,.2f} | {validator} |\n")

            if len(matches) > 10:
                f.write(f"\n*...and {len(matches) - 10} more addresses*\n")

            f.write("\n")

    def _write_top_nodes(self, f: TextIO, result: TraceResult) -> None:
        """Write top nodes section."""
        f.write("## Top Addresses by Volume\n\n")

        # Sort by total received
        nodes = sorted(
            result.graph.nodes.values(),
            key=lambda n: n.total_received_avax,
            reverse=True
        )[:20]

        f.write("### Top Recipients\n\n")
        f.write("| Address | Received (AVAX) | Category | Depth |\n")
        f.write("|---------|-----------------|----------|-------|\n")

        for node in nodes:
            category = node.genesis_category or "-"
            f.write(
                f"| `{str(node.address)[:14]}...` | "
                f"{node.total_received_avax:,.2f} | "
                f"{category} | {node.depth} |\n"
            )

        f.write("\n")

        # Top senders
        senders = sorted(
            result.graph.nodes.values(),
            key=lambda n: n.total_sent_avax,
            reverse=True
        )[:20]

        f.write("### Top Senders\n\n")
        f.write("| Address | Sent (AVAX) | Category | Depth |\n")
        f.write("|---------|-------------|----------|-------|\n")

        for node in senders:
            if node.total_sent_avax == 0:
                continue
            category = node.genesis_category or "-"
            f.write(
                f"| `{str(node.address)[:14]}...` | "
                f"{node.total_sent_avax:,.2f} | "
                f"{category} | {node.depth} |\n"
            )

        f.write("\n")

    def _write_errors(self, f: TextIO, result: TraceResult) -> None:
        """Write errors section if any."""
        if not result.errors:
            return

        f.write("## Errors\n\n")
        f.write(f"*{len(result.errors)} errors occurred during tracing:*\n\n")

        for error in result.errors[:20]:  # Limit to first 20
            f.write(f"- {error}\n")

        if len(result.errors) > 20:
            f.write(f"\n*...and {len(result.errors) - 20} more errors*\n")

        f.write("\n")
