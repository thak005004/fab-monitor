"""Triage agent (§11): a pure function."""

from __future__ import annotations

import pytest

from agents.diagnosis.agent import Diagnosis
from agents.triage import triage
from config_loader import load_config

CONFIG = load_config()
INC = {"incident_id": "INC-0001", "tool_id": "T-01", "sensor_id": "S-01-TEMP", "rule_fired": "sustained_run",
       "severity": "medium", "onset_ts": "2026-01-05T09:00:00+00:00", "owner_id": None}
DIAGNOSES = [
    Diagnosis("diagnosed", "DX-1", ["Heater board replaced"], ["M-0001"], "medium"),
    Diagnosis("abstained", "DX-2"),
    Diagnosis("rejected", "DX-3", ["x"], ["M-9999"], "high", reason="cited M-9999, not in evidence"),
    Diagnosis("unavailable", "DX-4", reason="timeout"),
]


@pytest.mark.parametrize("rule", ["sustained_run", "beyond_spec", "capability_degraded"])
def test_severity_comes_from_the_rule_and_never_from_the_diagnosis(rule):
    inc = {**INC, "rule_fired": rule}
    decisions = [triage(inc, d, ["LOT-0001"], ["P-01", "P-02"], set(), CONFIG, "new") for d in DIAGNOSES]
    assert {d.severity for d in decisions} == {CONFIG.severity_of(rule)}
    assert len({(d.owner_id, d.recommend_hold) for d in decisions}) == 1


def test_owner_is_first_qualified_available_not_yet_notified_by_person_id():
    d = triage(INC, DIAGNOSES[1], [], ["P-03", "P-01", "P-02"], {"P-01"}, CONFIG, "new")
    assert d.owner_id == "P-02" and not d.on_call


def test_no_qualified_available_person_goes_to_on_call():
    d = triage(INC, DIAGNOSES[1], [], [], set(), CONFIG, "new")
    assert d.owner_id == CONFIG.oncall_person_id and d.on_call and d.message.startswith("[ON-CALL]")
    d = triage(INC, DIAGNOSES[1], [], ["P-01"], {"P-01"}, CONFIG, "escalated")  # everyone already notified
    assert d.owner_id == CONFIG.oncall_person_id and d.on_call


def test_existing_available_owner_keeps_the_incident():
    inc = {**INC, "owner_id": "P-02"}
    assert triage(inc, DIAGNOSES[0], [], ["P-01", "P-02"], {"P-02"}, CONFIG, "upgraded").owner_id == "P-02"
    # Owner no longer available -> picked again by the §11 rule.
    assert triage(inc, DIAGNOSES[0], [], ["P-01"], {"P-02"}, CONFIG, "upgraded").owner_id == "P-01"


@pytest.mark.parametrize("rule, lots, hold", [
    ("beyond_spec", ["LOT-0001"], True),
    ("beyond_spec", [], False),
    ("sustained_run", ["LOT-0001"], False),
])
def test_hold_recommended_only_for_high_severity_with_lots_at_risk(rule, lots, hold):
    assert triage({**INC, "rule_fired": rule}, DIAGNOSES[0], lots, ["P-01"], set(), CONFIG, "new").recommend_hold is hold


def test_message_is_templated_and_shows_only_trusted_diagnoses():
    ok = triage(INC, DIAGNOSES[0], ["LOT-0007"], ["P-01"], set(), CONFIG, "new").message
    assert "M-0001" in ok and "Heater board replaced" in ok and "LOT-0007" in ok and "Synthetic data" in ok
    rejected = triage(INC, DIAGNOSES[2], [], ["P-01"], set(), CONFIG, "new").message
    assert "failed verification" in rejected and "M-9999" in rejected and "x." not in rejected
    assert "unavailable (timeout)" in triage(INC, DIAGNOSES[3], [], ["P-01"], set(), CONFIG, "new").message
