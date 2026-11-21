"""Load test: start a real server, hammer it from many clients, then audit the database.

    python scripts/loadtest.py --clients 32 --seconds 20

Each client loops over random transfers between a small set of wallets (so they
contend for the same rows), plus holds, captures and keyed retries. Afterwards
the money in the wallets must still add up and ``python -m ledger check`` must
find nothing.
"""

from __future__ import annotations

import argparse
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--wallets", type=int, default=8)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="ledger-load-")
    db_path = os.path.join(workdir, "ledger.db")
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    server = subprocess.Popen(
        [sys.executable, "-m", "ledger", "serve", "--db", db_path, "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                httpx.get(base + "/healthz").raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise SystemExit("server did not start") from None
                time.sleep(0.1)

        with httpx.Client(base_url=base) as c:

            def create(**body: object) -> str:
                account_id: str = c.post("/v1/accounts", json=body).json()["id"]
                return account_id

            funding = create(name="funding", currency="USD", allow_negative=True)
            wallets = [create(name=f"w{i}", currency="USD") for i in range(args.wallets)]
            per_wallet = 100_000
            for w in wallets:
                c.post(
                    "/v1/transfers",
                    json={"from_account_id": funding, "to_account_id": w, "amount": per_wallet},
                )

        latencies: list[float] = []
        outcomes: Counter[str] = Counter()
        lock = threading.Lock()
        stop = time.monotonic() + args.seconds

        def client(seed: int) -> None:
            rng = random.Random(seed)
            mine: list[float] = []
            seen: Counter[str] = Counter()
            holds: list[str] = []
            last_transfer: tuple[dict[str, object], str] | None = None
            with httpx.Client(base_url=base, timeout=30) as c:
                n = 0
                while time.monotonic() < stop:
                    n += 1
                    a, b = rng.sample(wallets, 2)
                    amount = rng.randint(1, 5_000)
                    op = rng.random()
                    key = None
                    if op < 0.1 and last_transfer is not None:
                        # A client that timed out and retries: same body, same key.
                        path = "/v1/transfers"
                        body, key = last_transfer
                    elif op < 0.6:
                        path = "/v1/transfers"
                        body = {"from_account_id": a, "to_account_id": b, "amount": amount}
                        key = f"client{seed}-{n}"
                        last_transfer = (body, key)
                    elif op < 0.8 or not holds:
                        path = "/v1/holds"
                        body = {"account_id": a, "destination_account_id": b, "amount": amount}
                    else:
                        path = f"/v1/holds/{holds.pop()}/capture"
                        body = {}
                    started = time.perf_counter()
                    r = c.post(path, json=body, headers={"Idempotency-Key": key} if key else {})
                    mine.append((time.perf_counter() - started) * 1000)
                    label = str(r.status_code)
                    if r.status_code >= 400:
                        label += " " + r.json()["code"]
                    elif r.headers.get("Idempotent-Replayed"):
                        label += " replayed"
                    seen[label] += 1
                    if path == "/v1/holds" and r.status_code == 201:
                        holds.append(r.json()["id"])
            with lock:
                latencies.extend(mine)
                outcomes.update(seen)

        started = time.monotonic()
        threads = [threading.Thread(target=client, args=(i,)) for i in range(args.clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - started

        with httpx.Client(base_url=base) as c:
            total = sum(c.get(f"/v1/accounts/{w}").json()["balance"] for w in wallets)
            negative = [w for w in wallets if c.get(f"/v1/accounts/{w}").json()["available"] < 0]
    finally:
        server.terminate()
        server.wait(timeout=10)

    audit = subprocess.run(
        [sys.executable, "-m", "ledger", "check", "--db", db_path],
        env=env,
        capture_output=True,
        text=True,
    )
    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    print(f"clients {args.clients}, wallets {args.wallets}, {elapsed:.1f} s")
    print(f"requests {len(latencies)}, {len(latencies) / elapsed:.0f} per second")
    print(f"latency ms: p50 {q[49]:.1f}, p95 {q[94]:.1f}, p99 {q[98]:.1f}, max {latencies[-1]:.1f}")
    for label, count in sorted(outcomes.items()):
        print(f"  {label:<32} {count}")
    print(f"money in wallets: {total} (expected {per_wallet * len(wallets)})")
    print(f"wallets with negative available funds: {len(negative)}")
    print(f"audit: {audit.stdout.strip().splitlines()[-1]}")
    ok = total == per_wallet * len(wallets) and not negative and audit.returncode == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
