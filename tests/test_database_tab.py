"""The read-only database tab: every table grouped with its row count, the latest
events, and the summary of eval_results/database_report.md. Fake client only."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from dashboard import explain

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "dashboard" / "app.py")


def test_every_schema_table_is_in_exactly_one_group():
    schema = (ROOT / "db" / "schema.sql").read_text()
    tables = {line.split()[2] for line in schema.splitlines() if line.startswith("CREATE TABLE")}
    grouped = [t for _, _, rows in explain.DB_GROUPS for t, _ in rows]
    assert sorted(grouped) == sorted(tables)
    assert [g for g, _, _ in explain.DB_GROUPS] == [
        "Reference data", "Live state", "Append-only history", "Outputs and audit", "Ground truth"]


def test_summary_numbers_match_the_report():
    report = (ROOT / "eval_results" / "database_report.md").read_text()
    r = explain.DB_REPORT
    assert f"{r['replay_events']:,} events" in report and "every row and every column matches exactly" in report
    assert f"**{r['violations']}**" in report and f"{r['audit_ticks']:,}-tick run" in report
    assert f"{r['index_ms']} ms" in report and f"{r['no_index_ms']} ms" in report
    assert "550–620 times slower" in report
    assert explain.DB_REPORT_URL.endswith("/blob/main/eval_results/database_report.md")


def test_tab_loads_with_correct_counts_after_the_demo(monkeypatch):
    monkeypatch.setenv("FAB_MONITOR_LLM", "fake")
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    next(b for b in at.sidebar.button if b.label == "+50").click().run()  # demo step 2: minute 200
    assert not at.exception, at.exception
    conn = at.session_state.system.conn

    frames = [d.value for d in at.dataframe if list(d.value.columns) == ["Table", "What it holds", "Rows"]]
    assert len(frames) == 5
    shown = {row["Table"]: row["Rows"] for df in frames for _, row in df.iterrows()}
    actual = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in shown}
    assert shown == actual
    assert (shown["tools"], shown["sensors"], shown["incidents"]) == (5, 15, 8)
    assert shown["events"] == conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 3000

    events = next(d.value for d in at.dataframe if "Details" in d.value.columns)
    latest = [r[0] for r in conn.execute("SELECT event_id FROM events ORDER BY event_id DESC LIMIT 20")]
    assert list(events["Event"]) == latest
    assert events.iloc[0]["Type"] == "Clock tick" and events.iloc[0]["Minute"] == 200

    text = " ".join(m.value for m in at.markdown)
    assert "Event replay: matched exactly." in text and "Integrity audit: 0 violations" in text
    assert explain.DB_REPORT_URL in text
