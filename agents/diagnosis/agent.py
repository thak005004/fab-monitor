"""Diagnosis agent (spec §10): call -> validate -> verify -> (one self-correction) -> store.

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
from agents.diagnosis.verify import all_problems, check_citation
from config_loader import Config
from db import repo
from sim.clock import Clock

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "diagnosis_v2.txt"
PROMPT_VERSION = PROMPT_PATH.stem
_SYSTEM, _rest = PROMPT_PATH.read_text().split("=== USER ===\n")
_USER_TEMPLATE, _CORRECTION_TEMPLATE = _rest.split("=== CORRECTION ===\n")


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
    first_attempt: dict | None = None  # the rejected first answer, if a self-correction was tried
    correction_attempts: int = 0

    @property
    def self_corrected(self) -> bool:
        return self.correction_attempts > 0 and self.status in ("diagnosed", "abstained")

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

    problems = all_problems(output, bundle, clock, config)
    if not problems:
        return _store(conn, incident, bundle, _from_output(output), raws, answered_by, now)

    first = {"status": "rejected", "likely_factors": output["likely_factors"], "cited_evidence": output["cited_evidence"],
             "confidence": output["confidence"], "reason": "; ".join(problems)}
    # One self-correction, never more. It is an LLM call, so it counts toward the
    # per-tick limit: skip it if this tick's budget is already used up.
    if repo.count_llm_calls_at(conn, now) + 1 >= config.max_diagnoses_per_tick:
        return _store(conn, incident, bundle,
                      Diagnosis("rejected", None, output["likely_factors"], output["cited_evidence"], output["confidence"],
                                f"{first['reason']} (no self-correction: rate limited)"),
                      raws, answered_by, now)

    followup = _CORRECTION_TEMPLATE.replace("{reasons}", "\n".join(f"- {p}" for p in problems)).replace(
        "{citable_ids}", ", ".join(_citable_ids(bundle, clock, config)) or "none")
    history = [{"role": "user", "content": user}, {"role": "assistant", "content": raws[-1]}]
    try:
        reply = client.complete(_SYSTEM, followup, config.diagnosis_timeout_seconds, history=history)
        raws.append(reply.text)
        answered_by = reply.model
        revised = DiagnosisOutput.model_validate(json.loads(reply.text)).model_dump()
    except (LLMTimeout, LLMUnavailable) as e:
        failed = f"self-correction got no answer: {e}"
    except (json.JSONDecodeError, ValidationError, TypeError) as e:
        failed = f"self-correction was invalid: {_short(e)}"
    except Exception as e:  # a client bug must not stop the alert
        failed = f"self-correction client error: {type(e).__name__}"
    else:
        again = all_problems(revised, bundle, clock, config)
        if not again:
            return _store(conn, incident, bundle,
                          _from_output(revised, first_attempt=first, correction_attempts=1), raws, answered_by, now)
        return _store(conn, incident, bundle,
                      _from_output(revised, status="rejected", reason="; ".join(again), first_attempt=first,
                                   correction_attempts=1), raws, answered_by, now)
    # The correction itself failed: the first answer stays rejected.
    return _store(conn, incident, bundle,
                  _from_output(output, status="rejected", reason=f"{first['reason']}; {failed}", first_attempt=first,
                               correction_attempts=1), raws, answered_by, now)


def _from_output(output: dict, status: str | None = None, reason: str | None = None,
                 first_attempt: dict | None = None, correction_attempts: int = 0) -> Diagnosis:
    return Diagnosis(status or output["status"], None, output["likely_factors"], output["cited_evidence"],
                     output["confidence"], reason, first_attempt, correction_attempts)


def _citable_ids(bundle: dict, clock: Clock, config: Config) -> list[str]:
    """Maintenance and recipe IDs the checker would accept (readings are listed in the evidence)."""
    return [item["id"] for kind in ("maintenance", "recipe_changes") for item in bundle[kind]
            if check_citation(item["id"], bundle, clock, config) is None]


def _store(conn, incident, bundle, d: Diagnosis, raws: list[str], model: str | None, now: str) -> Diagnosis:
    """Every attempt is stored, with the exact evidence bundle and raw responses. A
    self-correction is stored in the same row: raw_response holds both replies and
    first_attempt the rejected first answer with its reasons."""
    diagnosis_id = f"DX-{repo.count_diagnoses(conn) + 1:05d}"
    repo.insert_diagnosis(
        conn, diagnosis_id, incident["incident_id"], d.status,
        json.dumps(d.likely_factors), json.dumps(d.cited_evidence), d.confidence, d.reason,
        json.dumps(bundle, sort_keys=True), json.dumps(raws) if raws else None, PROMPT_VERSION, model, now,
        json.dumps(d.first_attempt) if d.first_attempt else None, d.correction_attempts,
    )
    return Diagnosis(d.status, diagnosis_id, d.likely_factors, d.cited_evidence, d.confidence, d.reason,
                     d.first_attempt, d.correction_attempts)


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc'])) or '<root>'}: {x['msg']}" for x in e.errors())[:300]
    return f"{type(e).__name__}: {e}"[:300]
