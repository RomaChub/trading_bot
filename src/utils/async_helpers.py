"""Async utilities for non-blocking operations"""
import asyncio
from typing import Callable, TypeVar, Any, Optional
from functools import wraps
import concurrent.futures

T = TypeVar('T')


async def run_in_executor(func: Callable[..., T], *args, **kwargs) -> Optional[T]:
    """Run blocking function in executor with error handling"""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
    except Exception as e:
        print(f"⚠️ Error in executor: {e}")
        return None


async def safe_async_call(coro, default=None, error_msg="Error"):
    """Safely execute async call with error handling"""
    try:
        return await coro
    except asyncio.TimeoutError:
        print(f"⚠️ Timeout: {error_msg}")
        return default
    except Exception as e:
        print(f"⚠️ {error_msg}: {e}")
        return default


def to_sync(async_func):
    """Decorator to run async function synchronously"""
    @wraps(async_func)
    def wrapper(*args, **kwargs):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        return loop.run_until_complete(async_func(*args, **kwargs))
    return wrapper


class AsyncTaskManager:
    """Manages async tasks with graceful shutdown"""
    
    def __init__(self):
        self.tasks = set()
        self._shutdown = False
    
    def create_task(self, coro):
        """Create and track task"""
        if self._shutdown:
            return None
        
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task
    
    async def shutdown(self, timeout=5):
        """Gracefully shutdown all tasks"""
        self._shutdown = True
        
        if not self.tasks:
            return
        
        # Cancel all tasks
        for task in self.tasks:
            task.cancel()
        
        # Wait for cancellation with timeout
        await asyncio.wait(self.tasks, timeout=timeout)
        
        # Force cleanup remaining
        for task in self.tasks:
            if not task.done():
                task.cancel()
    
    @property
    def is_shutdown(self):
        return self._shutdown




