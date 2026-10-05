# Fab Tool Health Monitor

**All data in this project is synthetic.** Every tool, sensor, person, lot and reading is simulated. Nothing here comes from a real fab.

Sensor readings stream in from simulated semiconductor fab tools. A monitor agent applies standard SPC (statistical process control) rules to catch a sensor drifting out of control. An LLM diagnosis agent proposes likely contributing factors from the maintenance and recipe history, but every piece of evidence it cites is checked in code, and it is allowed to say "not enough evidence". A deterministic triage agent sets severity, works out which lots are at risk, recommends holds, and notifies a qualified person, escalating if nobody responds. Every decision is logged with the evidence and the config version behind it.

The full design is in [`docs/SPEC.md`](docs/SPEC.md). A step-by-step demo is in [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md).

## Design principles

1. **Everything is an event.** Readings, maintenance, recipe changes, people becoming unavailable, human actions, and clock ticks all flow through one bus.
2. **The AI proposes, code verifies, people decide.** The LLM never sets severity, never holds a lot, and never notifies anyone on its own.
3. **Fail safe, not silent.** Bad data is quarantined, a bad config is rejected, and an LLM outage still produces an alert.
4. **Everything is reproducible.** Fixed random seed, simulated clock, versioned config and prompts.

## Setup

Python 3.11+. From the project root:

```
python3.12 -m venv .venv
.venv/bin/pip install "anthropic>=1.11" "pydantic>=2" "pytest>=8" "streamlit>=1.40"
```

(The same dependencies are listed in `pyproject.toml`. Commands below run from the project root.)

## Run the tests

```
.venv/bin/pytest -q
```

The tests never call the real API (they use a scripted fake client). The whole suite takes about two minutes.

## Run the dashboard

```
.venv/bin/streamlit run dashboard/app.py
```

It opens on a demo scenario: a drift on T-01's temperature right after a planted maintenance entry, a noisy-but-healthy sensor, and two fault-free tools for a live recipe change. From the sidebar you can advance time, inject faults, change a recipe, mark people unavailable, send a malformed event, and kill the LLM. The page shows the tools, sensor charts with control/spec limits and relearning bands, notified incidents and the low-severity watch list, incident detail with each verified citation, per-person inboxes, and the dead-letter queue.

Diagnoses use Claude when `ANTHROPIC_API_KEY` is set, and the scripted fake client otherwise. Set `FAB_MONITOR_LLM=fake` to force the fake. The key is read only from the environment and never stored or printed.

## Hosted demo

The dashboard can run on Streamlit Community Cloud: point the app at `dashboard/app.py`, choose Python 3.11 or later, and it installs from `requirements.txt`. Set `FAB_MONITOR_HOSTED` (any non-empty value except `0` or `false`), either as an environment variable or as a secret in the app's settings. In hosted mode:

- Diagnoses always come from the scripted fake client. No API key is ever read, so there is nothing to leak and no API cost.
- The banner reads "Simulated data, scripted model responses. The live model runs in the local version."
- Every browser session gets its own SQLite database in a temporary folder, created when the session starts. Visitors never share state, and "Load demo scenario" replaces only that visitor's database.

Without `FAB_MONITOR_HOSTED`, the dashboard behaves as described above (Claude if `ANTHROPIC_API_KEY` is set).

## Run the evaluation

```
.venv/bin/python eval.py
```

This writes [`eval_results/summary.md`](eval_results/summary.md) in about 1–2 minutes. It covers three things:
- Detection over 20 seeds: time to first notification, notified before going out of spec, lead time.
- False alarms per 1,000 sensor-ticks, with and without escalations.
- The real LLM runs saved in `eval_results/`.

To add a real LLM run (it costs a few API calls per scenario):

```
read -s ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
.venv/bin/python scripts/real_llm_run.py      # saves the next eval_results/real_llm_run_<n>.json
```

Other scripts: `scripts/run_sim.py` (one scenario, printed), `scripts/false_alarm_rate.py`, `scripts/rolling_vs_frozen.py`, and `scripts/demo_walkthrough.py` (the demo, headless).

## Layout

| Path | What |
|---|---|
| `sim/` | Simulated clock, seeded world, fault library, simulator |
| `events/` | Event types, adapters (one per source), the bus |
| `state/` | Merge-only world state; baseline lifecycle (learn, freeze, relearn, capability check) |
| `agents/monitor.py` | SPC rules and the per-tick sweep (dropout, baseline windows, stale incidents) |
| `agents/diagnosis/` | LLM clients, evidence bundle, citation verifier, the agent |
| `agents/triage.py` | Severity, owner, hold recommendation, templated message |
| `incidents.py`, `lots_at_risk.py`, `notifications.py` | Incident lifecycle, lots at risk, notifications/escalation/reassignment |
| `orchestrator.py` | Routes every event; wires the running system |
| `dashboard/app.py` | Streamlit dashboard (views and controls only) |
| `dashboard/explain.py`, `.streamlit/config.toml` | Plain-language wording, status colors and one-line incident summaries; the page theme |
| `eval.py` | Evaluation report |
| `config.json`, `prompts/diagnosis_v2.txt` | Versioned config and prompt (v1 kept for earlier runs) |

Notifications go to a per-person inbox on the dashboard. A real deployment would add a webhook (Slack, email or paging) in `notifications.py`, at `_insert()`, where each notification is written.

## Known limitations

- Synthetic data; small evaluation sets. The numbers show the pipeline works, not real-world accuracy.
- A lot runs on one tool, not many process steps.
- Dropouts don't flag lots as unverified.
- Verification proves citations are real and earlier than the problem, not that the reasoning is right.
- Control rules and thresholds are standard SPC defaults, not tuned with real process engineers.
- The verifier can't catch a citation that is on the right tool and before onset but unrelated (fault 8's unrelated decoy). Only the evaluation, comparing against `planted_cause_id`, catches it.
- "Before onset" uses the estimated onset, which can be later than the true fault start.
- The persistence notice never fires in automated simulation because nobody acknowledges there. It is covered by its unit test, and it does fire in the dashboard demo once a person acknowledges.
- The system holds lots but doesn't model taking a tool out of production, so lots that start on a tool after a hold is confirmed are marked at risk but not held automatically.
- The model doesn't accept a temperature setting, so outputs can vary between runs. Stored raw responses and verification are what make results auditable.

Cut from scope: config hot-reload, fault 7 (capability degraded after a recipe change; the capability check itself is unit-tested), the Pareto chart, the explainer agent, and the scale benchmark.
