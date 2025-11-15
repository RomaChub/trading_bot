"""Rate limiter for API requests to prevent exceeding Binance limits"""
import time
import asyncio
from collections import deque
from typing import Optional
import threading


class RateLimiter:
    """
    Thread-safe rate limiter that ensures we don't exceed API limits.
    
    Binance limit: 2400 requests per minute = 40 requests per second
    We'll use a conservative limit of 35 requests per second to stay safe.
    """
    
    def __init__(self, max_requests_per_second: float = 35.0, max_requests_per_minute: int = 2100):
        """
        Initialize rate limiter.
        
        Args:
            max_requests_per_second: Maximum requests per second (default: 35)
            max_requests_per_minute: Maximum requests per minute (default: 2100, Binance limit)
        """
        self.max_rps = max_requests_per_second
        self.max_rpm = max_requests_per_minute
        self.min_interval = 1.0 / max_requests_per_second
        
        # Thread-safe request tracking
        self._lock = threading.Lock()
        self._request_times = deque()  # Timestamps of recent requests
        self._minute_times = deque()  # Timestamps from last minute
        
        # Statistics
        self._total_requests = 0
        self._blocked_requests = 0
    
    def _cleanup_old_requests(self, current_time: float):
        """Remove old request timestamps outside the time window"""
        # Clean up requests older than 1 second
        while self._request_times and current_time - self._request_times[0] >= 1.0:
            self._request_times.popleft()
        
        # Clean up requests older than 1 minute
        while self._minute_times and current_time - self._minute_times[0] >= 60.0:
            self._minute_times.popleft()
    
    def wait_if_needed(self) -> float:
        """
        Block until it's safe to make a request.
        Returns the wait time (0 if no wait was needed).
        
        This is a synchronous method for use in blocking code.
        """
        with self._lock:
            current_time = time.time()
            self._cleanup_old_requests(current_time)
            
            # Check per-second limit
            if len(self._request_times) >= self.max_rps:
                # Need to wait until oldest request is 1 second old
                oldest_time = self._request_times[0]
                wait_time = 1.0 - (current_time - oldest_time) + 0.01  # Small buffer
                if wait_time > 0:
                    time.sleep(wait_time)
                    current_time = time.time()
                    self._cleanup_old_requests(current_time)
            
            # Check per-minute limit
            if len(self._minute_times) >= self.max_rpm:
                # Need to wait until oldest request is 1 minute old
                oldest_time = self._minute_times[0]
                wait_time = 60.0 - (current_time - oldest_time) + 0.1  # Small buffer
                if wait_time > 0:
                    time.sleep(wait_time)
                    current_time = time.time()
                    self._cleanup_old_requests(current_time)
            
            # Record this request
            self._request_times.append(current_time)
            self._minute_times.append(current_time)
            self._total_requests += 1
            
            return 0.0  # No wait time needed (already waited if needed)
    
    async def async_wait_if_needed(self) -> float:
        """
        Async version that yields control while waiting.
        Returns the wait time (0 if no wait was needed).
        """
        with self._lock:
            current_time = time.time()
            self._cleanup_old_requests(current_time)
            
            wait_time = 0.0
            
            # Check per-second limit
            if len(self._request_times) >= self.max_rps:
                oldest_time = self._request_times[0]
                wait_time = max(wait_time, 1.0 - (current_time - oldest_time) + 0.01)
            
            # Check per-minute limit
            if len(self._minute_times) >= self.max_rpm:
                oldest_time = self._minute_times[0]
                wait_time = max(wait_time, 60.0 - (current_time - oldest_time) + 0.1)
            
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                current_time = time.time()
                self._cleanup_old_requests(current_time)
            
            # Record this request
            self._request_times.append(current_time)
            self._minute_times.append(current_time)
            self._total_requests += 1
            
            return wait_time
    
    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        with self._lock:
            return {
                'total_requests': self._total_requests,
                'blocked_requests': self._blocked_requests,
                'current_rps': len(self._request_times),
                'current_rpm': len(self._minute_times),
            }


# Global rate limiter instance (shared across all API calls)
_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter instance"""
    global _global_rate_limiter
    if _global_rate_limiter is None:
        _global_rate_limiter = RateLimiter()
    return _global_rate_limiter


def set_rate_limiter(limiter: RateLimiter):
    """Set a custom rate limiter (for testing)"""
    global _global_rate_limiter
    _global_rate_limiter = limiter

