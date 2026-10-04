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

SEVERITY_COLORS = {"low": "#c9a227", "medium": "#e07b39", "high": "#d1334d"}
ACTIONS = {"acknowledge": "Acknowledge", "confirm_hold": "Confirm hold", "dismiss": "Dismiss", "resolve": "Resolve"}

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
        return FakeClient("valid"), "Scripted fake client (hosted demo)"
    choice = os.environ.get("FAB_MONITOR_LLM") or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "fake")
    if choice == "anthropic":
        model = load_config().diagnosis_model
        return AnthropicClient(model), f"Claude API ({model})"
    return FakeClient("valid"), "Scripted fake client (no API key set)"


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
    st.session_state.flash = ("info", "Demo loaded: drift on S-01-TEMP starts 30 ticks after warm-up, right after a "
                                      "planted maintenance entry. S-04-TEMP is noisy but healthy. T-03 and T-05 are "
                                      "fault-free: try a recipe change there.")


def flash(kind: str, text: str) -> None:
    st.session_state.flash = (kind, text)


if "system" not in st.session_state:
    load_demo()
system: System = st.session_state.system
conn, clock, config = system.conn, system.clock, system.config


# ----- sidebar: controls ---------------------------------------------------------

with st.sidebar:
    st.header("Simulation")
    if st.button("Load demo scenario", width="stretch"):
        load_demo()
        st.rerun()

    st.caption("Advance ticks")
    cols = st.columns(3)
    for col, n in zip(cols, (1, 10, 50)):
        if col.button(f"+{n}", width="stretch"):
            system.advance(n)
            st.rerun()

    # Keyed with a constant default so the widget keeps its own state across reruns.
    killed = st.toggle("Kill LLM", value=False, key="kill_llm",
                       help="Swap in FailingClient: diagnoses become 'unavailable', alerts still go out.")
    system.orchestrator.llm = FailingClient() if killed else st.session_state.llm
    st.caption(f"LLM: {'killed (FailingClient)' if killed else st.session_state.llm_label}")

    st.subheader("Inject fault")
    tools = [t.tool_id for t in system.world.tools]
    f_tool = st.selectbox("Tool", tools, key="f_tool")
    f_sensor = st.selectbox("Sensor", [s.sensor_id for s in system.world.sensors if s.tool_id == f_tool], key="f_sensor")
    f_type = st.selectbox("Fault type", LIVE_FAULT_TYPES, key="f_type")
    if st.button("Inject", width="stretch"):
        try:
            fid = system.inject(f_type, f_sensor)
            flash("success", f"Injected {fid}: {f_type} on {f_sensor} (recorded in fault_injections).")
        except ValueError as e:
            flash("error", f"Could not inject: {e}")
        st.rerun()

    st.subheader("Recipe change")
    r_tool = st.selectbox("Tool", tools, index=tools.index("T-05") if "T-05" in tools else 0, key="r_tool")
    r_recipe = st.text_input("New recipe ID", value=f"R-{r_tool[2:]}-B", key="r_recipe")
    if st.button("Change recipe", width="stretch"):
        ok = system.recipe_change(r_tool, r_recipe)
        flash("success" if ok else "error",
              f"{r_tool} switched to {r_recipe}: its sensors are relearning." if ok else "Recipe change was rejected.")
        st.rerun()

    st.subheader("People")
    people = repo.people(conn)
    p_id = st.selectbox("Person", [p["person_id"] for p in people],
                        format_func=lambda pid: next(f"{pid} {p['name']} ({'available' if p['available'] else 'unavailable'})"
                                                     for p in people if p["person_id"] == pid), key="p_id")
    available = next(p["available"] for p in people if p["person_id"] == p_id)
    if st.button("Mark unavailable" if available else "Mark available", width="stretch"):
        system.set_availability(p_id, not available)
        flash("success", f"{p_id} marked {'unavailable: open incidents they own are reassigned' if available else 'available'}.")
        st.rerun()

    st.subheader("Bad data")
    if st.button("Send a malformed event", width="stretch"):
        accepted = system.send_malformed()
        flash("warning" if not accepted else "error",
              "Malformed reading quarantined in dead_letter; the pipeline kept going." if not accepted
              else "Unexpected: the malformed event was accepted.")
        st.rerun()


# ----- header ---------------------------------------------------------------------

st.error(HOSTED_BANNER if hosted_mode() else LOCAL_BANNER, icon="⚠️")
if "flash" in st.session_state:
    kind, text = st.session_state.pop("flash")
    getattr(st, kind)(text)

notified_active = repo.incidents_by_tier(conn, actionable=True, active_only=True)
watch = repo.incidents_by_tier(conn, actionable=False, active_only=True)
m = st.columns([1, 1.6, 1.3, 1.1, 0.9, 1.2])
m[0].metric("Tick", clock.tick, help=clock.now_iso())
m[1].metric("Active config", config.version)
m[2].metric("Active notified incidents", len(notified_active))
m[3].metric("Watch list (low)", len(watch))
m[4].metric("Lots held", repo.count_by_status(conn, "lots", "status", "held"))
m[5].metric("Dead-letter records", repo.count_rows(conn, "dead_letter"))

# Tool grid
st.subheader("Tools")
grid = st.columns(len(system.world.tools))
states = {r["sensor_id"]: r for r in repo.all_sensor_states(conn)}
for col, t in zip(grid, repo.tool_overview(conn)):
    with col.container(border=True):
        high, med, low = t["high"] or 0, t["medium"] or 0, t["low"] or 0
        badge = "🔴" if high else "🟠" if med else "🟡" if low else "🟢"
        st.markdown(f"**{badge} {t['tool_id']}** · {t['kind']}")
        st.caption(f"Status: {t['status']} · Recipe: {t['current_recipe_id']}")
        st.caption(f"Active incidents: {high} high · {med} medium · {low} low")
        sensors = [s for s in system.world.sensors if s.tool_id == t["tool_id"]]
        st.caption(" · ".join(f"{s.sensor_id[5:]}: {states[s.sensor_id]['baseline_status']}" for s in sensors))

tab_chart, tab_inc, tab_inbox, tab_dl = st.tabs(["Sensor chart", "Incidents", "Inboxes", "Dead letter"])


# ----- sensor chart ------------------------------------------------------------------

with tab_chart:
    sensor_ids = [s.sensor_id for s in system.world.sensors]
    c1, c2 = st.columns([1, 2])
    sid = c1.selectbox("Sensor", sensor_ids, index=sensor_ids.index("S-01-TEMP"), key="chart_sensor")
    span = c2.slider("Ticks shown", 50, 1000, 300, step=50, key="chart_span")
    sensor = repo.get_sensor(conn, sid)
    state = states[sid]
    first_tick = max(0, clock.tick - span)
    rows = repo.readings_since(conn, sid, clock.iso_at(first_tick))
    df = pd.DataFrame([{"tick": clock.tick_of(r["ts"]), "value": r["value"], "reading": r["reading_id"]} for r in rows])

    if df.empty:
        st.info("No readings in this range yet.")
    else:
        x = alt.X("tick:Q", title="tick", scale=alt.Scale(domain=[first_tick, max(clock.tick, first_tick + 1)]))
        layers = []
        windows = [w for w in relearning_windows(conn, sid, clock) if clock.tick_of(w[1]) >= first_tick]
        if windows:
            wdf = pd.DataFrame([{"start": max(clock.tick_of(a), first_tick), "end": clock.tick_of(b),
                                 "label": "relearning (ongoing)" if ongoing else "relearning"} for a, b, ongoing in windows])
            layers.append(alt.Chart(wdf).mark_rect(opacity=0.15, color="#7f7f7f").encode(
                x=alt.X("start:Q"), x2="end:Q", tooltip=["label", "start", "end"]))
        layers.append(alt.Chart(df).mark_line(point=alt.OverlayMarkDef(size=12)).encode(
            x=x, y=alt.Y("value:Q", title=f"{sensor['sensor_type']} ({sensor['unit']})", scale=alt.Scale(zero=False)),
            tooltip=["tick", "value", "reading"]))
        limits = [{"y": sensor["spec_lower"], "kind": "spec limit"}, {"y": sensor["spec_upper"], "kind": "spec limit"}]
        if state["control_mean"] is not None:
            k, mu, sd = config.sigma_threshold, state["control_mean"], state["control_stddev"]
            limits += [{"y": mu - k * sd, "kind": "control limit"}, {"y": mu + k * sd, "kind": "control limit"},
                       {"y": mu, "kind": "control mean"}]
        layers.append(alt.Chart(pd.DataFrame(limits)).mark_rule().encode(
            y="y:Q", tooltip=["kind", "y"],
            color=alt.Color("kind:N", scale=alt.Scale(domain=["spec limit", "control limit", "control mean"],
                                                      range=["#d1334d", "#4c78a8", "#9ecae9"]), title="Limits"),
            strokeDash=alt.condition(alt.datum.kind == "spec limit", alt.value([1, 0]), alt.value([6, 4]))))
        incs = [i for i in repo.incidents_on_sensor(conn, sid) if clock.tick_of(i["opened_at"]) >= first_tick]
        if incs:
            idf = pd.DataFrame([{"tick": clock.tick_of(i["opened_at"]), "incident": i["incident_id"],
                                 "rule": i["rule_fired"], "severity": i["severity"], "status": i["status"]} for i in incs])
            layers.append(alt.Chart(idf).mark_rule(strokeWidth=2).encode(
                x="tick:Q", tooltip=["incident", "rule", "severity", "status", "tick"],
                color=alt.Color("severity:N", title="Incident opened",
                                scale=alt.Scale(domain=list(SEVERITY_COLORS), range=list(SEVERITY_COLORS.values())))))
        st.altair_chart(alt.layer(*layers).resolve_scale(color="independent").properties(height=380),
                        width="stretch")
        st.caption(f"Baseline: {state['baseline_status']}. Grey bands are relearning windows after a recipe change; "
                   "vertical lines mark when an incident opened (hover for details).")


# ----- incidents ---------------------------------------------------------------------

def incident_table(rows) -> pd.DataFrame:
    return pd.DataFrame([{
        "incident": r["incident_id"], "sensor": r["sensor_id"], "rule": r["rule_fired"], "severity": r["severity"],
        "status": r["status"], "onset tick": clock.tick_of(r["onset_ts"]), "opened tick": clock.tick_of(r["opened_at"]),
        "owner": r["owner_id"] or "-", "hold rec.": "YES" if r["recommend_hold"] else "",
        "lots at risk": len(json.loads(r["lots_at_risk"] or "[]")),
    } for r in rows])


with tab_inc:
    notified = repo.incidents_by_tier(conn, actionable=True, active_only=False)
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Notified incidents** (medium and high: diagnosed, triaged, sent to a person)")
        if notified:
            st.dataframe(incident_table(notified), hide_index=True, width="stretch")
        else:
            st.caption("None yet.")
    with right:
        st.markdown("**Watch list** (active low-severity incidents: dashboard only, nobody is paged)")
        if watch:
            st.dataframe(incident_table(watch)[["incident", "sensor", "rule", "status", "opened tick"]],
                         hide_index=True, width="stretch")
        else:
            st.caption("Empty.")

    choices = [r["incident_id"] for r in notified] + [r["incident_id"] for r in watch]
    if choices:
        st.divider()
        iid = st.selectbox("Incident detail", choices, key="incident_detail")
        inc = repo.get_incident(conn, iid)
        st.markdown(
            f"**{iid}** · {inc['sensor_id']} on {inc['tool_id']} · rule **{inc['rule_fired']}** · "
            f"severity **{inc['severity']}** · status **{inc['status']}** · onset tick {clock.tick_of(inc['onset_ts'])} · "
            f"opened tick {clock.tick_of(inc['opened_at'])} · owner {inc['owner_id'] or '-'} · "
            f"escalation level {inc['escalation_level']}")
        if inc["recommend_hold"]:
            st.warning("**Hold recommended** (high severity with lots at risk). A person confirms it below.")

        lots = repo.lots_by_ids(conn, json.loads(inc["lots_at_risk"] or "[]"))
        st.markdown("**Lots at risk:** " + (", ".join(f"{l['lot_id']} ({l['status']})" for l in lots) or "none"))

        dxs = repo.diagnoses_for_incident(conn, iid)
        if not dxs:
            st.caption("No diagnosis (low-severity watch-list incidents and dropouts are never diagnosed).")
        else:
            dx = dxs[-1]
            bundle = json.loads(dx["evidence_bundle"])
            items = bundle_items(bundle)
            status = dx["status"]
            label = {"diagnosed": "✅ diagnosed (citations verified)", "abstained": "➖ abstained",
                     "rejected": "❌ rejected by the verifier, not trusted", "unavailable": "⚠️ unavailable"}[status]
            st.markdown(f"**Diagnosis {dx['diagnosis_id']}:** {label} · model {dx['model'] or '-'} · "
                        f"confidence {dx['confidence'] or '-'}")
            if dx["rejection_reason"]:
                st.caption(f"Reason: {dx['rejection_reason']}")
            factors = json.loads(dx["likely_factors"] or "[]")
            if factors and status == "diagnosed":
                st.markdown("**Likely contributing factors:**\n" + "\n".join(f"- {f}" for f in factors))
            elif factors:
                with st.expander("Model output (failed verification, not trusted)"):
                    st.markdown("\n".join(f"- {f}" for f in factors))
            cited = json.loads(dx["cited_evidence"] or "[]")
            if cited:
                st.markdown("**Citations**")
                rows = []
                for cid in cited:
                    item = items.get(cid, {})
                    problem = check_citation(cid, bundle, clock, config)
                    text = item.get("description") or (f"recipe {item['recipe_id']}" if "recipe_id" in item else
                                                       f"reading value {item['value']:.3f}" if "value" in item else "-")
                    rows.append({"verified": "✅" if problem is None else "❌", "id": cid,
                                 "tick": clock.tick_of(item["ts"]) if "ts" in item else "-",
                                 "text": text, "check": problem or "in evidence, right tool, not after onset"})
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if len(dxs) > 1:
                st.caption(f"{len(dxs)} diagnoses for this incident; showing the latest.")

        st.markdown("**Actions**")
        people_ids = [p["person_id"] for p in repo.people(conn)]
        a1, a2 = st.columns([1, 3])
        actor = a1.selectbox("Acting as", people_ids,
                             index=people_ids.index(inc["owner_id"]) if inc["owner_id"] in people_ids else 0,
                             key=f"actor_{iid}")
        reason = a1.selectbox("Dismiss reason", ["false_alarm", "duplicate", "other"], key=f"reason_{iid}")
        buttons = a2.columns(4)
        for col, (action, text) in zip(buttons, ACTIONS.items()):
            if col.button(text, key=f"{action}_{iid}", width="stretch"):
                before = inc["status"]
                after = system.alert_action(iid, action, actor, reason if action == "dismiss" else None)
                flash("success" if after != before else "error",
                      f"{iid}: {before} → {after}" if after != before
                      else f"{text} is not allowed for an incident that is {before} (rejected and logged).")
                st.rerun()
    else:
        st.caption("No incidents yet. Advance the simulation.")


# ----- inboxes ----------------------------------------------------------------------

with tab_inbox:
    people = repo.people(conn)
    pid = st.selectbox("Inbox for", [p["person_id"] for p in people],
                       format_func=lambda x: next(f"{x} {p['name']}" for p in people if p["person_id"] == x),
                       key="inbox_person")
    notes = repo.notifications_for_person(conn, pid)
    if not notes:
        st.caption("No notifications.")
    for n in notes:
        ack = "acknowledged" if n["acknowledged_at"] else "not acknowledged"
        title = (f"{n['notification_id']} · tick {clock.tick_of(n['sent_at'])} · {n['reason']} · "
                 f"escalation level {n['escalation_level']} · {n['incident_id']} ({n['severity']}, {n['incident_status']}) · {ack}")
        with st.expander(title):
            st.text(n["message"])


# ----- dead letter ------------------------------------------------------------------

with tab_dl:
    st.metric("Quarantined records", repo.count_rows(conn, "dead_letter"))
    for r in repo.recent_dead_letters(conn):
        st.markdown(f"**{r['id']}** · source `{r['source']}` · {r['error_reason']}")
        st.code(r["raw_payload"], language="json")
