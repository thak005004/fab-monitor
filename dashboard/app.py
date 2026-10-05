"""Fab Tool Health Monitor dashboard (spec §16). ALL DATA IS SYNTHETIC.

    .venv/bin/streamlit run dashboard/app.py

Thin by design: every action goes through orchestrator.System (which publishes
events on the bus) and every view reads through db.repo. No monitoring,
incident, diagnosis or notification logic lives here.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `streamlit run dashboard/app.py` finds the packages

from agents.diagnosis.client import AnthropicClient, FailingClient, FakeClient  # noqa: E402
from agents.diagnosis.evidence import bundle_items  # noqa: E402
from agents.diagnosis.verify import check_citation  # noqa: E402
from config_loader import load_config  # noqa: E402
from db import repo  # noqa: E402
from orchestrator import System, build_system  # noqa: E402
from sim.faults import LIVE_FAULT_TYPES, demo_faults  # noqa: E402
from sim.simulator import DEFAULT_WARMUP_TICKS  # noqa: E402
from state.baseline import relearning_windows  # noqa: E402

from dashboard import explain  # noqa: E402
from dashboard.explain import Names  # noqa: E402

# Chart colors from the theme (.streamlit/config.toml): deep blue for readings, a light tint of it
# for the normal range, near-black for the allowed range. Status colors come from explain.LOOK.
CHART = {"reading": "#1d3f8f", "normal": "#5b7fc7", "allowed": "#2b2f38", "relearn": "#d5d9e0"}
ACTIONS = {"acknowledge": "Acknowledge", "confirm_hold": "Confirm hold", "dismiss": "Dismiss", "resolve": "Resolve"}
ACTION_HELP = {
    "acknowledge": "Tell the system you're handling this; it stops escalating to other people.",
    "confirm_hold": "Put the product batches at risk on hold so they aren't shipped until checked.",
    "dismiss": "Close this as not a real problem; a false alarm also pauses similar alerts on this sensor briefly.",
    "resolve": "Close this after the problem has been fixed.",
}

st.set_page_config(page_title="Fab Tool Monitor (simulated)", layout="wide")


# ----- session -----------------------------------------------------------------

HOSTED_BANNER = "**Simulated data, scripted model responses.** The live model runs in the local version."
LOCAL_BANNER = "**Simulated data.** Every tool, sensor, person, lot and reading on this page is synthetic."


def _truthy(value) -> bool:
    return str(value).strip().lower() not in ("", "0", "false", "no", "none")


def hosted_mode() -> bool:
    """FAB_MONITOR_HOSTED set in the environment, or as a Streamlit secret (Community Cloud)."""
    if _truthy(os.environ.get("FAB_MONITOR_HOSTED", "")):
        return True
    try:
        return _truthy(st.secrets.get("FAB_MONITOR_HOSTED", ""))
    except Exception:  # no secrets file at all: not hosted
        return False


def make_llm():
    """Hosted: always the scripted fake, and no API key is ever read.
    Local: Claude if a key is available (or FAB_MONITOR_LLM=anthropic), else the
    scripted fake. FAB_MONITOR_LLM=fake forces the fake (used by the tests)."""
    if hosted_mode():
        # Scripted so the self-correction can be seen: when there is a maintenance record
        # to cite, the first answer cites a made-up one and the checker sends it back.
        return FakeClient("self_correct_demo"), "scripted responses (hosted demo)"
    choice = os.environ.get("FAB_MONITOR_LLM") or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "fake")
    if choice == "anthropic":
        model = load_config().diagnosis_model
        return AnthropicClient(model), f"Claude API ({model})"
    return FakeClient("valid"), "scripted responses (no API key set)"


def load_demo() -> None:
    """A fresh demo in a fresh database. Each browser session has its own
    st.session_state, so its own database folder: visitors never share state.
    Reloading replaces this session's database and removes the old one."""
    old = st.session_state.get("system")
    if old is not None:
        old.conn.close()
        shutil.rmtree(st.session_state.db_dir, ignore_errors=True)
    llm, label = make_llm()
    st.session_state.db_dir = tempfile.mkdtemp(prefix="fab-monitor-")
    db_path = Path(st.session_state.db_dir) / "dashboard.db"
    st.session_state.system = build_system(
        db_path, seed=42, llm=llm, faults_after_warmup=demo_faults(DEFAULT_WARMUP_TICKS), check_same_thread=False,
    )
    st.session_state.llm, st.session_state.llm_label = llm, label
    st.session_state.flash = ("info", "Demo loaded. At minute 180, T-01's temperature (S-01-TEMP) starts drifting, "
                                      "right after a maintenance entry. S-04-TEMP is noisy but healthy. T-03 and T-05 "
                                      "have no problems: try a recipe change there.")


def flash(kind: str, text: str) -> None:
    st.session_state.flash = (kind, text)


if "system" not in st.session_state:
    load_demo()
system: System = st.session_state.system
conn, clock, config = system.conn, system.clock, system.config
names = Names(conn)


# ----- sidebar: controls ---------------------------------------------------------

with st.sidebar:
    st.subheader("Run the simulation")
    if st.button("Load demo scenario", width="stretch",
                 help="Start over with the demo: a slow temperature drift on T-01 that begins at minute 180."):
        load_demo()
        st.rerun()

    st.caption("Move time forward (simulated minutes)")
    cols = st.columns(3)
    for col, n in zip(cols, (1, 10, 50)):
        if col.button(f"+{n}", width="stretch",
                      help=f"Run the factory forward {n} simulated minute{'s' if n > 1 else ''}; every sensor reports once a minute."):
            system.advance(n)
            st.rerun()

    # Keyed with a constant default so the widget keeps its own state across reruns.
    killed = st.toggle("Simulate AI outage", value=False, key="kill_llm",
                       help="Turn the AI off (Kill LLM: the FailingClient) to show that alerts still go out without it.")
    system.orchestrator.llm = FailingClient() if killed else st.session_state.llm
    st.caption(f"AI: {'off (simulated outage)' if killed else st.session_state.llm_label}")

    st.divider()
    st.subheader("Cause a problem")
    st.markdown("**Break a sensor**")
    tools = [t.tool_id for t in system.world.tools]
    f_tool = st.selectbox("Machine", tools, key="f_tool", format_func=names.tool,
                          help="The machine (tool) to cause a problem on.")
    f_sensor = st.selectbox("Sensor", [s.sensor_id for s in system.world.sensors if s.tool_id == f_tool], key="f_sensor",
                            format_func=lambda sid: f"{names.sensor_type(sid)} ({sid})",
                            help="Which of the machine's sensors gets the problem.")
    f_type = st.selectbox("Kind of problem", LIVE_FAULT_TYPES, key="f_type",
                          format_func=lambda f: explain.FAULT_TYPES.get(f, f),
                          help="The kind of problem to simulate (fault type); it starts within a minute or two.")
    if st.button("Start this problem", width="stretch",
                 help="Start this simulated problem (inject a fault) so you can watch the system catch it."):
        try:
            fid = system.inject(f_type, f_sensor)
            flash("success", f"Started {explain.FAULT_TYPES.get(f_type, f_type)} on {names.sensor(f_sensor)} "
                             f"(fault {fid}, {f_type}).")
        except ValueError as e:
            flash("error", f"Could not start the problem: {e}")
        st.rerun()

    st.markdown("**Switch a recipe**")
    r_tool = st.selectbox("Machine", tools, index=tools.index("T-05") if "T-05" in tools else 0, key="r_tool",
                          format_func=names.tool, help="The machine that switches to a new process recipe.")
    r_recipe = st.text_input("New recipe name", value=f"R-{r_tool[2:]}-B", key="r_recipe",
                             help="Name of the new recipe (recipe ID): the process settings the machine runs.")
    if st.button("Change recipe", width="stretch",
                 help="A new recipe changes what 'normal' looks like, so the machine's sensors re-learn it."):
        ok = system.recipe_change(r_tool, r_recipe)
        flash("success" if ok else "error",
              f"{names.tool(r_tool)} switched to recipe {r_recipe}: its sensors are learning the new normal (relearning)."
              if ok else "Recipe change was rejected.")
        st.rerun()

    st.markdown("**Garble a message**")
    if st.button("Send bad data", width="stretch",
                 help="Send a garbled sensor message; it should be rejected and set aside without stopping anything."):
        accepted = system.send_malformed()
        flash("warning" if not accepted else "error",
              "Bad data rejected and set aside (dead letter); monitoring kept going." if not accepted
              else "Unexpected: the bad data was accepted.")
        st.rerun()

    st.divider()
    st.subheader("People")
    people = repo.people(conn)
    p_id = st.selectbox("Person", [p["person_id"] for p in people],
                        format_func=lambda pid: next(f"{p['name']} ({pid}): {'available' if p['available'] else 'unavailable'}"
                                                     for p in people if p["person_id"] == pid), key="p_id",
                        help="Engineers who can be alerted; P-ONCALL is the on-call backup.")
    available = next(p["available"] for p in people if p["person_id"] == p_id)
    if st.button("Mark unavailable" if available else "Mark available", width="stretch",
                 help="Someone calls out: their open, unanswered alerts move to the next qualified person."):
        system.set_availability(p_id, not available)
        flash("success", f"{names.person(p_id)} marked "
                         f"{'unavailable: their open alerts are reassigned' if available else 'available'}.")
        st.rerun()


# ----- header ---------------------------------------------------------------------

st.title("Fab Tool Health Monitor", anchor=False)
st.caption("Watches sensors on chip-making machines, catches problems early, and tells the right person "
           "which product is at risk.")
st.info(HOSTED_BANNER if hosted_mode() else LOCAL_BANNER, icon=":material/science:")
with st.expander("What you're looking at", expanded=True):
    st.markdown(explain.INTRO)
# The flash message always gets its own slot, empty or not. If it came and went
# as a top-level element, everything below it (the tabs) would shift position,
# and the browser would rebuild the tabs and jump back to the first one after
# every action button.
flash_slot = st.container()
if "flash" in st.session_state:
    kind, text = st.session_state.pop("flash")
    getattr(flash_slot, kind)(text)

notified_active = repo.incidents_by_tier(conn, actionable=True, active_only=True)
watch = repo.incidents_by_tier(conn, actionable=False, active_only=True)
m = st.columns(5)
m[0].metric("Minute", clock.tick, border=True,
            help=f"Simulated minute (tick): time on this page is simulated; one minute per step. Clock reads {clock.now_iso()}.")
m[1].metric("Open alerts", len(notified_active), border=True,
            help="Problems at Alert or Urgent level that a person has been told about and that aren't closed yet.")
m[2].metric("Watch list", len(watch), border=True,
            help="Unusual but not alarming yet (low severity): shown here, but no one is paged.")
m[3].metric("Batches held", repo.count_by_status(conn, "lots", "status", "held"), border=True,
            help="Product batches (lots) a person put on hold so they aren't shipped until checked.")
m[4].metric("Bad data", repo.count_rows(conn, "dead_letter"), border=True,
            help="Rejected bad data: garbled messages set aside (dead letter) instead of being trusted; monitoring keeps going.")

# Tool grid
st.subheader("Machines", anchor=False,
             help="Each card is one machine (tool) and its most serious open problem. 'Monitoring' = every sensor's "
                  "normal range is learned. 'Learning new normal' = re-learning after a recipe change (relearning), "
                  "when only the allowed-range check runs.")
grid = st.columns(len(system.world.tools))
states = {r["sensor_id"]: r for r in repo.all_sensor_states(conn)}
for col, t in zip(grid, repo.tool_overview(conn)):
    with col.container(border=True, height=150):
        high, med, low = t["high"] or 0, t["medium"] or 0, t["low"] or 0
        if high or med or low:
            state = "urgent" if high else "alert" if med else "watch"
            label = {"urgent": "urgent", "alert": "alert", "watch": "on watch"}[state]
            status_line = explain.badge(state, f"{high or med or low} {label}")
        else:
            status_line = explain.badge("healthy")
        st.markdown(f"**{t['name']}**")
        st.markdown(status_line)
        sensors = [s for s in system.world.sensors if s.tool_id == t["tool_id"]]
        relearning = [explain.SENSOR_TYPES.get(s.sensor_type, s.sensor_type) for s in sensors
                      if states[s.sensor_id]["baseline_status"] != "active"]
        st.caption(f"{t['tool_id']} · " + ("monitoring" if not relearning else "learning new normal"
                   + ("" if len(relearning) == len(sensors) else f" ({', '.join(relearning)})")))

with st.expander("Technical details"):
    st.metric("Settings version", config.version,
              help="The version of the alerting settings in use (config version); every alert records it.")
    st.markdown(
        f"- Clock: `{clock.now_iso()}` (tick {clock.tick})\n"
        f"- AI client: {st.session_state.llm_label}; configured model `{config.diagnosis_model}` (used with the Claude API), "
        f"timeout {config.diagnosis_timeout_seconds}s, at most {config.max_diagnoses_per_tick} AI calls a minute\n"
        f"- Normal range: mean ± {config.sigma_threshold:g}σ (control limits), learned from "
        f"{config.baseline_window_ticks} minutes (at least {config.min_baseline_points} readings)\n"
        f"- Sustained shift: {config.run_length} readings in a row on one side of the mean (run length)\n"
        f"- Sensor stopped reporting: no reading for {config.dropout_threshold_ticks} minutes (dropout)\n"
        f"- Recipe too close to limits: Cpk below {config.cpk_threshold:g}\n"
        f"- Escalate after {config.escalation_ticks} minutes without a response; remind after "
        f"{config.persistence_ticks} minutes still active; close after {config.stale_after_ticks} quiet minutes\n"
        f"- Severity of each rule: " + ", ".join(f"{k} → {v}" for k, v in config.severity_map.items()))

tab_feed, tab_chart, tab_inc, tab_inbox, tab_dl = st.tabs(
    ["Activity", "Sensor chart", "Incidents", "Inboxes", "Rejected bad data"])


# ----- activity feed ---------------------------------------------------------------

with tab_feed:
    st.caption("What happened, newest first, in plain English. Routine readings are left out.")
    feed = explain.activity_feed(conn, clock, config)
    if not feed:
        st.caption("Nothing notable yet. Advance the simulation.")
    with st.container(height=420, border=False):
        for minute, text in feed:
            st.markdown(f"**Minute {minute}:** {text}")


# ----- sensor chart ------------------------------------------------------------------

with tab_chart:
    sensor_ids = [s.sensor_id for s in system.world.sensors]
    c1, c2 = st.columns([3, 2])
    sid = c1.selectbox("Sensor", sensor_ids, index=sensor_ids.index("S-01-TEMP"), key="chart_sensor",
                       format_func=names.sensor, help="Which sensor's readings to plot.")
    span = c2.slider("Minutes shown", 50, 1000, 300, step=50, key="chart_span",
                     help="How far back the chart goes, in simulated minutes (ticks).")
    sensor = repo.get_sensor(conn, sid)
    state = states[sid]
    first_tick = max(0, clock.tick - span)
    rows = repo.readings_since(conn, sid, clock.iso_at(first_tick))
    df = pd.DataFrame([{"tick": clock.tick_of(r["ts"]), "value": r["value"], "reading": r["reading_id"]} for r in rows])

    if df.empty:
        st.info("No readings in this range yet.")
    else:
        # One x scale for every layer, starting at the first reading shown. (A
        # quantitative scale includes 0 by default, which the bands and markers
        # would otherwise pull in.)
        first_shown = int(df["tick"].min())
        xscale = alt.Scale(domain=[first_shown, max(clock.tick, first_shown + 1)], zero=False, nice=False)
        x = alt.X("tick:Q", title="Simulated minutes", scale=xscale)
        layers = []
        windows = [w for w in relearning_windows(conn, sid, clock) if clock.tick_of(w[1]) >= first_shown]
        if windows:
            wdf = pd.DataFrame([{"start": max(clock.tick_of(a), first_shown), "end": clock.tick_of(b),
                                 "label": "learning the new normal after a recipe change" + (" (ongoing)" if ongoing else "")}
                                for a, b, ongoing in windows])
            layers.append(alt.Chart(wdf).mark_rect(opacity=0.35, color=CHART["relearn"]).encode(
                x=alt.X("start:Q", scale=xscale), x2="end:Q", tooltip=["label", "start", "end"]))
        unit = sensor["unit"]
        stype = explain.SENSOR_TYPES.get(sensor["sensor_type"], sensor["sensor_type"])
        y = alt.Y("value:Q", title=f"{stype} ({unit})", scale=alt.Scale(zero=False))
        allowed, normal = "Allowed range (fixed spec limits)", "Normal range (learned control limits)"
        if state["control_mean"] is not None:
            k, mu, sd = config.sigma_threshold, state["control_mean"], state["control_stddev"]
            band = pd.DataFrame([{"lo": mu - k * sd, "hi": mu + k * sd, "mean": mu, "kind": normal}])
            # The normal range is a light shaded band, with its average as a faint line.
            layers.append(alt.Chart(band).mark_rect(opacity=0.14).encode(
                y="lo:Q", y2="hi:Q",
                color=alt.Color("kind:N", title=None, scale=alt.Scale(domain=[normal, allowed],
                                                                   range=[CHART["normal"], CHART["allowed"]]),
                                legend=alt.Legend(orient="top", labelLimit=320, symbolOpacity=0.5)),
                tooltip=[alt.Tooltip("kind", title="band"), alt.Tooltip("lo", title=f"from ({unit})", format=".3f"),
                         alt.Tooltip("hi", title=f"to ({unit})", format=".3f")]))
            layers.append(alt.Chart(band).mark_rule(color=CHART["normal"], opacity=0.5, strokeDash=[2, 3]).encode(
                y="mean:Q", tooltip=[alt.Tooltip("mean", title=f"normal average (control mean, {unit})", format=".3f")]))
        # The allowed range is two clear solid lines.
        spec = pd.DataFrame([{"y": sensor["spec_lower"], "kind": allowed, "tag": "allowed range"},
                             {"y": sensor["spec_upper"], "kind": allowed, "tag": "allowed range"}])
        layers.append(alt.Chart(spec).mark_rule(strokeWidth=2).encode(
            y="y:Q", color=alt.Color("kind:N", legend=None,
                                     scale=alt.Scale(domain=[normal, allowed], range=[CHART["normal"], CHART["allowed"]])),
            tooltip=[alt.Tooltip("kind", title="line"), alt.Tooltip("y", title=unit)]))
        layers.append(alt.Chart(spec).mark_text(align="left", dx=4, dy=-6, fontSize=11, color=CHART["allowed"]).encode(
            x=alt.value(0), y="y:Q", text="tag:N"))
        layers.append(alt.Chart(df).mark_line(color=CHART["reading"], strokeWidth=1.5,
                                              point=alt.OverlayMarkDef(size=10, color=CHART["reading"])).encode(
            x=x, y=y,
            tooltip=[alt.Tooltip("tick", title="minute"), alt.Tooltip("value", title=f"reading ({unit})"),
                     alt.Tooltip("reading", title="reading ID")]))
        incs = [i for i in repo.incidents_on_sensor(conn, sid) if clock.tick_of(i["opened_at"]) >= first_shown]
        if incs:
            idf = pd.DataFrame([{"tick": clock.tick_of(i["opened_at"]), "incident": i["incident_id"],
                                 "problem": explain.rule(i["rule_fired"], config),
                                 "state": explain.LOOK[explain.state_of(i["severity"], i["status"])][3],
                                 "level": explain.SEVERITY_SHORT[i["severity"]],
                                 "status": explain.status(i["status"])} for i in incs])
            looks = [explain.LOOK[k] for k in ("watch", "alert", "urgent", "closed")]
            layers.append(alt.Chart(idf).mark_rule(strokeWidth=2).encode(
                x=alt.X("tick:Q", scale=xscale),
                tooltip=["incident", "problem", "level", "status", alt.Tooltip("tick", title="detected at minute")],
                color=alt.Color("state:N", title="Problem detected",
                                scale=alt.Scale(domain=[l[3] for l in looks], range=[l[1] for l in looks]),
                                legend=alt.Legend(orient="top"))))
        st.altair_chart(alt.layer(*layers).resolve_scale(color="independent").properties(height=380),
                        width="stretch")
        st.caption(f"Sensor status: {explain.BASELINE.get(state['baseline_status'], state['baseline_status'])} "
                   f"({state['baseline_status']}). Blue line: readings. Shaded blue: the normal range. "
                   "Dark lines: the allowed range. Grey bands: learning the new normal after a recipe change. "
                   "Colored vertical lines: when a problem was detected (hover for details).")


# ----- incidents ---------------------------------------------------------------------

def incident_table(rows) -> pd.DataFrame:
    return pd.DataFrame([{
        "Level": explain.icon_label(explain.state_of(r["severity"], r["status"]), explain.SEVERITY_SHORT[r["severity"]]),
        "Incident": r["incident_id"], "Where": names.where(r["sensor_id"]),
        "What happened": explain.rule_short(r["rule_fired"]),
        "Status": explain.STATUS_SHORT.get(r["status"], r["status"]),
        "Minute": clock.tick_of(r["opened_at"]),
        "Batches at risk": f"{len(json.loads(r['lots_at_risk'] or '[]'))}"
                           + (" · hold recommended" if r["recommend_hold"] else ""),
    } for r in rows])


# Fixed widths (pixels) so every column fits a laptop-width page without cutting text off.
INCIDENT_COLUMNS = {
    "Level": st.column_config.TextColumn("Level", width=96, help="⚪ = closed."),
    "Incident": st.column_config.TextColumn("Incident", width=82),
    "Where": st.column_config.TextColumn("Where", width=128),
    "What happened": st.column_config.TextColumn("What happened", width=162),
    "Status": st.column_config.TextColumn("Status", width=150),
    "Minute": st.column_config.NumberColumn("Minute", width=62, help="Simulated minute the problem was detected (opened)."),
    "Batches at risk": st.column_config.TextColumn(
        "Batches at risk", width=166, help="Product batches (lots) on this machine since the problem started; "
                                           "'hold recommended' when the problem is Urgent."),
}


def describe_evidence(item: dict, unit: str) -> str:
    """A cited record in plain words, without its ID: 'maintenance note at minute 179: "..."'."""
    when = f" at minute {clock.tick_of(item['ts'])}" if "ts" in item else ""
    if item.get("kind") == "maintenance":
        return f"maintenance note{when}: \u201c{item.get('description', '')}\u201d"
    if item.get("kind") == "recipe_changes":
        return f"recipe change{when} to {item.get('recipe_id', '?')}"
    if "value" in item:
        return f"sensor reading{when}: {item['value']:.3f} {unit}"
    return "a record that isn't in the evidence"


with tab_inc:
    notified = repo.incidents_by_tier(conn, actionable=True, active_only=False)
    st.markdown("**Alerts sent to people** (Alert and Urgent levels)",
                help="Problems serious enough that the AI looked into them and a qualified person was notified.")
    if notified:
        st.dataframe(incident_table(notified), hide_index=True, width="stretch", column_config=INCIDENT_COLUMNS)
    else:
        st.caption("None yet.")
    st.markdown("**Watch list: unusual but not alarming yet**",
                help="Low-severity incidents: shown here only, no one is paged. They're promoted if things get worse.")
    if watch:
        st.dataframe(incident_table(watch)[["Level", "Incident", "Where", "What happened", "Minute"]],
                     hide_index=True, width="stretch", column_config=INCIDENT_COLUMNS)
    else:
        st.caption("Empty.")

    choices = [r["incident_id"] for r in notified] + [r["incident_id"] for r in watch]
    if choices:
        st.divider()
        iid = st.selectbox("Show details for", choices, key="incident_detail",
                           format_func=lambda i: f"{i}", help="Pick an incident to see what happened and act on it.")
        inc = repo.get_incident(conn, iid)
        sensor_row = repo.get_sensor(conn, inc["sensor_id"])
        inc_state = explain.state_of(inc["severity"], inc["status"])
        with st.container(border=True):
            st.markdown(f"{explain.badge(inc_state, explain.SEVERITY_SHORT[inc['severity']])} · **{iid}** · "
                        f"{names.sensor(inc['sensor_id'])}")
            st.markdown(f"##### {explain.incident_summary(conn, inc, clock, config, names)}")
            st.markdown(
                f"**{explain.rule(inc['rule_fired'], config)}** · level **{explain.severity(inc['severity'])}** · "
                f"status **{explain.status(inc['status'])}**")
            st.caption(f"Started around minute {clock.tick_of(inc['onset_ts'])} (onset) · detected at minute "
                       f"{clock.tick_of(inc['opened_at'])} · owner {names.person(inc['owner_id'])}")
            if inc["recommend_hold"]:
                st.warning("**Hold recommended:** urgent problem with product batches at risk. A person confirms it below.")

            lots = repo.lots_by_ids(conn, json.loads(inc["lots_at_risk"] or "[]"))
            st.markdown("**Product batches (lots) at risk:** "
                        + (", ".join(f"{l['lot_id']} ({'on hold' if l['status'] == 'held' else l['status'].replace('_', ' ')})"
                                     for l in lots) or "none"),
                        help="Every batch that was on this machine between when the problem started and now.")

        st.markdown("**What the AI thinks**", help="The AI only suggests likely contributing factors; code checks "
                    "every record it cites, and a person decides.")
        dxs = repo.diagnoses_for_incident(conn, iid)
        technical: list[str] = [
            f"Incident {iid}: rule `{inc['rule_fired']}`, severity `{inc['severity']}`, status `{inc['status']}`, "
            f"escalation level {inc['escalation_level']} (times escalated past the owner), "
            f"settings version `{inc['config_version']}`",
            f"Onset `{inc['onset_ts']}`", f"Opened `{inc['opened_at']}`", f"Last update `{inc['updated_at']}`",
            f"Trigger readings: {', '.join(json.loads(inc['trigger_reading_ids'] or '[]')) or '-'}",
        ]
        evidence_lines: list[str] = []
        if not dxs:
            st.caption("No AI diagnosis: watch-list items and sensors that stopped reporting aren't sent to the AI.")
        else:
            dx = dxs[-1]
            bundle = json.loads(dx["evidence_bundle"])
            items = bundle_items(bundle)
            status = dx["status"]
            icon = {"diagnosed": "✅", "abstained": "➖", "rejected": "❌", "unavailable": "⚠️"}[status]
            st.markdown(f"{icon} {explain.DIAGNOSIS[status]} ({status})")
            model = {"fake": "scripted responses (fake)", "failing": "off: simulated outage (failing)"}.get(
                dx["model"], dx["model"] or "-")
            technical.append(f"AI diagnosis **{dx['diagnosis_id']}** ({status}) · AI model: {model} · "
                             f"confidence: {dx['confidence'] or '-'}"
                             + (f" · {len(dxs)} diagnoses for this incident, showing the latest" if len(dxs) > 1 else ""))
            if dx["rejection_reason"]:
                technical.append(f"Reason: {dx['rejection_reason']}")
            first = json.loads(dx["first_attempt"]) if dx["first_attempt"] else None
            if first:  # one self-correction was tried: show both attempts
                revised_ok = status in ("diagnosed", "abstained")
                note = ("AI revised its answer after the checker rejected it." if revised_ok
                        else "AI tried once to revise its answer, but the checker rejected it again.")
                if hosted_mode() and dx["model"] == "fake":
                    note += " (Scripted demo: the first answer deliberately cites a made-up record.)"
                (st.info if revised_ok else st.warning)(note)
                st.markdown(
                    f"**First answer (rejected):** cited {', '.join(first['cited_evidence']) or 'nothing'}"
                    + (f" · suggested: {'; '.join(first['likely_factors'])}" if first["likely_factors"] else "")
                    + f"  \n*Why the checker rejected it:* {first['reason']}")
                st.markdown(f"**Revised answer:** {explain.DIAGNOSIS[status]} ({status})"
                            + (f", citing {', '.join(json.loads(dx['cited_evidence'] or '[]'))}"
                               if json.loads(dx["cited_evidence"] or "[]") else ""))
            factors = json.loads(dx["likely_factors"] or "[]")
            if factors and status == "diagnosed":
                st.markdown("**Likely contributing factors:**\n" + "\n".join(f"- {f}" for f in factors))
            elif factors:
                with st.expander("What the AI said (failed the evidence check, not trusted)"):
                    st.markdown("\n".join(f"- {f}" for f in factors))
            cited = json.loads(dx["cited_evidence"] or "[]")
            if cited:
                checks = {cid: check_citation(cid, bundle, clock, config) for cid in cited}
                plain = [f"- {describe_evidence(items.get(cid, {}), sensor_row['unit'])} · "
                         + ("✓ checked against the records" if checks[cid] is None else "✗ failed the check") for cid in cited]
                st.markdown("**Evidence it pointed to**", help="Each record is checked: it must exist, belong to "
                            "this machine, and come before the problem started.")
                st.markdown("\n".join(plain))
                for cid in cited:
                    item = items.get(cid, {})
                    problem = checks[cid]
                    text = item.get("description") or (f"recipe {item['recipe_id']}" if "recipe_id" in item else
                                                       f"reading value {item['value']:.3f}" if "value" in item else "-")
                    when = f"minute {clock.tick_of(item['ts'])}" if "ts" in item else "not in the evidence"
                    check = ("✓ checked against the records (verified)" if problem is None
                             else f"✗ failed the check: {problem}")
                    evidence_lines.append(f"- **{cid}** · {when} · \u201c{text}\u201d · {check}")

        with st.expander("Technical details"):
            st.markdown("\n".join(f"- {t}" for t in technical))
            if evidence_lines:
                st.markdown("**Evidence the AI cited, by record ID**")
                st.markdown("\n".join(evidence_lines))

        st.markdown("**Actions**", help="What a person does next. The system recommends; people decide.")
        people_ids = [p["person_id"] for p in repo.people(conn)]
        a1, a2 = st.columns([1, 3])
        actor = a1.selectbox("Acting as", people_ids,
                             index=people_ids.index(inc["owner_id"]) if inc["owner_id"] in people_ids else 0,
                             key=f"actor_{iid}", format_func=names.person,
                             help="Who is taking the action (normally the person who was alerted).")
        reason = a1.selectbox("Dismiss reason", ["false_alarm", "duplicate", "other"], key=f"reason_{iid}",
                              format_func=lambda r: explain.DISMISS_REASONS[r],
                              help="Only used by Dismiss. 'False alarm' briefly pauses similar alerts on this sensor.")
        buttons = a2.columns(4)
        for col, (action, text) in zip(buttons, ACTIONS.items()):
            if col.button(text, key=f"{action}_{iid}", width="stretch", help=ACTION_HELP[action]):
                before = inc["status"]
                after = system.alert_action(iid, action, actor, reason if action == "dismiss" else None)
                flash("success" if after != before else "error",
                      f"{iid}: {explain.STATUS[before]} → {explain.STATUS[after]} ({before} → {after})" if after != before
                      else f"{text} isn't possible while {iid} is '{explain.STATUS[before]}' ({before}); "
                           "the request was rejected and logged.")
                st.rerun()
    else:
        st.caption("No incidents yet. Advance the simulation.")


# ----- inboxes ----------------------------------------------------------------------

with tab_inbox:
    people = repo.people(conn)
    pid = st.selectbox("Inbox for", [p["person_id"] for p in people], format_func=names.person, key="inbox_person",
                       help="The alerts this person received, newest first. Click one to read the full message.")
    notes = repo.notifications_for_person(conn, pid)
    if not notes:
        st.caption("No alerts for this person.")
    for n in notes:
        ack = "acknowledged" if n["acknowledged_at"] else "not acknowledged yet"
        look = explain.state_of(n["severity"], n["incident_status"])
        title = (f"{explain.icon_label(look, explain.SEVERITY_SHORT[n['severity']])} · Minute {clock.tick_of(n['sent_at'])} · "
                 f"{explain.NOTICE.get(n['reason'], n['reason']).capitalize()} · {n['incident_id']} · {ack}")
        with st.expander(title):
            st.caption(f"Incident status: {explain.STATUS.get(n['incident_status'], n['incident_status'])} · "
                       f"escalation level {n['escalation_level']} · message {n['notification_id']}",
                       help="Escalation level: how many times this alert has been passed up the line for no response.")
            st.text(n["message"])


# ----- dead letter ------------------------------------------------------------------

with tab_dl:
    st.caption("Garbled messages fail the format check and are set aside (dead letter), never used. "
               "Nothing stops when this happens.")
    dead = repo.recent_dead_letters(conn)
    if not dead:
        st.caption("Nothing rejected yet. Try **Send bad data** in the sidebar.")
    for r in dead:
        with st.container(border=True):
            st.markdown(f"**{r['id']}** · minute {clock.tick_of(r['ts'])} · from the `{r['source']}` feed · "
                        "failed the format check, set aside")
            with st.expander("Technical details"):
                st.markdown(f"Why rejected: {r['error_reason']}")
                st.code(r["raw_payload"], language="json", wrap_lines=True)
