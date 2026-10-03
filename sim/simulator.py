"""Generates raw records per tick. All data is synthetic.

The simulator does NOT write readings, maintenance, or recipe changes to the
DB. step() advances the clock and returns that tick's raw records, in order:
readings, then maintenance, then one tick record. The event bus ingests them.

Each raw record is an envelope whose `payload` matches the §4 payload for its
event type:
  {"event_type": ..., "source": <adapter name>, "tool_id": ..., "ts": <ISO>, "payload": {...}}

The only DB writes here are fault_injections rows (ground truth for eval).
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterable

from db import repo
from db.connection import transaction
from sim.clock import Clock
from sim.faults import DriftWithDecoys, Fault, GradualDrift, NoisyHealthy
from sim.seed import World

# Must exceed config.baseline_window_ticks (120) so every baseline activates
# during warm-up, with 30 ticks of margin.
DEFAULT_WARMUP_TICKS = 150


class Simulator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        world: World,
        clock: Clock,
        faults: Iterable[Fault] = (),
        warmup_ticks: int = DEFAULT_WARMUP_TICKS,
    ) -> None:
        """warmup_ticks: ticks 1..warmup_ticks are guaranteed healthy, so the
        baselines can be learned from them. Faults that change the signal must
        start after warm-up.
        """
        self.conn = conn
        self.world = world
        self.clock = clock
        self.warmup_ticks = warmup_ticks
        self.faults: list[tuple[str, Fault]] = []  # (fault_id, fault)
        self._scheduled_maintenance: dict[int, list[dict]] = {}
        self._reading_seq = 0
        self._maint_seq = 0
        self._fault_seq = 0
        # One noise stream per sensor, seeded from (seed, sensor_id). Noise is
        # drawn every tick even when a sensor drops out, so injecting a fault
        # on one sensor never changes any other sensor's values.
        self._noise = {s.sensor_id: random.Random(f"{world.seed}:{s.sensor_id}") for s in world.sensors}
        for fault in faults:
            self.inject(fault)

    # ----- fault injection -------------------------------------------------

    def inject(self, fault: Fault) -> str:
        """Schedule a fault and write its fault_injections row. Returns the F- ID."""
        profile = self.world.sensor(fault.sensor_id)
        if isinstance(fault, NoisyHealthy):
            # Only "healthy" if the extra noise is in the learned baseline.
            if fault.start_tick > 1 or self.clock.tick > 0:
                raise ValueError("NoisyHealthy must be present from the first reading")
            if profile.noise_multiplier <= 1:
                raise ValueError(f"{fault.sensor_id} is not noisy in the world; set it in build_world(noisy_sensors=...)")
        else:
            if fault.start_tick <= self.warmup_ticks:
                raise ValueError(f"{fault.fault_type} must start after warm-up (tick > {self.warmup_ticks})")
            if fault.start_tick <= self.clock.tick:
                raise ValueError("cannot inject a fault that starts in the past")

        # Validate every maintenance entry this fault needs before scheduling any,
        # so a bad fault can't leave half of itself behind.
        entries = []
        if isinstance(fault, GradualDrift) and fault.plants_maintenance:
            entries.append((fault.start_tick - 1, profile.tool_id))
        if isinstance(fault, DriftWithDecoys):
            entries += [(fault.start_tick + d.offset_ticks, d.tool_id or profile.tool_id) for d in fault.decoys]
        for tick, tool_id in entries:
            self._check_maintenance_slot(tick, tool_id)

        planted_cause_id = None
        expected_outcome = fault.expected_outcome
        if isinstance(fault, GradualDrift) and fault.plants_maintenance:
            planted_cause_id = self._schedule_maintenance(
                fault.start_tick - 1, profile.tool_id, fault.maintenance_description)
        if isinstance(fault, DriftWithDecoys):
            decoy_ids = [
                self._schedule_maintenance(fault.start_tick + d.offset_ticks, d.tool_id or profile.tool_id, d.description)
                for d in fault.decoys
            ]
            expected_outcome += f";decoys:{','.join(decoy_ids)}"

        self._fault_seq += 1
        fault_id = f"F-{self._fault_seq:04d}"
        with transaction(self.conn):
            repo.insert_fault_injection(
                self.conn,
                fault_id=fault_id,
                fault_type=fault.fault_type,
                tool_id=profile.tool_id,
                sensor_id=fault.sensor_id,
                start_ts=self.clock.iso_at(fault.start_tick),
                planted_cause_id=planted_cause_id,
                expected_outcome=expected_outcome,
            )
        self.faults.append((fault_id, fault))
        return fault_id

    # ----- ground-truth queries (for tests/eval, never the system under test)

    def active_faults(self, sensor_id: str, tick: int) -> list[Fault]:
        return [f for _, f in self.faults if f.sensor_id == sensor_id and f.active_at(tick)]

    def is_healthy(self, sensor_id: str, tick: int) -> bool:
        return not any(f.changes_signal for f in self.active_faults(sensor_id, tick))

    # ----- stepping --------------------------------------------------------

    @property
    def in_warmup(self) -> bool:
        return self.clock.tick < self.warmup_ticks

    def step(self) -> list[dict]:
        """Advance one tick and return its raw records: readings, maintenance, tick."""
        self.clock.advance()
        tick = self.clock.tick
        ts = self.clock.now_iso()
        records: list[dict] = []

        for profile in self.world.sensors:
            noise = self._noise[profile.sensor_id].gauss(0.0, profile.healthy_stddev)
            active = self.active_faults(profile.sensor_id, tick)
            if any(f.drops(tick) for f in active):
                continue
            offset = sum(f.offset(tick, profile) for f in active)
            self._reading_seq += 1
            records.append(
                {
                    "event_type": "reading",
                    "source": "sensor",
                    "tool_id": profile.tool_id,
                    "ts": ts,
                    "payload": {
                        "reading_id": f"RD-{self._reading_seq:06d}",
                        "sensor_id": profile.sensor_id,
                        "value": round(profile.healthy_mean + offset + noise, 4),
                    },
                }
            )

        for maint in self._scheduled_maintenance.pop(tick, []):
            records.append(
                {
                    "event_type": "maintenance",
                    "source": "maintenance",
                    "tool_id": maint["tool_id"],
                    "ts": ts,
                    "payload": dict(maint["payload"]),
                }
            )

        records.append({"event_type": "tick", "source": "clock", "tool_id": None, "ts": ts, "payload": {"tick": tick}})
        return records

    def run(self, n_ticks: int) -> list[dict]:
        records: list[dict] = []
        for _ in range(n_ticks):
            records.extend(self.step())
        return records

    def run_warmup(self) -> list[dict]:
        """Run whatever is left of the warm-up period."""
        return self.run(max(0, self.warmup_ticks - self.clock.tick))

    # ----- helpers ---------------------------------------------------------

    def _schedule_maintenance(self, tick: int, tool_id: str, description: str) -> str:
        """Schedule a maintenance record for a future tick; returns its M- ID."""
        self._check_maintenance_slot(tick, tool_id)
        log_id = self._next_maint_id()
        self._scheduled_maintenance.setdefault(tick, []).append(
            {"tool_id": tool_id, "payload": {"log_id": log_id, "description": description,
                                             "technician": self._technician_for(tool_id)}}
        )
        return log_id

    def _check_maintenance_slot(self, tick: int, tool_id: str) -> None:
        if tick <= self.clock.tick:
            raise ValueError(f"maintenance entry at tick {tick} would be in the past")
        if tool_id not in {t.tool_id for t in self.world.tools}:
            raise ValueError(f"unknown tool {tool_id}")

    def _next_maint_id(self) -> str:
        self._maint_seq += 1
        return f"M-{self._maint_seq:04d}"

    def _technician_for(self, tool_id: str) -> str:
        names = {p.person_id: p.name for p in self.world.people}
        return names[self.world.qualified_people(tool_id)[0]]
