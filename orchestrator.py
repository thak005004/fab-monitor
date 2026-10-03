"""Orchestrator (spec §13). The bus calls on_event for every accepted event."""

from __future__ import annotations

import json
import sqlite3

import incidents
import lots_at_risk
import notifications
from agents import monitor, triage
from agents.diagnosis import agent as diagnosis_agent
from agents.diagnosis.client import FailingClient, LLMClient
from config_loader import ACTIONABLE_SEVERITIES, Config
from db import repo
from events.types import Event, EventType
from sim.clock import Clock
from state.world_state import apply_event


class Orchestrator:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, config: Config, llm: LLMClient | None = None) -> None:
        self.conn = conn
        self.clock = clock
        self.config = config
        # No client configured -> every diagnosis is recorded as unavailable and
        # alerts still go out. Swapping in FailingClient is the "Kill LLM" toggle.
        self.llm: LLMClient = llm or FailingClient("no LLM client configured")

    def on_event(self, event: Event) -> None:
        apply_event(self.conn, event, self.clock, self.config)  # §5

        match event.event_type:
            case EventType.READING:
                sensor_id = event.payload["sensor_id"]
                sensor = repo.get_sensor(self.conn, sensor_id)
                state = repo.get_sensor_state(self.conn, sensor_id)
                recent = repo.recent_readings(
                    self.conn, sensor_id, state["baseline_activated_at"],
                    max(self.config.run_length, self.config.trend_length),
                )
                fired = monitor.fired_rules(sensor, recent, state, self.config)  # §7
                for t in monitor.log_only(fired, self.config):
                    self.log_firing(t)  # recorded for the dashboard, never an incident
                result = monitor.most_severe(fired, self.config)
                if result:
                    self.handle_trigger(result)

            case EventType.TICK:
                for trigger in monitor.sweep(self.conn, self.clock, self.config):  # dropout + windows + capability + expiry
                    self.handle_trigger(trigger)
                lots_at_risk.refresh_active(self.conn, self.clock)  # §9: lots starting mid-incident
                notifications.escalate_overdue(self.conn, self.clock, self.config)  # §12

            case EventType.RECIPE_CHANGE:
                pass  # apply_event already started relearning; no agents involved

            case EventType.PERSON_AVAILABILITY:
                if not event.payload["available"]:
                    notifications.reassign_from(self.conn, self.clock, self.config, event.payload["person_id"])

            case EventType.ALERT_ACTION:
                p = event.payload
                incidents.transition(self.conn, self.clock, p["incident_id"], p["action"], p["person_id"],
                                     p.get("reason"))  # §8, validated; confirm_hold holds the at-risk lots

            case EventType.MAINTENANCE:
                pass  # recorded in state; used later as evidence

    def handle_trigger(self, trigger: monitor.Trigger) -> None:
        incident, change = incidents.open_or_update(self.conn, trigger, self.clock, self.config)  # §8
        if change is incidents.Change.NONE:
            # Still triggering while acknowledged: one "persistent" notice (§12).
            if incident and notifications.persistence_due(self.conn, incident, self.clock, self.config):
                self._diagnose_triage_notify(incident, "persistent")
            return  # otherwise already handled, no LLM call
        if incident["severity"] not in ACTIONABLE_SEVERITIES:
            return  # low = watch list: on the dashboard, no lots, diagnosis or notification
        # NEW, UPGRADED (incl. low -> medium/high) or REACTIVATED at medium/high.
        self._diagnose_triage_notify(incident, change.value)

    def _diagnose_triage_notify(self, incident: dict, reason: str) -> None:
        lots: list[str] = []
        diagnosis = diagnosis_agent.Diagnosis.unavailable("not applicable")
        if incident["rule_fired"] != "dropout":
            lots = lots_at_risk.mark(self.conn, incident, self.clock)  # §9
            incident = dict(repo.get_incident(self.conn, incident["incident_id"]))
            diagnosis = diagnosis_agent.run(self.conn, incident, self.clock, self.config, self.llm)  # §10, never raises

        decision = triage.triage(  # §11
            incident, diagnosis, lots,
            candidates=repo.qualified_available_people(self.conn, incident["tool_id"]),
            already_notified=repo.notified_people(self.conn, incident["incident_id"]),
            config=self.config, reason=reason,
        )
        incidents.apply_decision(self.conn, incident, decision, diagnosis)
        notifications.send(self.conn, self.clock, incident, decision, reason=reason)  # §12

    def log_firing(self, trigger: monitor.Trigger) -> None:
        firing_id = f"LF-{repo.count_rows(self.conn, 'logged_firings') + 1:06d}"
        repo.insert_logged_firing(
            self.conn, firing_id, trigger.sensor_id, trigger.tool_id, trigger.rule_fired,
            trigger.onset_ts, json.dumps(list(trigger.triggering_reading_ids)), self.clock.now_iso(),
        )
