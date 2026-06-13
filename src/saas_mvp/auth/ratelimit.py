"""Simple in-memory sliding-window rate limiter for auth endpoints.

No external dependency needed — uses only stdlib threading + time.
Attach as a FastAPI dependency: ``Depends(auth_rate_limiter)``.

NOTE: Do NOT add `from __future__ import annotations` here.
  That turns all type hints into strings; FastAPI cannot then identify
  `Request` as the special HTTP-request object and falls back to treating
  it as a required query parameter (422 on every call).
"""

import threading
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request, status

from saas_mvp.config import settings


class SlidingWindowRateLimiter:
    """Per-IP sliding-window rate limiter (thread-safe, in-process)."""

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._lock = threading.Lock()
        self._log: dict = defaultdict(list)   # {ip: [monotonic_timestamp, ...]}

    def __call__(self, request: Request) -> None:
        """FastAPI dependency: raise 429 if caller exceeds the rate limit."""
        if not settings.rate_limit_enabled:
            return

        ip: str = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            calls = [t for t in self._log[ip] if t > cutoff]
            if len(calls) >= self._max_calls:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit exceeded: max {self._max_calls} requests "
                        f"per {self._window}s. Please try again later."
                    ),
                )
            calls.append(now)
            self._log[ip] = calls


# Singleton instances — shared across all worker threads.
# 20/min: generous for dev/test, still protective in production.
register_limiter = SlidingWindowRateLimiter(max_calls=20, window_seconds=60)
token_limiter = SlidingWindowRateLimiter(max_calls=20, window_seconds=60)
