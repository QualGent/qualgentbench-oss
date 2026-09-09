"""The CLI's event-loop runner must leave NO open subprocess transport behind — an
open one prints `RuntimeError: Event loop is closed` from its finaliser at
interpreter shutdown (seen under every board of a Docker run). The invariant is
checked directly, so it holds whatever platform-specific path produced the leak."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import textwrap
from asyncio.base_subprocess import BaseSubprocessTransport

from qualgentbench.cli import _run_async

KEEP: list = []


def _open_transports() -> list:
    return [o for o in gc.get_objects()
            if isinstance(o, BaseSubprocessTransport) and not o._closed]


async def _leaky():
    """A cancelled communicate() on a long-lived child, and a child whose grandchild
    inherits the pipe — the two ways the harness leaves transports open."""
    async def worker():
        p = await asyncio.create_subprocess_exec("sleep", "30", stdout=asyncio.subprocess.PIPE)
        KEEP.append(p)
        await p.communicate()
    t = asyncio.create_task(worker())
    await asyncio.sleep(0.2)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    KEEP.append(t)
    p = await asyncio.create_subprocess_exec("sh", "-c", "sleep 30 & exit 0",
                                             stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT,
                                             start_new_session=True)
    while p.returncode is None:
        await asyncio.sleep(0.05)
    KEEP.append(p)
    return len(KEEP)


def test_plain_asyncio_run_leaves_transports_open():
    """The premise: without the runner's sweep, transports DO stay open."""
    KEEP.clear()
    asyncio.run(_leaky())
    assert _open_transports(), "expected leaked transports to demonstrate the problem"
    for t in _open_transports():          # tidy so the next test starts clean
        t._closed = True
        for proto in list(t._pipes.values()):
            if proto and proto.pipe is not None:
                proto.pipe._closing = True
    for p in KEEP:
        if hasattr(p, "kill"):
            try:
                p.kill()
            except ProcessLookupError:
                pass


def test_run_async_leaves_no_open_subprocess_transport():
    KEEP.clear()
    assert _run_async(_leaky()) == 3
    assert _open_transports() == []
    for p in KEEP:
        if hasattr(p, "kill"):
            try:
                p.kill()
            except ProcessLookupError:
                pass


def test_run_async_prints_no_closed_loop_noise_at_exit():
    snippet = textwrap.dedent("""
        import asyncio, sys
        from qualgentbench.cli import _run_async
        KEEP = []
        async def f():
            for _ in range(3):
                p = await asyncio.create_subprocess_exec("sleep", "30", stdout=asyncio.subprocess.PIPE)
                KEEP.append(p)
            t = asyncio.create_task(KEEP[0].communicate())
            await asyncio.sleep(0.1); t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            return len(KEEP)
        print(_run_async(f()))
        for p in KEEP: p.kill()
    """)
    out = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "3", out.stderr
    assert "Event loop is closed" not in out.stderr, out.stderr
