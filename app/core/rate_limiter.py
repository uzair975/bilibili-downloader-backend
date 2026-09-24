import time
from collections import defaultdict
from fastapi import Request
try:
    from app.config import settings
    from app.core.exceptions import RateLimitExceededException
except ImportError:
    from backend.app.config import settings
    from backend.app.core.exceptions import RateLimitExceededException


class InMemoryRateLimiter:
    """Sliding-window IP-based rate limiter with zero external dependencies."""

    def __init__(self, max_requests: int = settings.RATE_LIMIT_REQUESTS, window: int = settings.RATE_LIMIT_WINDOW_SECONDS):
        self.max_requests = max_requests
        self.window = window
        self._history = defaultdict(list)
        self._last_clean = time.time()

    def _cleanup(self, now: float):
        """Periodically purge expired records to maintain negligible RAM usage."""
        if now - self._last_clean < 60:
            return
        self._last_clean = now
        expired = now - self.window
        keys_to_delete = []
        for ip, timestamps in self._history.items():
            self._history[ip] = [t for t in timestamps if t > expired]
            if not self._history[ip]:
                keys_to_delete.append(ip)
        for k in keys_to_delete:
            del self._history[k]

    def check(self, client_ip: str):
        now = time.time()
        self._cleanup(now)

        timestamps = self._history[client_ip]
        # Filter within window
        window_start = now - self.window
        valid_timestamps = [t for t in timestamps if t > window_start]
        self._history[client_ip] = valid_timestamps

        if len(valid_timestamps) >= self.max_requests:
            raise RateLimitExceededException(
                f"Rate limit of {self.max_requests} requests per {self.window}s reached. Try again shortly."
            )
        self._history[client_ip].append(now)


rate_limiter = InMemoryRateLimiter()


async def check_rate_limit(request: Request):
    """FastAPI dependency for IP rate limiting."""
    client_ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "127.0.0.1")
    )
    rate_limiter.check(client_ip)
