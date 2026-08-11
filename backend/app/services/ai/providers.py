"""Which chat model the transport talks to, and how to get JSON out of it.

Three provider values cover far more than three services, for the same reason
`STORAGE_BACKEND=s3` covers S3, R2, MinIO and B2: **OpenAI-compatible is the
lingua franca.** Groq, Ollama, OpenRouter, Together, vLLM and LM Studio all
expose the OpenAI chat API, so they are the `openai` provider with a different
`AI_BASE_URL` rather than five more integration packages.

  * `gemini`     -- `langchain-google-genai`. Installed; the default.
  * `anthropic`  -- `langchain-anthropic`. Optional extra.
  * `openai`     -- `langchain-openai`. Optional extra. With `AI_BASE_URL` set,
                    also Groq, Ollama, OpenRouter and anything else speaking
                    that API.

The optional packages are imported inside their builders, so a deployment on
Gemini never pays for them and a missing one produces a sentence naming the
package to install rather than an ImportError from three frames down.

## The part that is not configuration

**Only some of these have a JSON mode.** Gemini takes
`response_mime_type="application/json"`; OpenAI takes
`response_format={"type": "json_object"}`; **Anthropic has neither** -- you ask
for JSON in the prompt and hope. Every caller above the transport is written
against *parsed JSON*, so "hope" is not available.

That is what `extract_json` is for. It is not defensive decoration: without it,
switching to Anthropic breaks question generation on the first call, and the
symptom is the deterministic fallback quietly taking over -- an interview that
works and is merely generic, which is the failure mode this project is most
practised at not noticing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

Provider = Literal["gemini", "anthropic", "openai"]

# A fenced block, with or without a language tag. Anthropic in particular likes
# to wrap JSON in ```json ... ``` no matter how the prompt is worded.
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)

# The package each provider needs, so a missing one names its own fix.
_PACKAGES = {
    "gemini": "langchain-google-genai",
    "anthropic": "langchain-anthropic",
    "openai": "langchain-openai",
}


class ProviderError(RuntimeError):
    """Raised when a provider cannot be built -- unknown name, missing package."""


def build_chat_model(
    *,
    provider: Provider,
    model: str,
    api_key: str,
    timeout: float,
    max_retries: int,
    base_url: str | None = None,
    json_mode: bool = True,
) -> Any:
    """Construct the LangChain chat model for `provider`.

    `max_retries` is passed through rather than defaulted per provider: every
    integration here ships a different default (six, two, two), and the reason
    this project wants one is the same for all of them -- there are already two
    retry layers above, and the free tier is twenty requests a day.

    `json_mode` asks the provider for structured output where it has such a
    thing. It is a request, not a requirement: `extract_json` handles replies
    from providers that have no JSON mode at all, which is what makes turning
    this off safe rather than catastrophic. Anthropic ignores it entirely --
    there is nothing to ask for.
    """
    try:
        match provider:
            case "gemini":
                from langchain_google_genai import ChatGoogleGenerativeAI

                return ChatGoogleGenerativeAI(
                    model=model,
                    api_key=api_key,
                    request_timeout=timeout,
                    max_retries=max_retries,
                    response_mime_type=(
                        "application/json" if json_mode else None
                    ),
                )

            case "anthropic":
                from langchain_anthropic import ChatAnthropic
                from pydantic import SecretStr

                # No JSON mode to ask for, so `json_mode` is deliberately
                # ignored here. The prompt requests JSON and `extract_json`
                # handles what comes back -- which is why this provider works
                # at all, and why turning JSON mode off elsewhere is safe.
                #
                # `max_tokens` resolves to 4096, a hard ceiling where Gemini and
                # OpenAI are effectively unbounded. Comfortable for five
                # questions or one evaluation; worth knowing it exists.
                return ChatAnthropic(
                    model_name=model,
                    api_key=SecretStr(api_key),
                    timeout=timeout,
                    max_retries=max_retries,
                    stop=None,
                )

            case "openai":
                from langchain_openai import ChatOpenAI
                from pydantic import SecretStr

                return ChatOpenAI(
                    model=model,
                    # SecretStr, so a repr of the client cannot print the key --
                    # the same concern that moved it out of the query string
                    # when it turned up in the logs.
                    api_key=SecretStr(api_key),
                    base_url=base_url,
                    timeout=timeout,
                    max_retries=max_retries,
                    # Genuine OpenAI honours this, Groq accepts it, recent
                    # Ollama accepts it -- but an older shim behind AI_BASE_URL
                    # can reject the unknown field outright, which turns every
                    # call into a 400 and silently hands the interview to the
                    # deterministic fallback. AI_JSON_MODE=false is the way out,
                    # and it is safe because `extract_json` never depended on
                    # this being set.
                    model_kwargs=(
                        {"response_format": {"type": "json_object"}}
                        if json_mode
                        else {}
                    ),
                )

            case _:
                raise ProviderError(
                    f"Unknown AI_PROVIDER: {provider!r}. "
                    f"Expected one of {', '.join(sorted(_PACKAGES))}."
                )
    except ImportError as exc:
        package = _PACKAGES.get(provider, "the provider package")
        raise ProviderError(
            f"AI_PROVIDER={provider} needs {package}, which is not installed. "
            f'Install it with: pip install "{package}"'
        ) from exc


def extract_json(text: str) -> Any:
    """Parse JSON out of a model reply, tolerating the ways models wrap it.

    Tried in order, cheapest first:

    1. The whole string. What a real JSON mode returns, and the only path taken
       on Gemini and OpenAI.
    2. The contents of a ``` fence. Anthropic reaches for one reflexively.
    3. The outermost {...} or [...] span. Covers "Here is the JSON you asked
       for:" and a trailing "Let me know if you need anything else."

    Raises `ValueError` if none of them yields JSON, which the caller turns into
    `ModelError` -- so a provider that cannot be coaxed into JSON degrades to
    the deterministic fallback rather than crashing a request.

    Deliberately *not* a repair step: nothing here fixes trailing commas or
    single quotes. Guessing at malformed JSON risks silently changing a score
    or a question, and a clean failure into the fallback is better than a
    plausible wrong answer.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("The model returned an empty reply.")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(stripped)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    span = _outermost_span(stripped)
    if span is not None:
        try:
            return json.loads(span)
        except json.JSONDecodeError:
            pass

    raise ValueError("No JSON found in the model reply.")


def _outermost_span(text: str) -> str | None:
    """The substring from the first opening bracket to its matching close.

    Bracket-counted rather than a greedy regex, and *string-aware*: a brace
    inside a quoted value ("salary: {competitive}") would otherwise unbalance
    the count and truncate the object at the wrong place.
    """
    starts = [index for index in (text.find("{"), text.find("[")) if index != -1]
    if not starts:
        return None
    start = min(starts)
    opening = text[start]
    closing = "}" if opening == "{" else "]"

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
