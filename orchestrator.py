"""Orchestrator (spec §13). The bus calls on_event for every accepted event."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import incidents
import lots_at_risk
import notifications
from agents import monitor, triage
from agents.diagnosis import agent as diagnosis_agent
from agents.diagnosis.client import FailingClient, LLMClient
from config_loader import ACTIONABLE_SEVERITIES, Config, load_config
from db import repo
from db.connection import connect, init_schema
from events.adapters import default_adapters
from events.bus import Bus
from events.types import Event, EventType
from sim.clock import Clock
from sim.faults import NoisyHealthy, live_fault
from sim.seed import World, build_world, seed_database
from sim.simulator import DEFAULT_WARMUP_TICKS, Simulator
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


# ===== Wiring: a running system (used by the dashboard) =====================

@dataclass
class System:
    """One running pipeline: simulator -> bus -> orchestrator, on one database.
    Every action goes through the bus as an event, like any other source."""

    conn: sqlite3.Connection
    clock: Clock
    world: World
    sim: Simulator
    bus: Bus
    orchestrator: Orchestrator
    config: Config

    def advance(self, n_ticks: int = 1) -> None:
        for _ in range(n_ticks):
            self.bus.publish_tick(self.sim.step())

    def publish(self, event_type: str, source: str, tool_id: str | None, payload: dict) -> bool:
        """Publish one record at the current time, in its own transaction.
        Returns False if it was quarantined in dead_letter."""
        raw = {"event_type": event_type, "source": source, "tool_id": tool_id,
               "ts": self.clock.now_iso(), "payload": payload}
        return bool(self.bus.publish_tick([raw]))

    def recipe_change(self, tool_id: str, recipe_id: str) -> bool:
        change_id = self.sim.next_recipe_change_id()
        return self.publish("recipe_change", "recipe", tool_id, {"change_id": change_id, "recipe_id": recipe_id})

    def set_availability(self, person_id: str, available: bool) -> bool:
        return self.publish("person_availability", "people", None, {"person_id": person_id, "available": available})

    def alert_action(self, incident_id: str, action: str, person_id: str, reason: str | None = None) -> str:
        """Returns the incident's status afterwards (unchanged if the transition was rejected)."""
        payload = {"incident_id": incident_id, "action": action, "person_id": person_id}
        if reason:
            payload["reason"] = reason
        self.publish("alert_action", "dashboard", None, payload)
        return repo.get_incident(self.conn, incident_id)["status"]

    def send_malformed(self) -> bool:
        """A sensor record whose value isn't a number: it must land in dead_letter."""
        sensor = self.world.sensors[0]
        return self.publish("reading", "sensor", sensor.tool_id,
                            {"reading_id": "RD-999999", "sensor_id": sensor.sensor_id, "value": "not a number"})

    def inject(self, fault_type: str, sensor_id: str) -> str:
        return self.sim.inject(live_fault(fault_type, self.world, sensor_id, self.clock.tick))


def build_system(
    db_path: str | Path,
    seed: int = 42,
    config: Config | None = None,
    llm: LLMClient | None = None,
    warmup_ticks: int = DEFAULT_WARMUP_TICKS,
    total_ticks: int = 3000,
    faults_after_warmup: list | tuple = (),
    check_same_thread: bool = True,
) -> System:
    """Seed a fresh database, run the warm-up through the bus (§6), then inject
    faults. total_ticks is how far ahead the seeded lots reach."""
    config = config or load_config()
    if warmup_ticks <= config.baseline_window_ticks:
        raise ValueError("warm-up must be longer than baseline_window_ticks")
    conn = connect(db_path, check_same_thread=check_same_thread)
    init_schema(conn)
    clock = Clock()
    world = build_world(seed=seed, total_ticks=total_ticks)
    seed_database(conn, world, clock, baseline_window_ticks=config.baseline_window_ticks)
    noisy = [NoisyHealthy(s.sensor_id) for s in world.sensors if s.noise_multiplier > 1]  # fault 4, from tick 0
    sim = Simulator(conn, world, clock, faults=noisy, warmup_ticks=warmup_ticks)
    orchestrator = Orchestrator(conn, clock, config, llm=llm)
    system = System(conn, clock, world, sim, Bus(conn, orchestrator, clock, default_adapters(conn)), orchestrator, config)
    system.advance(warmup_ticks)
    not_active = [r["sensor_id"] for r in repo.all_sensor_states(conn) if r["baseline_status"] != "active"]
    if not_active:
        raise RuntimeError(f"warm-up ended with baselines not active: {not_active}")
    for fault in faults_after_warmup:
        sim.inject(fault)
    return system
