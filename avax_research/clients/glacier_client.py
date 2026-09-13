"""
High-level Glacier API client wrapper.

Provides a clean interface for querying P/X/C chain data with:
- Automatic pagination
- Rate limiting with proper 429 handling
- Disk caching (transactions are immutable)
- Cross-chain address handling
"""

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from typing import Any, TypeVar

import httpx
from glacier_client.api.evm_transactions import (
    list_native_transactions,
)
from glacier_client.api.primary_network import (
    get_chain_ids_for_addresses,
)
from glacier_client.api.primary_network_balances import (
    get_balances_by_addresses,
)
from glacier_client.api.primary_network_transactions import (
    get_tx_by_hash,
    list_latest_primary_network_transactions,
)

# The generated Glacier client (github.com/tactical-retreat/glacier-client)
from glacier_client.client import Client
from glacier_client.models.blockchain_id import BlockchainId
from glacier_client.models.network import Network
from glacier_client.models.primary_network_tx_type import PrimaryNetworkTxType
from glacier_client.models.sort_order import SortOrder
from glacier_client.models.too_many_requests import TooManyRequests
from glacier_client.types import UNSET, Response

from ..api_keys import load_api_keys
from ..cache.disk_cache import DiskCache
from ..cache.rate_limiter import RateLimiter
from ..models.address import AvaxAddress, Chain

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Both hosts serve the same API, but existing keys are only accepted by glacier-api (data-api answers
# "Api key is invalid" for them, checked 2026-09-13). Unauthenticated requests work on either.
GLACIER_BASE_URL = "https://glacier-api.avax.network"


@dataclass
class TransactionRecord:
    """Normalized transaction record from any chain."""
    tx_hash: str
    chain: Chain
    tx_type: str
    timestamp: datetime
    block_number: int
    from_addresses: list[str]
    to_addresses: list[str]
    amount_navax: int  # nanoAVAX
    is_cross_chain: bool = False
    source_chain: str | None = None
    destination_chain: str | None = None
    raw_data: dict = field(default_factory=dict)

    @property
    def amount_avax(self) -> float:
        return self.amount_navax / 1e9


class RateLimitError(Exception):
    """Raised when rate limit is exceeded and retries are exhausted."""
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class GlacierClient:
    """
    High-level Glacier API client with caching and rate limiting.

    Properly handles 429 rate limit responses using retry-after headers.
    Supports multiple API keys with automatic rotation on rate limit.

    Usage:
        # Single key (backwards compatible)
        client = GlacierClient()

        # Multiple keys for higher throughput (default: glacier_api_keys.txt, see README)
        client = GlacierClient(api_keys=["key1", "key2", "key3"])

        txs = list(client.get_p_chain_transactions("P-avax1..."))
    """
    # Support both single key (legacy) and multiple keys
    api_key: str | None = None  # Deprecated, use api_keys
    api_keys: list[str] = field(default_factory=load_api_keys)  # [] = unauthenticated
    # Rate limit per key
    rate_limit: float = 1.0  # requests per second per key
    cache_path: str = "data/cache/glacier_cache.db"
    network: Network = field(default_factory=lambda: Network.MAINNET)
    max_retries: int = 10  # Max retries on rate limit (with exponential backoff)

    _client: Client = field(init=False)
    _cache: DiskCache = field(init=False)
    _rate_limiter: RateLimiter = field(init=False)
    _remaining_requests: int | None = field(init=False, default=None)
    _current_key_index: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        # Handle legacy single api_key parameter
        if self.api_key is not None:
            self.api_keys = [self.api_key]

        if self.api_keys:
            logger.info(f"GlacierClient initialized with {len(self.api_keys)} API key(s)")
        else:
            logger.warning("No Glacier API keys configured; using the unauthenticated rate limit (see README)")
        self._keys: list[str | None] = list(self.api_keys) or [None]

        self._current_key_index = 0
        self._client = self._make_client(self._keys[0])
        self._cache = DiskCache(self.cache_path)
        # Rate limit scales with number of keys (each key has its own quota)
        effective_rate = self.rate_limit * len(self._keys)
        self._rate_limiter = RateLimiter(rate=effective_rate, burst=2)

    def _make_client(self, api_key: str | None) -> Client:
        """Create a new client with the given API key (None = unauthenticated)."""
        return Client(
            base_url=GLACIER_BASE_URL,
            headers={"x-glacier-api-key": api_key} if api_key else {},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def _get_next_client(self) -> Client:
        """Get client with next API key (round-robin)."""
        self._current_key_index = (self._current_key_index + 1) % len(self._keys)
        return self._make_client(self._keys[self._current_key_index])

    @property
    def current_api_key(self) -> str | None:
        """Get the currently active API key (None when unauthenticated)."""
        return self._keys[self._current_key_index]

    def _wait_rate_limit(self) -> None:
        """Wait for rate limit token."""
        self._rate_limiter.acquire()

    @staticmethod
    def _to_checksum_address(address: str) -> str:
        """Convert address to EIP-55 checksum format."""
        from Crypto.Hash import keccak
        address = address.lower().replace("0x", "")
        k = keccak.new(digest_bits=256)
        k.update(address.encode())
        hash_hex = k.hexdigest()
        checksum = "0x"
        for i, char in enumerate(address):
            if char in "0123456789":
                checksum += char
            elif int(hash_hex[i], 16) >= 8:
                checksum += char.upper()
            else:
                checksum += char
        return checksum

    def _handle_response(self, response: Response[T], operation: str = "request") -> T:
        """
        Handle API response, checking for rate limits and errors.

        Args:
            response: Response from sync_detailed call
            operation: Description for logging

        Returns:
            Parsed response data

        Raises:
            RateLimitError: If rate limited and retries exhausted
        """
        # Update our tracking of remaining requests
        if "ratelimit-remaining" in response.headers:
            try:
                self._remaining_requests = int(response.headers["ratelimit-remaining"])
                if self._remaining_requests < 10:
                    logger.warning(f"Low rate limit remaining: {self._remaining_requests}")
            except ValueError:
                pass

        # Log rate limit info at debug level
        if "ratelimit-limit" in response.headers:
            logger.debug(
                f"Rate limit: {response.headers.get('ratelimit-remaining', '?')}/"
                f"{response.headers.get('ratelimit-limit', '?')} remaining"
            )

        # Check for rate limit error - check both int and HTTPStatus
        status_code = response.status_code
        is_rate_limited = (
            status_code == 429
            or status_code == HTTPStatus.TOO_MANY_REQUESTS
            or isinstance(response.parsed, TooManyRequests)
        )

        if is_rate_limited:
            retry_after = None
            if "retry-after" in response.headers:
                try:
                    retry_after = float(response.headers["retry-after"])
                except ValueError:
                    pass

            # Also check ratelimit-reset
            if retry_after is None and "ratelimit-reset" in response.headers:
                try:
                    retry_after = float(response.headers["ratelimit-reset"])
                except ValueError:
                    pass

            if retry_after is None:
                retry_after = 60.0  # Default to 60 seconds

            logger.warning(
                f"Rate limited on {operation}. status={status_code}, retry-after: {retry_after}s"
            )

            # Report to rate limiter for backoff
            self._rate_limiter.report_rate_limit(retry_after)

            raise RateLimitError(
                f"Rate limited on {operation}",
                retry_after=retry_after
            )

        # Check for other HTTP errors
        if status_code >= 400:
            error_msg = f"HTTP {status_code} on {operation}"
            if response.parsed:
                error_msg += f": {response.parsed}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        return response.parsed

    def _call_with_retry(self, func, *args, operation: str = "request", **kwargs) -> Any:
        """
        Call an API function with retry logic for rate limits.

        Rotates API keys round-robin on every request.

        Args:
            func: The sync_detailed function to call
            *args: Positional args for func
            operation: Description for logging
            **kwargs: Keyword args for func

        Returns:
            Parsed response
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._wait_rate_limit()

            # Rotate to next key for each request
            self._client = self._get_next_client()

            try:
                response = func(*args, **kwargs)
                result = self._handle_response(response, operation)
                if attempt > 0:
                    logger.info(f"Retry successful for {operation}")
                self._rate_limiter.reset_backoff()  # Success, reset backoff
                return result

            except RateLimitError as e:
                last_error = e
                if attempt < self.max_retries:
                    # Use retry_after header if available, otherwise exponential backoff
                    base_wait = min(5 * (2 ** attempt), 60)
                    wait_time = e.retry_after if e.retry_after else base_wait
                    logger.warning(
                        f"Rate limited on {operation}, waiting {wait_time:.1f}s before retry "
                        f"(attempt {attempt + 1}/{self.max_retries})"
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"Rate limit retries exhausted for {operation} "
                        f"after {self.max_retries + 1} attempts"
                    )

            except RuntimeError as e:
                # Non-retryable errors (like 4xx errors other than 429)
                logger.error(f"Non-retryable error on {operation}: {e}")
                raise

        if last_error:
            raise last_error
        raise RuntimeError(f"Unexpected state in _call_with_retry for {operation}")

    def get_chain_ids_for_address(self, address: str | AvaxAddress) -> list[str]:
        """
        Get the chain IDs where an address has activity.

        Args:
            address: Address in any format

        Returns:
            List of chain IDs (e.g., ["p-chain", "x-chain"])
        """
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        if not address.is_primary:
            return []  # the endpoint takes P/X addresses only
        addr_str = address.p_address

        # Check cache
        cached = self._cache.get("chain_ids", addr_str)
        if cached is not None:
            return cached

        response = self._call_with_retry(
            get_chain_ids_for_addresses.sync_detailed,
            network=self.network,
            addresses=addr_str,
            client=self._client,
            operation=f"get_chain_ids({addr_str[:20]}...)",
        )

        if hasattr(response, "addresses"):
            chain_ids = []
            for addr_info in response.addresses:
                if hasattr(addr_info, "chain_ids"):
                    chain_ids.extend(addr_info.chain_ids)
            self._cache.set("chain_ids", addr_str, value=chain_ids, address=addr_str)
            return chain_ids

        return []

    def get_p_chain_transactions(
        self,
        address: str | AvaxAddress,
        tx_types: list[str] | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> Iterator[TransactionRecord]:
        """
        Get P-chain transactions for an address.

        Args:
            address: Address in any format (will be converted to P-chain format)
            tx_types: Filter by transaction types
            start_timestamp: Start timestamp (seconds)
            end_timestamp: End timestamp (seconds)
            page_size: Results per page
            max_pages: Maximum pages to fetch

        Yields:
            TransactionRecord for each transaction
        """
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        if not address.is_primary:
            return  # a C-Chain address has no P-Chain transactions
        addr_str = address.p_address

        # Build deterministic cache key - sort tx_types for consistency
        tx_types_str = ",".join(sorted(tx_types)) if tx_types else ""
        cache_key = f"{addr_str}:{tx_types_str}:{start_timestamp or ''}:{end_timestamp or ''}"

        # Check if we have cached transactions
        cached = self._cache.get("p_chain_txs", cache_key)
        if cached is not None:
            logger.debug(f"Cache HIT for P-chain txs: {addr_str[:20]}... ({len(cached)} txs)")
            for tx_data in cached:
                yield self._parse_p_chain_tx(tx_data)
            return

        logger.debug(f"Cache MISS for P-chain txs: {addr_str[:20]}...")

        # Fetch ALL transactions first, then cache, then yield
        # This ensures caching happens even if consumer doesn't fully consume the generator
        all_txs: list[dict] = []
        page_token = UNSET
        pages_fetched = 0

        # Convert tx_types to enum
        tx_type_enums = None
        if tx_types:
            tx_type_enums = [PrimaryNetworkTxType(t) for t in tx_types]

        while pages_fetched < max_pages:
            try:
                response = self._call_with_retry(
                    list_latest_primary_network_transactions.sync_detailed,
                    network=self.network,
                    blockchain_id=BlockchainId.P_CHAIN,
                    addresses=addr_str,
                    tx_types=tx_type_enums if tx_type_enums else UNSET,
                    start_timestamp=start_timestamp if start_timestamp else UNSET,
                    end_timestamp=end_timestamp if end_timestamp else UNSET,
                    page_token=page_token,
                    page_size=page_size,
                    sort_order=SortOrder.ASC,
                    client=self._client,
                    operation=f"p_chain_txs({addr_str[:20]}..., page {pages_fetched})",
                )
            except RateLimitError:
                logger.error(f"Rate limit exhausted fetching P-chain txs for {addr_str}")
                break

            if not hasattr(response, "transactions"):
                logger.warning(f"Unexpected response for P-chain txs: {type(response)}")
                break

            for tx in response.transactions:
                all_txs.append(tx.to_dict())

            pages_fetched += 1

            if hasattr(response, "next_page_token") and response.next_page_token:
                page_token = response.next_page_token
            else:
                break

        # Cache results BEFORE yielding (including empty results to avoid re-querying)
        logger.debug(f"Caching {len(all_txs)} P-chain txs for {addr_str[:20]}...")
        self._cache.set(
            "p_chain_txs", cache_key,
            value=all_txs,
            chain="P",
            address=addr_str,
        )

        # Now yield from the collected transactions
        for tx_data in all_txs:
            yield self._parse_p_chain_tx(tx_data)

    def get_x_chain_transactions(
        self,
        address: str | AvaxAddress,
        tx_types: list[str] | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> Iterator[TransactionRecord]:
        """
        Get X-chain transactions for an address.

        Args:
            address: Address in any format (will be converted to X-chain format)
            tx_types: Filter by transaction types
            start_timestamp: Start timestamp (seconds)
            end_timestamp: End timestamp (seconds)
            page_size: Results per page
            max_pages: Maximum pages to fetch

        Yields:
            TransactionRecord for each transaction
        """
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        if not address.is_primary:
            return  # a C-Chain address has no X-Chain transactions
        addr_str = address.x_address

        # Build deterministic cache key - sort tx_types for consistency
        tx_types_str = ",".join(sorted(tx_types)) if tx_types else ""
        cache_key = f"{addr_str}:{tx_types_str}:{start_timestamp or ''}:{end_timestamp or ''}"

        # Check cache
        cached = self._cache.get("x_chain_txs", cache_key)
        if cached is not None:
            logger.debug(f"Cache HIT for X-chain txs: {addr_str[:20]}... ({len(cached)} txs)")
            for tx_data in cached:
                yield self._parse_x_chain_tx(tx_data)
            return

        logger.debug(f"Cache MISS for X-chain txs: {addr_str[:20]}...")

        # Fetch ALL transactions first, then cache, then yield
        all_txs: list[dict] = []
        page_token = UNSET
        pages_fetched = 0

        tx_type_enums = None
        if tx_types:
            tx_type_enums = [PrimaryNetworkTxType(t) for t in tx_types]

        while pages_fetched < max_pages:
            try:
                response = self._call_with_retry(
                    list_latest_primary_network_transactions.sync_detailed,
                    network=self.network,
                    blockchain_id=BlockchainId.X_CHAIN,
                    addresses=addr_str,
                    tx_types=tx_type_enums if tx_type_enums else UNSET,
                    start_timestamp=start_timestamp if start_timestamp else UNSET,
                    end_timestamp=end_timestamp if end_timestamp else UNSET,
                    page_token=page_token,
                    page_size=page_size,
                    sort_order=SortOrder.ASC,
                    client=self._client,
                    operation=f"x_chain_txs({addr_str[:20]}..., page {pages_fetched})",
                )
            except RateLimitError:
                logger.error(f"Rate limit exhausted fetching X-chain txs for {addr_str}")
                break

            if not hasattr(response, "transactions"):
                logger.warning(f"Unexpected response for X-chain txs: {type(response)}")
                break

            for tx in response.transactions:
                all_txs.append(tx.to_dict())

            pages_fetched += 1

            if hasattr(response, "next_page_token") and response.next_page_token:
                page_token = response.next_page_token
            else:
                break

        # Cache results BEFORE yielding (including empty results to avoid re-querying)
        logger.debug(f"Caching {len(all_txs)} X-chain txs for {addr_str[:20]}...")
        self._cache.set(
            "x_chain_txs", cache_key,
            value=all_txs,
            chain="X",
            address=addr_str,
        )

        # Now yield from the collected transactions
        for tx_data in all_txs:
            yield self._parse_x_chain_tx(tx_data)

    def get_c_chain_native_transactions(
        self,
        address: str | AvaxAddress,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> Iterator[TransactionRecord]:
        """
        Get C-chain native AVAX transactions for an address.

        Args:
            address: Address in any format (will be converted to C-chain format)
            page_size: Results per page
            max_pages: Maximum pages to fetch

        Yields:
            TransactionRecord for each transaction
        """
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        if not address.is_evm:
            return  # a P/X address has no C-Chain EVM transactions
        addr_str = address.c_address

        cache_key = f"{addr_str}:native"

        cached = self._cache.get("c_chain_txs", cache_key)
        if cached is not None:
            for tx_data in cached:
                yield self._parse_c_chain_tx(tx_data)
            return

        all_txs: list[dict] = []
        page_token = UNSET
        pages_fetched = 0

        while pages_fetched < max_pages:
            try:
                response = self._call_with_retry(
                    list_native_transactions.sync_detailed,
                    chain_id="43114",  # Avalanche C-chain
                    address=addr_str,
                    page_token=page_token if page_token else UNSET,
                    page_size=page_size,
                    client=self._client,
                    operation=f"c_chain_txs({addr_str[:12]}..., page {pages_fetched})",
                )
            except RateLimitError:
                logger.error(f"Rate limit exhausted fetching C-chain txs for {addr_str}")
                break

            if not hasattr(response, "transactions"):
                logger.warning(f"Unexpected response for C-chain txs: {type(response)}")
                break

            for tx in response.transactions:
                tx_dict = tx.to_dict()
                all_txs.append(tx_dict)
                yield self._parse_c_chain_tx(tx_dict)

            pages_fetched += 1

            if hasattr(response, "next_page_token") and response.next_page_token:
                page_token = response.next_page_token
            else:
                break

        if all_txs:
            self._cache.set(
                "c_chain_txs", cache_key,
                value=all_txs,
                chain="C",
                address=addr_str,
            )

    def get_transaction_by_hash(
        self,
        tx_hash: str,
        chain: Chain,
    ) -> TransactionRecord | None:
        """
        Get a specific transaction by hash.

        Args:
            tx_hash: Transaction hash
            chain: Chain where the transaction occurred

        Returns:
            TransactionRecord or None if not found
        """
        cache_key = f"{chain.value}:{tx_hash}"
        cached = self._cache.get("tx", cache_key)
        if cached is not None:
            if chain == Chain.P:
                return self._parse_p_chain_tx(cached)
            elif chain == Chain.X:
                return self._parse_x_chain_tx(cached)
            else:
                return self._parse_c_chain_tx(cached)

        if chain == Chain.C:
            # C-chain transactions need different handling
            logger.warning("C-chain tx lookup by hash not yet implemented")
            return None

        blockchain_id = BlockchainId.P_CHAIN if chain == Chain.P else BlockchainId.X_CHAIN

        try:
            response = self._call_with_retry(
                get_tx_by_hash.sync_detailed,
                network=self.network,
                blockchain_id=blockchain_id,
                tx_hash=tx_hash,
                client=self._client,
                operation=f"get_tx({tx_hash[:16]}...)",
            )
        except RateLimitError:
            logger.error(f"Rate limit exhausted fetching tx {tx_hash}")
            return None

        if hasattr(response, "tx_hash"):
            tx_dict = response.to_dict()
            self._cache.set("tx", cache_key, value=tx_dict, chain=chain.value)
            if chain == Chain.P:
                return self._parse_p_chain_tx(tx_dict)
            else:
                return self._parse_x_chain_tx(tx_dict)

        return None

    def get_balances(self, address: str | AvaxAddress) -> dict[str, int]:
        """
        Get current balances for an address on P/X chains.

        Args:
            address: Address in any format

        Returns:
            Dict mapping chain to balance in nanoAVAX
        """
        addr = address if isinstance(address, AvaxAddress) else AvaxAddress.from_any(address)
        if not addr.is_primary:
            return {}  # P/X balances only

        try:
            response = self._call_with_retry(
                get_balances_by_addresses.sync_detailed,
                network=self.network,
                addresses=addr.p_address,  # P and X use same address
                client=self._client,
                operation=f"get_balances({str(addr)[:16]}...)",
            )
        except RateLimitError:
            logger.error(f"Rate limit exhausted fetching balances for {addr}")
            return {}

        balances: dict[str, int] = {}
        if hasattr(response, "balances"):
            for balance in response.balances:
                if hasattr(balance, "chain_id") and hasattr(balance, "balance"):
                    chain_id = str(balance.chain_id)
                    if "p-chain" in chain_id.lower():
                        balances["P"] = int(balance.balance)
                    elif "x-chain" in chain_id.lower():
                        balances["X"] = int(balance.balance)

        return balances

    def _parse_p_chain_tx(self, tx_data: dict) -> TransactionRecord:
        """
        Parse P-chain transaction data into TransactionRecord.

        Handles UTXO change address filtering:
        - If an address appears in both inputs (consumed) and outputs (emitted),
          the output going back to that address is likely change, not a real transfer.
        - We filter these out from to_addresses to get the actual recipients.
        """
        # Extract addresses from consumed UTXOs (senders)
        from_addresses_set: set[str] = set()
        for utxo in tx_data.get("consumedUtxos", []):
            from_addresses_set.update(utxo.get("addresses", []))

        # Extract addresses from emitted UTXOs (recipients)
        # But filter out change addresses (sender appearing in outputs)
        to_addresses_list: list[str] = []
        for utxo in tx_data.get("emittedUtxos", []):
            addrs = utxo.get("addresses", [])
            for addr in addrs:
                # Normalize for comparison (case-insensitive)
                if addr.lower() not in {a.lower() for a in from_addresses_set}:
                    to_addresses_list.append(addr)

        # If all outputs went back to sender, it's a self-transfer or consolidation
        # In this case, keep the original addresses
        if not to_addresses_list and from_addresses_set:
            to_addresses_list = list(from_addresses_set)

        # Calculate total value - try value array first, then amountUnlocked
        amount = 0
        for val in tx_data.get("value", []):
            if val.get("assetId") == "FvwEAhmxKfeiG8SnEvq42hc6whRyY3EFYAvebMqDNDGCgxN5Z":
                # AVAX asset ID
                amount += int(val.get("amount", 0))
        # Fall back to amountUnlocked if value is empty
        if amount == 0:
            for val in tx_data.get("amountUnlocked", []):
                if val.get("assetId") == "FvwEAhmxKfeiG8SnEvq42hc6whRyY3EFYAvebMqDNDGCgxN5Z":
                    amount += int(val.get("amount", 0))

        # Check for cross-chain
        source_chain = tx_data.get("sourceChain")
        dest_chain = tx_data.get("destinationChain")
        is_cross_chain = source_chain is not None or dest_chain is not None

        return TransactionRecord(
            tx_hash=tx_data.get("txHash", ""),
            chain=Chain.P,
            tx_type=tx_data.get("txType", "Unknown"),
            timestamp=datetime.fromtimestamp(tx_data.get("blockTimestamp", 0)),
            block_number=int(tx_data.get("blockNumber", 0)),
            from_addresses=list(from_addresses_set),
            to_addresses=list(set(to_addresses_list)),
            amount_navax=amount,
            is_cross_chain=is_cross_chain,
            source_chain=source_chain,
            destination_chain=dest_chain,
            raw_data=tx_data,
        )

    def _parse_x_chain_tx(self, tx_data: dict) -> TransactionRecord:
        """
        Parse X-chain transaction data into TransactionRecord.

        Handles UTXO change address filtering:
        - If an address appears in both inputs (consumed) and outputs (emitted),
          the output going back to that address is likely change, not a real transfer.
        - We filter these out from to_addresses to get the actual recipients.
        """
        # Extract addresses from consumed UTXOs (senders)
        from_addresses_set: set[str] = set()
        for utxo in tx_data.get("consumedUtxos", []):
            from_addresses_set.update(utxo.get("addresses", []))

        # Extract addresses from emitted UTXOs (recipients)
        # But filter out change addresses (sender appearing in outputs)
        to_addresses_list: list[str] = []
        for utxo in tx_data.get("emittedUtxos", []):
            addrs = utxo.get("addresses", [])
            for addr in addrs:
                # Normalize for comparison (case-insensitive)
                if addr.lower() not in {a.lower() for a in from_addresses_set}:
                    to_addresses_list.append(addr)

        # If all outputs went back to sender, it's a self-transfer or consolidation
        if not to_addresses_list and from_addresses_set:
            to_addresses_list = list(from_addresses_set)

        # Calculate total value - X-chain uses amountUnlocked/amountBurned structure
        amount = 0
        # Try amountUnlocked first (present in most X-chain tx responses)
        for val in tx_data.get("amountUnlocked", []):
            # Check for AVAX asset ID
            if val.get("assetId") == "FvwEAhmxKfeiG8SnEvq42hc6whRyY3EFYAvebMqDNDGCgxN5Z":
                amount += int(val.get("amount", 0))
        # Fall back to value array if amountUnlocked is empty
        if amount == 0:
            for val in tx_data.get("value", []):
                if val.get("assetId") == "FvwEAhmxKfeiG8SnEvq42hc6whRyY3EFYAvebMqDNDGCgxN5Z":
                    amount += int(val.get("amount", 0))

        source_chain = tx_data.get("sourceChain")
        dest_chain = tx_data.get("destinationChain")
        is_cross_chain = source_chain is not None or dest_chain is not None

        # X-chain uses 'timestamp' field (not 'blockTimestamp')
        ts = tx_data.get("timestamp") or tx_data.get("blockTimestamp") or 0

        return TransactionRecord(
            tx_hash=tx_data.get("txHash", ""),
            chain=Chain.X,
            tx_type=tx_data.get("txType", "Unknown"),
            timestamp=datetime.fromtimestamp(ts),
            block_number=int(tx_data.get("blockNumber") or tx_data.get("blockHeight") or 0),
            from_addresses=list(from_addresses_set),
            to_addresses=list(set(to_addresses_list)),
            amount_navax=amount,
            is_cross_chain=is_cross_chain,
            source_chain=source_chain,
            destination_chain=dest_chain,
            raw_data=tx_data,
        )

    def get_c_chain_atomic_transactions(
        self,
        address: str | AvaxAddress,
        tx_types: list[str] | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> Iterator[TransactionRecord]:
        """
        Get C-chain atomic transactions (ImportTx/ExportTx) for an address.

        These are cross-chain transfers to/from C-chain via the Primary Network API.

        Args:
            address: Address in any format
            tx_types: Filter by transaction types (e.g., ["ImportTx", "ExportTx"])
            page_size: Results per page
            max_pages: Maximum pages to fetch

        Yields:
            TransactionRecord for each transaction
        """
        # The endpoint indexes atomic transactions by both sides: the 0x EVM input/output addresses and the
        # bech32 owners of the P/X UTXOs they consume or create.
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        if address.is_evm:
            addr_str = self._to_checksum_address(address.c_address)
        else:
            addr_str = address.bech32

        # Build deterministic cache key - sort tx_types for consistency
        tx_types_str = ",".join(sorted(tx_types)) if tx_types else ""
        cache_key = f"{addr_str}:atomic:{tx_types_str}"

        cached = self._cache.get("c_chain_atomic_txs", cache_key)
        if cached is not None:
            logger.debug(f"Cache HIT for C-chain atomic txs: {addr_str[:12]}... ({len(cached)} txs)")
            for tx_data in cached:
                yield self._parse_c_chain_atomic_tx(tx_data)
            return

        logger.debug(f"Cache MISS for C-chain atomic txs: {addr_str[:12]}...")

        # Fetch ALL transactions first, then cache, then yield
        all_txs: list[dict] = []
        page_token = UNSET
        pages_fetched = 0

        tx_type_enums = None
        if tx_types:
            tx_type_enums = [PrimaryNetworkTxType(t) for t in tx_types]

        while pages_fetched < max_pages:
            try:
                response = self._call_with_retry(
                    list_latest_primary_network_transactions.sync_detailed,
                    network=self.network,
                    blockchain_id=BlockchainId.VALUE_3,  # C-chain full ID
                    addresses=addr_str,
                    tx_types=tx_type_enums if tx_type_enums else UNSET,
                    page_token=page_token,
                    page_size=page_size,
                    sort_order=SortOrder.ASC,
                    client=self._client,
                    operation=f"c_chain_atomic_txs({addr_str[:12]}..., page {pages_fetched})",
                )
            except RateLimitError:
                logger.error(f"Rate limit exhausted fetching C-chain atomic txs for {addr_str}")
                break

            if not hasattr(response, "transactions"):
                logger.warning(f"Unexpected response for C-chain atomic txs: {type(response)}")
                break

            for tx in response.transactions:
                all_txs.append(tx.to_dict())

            pages_fetched += 1

            if hasattr(response, "next_page_token") and response.next_page_token:
                page_token = response.next_page_token
            else:
                break

        # Cache results BEFORE yielding (including empty results to avoid re-querying)
        logger.debug(f"Caching {len(all_txs)} C-chain atomic txs for {addr_str[:12]}...")
        self._cache.set(
            "c_chain_atomic_txs", cache_key,
            value=all_txs,
            chain="C",
            address=addr_str,
        )

        # Now yield from the collected transactions
        for tx_data in all_txs:
            yield self._parse_c_chain_atomic_tx(tx_data)

    def _parse_c_chain_atomic_tx(self, tx_data: dict) -> TransactionRecord:
        """Parse C-chain atomic transaction (ImportTx/ExportTx) into TransactionRecord."""
        # Extract addresses from consumed UTXOs (sources)
        from_addresses_set: set[str] = set()
        for utxo in tx_data.get("consumedUtxos", []):
            from_addresses_set.update(utxo.get("addresses", []))

        # Extract addresses from EVM outputs (destinations for ImportTx)
        to_addresses_list: list[str] = []
        for output in tx_data.get("evmOutputs", []):
            if "toAddress" in output:
                to_addresses_list.append(output["toAddress"])

        # Also check emittedUtxos for ExportTx
        for utxo in tx_data.get("emittedUtxos", []):
            addrs = utxo.get("addresses", [])
            for addr in addrs:
                if addr.lower() not in {a.lower() for a in from_addresses_set}:
                    to_addresses_list.append(addr)

        # Calculate total value from amountUnlocked
        amount = 0
        for val in tx_data.get("amountUnlocked", []):
            if val.get("assetId") == "FvwEAhmxKfeiG8SnEvq42hc6whRyY3EFYAvebMqDNDGCgxN5Z":
                amount += int(val.get("amount", 0))

        source_chain = tx_data.get("sourceChain")
        dest_chain = tx_data.get("destinationChain")

        return TransactionRecord(
            tx_hash=tx_data.get("txHash", ""),
            chain=Chain.C,
            tx_type=tx_data.get("txType", "Unknown"),
            timestamp=datetime.fromtimestamp(tx_data.get("timestamp", 0)),
            block_number=int(tx_data.get("blockHeight", 0)),
            from_addresses=list(from_addresses_set),
            to_addresses=list(set(to_addresses_list)),
            amount_navax=amount,
            is_cross_chain=True,
            source_chain=source_chain,
            destination_chain=dest_chain,
            raw_data=tx_data,
        )

    def _parse_c_chain_tx(self, tx_data: dict) -> TransactionRecord:
        """Parse C-chain transaction data into TransactionRecord."""
        # C-chain values are in wei (1e18), convert to nAVAX (1e9)
        value_wei = int(tx_data.get("value", 0))
        value_navax = value_wei // (10 ** 9)  # wei to nAVAX

        return TransactionRecord(
            tx_hash=tx_data.get("txHash", tx_data.get("hash", "")),
            chain=Chain.C,
            tx_type="EvmTransfer",
            timestamp=datetime.fromtimestamp(tx_data.get("blockTimestamp", 0)),
            block_number=int(tx_data.get("blockNumber", 0)),
            from_addresses=[tx_data.get("from", {}).get("address", "")] if tx_data.get("from") else [],
            to_addresses=[tx_data.get("to", {}).get("address", "")] if tx_data.get("to") else [],
            amount_navax=value_navax,
            is_cross_chain=False,
            raw_data=tx_data,
        )

    def is_cached(self, address: AvaxAddress | str, chain: str, tx_types: list[str] | None = None) -> bool:
        """
        Check if transactions for an address are cached.

        Args:
            address: Address to check
            chain: "P" or "X"
            tx_types: Transaction types (uses default if None)

        Returns:
            True if cached data exists
        """
        if isinstance(address, str):
            address = AvaxAddress.from_any(address)
        if not address.is_primary:
            return False
        addr_str = address.p_address

        if tx_types is None:
            tx_types = ["BaseTx", "ExportTx", "ImportTx"]

        tx_types_str = ",".join(sorted(tx_types)) if tx_types else ""
        cache_key = f"{addr_str}:{tx_types_str}::"

        namespace = "p_chain_txs" if chain == "P" else "x_chain_txs"
        return self._cache.exists(namespace, cache_key)

    def close(self) -> None:
        """Close the client and cache connections."""
        self._cache.close()

    def __enter__(self) -> "GlacierClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
