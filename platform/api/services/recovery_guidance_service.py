"""Optional, de-identified LLM guidance for a RemitGuard recovery case.

The HiDevs virtual key is deliberately used only with a small structured case
summary.  Raw EOB text, claim numbers, patient identifiers, and uploaded files
never leave the RemitGuard process through this client.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict

logger = logging.getLogger(__name__)


class RecoveryGuidanceService:
    """Call an OpenAI-compatible virtual LLM endpoint with safe case metadata."""

    def __init__(self) -> None:
        self.api_key = os.environ.get("HIDEVS_LLM_API_KEY", "")
        self.base_url = os.environ.get("HIDEVS_LLM_BASE_URL", "https://llm.hidevs.xyz").rstrip("/")
        self.model = os.environ.get("HIDEVS_LLM_MODEL", "gemini-3.5-flash")
        self.timeout_s = float(os.environ.get("HIDEVS_LLM_TIMEOUT_SECONDS", "12"))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model if self.enabled else None,
            "reason": None if self.enabled else "HIDEVS_LLM_API_KEY is not configured.",
        }

    def create_brief(self, case: Dict[str, Any]) -> str:
        if not self.enabled:
            raise RuntimeError("Recovery guidance is not configured.")

        # `case` is validated by the route, but retain a hard allow-list here
        # so a future caller cannot accidentally forward EOB text or PHI.
        safe_case = {
            "decision": case["decision"],
            "recovery_at_risk": case["recovery_at_risk"],
            "evidence_count": case["evidence_count"],
            "signal_types": case["signal_types"],
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are RemitGuard's recovery-operations copilot. Produce a concise, "
                    "actionable recovery brief from de-identified metadata only. Do not invent "
                    "claim, patient, payer, policy, contract, or legal facts. Do not provide "
                    "medical or legal advice. A human billing coordinator must verify the "
                    "source EOB before acting."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Return exactly four short Markdown sections: **Decision**, **Verify now** "
                    "(two bullets), **Payer question**, and **Escalate if**. Make it practical "
                    "for a billing coordinator, but only use the provided metadata. "
                    "Use only this de-identified case metadata:\n"
                    + json.dumps(safe_case, separators=(",", ":"))
                ),
            },
        ]
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            # Gemini 3.5 Flash uses a small internal reasoning budget before
            # emitting the concise brief. Leave enough room for both phases.
            "max_tokens": 1024,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"].strip()
            if not content:
                raise RuntimeError("The guidance service returned an empty response.")
            return content
        except urllib.error.HTTPError as exc:
            # Never include a provider body: it could contain implementation or
            # account details. The status code is sufficient to troubleshoot.
            logger.warning("Recovery guidance provider returned HTTP %s", exc.code)
            raise RuntimeError("The guidance service rejected this request.") from exc
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            logger.warning("Recovery guidance unavailable: %s", type(exc).__name__)
            raise RuntimeError("The guidance service is temporarily unavailable.") from exc


_guidance_service: RecoveryGuidanceService | None = None


def get_recovery_guidance_service() -> RecoveryGuidanceService:
    global _guidance_service
    if _guidance_service is None:
        _guidance_service = RecoveryGuidanceService()
    return _guidance_service
