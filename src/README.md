# Source code

The AIORI template asks for the implementation to be in `src/`. Threadline's code stays at the repository root so that `python server.py` and the hosted demo (Vercel) keep working without path changes. This file maps where each part lives.

| Path | What it contains |
|---|---|
| [`../server.py`](../server.py) | HTTP server and JSON API (standard-library `ThreadingHTTPServer`) |
| [`../threadline/events.py`](../threadline/events.py) | Message envelope, validation, idempotency key |
| [`../threadline/adapters.py`](../threadline/adapters.py) | Agent adapters: WebSocket JSON, HTTP, JSON-RPC 2.0 |
| [`../threadline/store.py`](../threadline/store.py) | Append-only SQLite event log |
| [`../threadline/recovery.py`](../threadline/recovery.py) | Pure, deterministic recovery engine (`replay()`) |
| [`../threadline/simulator.py`](../threadline/simulator.py) | Fault injection and the 8 experiment scenarios |
| [`../threadline/service.py`](../threadline/service.py) | Gateway: ingest, recover, deliver, resume |
| [`../threadline/bench.py`](../threadline/bench.py) | Benchmark of all scenarios |
| [`../threadline/campaign.py`](../threadline/campaign.py) | Seeded random fault campaign vs. naive recovery |
| [`../static/`](../static/) | Operator dashboard (HTML, CSS, vanilla JavaScript) |
| [`../api/index.py`](../api/index.py) | Entry point for the hosted demo on Vercel |
| [`../tests/`](../tests/) | 56 automated tests |

Run it: `python server.py`, then open http://localhost:8080. Tests: `python -m unittest discover -s tests -t .`. Requires Python 3.10+ and no third-party packages.

Live demo: https://threadline-brown.vercel.app
