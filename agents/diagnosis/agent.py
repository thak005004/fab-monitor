"""Diagnosis agent (spec §10): call -> validate -> verify -> store.

run() never raises for anything the model or the API does. Timeouts, API
errors, invalid output, failed verification and rate limiting all come back
as a status, so the alert always reaches a person.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.diagnosis import evidence
from agents.diagnosis.client import LLMClient, LLMTimeout, LLMUnavailable
from agents.diagnosis.verify import verify
from config_loader import Config
from db import repo
from sim.clock import Clock

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "diagnosis_v1.txt"
PROMPT_VERSION = PROMPT_PATH.stem
_SYSTEM, _USER_TEMPLATE = PROMPT_PATH.read_text().split("=== USER ===\n")


class DiagnosisOutput(BaseModel):
    """The §10 output schema."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["diagnosed", "abstained"]
    likely_factors: list[str] = Field(max_length=3)
    cited_evidence: list[str]
    confidence: Literal["low", "medium", "high"] | None


@dataclass(frozen=True)
class Diagnosis:
    status: str  # diagnosed | abstained | rejected | unavailable
    diagnosis_id: str | None = None
    likely_factors: list[str] = field(default_factory=list)
    cited_evidence: list[str] = field(default_factory=list)
    confidence: str | None = None
    reason: str | None = None  # why rejected or unavailable

    @classmethod
    def unavailable(cls, reason: str) -> Diagnosis:
        """Not stored: used where no diagnosis is attempted (e.g. dropout)."""
        return cls("unavailable", reason=reason)

    @property
    def trusted(self) -> bool:
        return self.status == "diagnosed"


def run(conn: sqlite3.Connection, incident: dict, clock: Clock, config: Config, client: LLMClient) -> Diagnosis:
    now = clock.now_iso()
    bundle = evidence.build_bundle(conn, incident, clock, config)

    if repo.count_llm_calls_at(conn, now) >= config.max_diagnoses_per_tick:
        return _store(conn, incident, bundle, Diagnosis("unavailable", reason="rate limited"), [], None, now)

    user = _USER_TEMPLATE.replace("{evidence}", evidence.render(bundle))
    raws: list[str] = []
    answered_by = client.name  # replaced by the model that actually answered, once one does
    output = None
    problem = None
    for _attempt in range(2):  # invalid output is retried once
        try:
            reply = client.complete(_SYSTEM, user, config.diagnosis_timeout_seconds)
            raw, answered_by = reply.text, reply.model
        except LLMTimeout as e:
            return _store(conn, incident, bundle, Diagnosis("unavailable", reason=f"timeout: {e}"), raws, answered_by, now)
        except LLMUnavailable as e:
            return _store(conn, incident, bundle, Diagnosis("unavailable", reason=f"LLM unavailable: {e}"), raws, answered_by, now)
        except Exception as e:  # a client bug must not stop the alert
            return _store(conn, incident, bundle, Diagnosis("unavailable", reason=f"client error: {type(e).__name__}"),
                          raws, answered_by, now)
        raws.append(raw)
        try:
            output = DiagnosisOutput.model_validate(json.loads(raw)).model_dump()
            break
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            problem = _short(e)
    if output is None:
        return _store(conn, incident, bundle, Diagnosis("unavailable", reason=f"invalid output twice: {problem}"),
                      raws, answered_by, now)

    rejection = verify(output, bundle, clock, config)
    status = "rejected" if rejection else output["status"]
    return _store(
        conn, incident, bundle,
        Diagnosis(status, None, output["likely_factors"], output["cited_evidence"], output["confidence"], rejection),
        raws, answered_by, now,
    )


def _store(conn, incident, bundle, d: Diagnosis, raws: list[str], model: str | None, now: str) -> Diagnosis:
    """Every attempt is stored, with the exact evidence bundle and raw responses."""
    diagnosis_id = f"DX-{repo.count_diagnoses(conn) + 1:05d}"
    repo.insert_diagnosis(
        conn, diagnosis_id, incident["incident_id"], d.status,
        json.dumps(d.likely_factors), json.dumps(d.cited_evidence), d.confidence, d.reason,
        json.dumps(bundle, sort_keys=True), json.dumps(raws) if raws else None, PROMPT_VERSION, model, now,
    )
    return Diagnosis(d.status, diagnosis_id, d.likely_factors, d.cited_evidence, d.confidence, d.reason)


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc'])) or '<root>'}: {x['msg']}" for x in e.errors())[:300]
    return f"{type(e).__name__}: {e}"[:300]
