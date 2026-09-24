"""Thin Gemini / embedding client on Vertex AI (google-genai SDK).

Every caller must survive LLMUnavailable: retrieval falls back to keywords,
drafting falls back to the runbook template. Inputs and outputs are redacted.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .redact import redact

log = logging.getLogger(__name__)

EMBED_MODEL = "text-embedding-005"
EMBED_DIM = 768


class LLMUnavailable(RuntimeError):
    pass


class Gemini:
    def __init__(self, project: str, location: str | None = None, model: str | None = None):
        self.project = project
        # Gemini models are served from "global" or a region; keep it separate from the BQ location.
        self.location = location or os.environ.get("GEMINI_LOCATION", "global")
        self.embed_location = os.environ.get("EMBED_LOCATION") or os.environ.get("GCP_REGION") or "us-central1"
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
        self._client = None
        self._embed_client = None

    def _clients(self):
        try:
            from google import genai
        except ImportError as exc:
            raise LLMUnavailable("google-genai not installed") from exc
        if self._client is None:
            self._client = genai.Client(vertexai=True, project=self.project, location=self.location)
            self._embed_client = genai.Client(vertexai=True, project=self.project, location=self.embed_location)
        return self._client, self._embed_client

    def _retry(self, fn, what: str, attempts: int = 3):
        last: Exception | None = None
        for i in range(attempts):
            try:
                return fn()
            except LLMUnavailable:
                raise
            except Exception as exc:  # SDK raises a variety of transport/API errors
                last = exc
                if i < attempts - 1:
                    delay = 1.5 * 2 ** i
                    log.warning("%s failed (%s); retry in %.1fs", what, type(exc).__name__, delay)
                    time.sleep(delay)
        raise LLMUnavailable(f"{what} failed after {attempts} attempts: {last}") from last

    def generate_json(self, prompt: str, schema: dict[str, Any], temperature: float = 0.2) -> dict[str, Any]:
        client, _ = self._clients()
        from google.genai import types

        cfg = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
            # No tools are passed; turning AFC off also silences the SDK's per-call warnings.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        def call():
            resp = client.models.generate_content(model=self.model, contents=redact(prompt), config=cfg)
            return json.loads(redact(resp.text))

        return self._retry(call, f"gemini {self.model}")

    def embed_query(self, text: str) -> list[float]:
        _, client = self._clients()
        from google.genai import types

        cfg = types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=EMBED_DIM)

        def call():
            resp = client.models.embed_content(model=EMBED_MODEL, contents=[redact(text)], config=cfg)
            return list(resp.embeddings[0].values)

        return self._retry(call, EMBED_MODEL)
