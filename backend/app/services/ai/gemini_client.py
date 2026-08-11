"""Thin async client for the Google Gemini REST API.

Kept SDK-free (uses httpx) so the only new dependency is httpx, which the
application already needs. The client returns parsed JSON from the model when
asked, raising GeminiError on transport or contract failures so callers can
fall back gracefully.

This is also the choke point for PII redaction: the prompt is redacted here,
not by the callers that build it. Putting it at the boundary means a prompt
that reaches Google unredacted is impossible rather than merely unlikely --
a future call site cannot forget a step it never has to take.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.services.ai.masking import Redactor, default_redactor
from app.services.ai.tracing import traced

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(RuntimeError):
    """Raised when the Gemini API cannot be reached or returns bad data."""


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 30.0,
        redactor: Redactor | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        # Defaulting to the pattern-only redactor rather than to None means a
        # caller that does not know whose data this is still cannot send an
        # email address or a phone number; it only loses the name.
        self._redactor = redactor or default_redactor()

    @traced("gemini.generate_json", run_type="llm")
    async def generate_json(
        self, *, system_instruction: str, prompt: str
    ) -> Any:
        """Call the model in JSON mode and return the parsed payload.

        The prompt is redacted first. `system_instruction` is not: it is a
        constant written in this repository, never user data, and running it
        through the rules would only risk mangling the response schema.
        """
        redaction = self._redactor.apply(prompt)
        if redaction.counts:
            # The tally only, never the values -- logging what was redacted
            # would put it straight back where it must not be.
            logger.info("Redacted %s from prompt before the model call.", redaction.summary())

        url = f"{_BASE_URL}/models/{self._model}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": redaction.text}]}],
            "generationConfig": {"response_mime_type": "application/json"},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # The key goes in a header, never in the query string: httpx
                # logs the full request URL, so `?key=...` printed the secret
                # into the application logs on every call.
                response = await client.post(
                    url, headers={"x-goog-api-key": self._api_key}, json=body
                )
        except httpx.HTTPError as exc:  # pragma: no cover - network failure
            raise GeminiError(f"Gemini request failed: {exc}") from exc

        if response.status_code != 200:
            raise GeminiError(
                f"Gemini returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except (KeyError, IndexError, json.JSONDecodeError, ValueError) as exc:
            raise GeminiError(f"Unexpected Gemini response shape: {exc}") from exc
