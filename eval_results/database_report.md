# Database checks

All data is synthetic. Every run used seed 42, config version 2026-10-05-a and the scripted fake LLM client. The three scripts only read existing databases or write new temporary ones.

## 1. Rebuilding the state from the event log (`scripts/replay_events.py`)

**Question:** is the event log a complete record? If you start from an empty database (with the same reference data and lots) and feed it only the logged events, do you get the same state?

**Run:** the dashboard demo scenario to minute 404. It includes every kind of event a person can send: acknowledge, recipe change, availability change, confirm hold, a deliberately invalid action (rejected, but still logged), and a malformed record (which goes to dead letter, not into the events table). That came to 6,470 events, including 6,060 readings and 10 diagnoses, plus 1 rejected record. All 6,470 events were replayed in `event_id` order through the same bus and orchestrator. No simulator was involved; the clock was moved to each event's timestamp.

| Table | Rows (original) | Rows (rebuilt) | Result |
|---|---:|---:|---|
| tool_state | 5 | 5 | identical |
| sensor_state | 15 | 15 | identical |
| baseline_windows | 18 | 18 | identical |
| incidents | 17 | 17 | identical |
| notifications | 14 | 14 | identical |
| lots | 427 | 427 | identical |

**Result:** every row and every column matches exactly, with no differences to report. A test (`tests/test_database_scripts.py`) replays a run to minute 254 and requires an identical state. A second test edits the rebuilt database and checks that the comparison reports the change.

**What this relies on:**
- **Same seed for the reference data and lots.** These are seeded, not logged as events.
- **A deterministic LLM client.** With the real model, the diagnoses could differ on replay. Incidents, lots and notifications would still match, but a notification message or an incident's `latest_diagnosis_id` could differ if the diagnosis did.
- **Rejected records aren't replayed.** They never enter the events table, so they don't affect the rebuilt state.

## 2. Integrity audit (`scripts/integrity_audit.py`)

Six checks. Each reports a count and up to five examples:

| Check | Demo scenario (minute 404) | 10,000-tick run |
|---|---:|---:|
| Readings or events referring to unknown sensors, tools or people (including a reading whose tool doesn't own its sensor, and actions on unknown incidents) | 0 | 0 |
| Incidents at medium or high with no notification | 0 | 0 |
| Diagnoses citing an evidence ID not in their own stored bundle | 0 | 0 |
| Held lots with no hold-confirmed incident | 0 | 0 |
| Notifications for incidents that don't exist | 0 | 0 |
| Incidents whose status history breaks the allowed transitions | 0 | 0 |
| **Total** | **0** | **0** |

**Size of the two runs:**
- **Demo scenario:** 6,468 events, 6,060 readings, 17 incidents, 14 notifications, 10 diagnoses, 2 lots held.
- **10,000-tick run:** 159,981 events, 149,980 readings, 671 incidents, 559 notifications, 391 diagnoses, 1,423 lots. Faults 1–4 were injected after warm-up, and the run took about 80 seconds.

**How some checks are defined:**
- **Status history:** there is no status-history table. The audit rebuilds each incident's history from its logged `alert_action` events, run through the allowed transitions in `incidents.TRANSITIONS`. An action that isn't allowed from the current status counts as a rejected request. The one automatic change is open → expired, at the incident's last update. A violation is a stored status that this history can't reach.
- **Diagnoses:** only accepted diagnoses count. A *rejected* diagnosis citing a missing ID is the checker working as designed. The audit reports how many there were (0 in both runs with the "valid" fake script).
- **Held lots:** a held lot must be in the lots at risk of an incident with an accepted `confirm_hold` in its history.

**Limits of these runs:**
- **The 10,000-tick run is unattended.** Nobody acknowledges or holds anything, so the status-history and held-lot checks only have something to check in the demo scenario. That scenario has an acknowledge, a confirm hold and 2 held lots.
- **Two checks can't fail in a real run.** The database's foreign keys already rule out readings for unknown sensors and notifications for unknown incidents. The audit checks them anyway, and also covers references the foreign keys don't: payload IDs inside events, and a reading's tool matching its sensor.

**Testing the audit itself:**
- **Clean run:** a test requires zero violations on the demo scenario.
- **Planted violations:** a second test plants one violation of each kind in a copy of the database: an unknown sensor, an orphan notification, a status no action leads to, a held lot without a confirmed hold, an uncited evidence ID, and a medium incident with its notifications removed. All six checks catch their violation, one each.

## 3. Index benchmark (`scripts/index_benchmark.py`)

**Setup:**
- **Database:** a new database with 150,000 readings (15 sensors × 10,000 minutes).
- **Query:** the exact one the orchestrator runs on every incoming reading (`db.repo.recent_readings`): one sensor's latest 9 readings since its baseline became active.
- **Timing:** each query is timed separately, after a warm-up pass. The run was done twice.

| | Median per query | 5th–95th percentile | Runs |
|---|---:|---:|---:|
| With the index on `(sensor_id, ts)` | 0.041 ms | 0.041–0.054 ms | 2,000 |
| Index dropped | 25.6 ms | 22.2–37.0 ms | 200 |
| Second run, with index | 0.042 ms | 0.041–0.060 ms | 2,000 |
| Second run, index dropped | 22.9 ms | 21.9–30.1 ms | 200 |

**Result:** the query is about 550–620 times slower without the index. With the index the median is the same in both runs; without it, the median varies by about 10%.

**What it means for the system:** every reading runs this query once. With 15 readings a minute, the unindexed version would cost roughly 0.35 seconds of database time per simulated minute at this table size, and it grows with the table. With the index it stays under a millisecond.

**SQLite's EXPLAIN QUERY PLAN:**

With the index:
```
SEARCH readings USING INDEX idx_readings_sensor_ts (sensor_id=? AND ts>?)
USE TEMP B-TREE FOR RIGHT PART OF ORDER BY
```

Index dropped:
```
SCAN readings
USE TEMP B-TREE FOR ORDER BY
```

**How to read the plans:**
- **With the index:** SQLite jumps straight to the one sensor's readings in time order. The "right part" sort covers only the `reading_id` tie-breaker, among readings with the same timestamp.
- **Without the index:** it reads all 150,000 rows and sorts the matches.

**Unchanged:** the schema, including this index.
