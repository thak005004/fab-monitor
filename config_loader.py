"""Loads and validates config.json (spec §14). Hot-reload is not built yet."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")

RULES = ("beyond_spec", "beyond_3sigma", "sustained_run", "trending", "dropout", "capability_degraded")
Severity = Literal["low", "medium", "high"]
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
# Low-severity incidents are a watch list: shown on the dashboard, never
# diagnosed or notified. Medium and high reach a person (spec §8).
ACTIONABLE_SEVERITIES = frozenset({"medium", "high"})


class Config(BaseModel):
    # Unknown keys are rejected: a typo'd threshold name must fail loudly, not
    # silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    sigma_threshold: float = Field(gt=0)
    run_length: int = Field(ge=2)
    trend_length: int = Field(ge=2)
    # trending is still evaluated and recorded, but never opens or upgrades an incident.
    trending_log_only: bool
    baseline_window_ticks: int = Field(ge=1)
    min_baseline_points: int = Field(ge=2)  # a stddev needs at least 2 points
    stddev_floor: float = Field(gt=0)
    cpk_threshold: float = Field(gt=0)
    dropout_threshold_ticks: int = Field(ge=1)
    escalation_ticks: int = Field(ge=1)
    # An acknowledged medium/high incident still triggering this long after its
    # latest notification is re-diagnosed and its owner notified ("persistent"), once.
    persistence_ticks: int = Field(ge=1)
    dismissal_cooldown_ticks: int = Field(ge=0)
    stale_after_ticks: int = Field(ge=1)
    # A trigger after at least this many quiet ticks reactivates an open incident.
    # Only has an effect if smaller than stale_after_ticks (otherwise it expires first).
    reactivation_quiet_ticks: int = Field(ge=1)
    diagnosis_lookback_ticks: int = Field(ge=1)
    max_evidence_readings: int = Field(ge=1)
    onset_grace_ticks: int = Field(ge=0)
    diagnosis_timeout_seconds: float = Field(gt=0)
    diagnosis_model: str = Field(min_length=1)
    # LLM calls allowed per tick; beyond it a diagnosis is recorded as unavailable
    # ("rate limited") and the alert still goes out.
    max_diagnoses_per_tick: int = Field(ge=0)
    severity_map: dict[str, Severity]
    oncall_person_id: str = Field(min_length=1)

    @field_validator("severity_map")
    @classmethod
    def _every_rule_has_a_severity(cls, v: dict[str, str]) -> dict[str, str]:
        missing = set(RULES) - set(v)
        unknown = set(v) - set(RULES)
        if missing or unknown:
            raise ValueError(f"severity_map missing {sorted(missing)}, unknown {sorted(unknown)}")
        return v

    @model_validator(mode="after")
    def _window_can_hold_enough_points(self) -> Config:
        # One reading per sensor per tick: a window shorter than the minimum
        # point count could never close on its first attempt.
        if self.min_baseline_points > self.baseline_window_ticks:
            raise ValueError("min_baseline_points must be <= baseline_window_ticks")
        return self

    def severity_of(self, rule: str) -> str:
        return self.severity_map[rule]

    def is_log_only(self, rule: str) -> bool:
        return rule == "trending" and self.trending_log_only


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Raises pydantic.ValidationError (or json.JSONDecodeError) on a bad config."""
    return Config.model_validate(json.loads(Path(path).read_text()))
