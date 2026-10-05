"""LLM clients for the diagnosis agent (spec §10).

LLMClient has one method: complete(system, user, timeout_s) -> LLMReply (the
raw text and the model that actually produced it). Clients raise LLMTimeout or
LLMUnavailable; the agent turns both into a diagnosis status. They never
decide anything about the incident.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Protocol

# JSON schema the model's reply must match (§10). Sent to the API as a
# structured-output format; the agent still validates the reply with pydantic.
OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["diagnosed", "abstained"]},
        "likely_factors": {"type": "array", "items": {"type": "string"}},
        "cited_evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"anyOf": [{"type": "string", "enum": ["low", "medium", "high"]}, {"type": "null"}]},
    },
    "required": ["status", "likely_factors", "cited_evidence", "confidence"],
    "additionalProperties": False,
}


class LLMTimeout(Exception):
    pass


class LLMUnavailable(Exception):
    pass


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str  # the model that actually answered; stored in diagnoses.model


class LLMClient(Protocol):
    name: str  # the model requested; recorded when no model answered (timeout, error)

    def complete(self, system: str, user: str, timeout_s: float, history: list[dict] | None = None) -> LLMReply:
        """history: earlier turns of this conversation ({"role", "content"} dicts), sent
        before `user`. Used only for the one self-correction request."""
        ...


class AnthropicClient:
    """Real calls to the Claude API.

    The API key comes only from the ANTHROPIC_API_KEY environment variable; it
    is never hard-coded, logged, or put into an error message.
    """

    # Diagnosis is a short, single-step reasoning task. Effort is set explicitly
    # because claude-opus-5-5 defaults to "medium" and the default may change.
    EFFORT = "medium"
    MAX_TOKENS = 16000  # leaves room for adaptive thinking; the JSON reply itself is small

    def __init__(self, model: str) -> None:
        import anthropic  # imported here so tests and offline runs don't need the SDK configured

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        self._anthropic = anthropic
        # max_retries=0: diagnosis_timeout_seconds is the whole budget for a call.
        # SDK retries would multiply it, and the alert must not wait on them.
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0)
        self.model = model
        self.name = model

    def complete(self, system: str, user: str, timeout_s: float, history: list[dict] | None = None) -> LLMReply:
        a = self._anthropic
        try:
            # No refusal fallback: a declined request is recorded as unavailable,
            # and only the configured model ever produces a diagnosis.
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                system=system,
                messages=[*(history or []), {"role": "user", "content": user}],
                output_config={"effort": self.EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_JSON_SCHEMA}},
                timeout=timeout_s,
            )
        except a.APITimeoutError:
            raise LLMTimeout(f"no response within {timeout_s}s") from None
        except a.RateLimitError:
            raise LLMUnavailable("API rate limit (429)") from None
        except a.APIStatusError as e:
            raise LLMUnavailable(f"API error {e.status_code}") from None
        except a.APIConnectionError:
            raise LLMUnavailable("could not connect to the API") from None

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise LLMUnavailable(f"model declined (refusal, category {category})")
        text = "".join(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "max_tokens":
            raise LLMUnavailable("response cut off at max_tokens")
        return LLMReply(text, response.model)


class FailingClient:
    """Always unavailable. Used by the dashboard's "Kill LLM" toggle and when no
    client is configured, so the alert path can be exercised without a model."""

    name = "failing"

    def __init__(self, reason: str = "LLM disabled (kill switch)") -> None:
        self.reason = reason

    def complete(self, system: str, user: str, timeout_s: float, history: list[dict] | None = None) -> LLMReply:
        raise LLMUnavailable(self.reason)


def _evidence(user: str) -> dict:
    m = re.search(r"<evidence>\s*(.*?)\s*</evidence>", user, re.S)
    return json.loads(m.group(1)) if m else {}


def _valid(user: str) -> str:
    """Cites the most recent maintenance entry at or before onset, if any."""
    ev = _evidence(user)
    onset = ev.get("incident", {}).get("onset_ts", "")
    before = [m for m in ev.get("maintenance", []) if m["ts"] <= onset]
    if not before:
        return _abstain(user)
    m = before[-1]
    return json.dumps({
        "status": "diagnosed",
        "likely_factors": [f"Maintenance shortly before onset: {m['description'][:80]}"],
        "cited_evidence": [m["id"]],
        "confidence": "medium",
    })


def _abstain(user: str) -> str:
    return json.dumps({"status": "abstained", "likely_factors": [], "cited_evidence": [], "confidence": None})


def _fabricated(user: str) -> str:
    return json.dumps({
        "status": "diagnosed", "likely_factors": ["Chamber seal replaced"],
        "cited_evidence": ["M-9999"], "confidence": "high",
    })


def _malformed(user: str) -> str:
    return '{"status": "diagnosed", "likely_factors": ["unterminated'


def _timeout(user: str) -> str:
    raise LLMTimeout("scripted timeout")


def _self_correct_demo(user: str, correction: bool = False) -> str:
    """Hosted-demo script. When the evidence has a maintenance entry to cite, the first
    answer deliberately cites a made-up record (M-9999) so the checker rejects it, and
    the correction is valid. With nothing to cite, it simply abstains."""
    if correction:
        return _valid(user)
    if _evidence(user).get("maintenance"):
        return _fabricated(user)
    return _abstain(user)


SCRIPTS: dict[str, Callable[[str], str]] = {
    "valid": _valid,
    "abstain": _abstain,
    "malformed": _malformed,
    "fabricated": _fabricated,
    "timeout": _timeout,
    "self_correct_demo": _self_correct_demo,
}
CORRECTION_AWARE = {"self_correct_demo"}


class FakeClient:
    """Scripted responses for tests. `script` is one name from SCRIPTS (used for
    every call) or a list of names/raw strings used in order (the last repeats).
    Records every call so tests can count them."""

    name = "fake"

    def __init__(self, script: str | list[str] = "valid") -> None:
        self.script = [script] if isinstance(script, str) else list(script)
        self.calls: list[tuple[str, str]] = []
        self.histories: list[list[dict] | None] = []

    def complete(self, system: str, user: str, timeout_s: float, history: list[dict] | None = None) -> LLMReply:
        step = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append((system, user))
        self.histories.append(history)
        # The evidence is in the first user turn; a correction turn carries it in history.
        evidence_text = history[0]["content"] if history else user
        if step in CORRECTION_AWARE:
            return LLMReply(SCRIPTS[step](evidence_text, correction=bool(history)), self.name)
        return LLMReply(SCRIPTS[step](evidence_text) if step in SCRIPTS else step, self.name)
