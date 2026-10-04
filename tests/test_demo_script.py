"""The demo in docs/DEMO_SCRIPT.md, replayed headless with the fake client: every
moment the script names must happen at the tick the script says."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from demo_walkthrough import run_walkthrough  # noqa: E402


@pytest.fixture(scope="module")
def facts():
    return run_walkthrough()


def notes(incident):
    return [(n["tick"], n["person"], n["reason"]) for n in incident["notifications"]]


def dxs(incident):
    return [(d["tick"], d["id"], d["status"], d["cited"]) for d in incident["diagnoses"]]


def test_step1_load(facts):
    f = facts["load"]
    assert (f["tick"], f["config"], f["baselines"], f["dead_letters"]) == (150, "2026-10-05-a", ["active"], 0)
    assert (f["planted_id"], f["planted_tick"], f["drift_start_tick"]) == ("M-0001", 179, 180)


def test_step2_drift_caught_at_198(facts):
    f = facts["advance_1"]
    inc = f["drift_incident"]
    assert f["tick"] == 200
    assert (inc["incident_id"], inc["rule"], inc["severity"], inc["onset_tick"], inc["opened_tick"], inc["owner"]) == \
           ("INC-0008", "sustained_run", "medium", 190, 198, "P-01")
    assert inc["lots_at_risk"] == ["LOT-0030"] and not inc["recommend_hold"]
    assert notes(inc) == [(198, "P-01", "new")]
    assert dxs(inc) == [(198, "DX-00004", "diagnosed", ["M-0001"])]
    assert f["planted_text"] == "Replaced chamber heater controller board; thermocouple recalibrated."
    others = [(o["incident_id"], o["sensor"], o["severity"], o["opened_tick"]) for o in f["other_incidents"]]
    assert others == [
        ("INC-0002", "S-02-TEMP", "low", 152), ("INC-0003", "S-03-TEMP", "medium", 167),
        ("INC-0004", "S-02-RF", "medium", 172), ("INC-0005", "S-02-TEMP", "medium", 179),
        ("INC-0006", "S-04-RF", "low", 180), ("INC-0007", "S-04-PRES", "low", 185),
    ]


def test_steps3_to_5_acknowledge_recipe_malformed(facts):
    assert facts["acknowledge"] == {"status": "acknowledged", "acting_as": "P-01"}
    assert facts["recipe_change"] == {"tick": 200, "tool": "T-05", "recipe_id": "R-05-B",
                                      "statuses": ["relearning"], "change_id": "RC-0001"}
    assert facts["malformed"] == {"dead_letters": 1, "id": "DL-000001",
                                  "reason": "bad payload: value: Input should be a valid number"}


def test_step6_llm_killed_alert_still_goes_out(facts):
    inc = facts["advance_2"]["drift_incident"]
    assert facts["advance_2"]["tick"] == 250
    assert notes(inc) == [(198, "P-01", "new"), (213, "P-01", "persistent")]
    assert dxs(inc)[-1] == (213, "DX-00005", "unavailable", [])
    assert inc["diagnoses"][-1]["reason"] == "LLM unavailable: LLM disabled (kill switch)"
    assert inc["lots_at_risk"] == ["LOT-0030", "LOT-0037"]


def test_step7_out_of_spec_at_254(facts):
    f = facts["out_of_spec"]
    inc = f["drift_incident"]
    assert f["tick"] == 254
    assert (inc["incident_id"], inc["rule"], inc["severity"], inc["recommend_hold"]) == ("INC-0008", "beyond_spec", "high", True)
    assert notes(inc)[-1] == (254, "P-01", "upgraded")
    assert dxs(inc)[-1] == (254, "DX-00006", "unavailable", [])
    assert inc["lots_at_risk"] == ["LOT-0030", "LOT-0037"]


def test_step8_hold_confirmed(facts):
    assert facts["confirm_hold"] == {"status": "hold_confirmed", "lots_held": 2, "held": ["LOT-0030", "LOT-0037"]}


def test_step9_relearn_band_and_t05(facts):
    f = facts["relearn_done"]
    assert f["tick"] == 404 and f["statuses"] == ["active"]
    assert f["band"] == [[200, 320, False]]
    assert [(i["incident_id"], i["sensor"], i["severity"], i["opened_tick"]) for i in f["t05_incidents_since_change"]] == [
        ("INC-0012", "S-05-PRES", "low", 337), ("INC-0014", "S-05-RF", "low", 339), ("INC-0015", "S-05-RF", "low", 367),
    ]
    assert f["drift_incident"]["lots_at_risk"] == ["LOT-0030", "LOT-0037", "LOT-0043", "LOT-0049", "LOT-0052", "LOT-0058"]
