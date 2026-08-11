"""Async chat client, over LangChain's provider integrations.

Which provider it talks to is `AI_PROVIDER` -- Gemini, Anthropic, or anything
speaking the OpenAI API (Groq, Ollama, OpenRouter, vLLM) via `AI_BASE_URL`. The
choosing lives in `providers.py`; this module owns the seam around it.

That seam -- `generate_json(system_instruction=..., prompt=...)` returning
parsed JSON and raising `ModelError` -- has not changed through two transport
rewrites now, first from hand-written httpx to LangChain and now from one
provider to any. The generator, the evaluator, the fallback layer and every
test above this line neither changed nor noticed, which is the whole return on
keeping a wrapper that "only forwards".

**Why hand the transport over.** Google has retired a model ID under this
project twice, once silently for weeks, and each time the fix was ours to find.
An integration package absorbs that class of change. It also makes the
multi-provider and streaming items on the roadmap a swap of this class rather
than another client written from scratch.

**Why keep the wrapper rather than call the model directly from the generator.**
Redaction. The prompt is redacted *here*, not by the callers that build it, so
a prompt reaching Google unredacted is impossible rather than merely unlikely
-- a future call site cannot forget a step it never has to take. Dissolving
this class into LangChain calls at each site would move that guarantee to
every one of them.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.ai import call_metrics
from app.services.ai.masking import Redactor, default_redactor
from app.services.ai.providers import Provider, build_chat_model, extract_json
from app.services.ai.tracing import traced

logger = logging.getLogger(__name__)

# One attempt, no provider-level retries. The integration defaults to six, and
# six is wrong here for two reasons:
#
#  * **Quota.** The free tier allows twenty requests per day for the whole
#    account, so one unlucky call could quietly spend a third of the day.
#  * **There are already two layers of retry above this one.**
#    FallbackQuestionGenerator and FallbackEvaluator swap in the deterministic
#    implementation the moment this raises, and the arq worker retries a failed
#    evaluation with backoff up to EVALUATION_MAX_TRIES. A third layer inside
#    the client multiplies with those (6 x 3 = 18 attempts for one evaluation)
#    and delays the fallback the interview depends on -- a dead provider took
#    78 seconds to surface here rather than the ~30 the timeout implies.
_MAX_ATTEMPTS = 1


class ModelError(RuntimeError):
    """Raised when the Gemini API cannot be reached or returns bad data."""


class ModelClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        provider: Provider = "gemini",
        base_url: str | None = None,
        timeout: float = 30.0,
        redactor: Redactor | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._provider: Provider = provider
        self._base_url = base_url
        self._timeout = timeout
        # Defaulting to the pattern-only redactor rather than to None means a
        # caller that does not know whose data this is still cannot send an
        # email address or a phone number; it only loses the name.
        self._redactor = redactor or default_redactor()
        self._chat: Any = None

    def _model_client(self) -> Any:
        """Build the chat model once, on first use.

        Built lazily and cached on the instance: each integration pulls in its
        own SDK and auth stack, and this class is only constructed when an API
        key exists, so an app running on the deterministic fallbacks never pays
        for the import.
        """
        if self._chat is None:
            self._chat = build_chat_model(
                provider=self._provider,
                model=self._model,
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=_MAX_ATTEMPTS,
                base_url=self._base_url,
            )
        return self._chat

    @traced("model.generate_json", run_type="llm")
    async def generate_json(self, *, system_instruction: str, prompt: str) -> Any:
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

        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            # Only the round-trip is measured. A reply that arrives and then
            # fails to parse below is a call that succeeded -- it took that
            # long and cost those tokens -- and shows up as a *fallback*
            # instead. Keeping the two apart is what makes "provider up,
            # output garbage" distinguishable from "provider down".
            with call_metrics.measure("generate", self._model) as call:
                reply = await self._model_client().ainvoke(
                    [
                        SystemMessage(content=system_instruction),
                        HumanMessage(content=redaction.text),
                    ]
                )
                call.usage(reply)
        except Exception as exc:  # noqa: BLE001 - the integration raises its own
            # Everything above this line is written against ModelError, and
            # the fallback layer keys on it. Letting a provider-specific
            # exception through would change what callers must handle every
            # time the integration is upgraded.
            raise ModelError(f"{self._provider} request failed: {exc}") from exc

        try:
            # Not `json.loads`: Anthropic has no JSON mode, so its reply may
            # arrive fenced or with a sentence around it. See providers.py.
            return extract_json(_text_of(reply))
        except (ValueError, TypeError) as exc:
            raise ModelError(
                f"Unexpected {self._provider} response shape: {exc}"
            ) from exc


def _text_of(reply: Any) -> str:
    """The reply's text, whichever shape the message carries it in.

    `content` is a plain string for a simple text reply and a list of content
    blocks when the model returns parts. Both occur, and a version bump can
    change which -- so both are handled rather than assumed.
    """
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        ]
        return "".join(parts)
    return str(content)
