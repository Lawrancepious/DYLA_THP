"""Lifecycle for a project-local PostgreSQL cluster.

Why a private cluster instead of the machine's system Postgres:
  1. No credentials for a grader to discover -- `trust` auth on loopback only.
  2. We need to kill and restart the datastore mid-sale for the durability
     experiment. Doing that to a system service needs admin rights and is rude.
  3. Reproducible: the same server settings every run, so latency numbers are
     comparable across runs.

Usage:  python scripts/pgctl.py {init|up|down|kill|status|psql}
If DATABASE_URL is set in the environment, this script is not needed -- the
seller will use that instead and these commands refuse to run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PGDATA = ROOT / ".pgdata"
LOGFILE = ROOT / ".pgdata" / "server.log"
PORT = os.environ.get("TICKETS_PG_PORT", "5433")
DBNAME = "tickets"


def _bin(name: str) -> str:
    """Locate a postgres binary: PATH first, then the usual Windows install."""
    found = shutil.which(name)
    if found:
        return found
    for base in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"), reverse=True):
        cand = base / f"{name}.exe"
        if cand.exists():
            return str(cand)
    sys.exit(f"could not find {name!r}; install PostgreSQL or put its bin/ on PATH")


def _run(args: list[str], timeout: int = 90, **kw) -> subprocess.CompletedProcess:
    """Every external call is bounded.

    An unbounded subprocess.run here once hung a whole experiment: pg_ctl had
    already started the server, but the psql that followed it never returned,
    and the driver waiting on this function blocked forever with no output. A
    timeout turns that into a legible error instead of a hang.
    """
    try:
        return subprocess.run(args, text=True, capture_output=True,
                              timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 1, "", f"timed out after {timeout}s: {' '.join(args[:2])}")


def init() -> None:
    if (PGDATA / "PG_VERSION").exists():
        print(f"cluster already initialised at {PGDATA}")
        return
    PGDATA.mkdir(parents=True, exist_ok=True)
    pwfile = PGDATA.parent / ".pgpw"
    pwfile.write_text("postgres")
    r = _run([_bin("initdb"), "-D", str(PGDATA), "-U", "postgres",
              "--auth-local=trust", "--auth-host=trust", "-E", "UTF8"])
    pwfile.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit(f"initdb failed:\n{r.stdout}\n{r.stderr}")
    # Loopback only. A cluster with trust auth must never listen on a real NIC.
    conf = PGDATA / "postgresql.conf"
    conf.write_text(
        conf.read_text()
        + f"\n# --- ticket-stampede overrides ---\n"
        f"port = {PORT}\n"
        "listen_addresses = '127.0.0.1'\n"
        "max_connections = 200\n"
        "fsync = on\n"                    # durability experiment depends on this
        "synchronous_commit = on\n"
        "log_min_duration_statement = 200ms\n"
    )
    print(f"initialised cluster at {PGDATA} (port {PORT})")


def up() -> None:
    init()
    if status(quiet=True):
        print("already running")
    else:
        r = _run([_bin("pg_ctl"), "-D", str(PGDATA), "-l", str(LOGFILE), "-w", "start"])
        if r.returncode != 0:
            sys.exit(f"pg_ctl start failed:\n{r.stdout}\n{r.stderr}\nsee {LOGFILE}")
    _ensure_db()
    print(f"postgres up on 127.0.0.1:{PORT}, database {DBNAME!r}")
    print(f"DATABASE_URL=postgresql://postgres@127.0.0.1:{PORT}/{DBNAME}")


def _ensure_db() -> None:
    """Create the database if absent, over the wire rather than through psql.

    This used to shell out to psql, which hung intermittently on Windows --
    twice, including once in the middle of the datastore-kill experiment after
    a crash-recovery restart, with the server already accepting connections.
    The whole run blocked with no output and no error.

    Rather than chase the reason, the dependency is gone. asyncpg is already a
    requirement of the seller, and a direct connection has no console, no pager
    and no password prompt that could block.
    """
    import asyncio

    import asyncpg

    async def go() -> None:
        # pg_ctl -w returns once the postmaster is listening, but after an
        # immediate shutdown it may still be replaying WAL and will refuse
        # connections for a moment longer. Retry rather than assume.
        last: Exception | None = None
        con = None
        for _ in range(40):
            try:
                con = await asyncpg.connect(
                    host="127.0.0.1", port=int(PORT), user="postgres",
                    database="postgres", timeout=5)
                break
            except (OSError, asyncpg.PostgresError) as exc:
                last = exc
                await asyncio.sleep(0.5)
        if con is None:
            sys.exit(f"could not connect to the cluster on :{PORT}: {last}")
        try:
            exists = await con.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", DBNAME)
            if not exists:
                await con.execute(f'CREATE DATABASE "{DBNAME}"')
        finally:
            await con.close()

    asyncio.run(go())


def down() -> None:
    """Graceful stop: finishes in-flight commits, then shuts down."""
    r = _run([_bin("pg_ctl"), "-D", str(PGDATA), "-m", "fast", "-w", "stop"])
    print(r.stdout.strip() or r.stderr.strip() or "stopped")


def kill() -> None:
    """Ungraceful stop -- the failure we actually want to test.

    `-m immediate` is the SIGQUIT equivalent: no clean shutdown checkpoint, so
    recovery must replay the write-ahead log on restart. This is the mode that
    would lose a confirmed sale if we had ever acknowledged one before its
    commit was durable.
    """
    r = _run([_bin("pg_ctl"), "-D", str(PGDATA), "-m", "immediate", "-w", "stop"])
    print(r.stdout.strip() or r.stderr.strip() or "killed")


def status(quiet: bool = False) -> bool:
    r = _run([_bin("pg_ctl"), "-D", str(PGDATA), "status"])
    running = r.returncode == 0
    if not quiet:
        print(r.stdout.strip() or r.stderr.strip())
    return running


def psql() -> None:
    os.execv(_bin("psql"), [_bin("psql"), "-h", "127.0.0.1", "-p", PORT,
                            "-U", "postgres", "-d", DBNAME])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if os.environ.get("DATABASE_URL") and cmd != "status":
        sys.exit("DATABASE_URL is set; managing the local cluster would be a no-op. "
                 "Unset it to use the project-local cluster.")
    fn = {"init": init, "up": up, "down": down, "kill": kill,
          "status": status, "psql": psql}.get(cmd)
    if not fn:
        sys.exit(__doc__)
    fn()
