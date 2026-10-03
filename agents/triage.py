"""Triage agent (spec §11). A pure function, no LLM, no database.

Severity comes from the monitor rule only. The diagnosis never changes it:
the AI informs, it doesn't decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.diagnosis.agent import Diagnosis
from config_loader import Config


@dataclass(frozen=True)
class TriageDecision:
    severity: str
    owner_id: str
    on_call: bool  # True when nobody qualified was available and on-call got it
    recommend_hold: bool
    message: str


def pick_owner(candidates: list[str], already_notified: set[str], config: Config) -> tuple[str, bool]:
    """First qualified, available, not-yet-notified person by person_id; else on-call.
    `candidates` must already be qualified for the tool and available."""
    for person in sorted(candidates):
        if person not in already_notified:
            return person, False
    return config.oncall_person_id, True


def triage(
    incident: dict,
    diagnosis: Diagnosis,
    lots_at_risk: list[str],
    candidates: list[str],
    already_notified: set[str],
    config: Config,
    reason: str,
) -> TriageDecision:
    """candidates: people qualified for the tool and available, from the caller.

    Owner: an incident that already has an available owner keeps them, so an
    upgrade, reactivation or persistence notice reaches the person already
    handling it. Otherwise the §11 rule picks one, falling back to on-call.
    """
    severity = config.severity_of(incident["rule_fired"])  # from the rule, never from the diagnosis
    current = incident.get("owner_id")
    if current and (current in candidates or current == config.oncall_person_id):
        owner, on_call = current, current == config.oncall_person_id
    else:
        owner, on_call = pick_owner(candidates, already_notified, config)
    hold = severity == "high" and bool(lots_at_risk)
    return TriageDecision(severity, owner, on_call, hold,
                          render_message(incident, diagnosis, lots_at_risk, severity, hold, reason, on_call))


def render_message(incident: dict, diagnosis: Diagnosis, lots: list[str], severity: str, hold: bool,
                   reason: str, on_call: bool) -> str:
    """Templated, never LLM-written, so notification content is predictable."""
    lines = [
        f"{'[ON-CALL] ' if on_call else ''}[{severity.upper()}] {reason}: {incident['sensor_id']} on "
        f"{incident['tool_id']} ({incident['incident_id']})",
        f"Rule: {incident['rule_fired']}. Estimated onset: {incident['onset_ts']}.",
        f"Lots at risk: {', '.join(lots) if lots else 'none'}.",
        f"Hold recommended: {'YES - confirm on the dashboard' if hold else 'no'}.",
    ]
    if diagnosis.trusted:
        cites = ", ".join(diagnosis.cited_evidence)
        factors = "; ".join(diagnosis.likely_factors)
        lines.append(f"Likely contributing factors (citations verified: {cites}): {factors}. "
                     f"Confidence: {diagnosis.confidence or 'not given'}.")
    elif diagnosis.status == "abstained":
        lines.append("Diagnosis: the evidence did not support any likely contributing factor.")
    elif diagnosis.status == "rejected":
        lines.append(f"Diagnosis: not shown, it failed verification ({diagnosis.reason}).")
    else:
        lines.append(f"Diagnosis: unavailable ({diagnosis.reason}).")
    lines.append("Synthetic data.")
    return "\n".join(lines)
