"""
WebSocket streaming demo — RemitGuard EOB pipeline.

Connects to ws://localhost:8000/ws/pipeline, submits both sample PDFs
and the sample ledger, then prints each streamed event with colour coding
as it arrives.

Usage:
    pip install websockets
    python test_ws.py
    python test_ws.py --host localhost --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time

# ── colour helpers ─────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
GREY    = "\033[90m"
CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


def _ts() -> str:
    return _c(time.strftime("%H:%M:%S"), GREY)


def _print_event(event: dict) -> None:
    kind = event.get("type", "unknown")

    if kind == "batch_start":
        print(f"{_ts()} {_c('BATCH START', BOLD, GREEN)} — {event['total_files']} file(s)")

    elif kind == "phase_start":
        phase = event.get("phase", "")
        agent = event.get("agent", "")
        fname = event.get("file", "")
        print(f"{_ts()} {_c(f'  → {phase}', GREY)}  [{_c(agent, GREY)}]  {_c(fname, GREY)}")

    elif kind == "phase_complete":
        phase = event.get("phase", "")
        extras = {k: v for k, v in event.items()
                  if k not in ("type", "phase", "file", "agent")}
        detail = "  ".join(f"{k}={v}" for k, v in extras.items())
        fname = event.get("file", "")
        print(f"{_ts()} {_c(f'  ✓ {phase}', CYAN)}  {_c(fname, GREY)}  {_c(detail, CYAN)}")

    elif kind == "file_complete":
        fname = event.get("file", "")
        status = event.get("status", "")
        flagged = event.get("flagged", False)
        net = event.get("net_received")
        paid = event.get("paid_amount")
        tickets = event.get("escalation_tickets", [])

        colour = GREEN if status == "APPROVED" else (YELLOW if status == "ESCALATED" else RED)
        net_str = f"  net=${net:,.2f}" if net is not None else ""
        paid_str = f"  paid=${paid:,.2f}" if paid is not None else ""
        ticket_str = f"  tickets={tickets}" if tickets else ""
        print(
            f"{_ts()} {_c(f'FILE COMPLETE', BOLD, colour)} "
            f"{_c(fname, colour)}  {_c(status, BOLD, colour)}"
            f"  flagged={flagged}{net_str}{paid_str}{ticket_str}"
        )

    elif kind == "batch_complete":
        s = event.get("summary", {})
        print()
        print(_c("━" * 60, BOLD, GREEN))
        print(_c("  BATCH COMPLETE", BOLD, GREEN))
        print(_c("━" * 60, BOLD, GREEN))
        for k, v in s.items():
            label = k.replace("_", " ").title()
            if "amount" in k.lower() and isinstance(v, (int, float)):
                print(f"  {label:<30} ${v:,.2f}")
            else:
                print(f"  {label:<30} {v}")
        print(_c("━" * 60, BOLD, GREEN))

    elif kind == "error":
        msg = event.get("message", "unknown error")
        print(f"{_ts()} {_c('ERROR', BOLD, RED)} {_c(msg, RED)}")

    else:
        print(f"{_ts()} {_c(json.dumps(event), GREY)}")


def _load_b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


async def run(host: str, port: int, samples_dir: str) -> None:
    try:
        import websockets  # type: ignore
    except ImportError:
        print(_c("ERROR: websockets library not installed. Run: pip install websockets", RED))
        sys.exit(1)

    pdf_names = [
        "anthem_haddad_sample.pdf",
        "cigna_unknown_phrasing_sample.pdf",
    ]
    ledger_name = "sample_ledger.csv"

    files_payload = []
    for name in pdf_names:
        path = os.path.join(samples_dir, name)
        if not os.path.exists(path):
            print(_c(f"WARNING: sample not found: {path}", YELLOW))
            continue
        files_payload.append({"name": name, "data": _load_b64(path)})

    ledger_b64 = None
    ledger_path = os.path.join(samples_dir, ledger_name)
    if os.path.exists(ledger_path):
        ledger_b64 = _load_b64(ledger_path)
    else:
        print(_c(f"WARNING: ledger not found: {ledger_path}", YELLOW))

    if not files_payload:
        print(_c("ERROR: no sample PDFs found — nothing to send", RED))
        sys.exit(1)

    uri = f"ws://{host}:{port}/ws/pipeline"
    print(_c(f"Connecting to {uri} …", GREY))

    job = {
        "files": files_payload,
        "ledger": ledger_b64,
        "facility_id": None,
    }

    async with websockets.connect(uri) as ws:
        print(_c(f"Connected — sending {len(files_payload)} file(s)\n", GREY))
        await ws.send(json.dumps(job))

        async for raw in ws:
            event = json.loads(raw)
            _print_event(event)
            if event.get("type") in ("batch_complete", "error"):
                break

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="RemitGuard WebSocket streaming demo")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--samples",
        default=os.path.join(os.path.dirname(__file__), "..", "samples"),
        help="Path to directory containing sample PDFs and ledger CSV",
    )
    args = parser.parse_args()

    samples_dir = os.path.abspath(args.samples)
    asyncio.run(run(args.host, args.port, samples_dir))


if __name__ == "__main__":
    main()
