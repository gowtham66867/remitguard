"""Privacy tests for the optional virtual-LLM recovery brief."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from api.services.recovery_guidance_service import RecoveryGuidanceService


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"- Verify with a coordinator."}}]}'


def test_guidance_sends_only_allowlisted_deidentified_metadata(monkeypatch):
    monkeypatch.setenv("HIDEVS_LLM_API_KEY", "test-key")
    service = RecoveryGuidanceService()
    sent = {}

    def fake_open(request, timeout):
        sent["url"] = request.full_url
        sent["body"] = request.data.decode("utf-8")
        return _Response()

    # A hypothetical caller may attach raw source fields, but the service must
    # discard them before making the outbound provider request.
    unsafe_case = {
        "decision": "HUMAN_REVIEW",
        "recovery_at_risk": 42.0,
        "evidence_count": 1,
        "signal_types": ["pattern"],
        "raw_eob_text": "Jane Patient / claim 9988",
        "claim_number": "9988",
    }
    with patch("urllib.request.urlopen", fake_open):
        assert service.create_brief(unsafe_case) == "- Verify with a coordinator."

    payload = json.loads(sent["body"])
    assert sent["url"] == "https://llm.hidevs.xyz/v1/chat/completions"
    assert "Jane Patient" not in sent["body"]
    assert payload["model"] == service.model
