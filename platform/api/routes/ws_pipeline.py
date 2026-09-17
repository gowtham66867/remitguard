"""
WebSocket route for streaming batch EOB pipeline progress.

Client sends one JSON message to start:
{
    "files": [{"name": "anthem.pdf", "data": "<base64>"}],
    "ledger": "<base64 csv or null>",
    "facility_id": null
}

Server streams events as each agent phase starts and completes.
"""

from __future__ import annotations

import asyncio
import base64
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/pipeline")
async def pipeline_ws(websocket: WebSocket):
    """
    WebSocket endpoint for streaming batch EOB pipeline progress.

    Events streamed:
      {"type": "batch_start",    "total_files": 2}
      {"type": "phase_start",    "phase": "extraction", "file": "anthem.pdf", "agent": "EOBAgent"}
      {"type": "phase_complete", "phase": "extraction", "file": "anthem.pdf", "confidence": 1.0, ...}
      {"type": "phase_start",    "phase": "detection",  ...}
      {"type": "phase_complete", "phase": "detection",  "flags_found": 1, ...}
      {"type": "phase_start",    "phase": "validation", ...}
      {"type": "phase_complete", "phase": "validation", "recommendation": "ESCALATE", ...}
      {"type": "phase_start",    "phase": "escalation_gate", ...}
      {"type": "file_complete",  "file": "anthem.pdf", "status": "ESCALATED", "flagged": true, ...}
      {"type": "batch_complete", "summary": {...}}
      {"type": "error", "message": "..."}  # on failure
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        job = json.loads(raw)

        files_data = job.get("files", [])
        ledger_b64 = job.get("ledger")
        facility_id = job.get("facility_id")

        await websocket.send_json({"type": "batch_start", "total_files": len(files_data)})

        # decode PDF files
        files = []
        for f in files_data:
            pdf_bytes = base64.b64decode(f["data"])
            files.append((f["name"], pdf_bytes))

        # decode ledger CSV if provided
        ledger = None
        if ledger_b64:
            from api.services.recoupment_service import RecoupmentService
            ledger_bytes = base64.b64decode(ledger_b64)
            ledger = RecoupmentService._parse_ledger_csv(ledger_bytes)

        from api.models.base import SessionLocal
        from agents.orchestrator import Orchestrator

        # Thread-safe queue bridges sync orchestrator emit → async websocket send
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def sync_emit(event: dict) -> None:
            """Called from synchronous orchestrator code; puts event on the async queue."""
            loop.call_soon_threadsafe(queue.put_nowait, event)

        db = SessionLocal()
        orch = Orchestrator(db=db)

        results = []
        for filename, pdf_bytes in files:
            result = orch.run(
                pdf_bytes,
                filename,
                ledger=ledger,
                facility_id=facility_id,
                emit=sync_emit,
            )
            results.append(result)

            # drain all queued events for this file before moving to the next
            while not queue.empty():
                event = queue.get_nowait()
                await websocket.send_json(event)

        # final batch summary
        total_amount = sum(
            abs(a)
            for r in results
            for f in r.flags
            for a in f.get("amounts_found", [])
        )
        summary = {
            "total_files": len(results),
            "approved": sum(1 for r in results if r.status == "APPROVED"),
            "escalated": sum(1 for r in results if r.status == "ESCALATED"),
            "rejected": sum(1 for r in results if r.status == "REJECTED"),
            "flagged_files": sum(1 for r in results if r.flagged),
            "total_amount_at_risk": total_amount,
            "tickets_created": sum(len(r.escalation_tickets) for r in results),
        }
        await websocket.send_json({"type": "batch_complete", "summary": summary})
        db.close()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
