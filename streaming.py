"""Bridge blocking upstream generators to SSE without blocking the ASGI loop."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
import anyio
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

_END = object()
_CLEANUPS: set[asyncio.Task] = set()
HEARTBEAT_INTERVAL = 15.0
log = logging.getLogger("ds2api.streaming")


def _track_cleanup(task):
    _CLEANUPS.add(task)
    def finished(done):
        _CLEANUPS.discard(done)
        if not done.cancelled() and done.exception() is not None:
            log.warning("stream_cleanup_failed", extra={"error_type": type(done.exception()).__name__})
    task.add_done_callback(finished)


class ReliableStreamingResponse(StreamingResponse):
    """Starlette can skip background tasks on send failures; always clean up."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    close = getattr(self.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    if self.background is not None:
                        await self.background()


async def run_owned_setup(factory, on_cancel):
    """Run blocking setup off-loop; reclaim its result if the caller leaves."""
    # Queued account acquisition must not exhaust the executor that performs
    # active stream reads, or all slots can wait on one another indefinitely.
    task = asyncio.create_task(run_in_threadpool(factory))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        async def reclaim():
            try:
                value = await task
            except Exception:
                # The factory must release its own resources on failure.
                return
            await asyncio.to_thread(on_cancel, value)
        cleanup = asyncio.create_task(reclaim())
        _track_cleanup(cleanup)
        raise


class HeartbeatStream:
    """One pending read, bounded buffering, and cleanup before account release.

    Cancelling an asyncio task cannot stop a blocking curl read. Keep ownership
    until that read returns (bounded by the upstream timeout), then close the
    generator in a worker. Never run close() concurrently with next().
    """

    def __init__(self, source, *, on_close=None, heartbeat=": keep-alive\n\n", interval=None):
        self.source = iter(source)
        self.on_close = on_close
        self.heartbeat = heartbeat
        self.interval = HEARTBEAT_INTERVAL if interval is None else interval
        self.pending = None
        self.cleanup = None
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            if self.pending is None:
                self.pending = asyncio.create_task(asyncio.to_thread(next, self.source, _END))
            done, _ = await asyncio.wait({self.pending}, timeout=self.interval)
            if not done:
                return self.heartbeat
            task, self.pending = self.pending, None
            item = task.result()
            if item is _END:
                await self.aclose()
                raise StopAsyncIteration
            return item
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        self.closed = True
        reading = self.pending is not None and not self.pending.done()
        if self.cleanup is None:
            async def finish():
                try:
                    if self.pending is not None:
                        with suppress(Exception):
                            await self.pending
                    close = getattr(self.source, "close", None)
                    if close is not None:
                        await asyncio.to_thread(close)
                finally:
                    if self.on_close is not None:
                        await asyncio.to_thread(self.on_close)

            self.cleanup = asyncio.create_task(finish())
            _track_cleanup(self.cleanup)
        if not reading:
            await asyncio.shield(self.cleanup)

    async def athrow(self, error):
        await self.aclose()
        raise error
