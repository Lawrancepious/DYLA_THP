"""A TCP proxy in front of Postgres that can be made slow on demand.

The brief asks what happens when the datastore goes slow for ten seconds. That
is a different failure from the datastore dying, and the difference is the
interesting part:

  * When Postgres is *dead*, connections are refused instantly. Requests fail
    fast, the pool never fills, and the seller sheds load whether it means to
    or not.
  * When Postgres is *slow*, every query still succeeds -- eventually. Nothing
    errors. The pool fills with connections that are busy rather than broken,
    and the queue in front of it grows without bound. Latency, not errors, is
    how this failure presents.

Killing the process cannot produce the second case, so this proxy exists.
Seller -> :5434 (here) -> :5433 (real Postgres), with a delay injected into
every forwarded chunk when armed.

The delay is read from a control file rather than a socket protocol: the
experiment driver writes a number, the proxy picks it up within ~50ms, and
there is no extra protocol to get wrong.

    python scripts/dbproxy.py                 # listen on 5434, forward to 5433
    echo 250 > runs/.proxy_delay_ms           # every chunk now waits 250ms
    echo 0   > runs/.proxy_delay_ms           # back to normal
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTROL = ROOT / "runs" / ".proxy_delay_ms"

LISTEN_PORT = int(os.environ.get("PROXY_LISTEN_PORT", "5434"))
TARGET_PORT = int(os.environ.get("PROXY_TARGET_PORT", "5433"))

_delay_s = 0.0


async def _watch_control() -> None:
    """Poll the control file. Deliberately not a socket: the experiment driver
    is a different process and a file is the least that can go wrong."""
    global _delay_s
    last = None
    while True:
        try:
            raw = CONTROL.read_text().strip()
            if raw != last:
                last = raw
                _delay_s = max(0.0, float(raw or 0)) / 1000.0
                print(f"[proxy] delay -> {_delay_s * 1000:.0f}ms", flush=True)
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.05)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction, pausing before each chunk when armed.

    The delay is applied per chunk rather than per connection, so a request that
    needs several round trips pays it several times -- which is what a genuinely
    slow datastore does to a multi-statement transaction, and is why this hurts
    the naive store far more than the safe one.
    """
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            if _delay_s:
                await asyncio.sleep(_delay_s)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle(client_r: asyncio.StreamReader,
                  client_w: asyncio.StreamWriter) -> None:
    try:
        server_r, server_w = await asyncio.open_connection("127.0.0.1", TARGET_PORT)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(
        _pump(client_r, server_w),
        _pump(server_r, client_w),
        return_exceptions=True,
    )


async def main() -> None:
    CONTROL.parent.mkdir(exist_ok=True)
    CONTROL.write_text("0")
    asyncio.create_task(_watch_control())
    server = await asyncio.start_server(_handle, "127.0.0.1", LISTEN_PORT)
    print(f"[proxy] 127.0.0.1:{LISTEN_PORT} -> 127.0.0.1:{TARGET_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
