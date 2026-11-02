"""Run a function over many items on several threads, for work that mostly waits on a server.

A vLLM server answers many requests at once (continuous batching), but a loop that asks one
question and waits for the answer keeps it at one. Threads are enough here: the waiting happens in
sockets, which release Python's global interpreter lock. Results keep the order of the items, so
reports do not depend on which thread finished first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int = 1) -> list[R]:
    """`[fn(x) for x in items]`, on `workers` threads when more than one. The first error is
    raised, and items not yet started are dropped rather than run."""
    if workers <= 1:
        return [fn(x) for x in items]
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        return list(pool.map(fn, items))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
