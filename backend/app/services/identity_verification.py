"""Run blocking identity verification with completion-owned worker admission."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")
VERIFICATION_WORKERS = 4
# Threads start lazily. Isolate certificate/network waits from GCS's executor.
_executor = ThreadPoolExecutor(max_workers=VERIFICATION_WORKERS, thread_name_prefix="firebase_verify")
_admission: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


async def run_verification(verify: Callable[[str], T], token: str) -> T:
    """Admit at most four submissions on the server's single event loop.

    Waiting callers submit no executor work. Once submitted, a native future owns
    admission until it actually completes, even if its asyncio caller cancels.
    Cancellation before a queued job starts also completes that native future.
    This bounds provider work, not total incoming HTTP requests or SDK duration.
    """
    loop = asyncio.get_running_loop()
    slots = _admission.setdefault(loop, asyncio.Semaphore(VERIFICATION_WORKERS))
    await slots.acquire()
    try:
        native_future = _executor.submit(verify, token)
    except BaseException:
        slots.release()
        raise

    def completed(_future: Future[T]) -> None:
        # Request cancellation must not release a still-running worker's slot.
        # A closed test/shutdown loop cannot accept new admissions anyway.
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(slots.release)
            except RuntimeError:
                pass  # the loop closed between the check and callback delivery

    native_future.add_done_callback(completed)
    return await asyncio.wrap_future(native_future, loop=loop)
