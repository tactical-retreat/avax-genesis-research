"""
Graph model for tracing fund flows across Avalanche chains.

The graph represents addresses as nodes and transactions as edges,
supporting cross-chain tracing with metadata about each hop.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .address import AvaxAddress, Chain


class TransactionType(Enum):
    """Types of transactions that move funds."""
    # P-Chain
    ADD_VALIDATOR = "AddValidatorTx"
    ADD_DELEGATOR = "AddDelegatorTx"
    ADD_SUBNET_VALIDATOR = "AddSubnetValidatorTx"
    CREATE_CHAIN = "CreateChainTx"
    CREATE_SUBNET = "CreateSubnetTx"
    REWARD_VALIDATOR = "RewardValidatorTx"
    ADVANCE_TIME = "AdvanceTimeTx"

    # X-Chain
    BASE = "BaseTx"  # Simple transfer
    CREATE_ASSET = "CreateAssetTx"
    OPERATION = "OperationTx"

    # Cross-chain
    IMPORT = "ImportTx"  # Funds arriving from another chain
    EXPORT = "ExportTx"  # Funds leaving to another chain

    # C-Chain
    EVM_TRANSFER = "EvmTransfer"  # Native AVAX transfer
    EVM_CONTRACT = "EvmContract"  # Contract interaction
    ATOMIC_IMPORT = "AtomicImportTx"  # C-chain atomic import
    ATOMIC_EXPORT = "AtomicExportTx"  # C-chain atomic export

    # Special
    GENESIS = "Genesis"  # Genesis allocation
    UNKNOWN = "Unknown"


@dataclass
class GraphEdge:
    """
    An edge in the trace graph representing a transfer of funds.

    Direction is from source -> target (funds flow direction).
    """
    source: AvaxAddress
    target: AvaxAddress
    chain: Chain
    tx_hash: str
    amount_navax: int  # Amount in nanoAVAX (1 AVAX = 1e9 nAVAX)
    timestamp: datetime
    tx_type: TransactionType
    block_height: int | None = None

    # For cross-chain transfers, track the linked transaction
    linked_tx_hash: str | None = None  # The corresponding Import/Export tx
    linked_chain: Chain | None = None

    @property
    def amount_avax(self) -> float:
        """Amount in AVAX."""
        return self.amount_navax / 1e9

    def __str__(self) -> str:
        return (
            f"{self.source.c_address[:10]}... -> {self.target.c_address[:10]}... "
            f"({self.amount_avax:.4f} AVAX on {self.chain.value}-chain)"
        )


@dataclass
class GraphNode:
    """
    A node in the trace graph representing an address.

    Contains metadata about the address including genesis categorization.
    """
    address: AvaxAddress

    # Genesis metadata (if this address received funds at genesis)
    genesis_category: str | None = None  # e.g., "16 quarterly unlocks"
    genesis_amount_avax: float = 0.0
    is_initial_validator: bool = False
    node_id: str | None = None  # For validators

    # Trace metadata
    depth: int = 0  # Depth from the starting address
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    # Aggregated statistics
    total_received_avax: float = 0.0
    total_sent_avax: float = 0.0

    def __hash__(self) -> int:
        return hash(self.address)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, GraphNode):
            return self.address == other.address
        return False


@dataclass
class TraceGraph:
    """
    A directed graph representing fund flows across Avalanche chains.

    Supports BFS traversal, cycle detection, and path finding.
    """
    nodes: dict[AvaxAddress, GraphNode] = field(default_factory=dict)

    # Edges indexed by source address (outgoing edges)
    outgoing: dict[AvaxAddress, list[GraphEdge]] = field(default_factory=dict)

    # Edges indexed by target address (incoming edges)
    incoming: dict[AvaxAddress, list[GraphEdge]] = field(default_factory=dict)

    # Track all edges for iteration
    all_edges: list[GraphEdge] = field(default_factory=list)

    def add_node(self, node: GraphNode) -> None:
        """Add a node to the graph."""
        if node.address not in self.nodes:
            self.nodes[node.address] = node
            self.outgoing[node.address] = []
            self.incoming[node.address] = []

    def get_or_create_node(self, address: AvaxAddress) -> GraphNode:
        """Get existing node or create a new one."""
        if address not in self.nodes:
            self.add_node(GraphNode(address=address))
        return self.nodes[address]

    def add_edge(self, edge: GraphEdge) -> None:
        """Add an edge to the graph, creating nodes if needed."""
        # Ensure nodes exist
        self.get_or_create_node(edge.source)
        self.get_or_create_node(edge.target)

        # Add edge to indices
        self.outgoing[edge.source].append(edge)
        self.incoming[edge.target].append(edge)
        self.all_edges.append(edge)

        # Update node statistics
        source_node = self.nodes[edge.source]
        target_node = self.nodes[edge.target]
        source_node.total_sent_avax += edge.amount_avax
        target_node.total_received_avax += edge.amount_avax

        # Update timestamps
        if source_node.first_seen is None or edge.timestamp < source_node.first_seen:
            source_node.first_seen = edge.timestamp
        if source_node.last_seen is None or edge.timestamp > source_node.last_seen:
            source_node.last_seen = edge.timestamp
        if target_node.first_seen is None or edge.timestamp < target_node.first_seen:
            target_node.first_seen = edge.timestamp
        if target_node.last_seen is None or edge.timestamp > target_node.last_seen:
            target_node.last_seen = edge.timestamp

    def get_incoming_edges(self, address: AvaxAddress) -> list[GraphEdge]:
        """Get all edges where funds flow TO this address."""
        return self.incoming.get(address, [])

    def get_outgoing_edges(self, address: AvaxAddress) -> list[GraphEdge]:
        """Get all edges where funds flow FROM this address."""
        return self.outgoing.get(address, [])

    def get_sources(self, address: AvaxAddress) -> Iterator[AvaxAddress]:
        """Get all addresses that sent funds to this address."""
        for edge in self.get_incoming_edges(address):
            yield edge.source

    def get_targets(self, address: AvaxAddress) -> Iterator[AvaxAddress]:
        """Get all addresses that received funds from this address."""
        for edge in self.get_outgoing_edges(address):
            yield edge.target

    def get_genesis_nodes(self) -> list[GraphNode]:
        """Get all nodes that have genesis allocations."""
        return [n for n in self.nodes.values() if n.genesis_category is not None]

    def find_paths_to_genesis(self, address: AvaxAddress, max_depth: int = 20) -> list[list[GraphEdge]]:
        """
        Find all paths from an address back to genesis addresses.

        Uses BFS to find shortest paths first.
        """
        paths: list[list[GraphEdge]] = []
        visited: set[AvaxAddress] = set()

        # BFS with path tracking
        queue: list[tuple[AvaxAddress, list[GraphEdge]]] = [(address, [])]

        while queue:
            current, path = queue.pop(0)

            if len(path) > max_depth:
                continue

            if current in visited:
                continue
            visited.add(current)

            node = self.nodes.get(current)
            if node and node.genesis_category:
                # Found a path to genesis
                paths.append(path)
                continue

            # Continue searching incoming edges
            for edge in self.get_incoming_edges(current):
                new_path = path + [edge]
                queue.append((edge.source, new_path))

        return paths

    def summary(self) -> dict:
        """Get a summary of the graph."""
        genesis_by_category: dict[str, int] = {}
        for node in self.nodes.values():
            if node.genesis_category:
                genesis_by_category[node.genesis_category] = (
                    genesis_by_category.get(node.genesis_category, 0) + 1
                )

        tx_types: dict[str, int] = {}
        for edge in self.all_edges:
            tx_types[edge.tx_type.value] = tx_types.get(edge.tx_type.value, 0) + 1

        chains: dict[str, int] = {}
        for edge in self.all_edges:
            chains[edge.chain.value] = chains.get(edge.chain.value, 0) + 1

        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.all_edges),
            "genesis_nodes": sum(genesis_by_category.values()),
            "genesis_by_category": genesis_by_category,
            "edges_by_tx_type": tx_types,
            "edges_by_chain": chains,
        }
