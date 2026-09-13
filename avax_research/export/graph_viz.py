"""
Graph visualization export (Mermaid and Graphviz).
"""

from pathlib import Path
from typing import TextIO

from ..models.address import AvaxAddress, Chain
from ..tracer.bfs_tracer import TraceResult

# Category colors for visualization
# Keys are genesis vesting categories from scripts/fetch_genesis.py (matched as substrings)
CATEGORY_COLORS = {
    "40 quarterly unlocks": "#ff6b6b",  # Red
    "16 quarterly unlocks": "#feca57",  # Yellow
    "6 quarterly unlocks": "#48dbfb",  # Light blue
    "4 quarterly unlocks": "#1dd1a1",  # Green
    "Single unlock": "#a29bfe",  # Purple
    "No lockup": "#ffeaa7",  # Pale yellow
    "Unknown": "#c8d6e5",  # Gray
}


def _short_addr(addr: AvaxAddress) -> str:
    """Get short form of address for display."""
    return str(addr)[:10] + "..."


def _get_node_color(category: str | None) -> str:
    """Get color for a node based on category."""
    if not category:
        return CATEGORY_COLORS["Unknown"]

    for key, color in CATEGORY_COLORS.items():
        if key.lower() in category.lower():
            return color

    return CATEGORY_COLORS["Unknown"]


class GraphVizExporter:
    """
    Export trace results as graph visualizations.

    Supports:
    - Mermaid diagrams (for Markdown/GitHub)
    - Graphviz DOT format
    """

    def __init__(self, output_dir: str | Path = "data/results"):
        """
        Initialize the exporter.

        Args:
            output_dir: Directory to write output files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_mermaid(
        self,
        result: TraceResult,
        filename: str = "graph.mmd",
        max_nodes: int = 50,
        max_edges: int = 100,
    ) -> Path:
        """
        Export graph as Mermaid diagram.

        Args:
            result: Trace result
            filename: Output filename
            max_nodes: Maximum nodes to include
            max_edges: Maximum edges to include

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w") as f:
            f.write("```mermaid\n")
            f.write("graph TD\n")

            self._write_mermaid_nodes(f, result, max_nodes)
            self._write_mermaid_edges(f, result, max_edges)
            self._write_mermaid_styles(f, result)

            f.write("```\n")

        return output_path

    def _write_mermaid_nodes(
        self,
        f: TextIO,
        result: TraceResult,
        max_nodes: int,
    ) -> None:
        """Write Mermaid node definitions."""
        # Prioritize: starting addresses, genesis, high volume
        priority_nodes = []

        # Starting addresses always included
        for addr in result.starting_addresses:
            if addr in result.graph.nodes:
                priority_nodes.append(result.graph.nodes[addr])

        # Genesis addresses
        for addr, _ in result.genesis_matches:
            if addr in result.graph.nodes and result.graph.nodes[addr] not in priority_nodes:
                priority_nodes.append(result.graph.nodes[addr])

        # Fill remaining with high-volume nodes
        remaining = max_nodes - len(priority_nodes)
        if remaining > 0:
            other_nodes = sorted(
                [n for n in result.graph.nodes.values() if n not in priority_nodes],
                key=lambda n: n.total_received_avax + n.total_sent_avax,
                reverse=True
            )
            priority_nodes.extend(other_nodes[:remaining])

        # Track which nodes we're including
        self._included_nodes: set[bytes] = {n.address.raw_bytes for n in priority_nodes}

        # Write node definitions
        for node in priority_nodes:
            node_id = self._mermaid_id(node.address)
            label = _short_addr(node.address)

            # Add category to label if genesis
            if node.genesis_category:
                # Abbreviate category
                cat_short = node.genesis_category.split()[0][:10]
                label = f"{label}<br/>{cat_short}"

            # Different shapes for different node types
            if node.address.raw_bytes in {a.raw_bytes for a in result.starting_addresses}:
                # Starting node - double circle
                f.write(f"    {node_id}(({label}))\n")
            elif node.genesis_category:
                # Genesis node - box
                f.write(f"    {node_id}[{label}]\n")
            else:
                # Regular node - rounded
                f.write(f"    {node_id}({label})\n")

    def _write_mermaid_edges(
        self,
        f: TextIO,
        result: TraceResult,
        max_edges: int,
    ) -> None:
        """Write Mermaid edge definitions."""
        # Only include edges between included nodes
        edges = [
            e for e in result.graph.all_edges
            if e.source.raw_bytes in self._included_nodes
            and e.target.raw_bytes in self._included_nodes
        ]

        # Sort by amount and take top edges
        edges = sorted(edges, key=lambda e: e.amount_navax, reverse=True)[:max_edges]

        for edge in edges:
            src_id = self._mermaid_id(edge.source)
            tgt_id = self._mermaid_id(edge.target)

            # Format amount
            if edge.amount_avax >= 1000:
                amount_str = f"{edge.amount_avax / 1000:.1f}K"
            else:
                amount_str = f"{edge.amount_avax:.1f}"

            # Different arrow styles for different chains
            if edge.chain == Chain.P:
                arrow = f"-->|P:{amount_str}|"
            elif edge.chain == Chain.X:
                arrow = f"-.->|X:{amount_str}|"
            else:
                arrow = f"==>|C:{amount_str}|"

            f.write(f"    {src_id} {arrow} {tgt_id}\n")

    def _write_mermaid_styles(
        self,
        f: TextIO,
        result: TraceResult,
    ) -> None:
        """Write Mermaid style definitions."""
        # Style genesis nodes by category
        styled: set[str] = set()

        for addr, match in result.genesis_matches:
            if addr.raw_bytes not in self._included_nodes:
                continue

            node_id = self._mermaid_id(addr)
            color = _get_node_color(match.category)

            if node_id not in styled:
                f.write(f"    style {node_id} fill:{color}\n")
                styled.add(node_id)

        # Style starting addresses
        for addr in result.starting_addresses:
            node_id = self._mermaid_id(addr)
            if node_id not in styled:
                f.write(f"    style {node_id} fill:#a29bfe,stroke:#6c5ce7,stroke-width:3px\n")
                styled.add(node_id)

    def _mermaid_id(self, addr: AvaxAddress) -> str:
        """Generate a valid Mermaid node ID."""
        # Use first 8 chars of hex address
        return ("e" if addr.is_evm else "p") + addr.raw_bytes.hex()[:8]

    def export_graphviz(
        self,
        result: TraceResult,
        filename: str = "graph.dot",
        max_nodes: int = 100,
        max_edges: int = 200,
    ) -> Path:
        """
        Export graph as Graphviz DOT format.

        Args:
            result: Trace result
            filename: Output filename
            max_nodes: Maximum nodes to include
            max_edges: Maximum edges to include

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w") as f:
            f.write("digraph FundFlow {\n")
            f.write("    rankdir=BT;\n")  # Bottom to top (funds flow up)
            f.write("    node [shape=box, style=rounded];\n")
            f.write("    edge [fontsize=10];\n\n")

            self._write_graphviz_nodes(f, result, max_nodes)
            self._write_graphviz_edges(f, result, max_edges)

            f.write("}\n")

        return output_path

    def _write_graphviz_nodes(
        self,
        f: TextIO,
        result: TraceResult,
        max_nodes: int,
    ) -> None:
        """Write Graphviz node definitions."""
        # Similar prioritization as Mermaid
        priority_nodes = []

        for addr in result.starting_addresses:
            if addr in result.graph.nodes:
                priority_nodes.append(result.graph.nodes[addr])

        for addr, _ in result.genesis_matches:
            if addr in result.graph.nodes and result.graph.nodes[addr] not in priority_nodes:
                priority_nodes.append(result.graph.nodes[addr])

        remaining = max_nodes - len(priority_nodes)
        if remaining > 0:
            other_nodes = sorted(
                [n for n in result.graph.nodes.values() if n not in priority_nodes],
                key=lambda n: n.total_received_avax + n.total_sent_avax,
                reverse=True
            )
            priority_nodes.extend(other_nodes[:remaining])

        self._included_nodes = {n.address.raw_bytes for n in priority_nodes}

        for node in priority_nodes:
            node_id = self._graphviz_id(node.address)
            label = _short_addr(node.address)

            attrs = []

            # Color by category
            if node.genesis_category:
                color = _get_node_color(node.genesis_category)
                attrs.append(f'fillcolor="{color}"')
                attrs.append('style="filled,rounded"')
                label += f"\\n{node.genesis_category[:20]}"

            # Special shape for starting addresses
            if node.address.raw_bytes in {a.raw_bytes for a in result.starting_addresses}:
                attrs.append('shape=doubleoctagon')
                attrs.append('penwidth=3')

            attrs.append(f'label="{label}"')

            f.write(f'    {node_id} [{", ".join(attrs)}];\n')

    def _write_graphviz_edges(
        self,
        f: TextIO,
        result: TraceResult,
        max_edges: int,
    ) -> None:
        """Write Graphviz edge definitions."""
        edges = [
            e for e in result.graph.all_edges
            if e.source.raw_bytes in self._included_nodes
            and e.target.raw_bytes in self._included_nodes
        ]

        edges = sorted(edges, key=lambda e: e.amount_navax, reverse=True)[:max_edges]

        f.write("\n")
        for edge in edges:
            src_id = self._graphviz_id(edge.source)
            tgt_id = self._graphviz_id(edge.target)

            # Format amount
            if edge.amount_avax >= 1000:
                amount_str = f"{edge.amount_avax / 1000:.1f}K AVAX"
            else:
                amount_str = f"{edge.amount_avax:.2f} AVAX"

            # Color by chain
            colors = {"C": "blue", "P": "red", "X": "green"}
            color = colors.get(edge.chain.value, "black")

            f.write(
                f'    {src_id} -> {tgt_id} '
                f'[label="{amount_str}", color="{color}"];\n'
            )

    def _graphviz_id(self, addr: AvaxAddress) -> str:
        """Generate a valid Graphviz node ID."""
        return ("e" if addr.is_evm else "p") + addr.raw_bytes.hex()[:8]

    def export_mermaid_markdown(
        self,
        result: TraceResult,
        filename: str = "graph.md",
        title: str = "Fund Flow Graph",
        max_nodes: int = 50,
        max_edges: int = 100,
    ) -> Path:
        """
        Export graph as Markdown file with embedded Mermaid.

        Args:
            result: Trace result
            filename: Output filename
            title: Document title
            max_nodes: Maximum nodes to include
            max_edges: Maximum edges to include

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w") as f:
            f.write(f"# {title}\n\n")

            # Legend
            f.write("## Legend\n\n")
            f.write("- **Double circle**: Starting/target address\n")
            f.write("- **Box**: Genesis allocation address\n")
            f.write("- **Rounded**: Intermediate address\n")
            f.write("- **C:amount**: C-chain transfer\n")
            f.write("- **P:amount**: P-chain transfer\n")
            f.write("- **X:amount**: X-chain transfer\n\n")

            # Color legend
            f.write("### Category Colors\n\n")
            for cat, color in CATEGORY_COLORS.items():
                f.write(f"- {cat}: `{color}`\n")
            f.write("\n")

            # Graph
            f.write("## Graph\n\n")

            # Mermaid diagram (without the wrapper since we're in a markdown file)
            f.write("```mermaid\n")
            f.write("graph TD\n")

            self._write_mermaid_nodes(f, result, max_nodes)
            self._write_mermaid_edges(f, result, max_edges)
            self._write_mermaid_styles(f, result)

            f.write("```\n")

        return output_path

    def export_all(
        self,
        result: TraceResult,
        prefix: str = "",
    ) -> dict[str, Path]:
        """
        Export all visualization formats.

        Args:
            result: Trace result
            prefix: Optional filename prefix

        Returns:
            Dict mapping format name to output path
        """
        prefix = f"{prefix}_" if prefix else ""

        return {
            "mermaid": self.export_mermaid(result, f"{prefix}graph.mmd"),
            "mermaid_md": self.export_mermaid_markdown(result, f"{prefix}graph.md"),
            "graphviz": self.export_graphviz(result, f"{prefix}graph.dot"),
        }
