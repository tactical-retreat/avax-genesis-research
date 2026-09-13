"""
CSV and JSON export for trace results.
"""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..tracer.bfs_tracer import CrossChainTx, TraceResult


class CSVExporter:
    """
    Export trace results to CSV and JSON formats.
    """

    def __init__(self, output_dir: str | Path = "data/results"):
        """
        Initialize the exporter.

        Args:
            output_dir: Directory to write output files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_edges(
        self,
        result: TraceResult,
        filename: str = "edges.csv",
    ) -> Path:
        """
        Export graph edges to CSV.

        Args:
            result: Trace result
            filename: Output filename

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "source_address_c",
                "source_address_p",
                "target_address_c",
                "target_address_p",
                "chain",
                "tx_hash",
                "amount_avax",
                "timestamp",
                "tx_type",
                "block_height",
                "source_category",
                "target_category",
            ])

            for edge in result.graph.all_edges:
                source_node = result.graph.nodes.get(edge.source)
                target_node = result.graph.nodes.get(edge.target)

                writer.writerow([
                    edge.source.c_address,
                    edge.source.p_address,
                    edge.target.c_address,
                    edge.target.p_address,
                    edge.chain.value,
                    edge.tx_hash,
                    f"{edge.amount_avax:.9f}",
                    edge.timestamp.isoformat(),
                    edge.tx_type.value,
                    edge.block_height or "",
                    source_node.genesis_category if source_node else "",
                    target_node.genesis_category if target_node else "",
                ])

        return output_path

    def export_nodes(
        self,
        result: TraceResult,
        filename: str = "nodes.csv",
    ) -> Path:
        """
        Export graph nodes to CSV.

        Args:
            result: Trace result
            filename: Output filename

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "address_c",
                "address_p",
                "address_x",
                "depth",
                "genesis_category",
                "genesis_amount_avax",
                "is_initial_validator",
                "node_id",
                "total_received_avax",
                "total_sent_avax",
                "first_seen",
                "last_seen",
            ])

            for node in result.graph.nodes.values():
                writer.writerow([
                    node.address.c_address,
                    node.address.p_address,
                    node.address.x_address,
                    node.depth,
                    node.genesis_category or "",
                    f"{node.genesis_amount_avax:.2f}" if node.genesis_amount_avax else "",
                    node.is_initial_validator,
                    node.node_id or "",
                    f"{node.total_received_avax:.9f}",
                    f"{node.total_sent_avax:.9f}",
                    node.first_seen.isoformat() if node.first_seen else "",
                    node.last_seen.isoformat() if node.last_seen else "",
                ])

        return output_path

    def export_genesis_matches(
        self,
        result: TraceResult,
        filename: str = "genesis_matches.csv",
    ) -> Path:
        """
        Export genesis matches to CSV.

        Args:
            result: Trace result
            filename: Output filename

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "address_c",
                "address_p",
                "category",
                "total_avax",
                "initial_avax",
                "locked_avax",
                "is_validator",
                "node_id",
            ])

            for addr, match in result.genesis_matches:
                writer.writerow([
                    addr.c_address,
                    addr.p_address,
                    match.category,
                    f"{match.total_avax:.2f}",
                    f"{match.initial_avax:.2f}",
                    f"{match.locked_avax:.2f}",
                    match.is_validator,
                    match.node_id or "",
                ])

        return output_path

    def export_cross_chain_txs(
        self,
        cross_chain_txs: list[CrossChainTx],
        filename: str = "cross_chain_txs.csv",
    ) -> Path:
        """
        Export import/export transactions to CSV.

        Args:
            cross_chain_txs: List of cross-chain transactions
            filename: Output filename

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "tx_hash",
                "tx_type",
                "source_chain",
                "dest_chain",
                "source_address",
                "dest_address",
                "amount_avax",
                "timestamp",
                "block_height",
            ])

            for tx in cross_chain_txs:
                writer.writerow([
                    tx.tx_hash,
                    tx.tx_type,
                    tx.source_chain,
                    tx.dest_chain,
                    tx.source_address,
                    tx.dest_address,
                    f"{tx.amount_avax:.9f}",
                    tx.timestamp.isoformat(),
                    tx.block_height or "",
                ])

        return output_path

    def export_json(
        self,
        result: TraceResult,
        filename: str = "trace_result.json",
    ) -> Path:
        """
        Export full trace result to JSON.

        Args:
            result: Trace result
            filename: Output filename

        Returns:
            Path to the output file
        """
        output_path = self.output_dir / filename

        def serialize(obj: Any) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if hasattr(obj, "raw_bytes"):  # AvaxAddress
                return {
                    "c": obj.c_address,
                    "p": obj.p_address,
                    "x": obj.x_address,
                }
            if hasattr(obj, "value"):  # Enum
                return obj.value
            if hasattr(obj, "__dict__"):
                return {k: serialize(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
            if isinstance(obj, (list, tuple)):
                return [serialize(item) for item in obj]
            if isinstance(obj, dict):
                return {k: serialize(v) for k, v in obj.items()}
            if isinstance(obj, bytes):
                return obj.hex()
            return obj

        data = {
            "summary": result.summary(),
            "starting_addresses": [
                {"c": a.c_address, "p": a.p_address, "x": a.x_address}
                for a in result.starting_addresses
            ],
            "genesis_matches": [
                {
                    "address": {"c": a.c_address, "p": a.p_address},
                    "category": m.category,
                    "total_avax": m.total_avax,
                    "is_validator": m.is_validator,
                    "node_id": m.node_id,
                }
                for a, m in result.genesis_matches
            ],
            "nodes": [
                serialize(node)
                for node in result.graph.nodes.values()
            ],
            "edges": [
                serialize(edge)
                for edge in result.graph.all_edges
            ],
            "errors": result.errors,
        }

        with open(output_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

        return output_path

    def export_all(
        self,
        result: TraceResult,
        prefix: str = "",
    ) -> dict[str, Path]:
        """
        Export all formats.

        Args:
            result: Trace result
            prefix: Optional filename prefix

        Returns:
            Dict mapping format name to output path
        """
        prefix = f"{prefix}_" if prefix else ""

        return {
            "edges": self.export_edges(result, f"{prefix}edges.csv"),
            "nodes": self.export_nodes(result, f"{prefix}nodes.csv"),
            "genesis": self.export_genesis_matches(result, f"{prefix}genesis_matches.csv"),
            "json": self.export_json(result, f"{prefix}trace_result.json"),
        }
