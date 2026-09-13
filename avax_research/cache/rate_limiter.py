"""
Token bucket rate limiter with exponential backoff.

Designed for API rate limiting with shared state across threads.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter with exponential backoff on 429 errors.

    Thread-safe for use across multiple API client instances.

    Args:
        rate: Maximum requests per second
        burst: Maximum burst size (tokens that can accumulate)
        max_backoff: Maximum backoff time in seconds
        initial_backoff: Initial backoff time after 429
    """
    rate: float = 5.0  # requests per second
    burst: int = 10  # max tokens
    max_backoff: float = 60.0  # max backoff seconds
    initial_backoff: float = 1.0  # initial backoff seconds

    _tokens: float = field(init=False)
    _last_update: float = field(init=False)
    _backoff: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._last_update = time.monotonic()
        self._backoff = 0.0

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_update = now

    def acquire(self, tokens: int = 1) -> float:
        """
        Acquire tokens, blocking if necessary.

        Returns the time waited in seconds.
        """
        with self._lock:
            self._refill()

            # Handle any active backoff
            if self._backoff > 0:
                wait_time = self._backoff
                self._backoff = 0
                logger.debug(f"Rate limiter backoff: {wait_time:.2f}s")
                time.sleep(wait_time)
                self._refill()

            # Wait for tokens if needed
            wait_time = 0.0
            while self._tokens < tokens:
                deficit = tokens - self._tokens
                sleep_time = deficit / self.rate
                wait_time += sleep_time
                time.sleep(sleep_time)
                self._refill()

            self._tokens -= tokens
            return wait_time

    async def acquire_async(self, tokens: int = 1) -> float:
        """
        Async version of acquire.

        Returns the time waited in seconds.
        """
        # Use thread-safe operations with async sleep
        with self._lock:
            self._refill()
            backoff = self._backoff
            if backoff > 0:
                self._backoff = 0

        if backoff > 0:
            logger.debug(f"Rate limiter backoff (async): {backoff:.2f}s")
            await asyncio.sleep(backoff)
            with self._lock:
                self._refill()

        wait_time = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return wait_time
                deficit = tokens - self._tokens
                sleep_time = deficit / self.rate

            wait_time += sleep_time
            await asyncio.sleep(sleep_time)

    def report_rate_limit(self, retry_after: float | None = None) -> None:
        """
        Report a 429 rate limit response, triggering exponential backoff.

        Args:
            retry_after: Optional Retry-After header value in seconds
        """
        with self._lock:
            if retry_after is not None:
                self._backoff = min(retry_after, self.max_backoff)
            else:
                # Exponential backoff
                current = self._backoff if self._backoff > 0 else self.initial_backoff
                self._backoff = min(current * 2, self.max_backoff)

            logger.warning(f"Rate limit hit, backoff set to {self._backoff:.2f}s")

    def reset_backoff(self) -> None:
        """Reset backoff after a successful request."""
        with self._lock:
            self._backoff = 0.0

    @property
    def available_tokens(self) -> float:
        """Get current available tokens (approximate, for monitoring)."""
        with self._lock:
            self._refill()
            return self._tokens


class RateLimiterContext:
    """
    Context manager for rate-limited operations.

    Usage:
        rate_limiter = RateLimiter()
        async with RateLimiterContext(rate_limiter):
            response = await client.get(url)
    """

    def __init__(self, limiter: RateLimiter, tokens: int = 1):
        self.limiter = limiter
        self.tokens = tokens
        self._success = False

    def __enter__(self) -> "RateLimiterContext":
        self.limiter.acquire(self.tokens)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.limiter.reset_backoff()

    async def __aenter__(self) -> "RateLimiterContext":
        await self.limiter.acquire_async(self.tokens)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.limiter.reset_backoff()

    def mark_rate_limited(self, retry_after: float | None = None) -> None:
        """Mark the request as rate limited."""
        self.limiter.report_rate_limit(retry_after)
