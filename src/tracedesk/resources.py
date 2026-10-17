"""Resource finalizers preserve earlier failures during stacked asynchronous cleanup."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


@asynccontextmanager
async def close_on_exit(close: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    # A generator receives the previous error as an active exception, preserving its chain.
    try:
        yield
    finally:
        await close()
