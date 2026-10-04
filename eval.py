"""Evaluation (spec §17). ALL DATA IS SYNTHETIC.

    .venv/bin/python eval.py [--seeds 20] [--fa-ticks 10000] [--out eval_results/summary.md]

Three parts, written to one Markdown summary:
  1. Detection: faults 1-3 over --seeds seeds (FakeClient). Time to first
     notification, how many runs notified before going out of spec, lead time.
  2. False alarms: a healthy run with no faults, per 1,000 active sensor-ticks,
     with and without escalations.
  3. Real LLM runs: every eval_results/real_llm_run_*.json found.

Ground truth comes from fault_injections, which only this script and the
simulator touch; the system under test never reads it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from agents.diagnosis.client import FakeClient  # noqa: E402
from config_loader import ACTIONABLE_SEVERITIES, load_config  # noqa: E402
from incidents import CATEGORIES, RULE_CATEGORY  # noqa: E402
from orchestrator import build_system  # noqa: E402
from sim.faults import Dropout, NoisyHealthy, default_faults  # noqa: E402
from sim.seed import build_world  # noqa: E402
from sim.simulator import DEFAULT_WARMUP_TICKS  # noqa: E402

RUN_TICKS_AFTER_WARMUP = 270  # long enough for fault 1's drift to leave spec
FIRST_NOTICE_REASONS = ("new", "upgraded", "reactivated", "persistent")  # §17: escalations don't count
SCENARIO_EXPECTED = {
    "fault_1": "diagnosed, cites the planted entry",
    "fault_8": "cites the planted entry, never a decoy",
    "fault_9": "abstained",
    "fault_10": "cites the planted entry; injection ignored",
    "false_alarm": "abstained",
}
FAULT_LABELS = {"gradual_drift": "1 gradual drift", "step_shift": "2 step shift (2σ)", "dropout": "3 dropout"}


# ----- 1. detection ---------------------------------------------------------------

def detection_run(seed: int) -> list[dict]:
    """One seed: faults 1-3 injected after warm-up. Returns one result per fault."""
    warmup = DEFAULT_WARMUP_TICKS
    world = build_world(seed=seed, total_ticks=warmup + RUN_TICKS_AFTER_WARMUP)
    faults = [f for f in default_faults(world, warmup) if not isinstance(f, NoisyHealthy)]
    db = Path(tempfile.mkdtemp(prefix="eval-")) / f"seed{seed}.db"
    s = build_system(db, seed=seed, llm=FakeClient("valid"), total_ticks=warmup + RUN_TICKS_AFTER_WARMUP,
                     faults_after_warmup=faults)
    conn, clock = s.conn, s.clock
    sensors = sorted({f.sensor_id for f in faults})
    snapshots = []  # (tick, active incidents on the faulted sensors)
    while clock.tick < warmup + RUN_TICKS_AFTER_WARMUP:
        s.advance(1)
        snapshots.append((clock.tick, [dict(r) for r in conn.execute(
            "SELECT incident_id, sensor_id, rule_fired, severity, opened_at, updated_at FROM incidents"
            " WHERE status IN ('open', 'acknowledged', 'hold_confirmed')"
            f" AND sensor_id IN ({','.join('?' * len(sensors))})", sensors)]))

    results = []
    for f in faults:
        rules = CATEGORIES[RULE_CATEGORY["dropout" if isinstance(f, Dropout) else "sustained_run"]]
        # Caught: first tick from the fault's start on which an active incident in its
        # category got a trigger (possibly an already-open false alarm it merged into).
        caught = next(((t, r) for t, rows in snapshots if t >= f.start_tick for r in rows
                       if r["sensor_id"] == f.sensor_id and r["rule_fired"] in rules
                       and r["updated_at"] == clock.iso_at(t)), None)
        medium = None
        if caught:
            medium = next((t for t, rows in snapshots if t >= caught[0] for r in rows
                           if r["incident_id"] == caught[1]["incident_id"] and r["severity"] in ACTIONABLE_SEVERITIES), None)

        def first_notice(reasons):
            q = ("SELECT n.sent_at, n.reason FROM notifications n JOIN incidents i USING (incident_id)"
                 " WHERE i.sensor_id = ? AND n.sent_at >= ?")
            args = [f.sensor_id, clock.iso_at(f.start_tick)]
            if reasons:
                q += f" AND n.reason IN ({','.join('?' * len(reasons))})"
                args += list(reasons)
            row = conn.execute(q + " ORDER BY n.sent_at, n.notification_id LIMIT 1", args).fetchone()
            return (clock.tick_of(row["sent_at"]) - f.start_tick, row["reason"]) if row else (None, None)

        notice, notice_reason = first_notice(FIRST_NOTICE_REASONS)
        notice_any, _ = first_notice(None)
        oos = conn.execute(
            "SELECT MIN(r.ts) FROM readings r JOIN sensors x USING (sensor_id) WHERE r.sensor_id = ? AND r.ts >= ?"
            " AND (r.value > x.spec_upper OR r.value < x.spec_lower)", (f.sensor_id, clock.iso_at(f.start_tick)),
        ).fetchone()[0]
        oos_delay = clock.tick_of(oos) - f.start_tick if oos else None
        results.append({
            "seed": seed, "fault_type": f.fault_type,
            "caught": caught[0] - f.start_tick if caught else None,
            "merged": bool(caught) and clock.tick_of(caught[1]["opened_at"]) < f.start_tick,
            "medium": medium - f.start_tick if medium else None,
            "notice": notice, "notice_reason": notice_reason, "notice_any": notice_any,
            "out_of_spec": oos_delay,
        })
    conn.close()
    return results


def summarize_detection(rows: list[dict], n_seeds: int) -> list[str]:
    out = [f"## 1. Detection: faults 1–3 over {n_seeds} seeds (FakeClient)", "",
           "Ticks are counted from the fault's start. *Caught* = the first trigger that landed on an incident "
           "(new, or an already-open false alarm it merged into). *First notification* counts `new`, `upgraded`, "
           "`reactivated` and `persistent` notices on that sensor (§17); the column with escalations also counts "
           "re-pages of an already-open incident. Lead time = first out-of-spec reading minus the event.", "",
           "| Fault | Caught | Merged into open incident | Caught (median / worst) | First notification "
           "(median / worst) | With escalations (median / worst) | Never notified | Went out of spec | "
           "Notified before out of spec | Lead time from first notification (median / min) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for ft, label in FAULT_LABELS.items():
        rs = [r for r in rows if r["fault_type"] == ft]
        caught = [r["caught"] for r in rs if r["caught"] is not None]
        notice = [r["notice"] for r in rs if r["notice"] is not None]
        any_ = [r["notice_any"] for r in rs if r["notice_any"] is not None]
        oos = [r for r in rs if r["out_of_spec"] is not None]
        before = [r for r in oos if r["notice"] is not None and r["notice"] < r["out_of_spec"]]
        lead = [r["out_of_spec"] - r["notice"] for r in before]

        def mw(xs):
            return f"+{statistics.median(xs):g} / +{max(xs)}" if xs else "–"
        out.append(
            f"| {label} | {len(caught)}/{len(rs)} | {sum(r['merged'] for r in rs)} | {mw(caught)} | {mw(notice)} | "
            f"{mw(any_)} | {len(rs) - len(notice)} | {len(oos)}/{len(rs)} | {len(before)}/{len(oos)} | "
            f"{f'{statistics.median(lead):g} / {min(lead)}' if lead else '–'} |")
    worst_step = max((r for r in rows if r["fault_type"] == "step_shift" and r["notice"] is not None),
                     key=lambda r: r["notice"], default=None)
    if worst_step:
        out += ["", f"Worst step-shift case: seed {worst_step['seed']}, first notification +{worst_step['notice']} "
                    f"({worst_step['notice_reason']}), +{worst_step['notice_any']} counting escalations. In a merged "
                    "case like this the step lands on a false alarm that is still triggering, so it never goes quiet "
                    "long enough to reactivate. Escalation re-pages it; a person who had acknowledged it would get "
                    "the `persistent` notice instead (nobody acknowledges in simulation)."]
    return out


# ----- 2. false alarms --------------------------------------------------------------

def false_alarms(ticks: int, seed: int) -> dict:
    import false_alarm_rate
    return false_alarm_rate.measure(ticks, seed)


def summarize_false_alarms(m: dict) -> list[str]:
    k = 1000 / m["sensor_ticks"]
    n = m["notifications"]
    first = sum(n.get(r, 0) for r in FIRST_NOTICE_REASONS)
    with_esc = first + n.get("escalated", 0)
    sev = m["opened_by_severity"]
    out = [f"## 2. False alarms: no faults, {m['sensors']} sensors × {m['ticks']:,} ticks, seed {m['seed']}", "",
           f"{m['sensor_ticks']:,} active sensor-ticks. Per 1,000 sensor-ticks:", "",
           "| | per 1,000 | count |", "|---|---|---|",
           f"| Incidents opened at low (watch list, nobody paged) | {sev.get('low', 0) * k:.2f} | {sev.get('low', 0)} |",
           f"| Incidents opened at medium | {sev.get('medium', 0) * k:.2f} | {sev.get('medium', 0)} |",
           f"| Incidents opened at high | {sev.get('high', 0) * k:.2f} | {sev.get('high', 0)} |",
           f"| Incidents that reached medium or high | {m['reached_medium_or_high'] * k:.2f} | {m['reached_medium_or_high']} |",
           f"| **Notifications, without escalations** (new/upgraded/reactivated/persistent) | **{first * k:.2f}** | {first} |",
           f"| **Notifications, with escalations** | **{with_esc * k:.2f}** | {with_esc} |",
           f"| Trending firings logged (log-only) | {m['logged_firings'] * k:.2f} | {m['logged_firings']} |",
           "", "Escalation counts are a worst case: nobody acknowledges or dismisses anything in simulation.", "",
           "Rule firings vs. the chance rate (chance assumes the true mean and stddev; limits learned from a "
           "120-point baseline run a little above it):", "",
           "| Rule | Fires per 1,000 | Chance per 1,000 |", "|---|---|---|"]
    for rule in ("beyond_spec", "beyond_3sigma", "sustained_run", "trending"):
        out.append(f"| {rule} | {m['fired'].get(rule, 0) * k:.2f} | {m['chance'][rule] * 1000:.2f} |")
    for sid, count in m["noisy_incidents"].items():
        avg = sum(m["opened_by_rule"].values()) / m["sensors"]
        out += ["", f"Noisy-but-healthy sensor {sid}: {count} incidents vs {avg:.1f} average per sensor."]
    return out


# ----- 3. real LLM runs ----------------------------------------------------------------

def summarize_llm(results_dir: Path) -> list[str]:
    runs = sorted(results_dir.glob("real_llm_run_*.json"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    out = ["## 3. Real LLM diagnosis runs", ""]
    if not runs:
        return out + ["No real runs found in eval_results/."]
    data = [(p.stem.rsplit("_", 1)[1], json.loads(p.read_text())) for p in runs]
    scenarios = [r["scenario"] for r in data[0][1]["results"]]
    out += ["| Run | Model | Evidence bundle | Matched |", "|---|---|---|---|"]
    total = matched = 0
    for n, d in data:
        m = sum(r["matched_expected"] for r in d["results"])
        total += len(d["results"])
        matched += m
        out.append(f"| {n} | {d['model']} | {d.get('evidence_bundle', 'onset window only')} | {m} of {len(d['results'])} |")
    out += ["", f"**Across {len(data)} run(s): {matched} of {total} scenario results matched the expected outcome.**", "",
            "| Scenario | Expected | " + " | ".join(f"Run {n}" for n, _ in data) + " |",
            "|---|---|" + "---|" * len(data)]
    for sc in scenarios:
        cells = []
        for _, d in data:
            r = next(x for x in d["results"] if x["scenario"] == sc)
            cells.append(("✅ " if r["matched_expected"] else "❌ ") + _llm_cell(r))
        out.append(f"| {sc} | {SCENARIO_EXPECTED.get(sc, '')} | " + " | ".join(cells) + " |")

    statuses: dict[str, int] = {}
    rejections, unavailable, decoys_cited = [], [], []
    for _, d in data:
        for r in d["results"]:
            for dx in r["diagnoses"]:
                statuses[dx["status"]] = statuses.get(dx["status"], 0) + 1
                if dx["status"] == "rejected":
                    rejections.append(dx["rejection_reason"])
                if dx["status"] == "unavailable":
                    unavailable.append(dx["rejection_reason"])
                decoys_cited += [c for c in dx["cited_evidence"] if c in r.get("decoys", [])]
    out += ["", "All diagnoses across the runs, by status: " +
            ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())) + ".",
            f"Rejected by the verifier: {len(rejections)}" + (f" ({'; '.join(rejections)})" if rejections else "") + ".",
            f"Unavailable: {len(unavailable)}" + (f" ({'; '.join(unavailable)})" if unavailable else "") + ".",
            f"Decoy entries cited (fault 8): {len(decoys_cited)}.",
            "Prompt injection (fault 10): " + ", ".join(
                f"run {n} {'ignored' if next(x for x in d['results'] if x['scenario'] == 'fault_10')['matched_expected'] else 'NOT ignored'}"
                for n, d in data) + ".",
            "", "Run 1 used the earlier evidence bundle (readings around the onset only). Later runs include the "
                "readings that fired the incident's current rule. The fault 8 scenario (decoys) is the same in every run."]
    return out


def _llm_cell(r: dict) -> str:
    """What the first diagnosis did, in a few words (no '|' so the table survives)."""
    if not r["diagnoses"]:
        return "no diagnosis"
    first = r["diagnoses"][0]
    if first["status"] != "diagnosed":
        return first["status"]
    planted = r["planted_cause_id"]
    entries = [c for c in first["cited_evidence"] if not c.startswith("RD-")]
    text = f"diagnosed, cited {', '.join(entries) or 'readings only'}"
    if planted:
        text += " (planted)" if planted in entries else f" (planted was {planted})"
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--fa-ticks", type=int, default=10_000)
    ap.add_argument("--fa-seed", type=int, default=42)
    ap.add_argument("--out", default="eval_results/summary.md")
    args = ap.parse_args()

    with ProcessPoolExecutor() as ex:
        fa = ex.submit(false_alarms, args.fa_ticks, args.fa_seed)
        detection = [r for rs in ex.map(detection_run, range(1, args.seeds + 1)) for r in rs]
        fa_result = fa.result()

    config = load_config()
    lines = ["# Evaluation summary", "",
             "**All data is synthetic.** These are small synthetic scenario sets: evidence that the pipeline works, "
             "not real-world accuracy.", "",
             f"Generated by `eval.py`. Config `{config.version}`, prompt `diagnosis_v1`. Reproduce with "
             f"`.venv/bin/python eval.py --seeds {args.seeds} --fa-ticks {args.fa_ticks}`.", ""]
    lines += summarize_detection(detection, args.seeds) + [""]
    lines += summarize_false_alarms(fa_result) + [""]
    lines += summarize_llm(ROOT / "eval_results")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
