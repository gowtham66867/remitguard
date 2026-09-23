"""
Load `platform/.env` into the process environment.

`.env.example` has always instructed `cp .env.example .env`, but nothing read
the result — the file sat there being ignored while `MOSS_PROJECT_ID` stayed
unset and the semantic layer silently ran its fallback. Importing this module
first closes that gap.

Import it BEFORE anything that reads configuration at import time
(`agents.semantic_matcher` resolves its threshold and credentials on import),
which is why it sits at the very top of `main.py` and the CLI entry points.

Real environment variables always win over the file, so Cloud Run's injected
values are never overridden by a stray `.env` inside an image.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load() -> bool:
    """Load .env if present. Returns True when a file was read."""
    if not os.path.exists(ENV_PATH):
        return False
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=False)
    except ImportError:
        # python-dotenv is a local-development convenience. Parse the simple
        # KEY=value case by hand rather than making it a hard dependency of
        # the container, which gets its configuration injected directly.
        with open(ENV_PATH) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    logger.info("Loaded configuration from %s", ENV_PATH)
    return True


loaded = load()
