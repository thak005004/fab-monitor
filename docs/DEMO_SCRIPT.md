# Demo script: Fab Tool Health Monitor dashboard

**All data in this demo is synthetic.** Say so at the start: every tool, sensor, person, lot and reading is simulated.

Every tick and ID below was produced by running this exact sequence of button presses headless, with the scripted fake LLM client (`scripts/demo_walkthrough.py`). `tests/test_demo_script.py` replays it and fails if any of these moments moves. Detection, incidents, lots and notifications don't depend on the LLM, so they are the same with the real model. Only the diagnosis contents can differ (see [With the real model](#with-the-real-model) at the end).

## Before you start

```
cd ~/fab-monitor
.venv/bin/streamlit run dashboard/app.py
```

The dashboard uses Claude if `ANTHROPIC_API_KEY` is set, otherwise the scripted fake client; the sidebar says which one is active. Simulated time: 1 tick = 1 minute.

## 1. Open the page (demo scenario loads)

The demo loads by itself, and **Load demo scenario** resets to the same state at any time.

**You see:**
- The red **Simulated data** banner.
- **Tick 150**, active config **2026-10-05-a**, dead-letter records **0**.
- Tool grid: every sensor on every tool shows **active**. The 150-tick warm-up went through the event bus, and each sensor learned its baseline from its first 120 readings.

**Say:** a drift is planted on T-01's temperature sensor. A maintenance entry, **M-0001** "Replaced chamber heater controller board; thermocouple recalibrated.", is logged at **tick 179**, and the drift starts at **tick 180**. S-04-TEMP is noisy but healthy, and T-03 and T-05 have no faults.

## 2. Press **+50** (sidebar) → tick 200: the drift is caught

**You see:**
- T-01's tile turns orange: 1 medium incident.
- **Incidents** tab → *Notified incidents*: **INC-0008** on **S-01-TEMP**, rule **sustained_run**, severity **medium**, onset tick **190**, opened tick **198**, owner **P-01**.
- Select **INC-0008** under *Incident detail*:
  - **Lots at risk:** LOT-0030 (at_risk). No hold is recommended yet (it's only medium).
  - **Diagnosis DX-00004: diagnosed (citations verified)**. The likely contributing factor cites **M-0001** ✅ ("in evidence, right tool, not after onset"), with its maintenance text shown.
- **Inboxes** tab → P-01 Avery Lin: a **new** notification at tick 198, escalation level 0.
- **Sensor chart** (S-01-TEMP): readings climbing toward the dashed upper control limit, the red spec limits, and an orange marker at tick 198.

**Also on screen (be honest about these):** six false alarms on healthy sensors appeared during these 50 ticks.
- **Three medium** were notified to a person: INC-0003 S-03-TEMP (167), INC-0004 S-02-RF (172) and INC-0005 S-02-TEMP (179).
- **Three low** are on the *Watch list* and nobody was paged: INC-0002 S-02-TEMP (152), INC-0006 S-04-RF (180) and INC-0007 S-04-PRES (185).

**Say:** SPC caught the drift 18 ticks after it began, 56 ticks before any product went out of spec. The diagnosis cites real evidence, and code checked every citation.

## 3. Acknowledge INC-0008

On *Incident detail* for INC-0008, **Acting as** shows **P-01**. Press **Acknowledge**.

**You see:** "INC-0008: open → acknowledged". Escalation for this incident stops.

## 4. Live recipe change on T-05

In the sidebar under **Recipe change**: Tool **T-05**, New recipe ID **R-05-B** (the defaults). Press **Change recipe**.

**You see:**
- "T-05 switched to R-05-B: its sensors are relearning."
- T-05's tile: TEMP, PRES and RF all **relearning**. The change is recorded as **RC-0001** at tick 200.
- **Sensor chart** → pick S-05-TEMP: a grey relearning band starts at tick 200.

**Say:** control rules are paused while the new operating point is learned. Spec limits are still checked.

## 5. Send a malformed event

Press **Send a malformed event** (sidebar, *Bad data*).

**You see:**
- "Malformed reading quarantined in dead_letter; the pipeline kept going."
- Dead-letter records **1**.
- **Dead letter** tab: **DL-000001**, source `sensor`, reason "bad payload: value: Input should be a valid number", with the raw payload.

## 6. Kill the LLM, then press **+50** → tick 250

Turn on **Kill LLM** in the sidebar. The caption changes to "LLM: killed (FailingClient)". Then press **+50**.

**You see** (INC-0008 detail):
- **Tick 213:** INC-0008 is acknowledged but still triggering 15 ticks after its last notice, so P-01 gets a **persistent** notification. Its re-diagnosis **DX-00005** is **unavailable** ("LLM unavailable: LLM disabled (kill switch)"), and the alert went out anyway.
- **Lots at risk:** LOT-0030 and **LOT-0037**. LOT-0037 started on T-01 at tick 233, during the incident, and was marked on that tick with no new notification.

**Say:** the AI is out, and the alert path still works.

## 7. Press **+1** four times → tick 254: the drift leaves spec

**You see** (INC-0008):
- Rule **beyond_spec**, severity **high** (upgraded from medium, still the same incident).
- **Hold recommended** (high severity with lots at risk): LOT-0030, LOT-0037.
- P-01 gets an **upgraded** notification at tick 254.
- Diagnosis **DX-00006**: **unavailable** (LLM still killed). Severity and the hold recommendation came from the rule, not from the AI.
- Sensor chart: the reading at tick 254 crosses the red upper spec line.

## 8. Confirm the hold

On INC-0008, press **Confirm hold** (acting as P-01).

**You see:** "INC-0008: acknowledged → hold_confirmed". **Lots held: 2** (LOT-0030, LOT-0037).

**Say:** the system recommended the hold; a person confirmed it.

## 9. Turn Kill LLM off, then press **+50** three times → tick 404

**You see:**
- T-05: all sensors **active** again. On the S-05-TEMP chart, the grey band runs **exactly from tick 200 to tick 320**, the real length of that window.
- No incident on T-05 during relearning. Later, three low watch-list items appear on T-05 (INC-0012 S-05-PRES at 337, INC-0014 S-05-RF at 339, INC-0015 S-05-RF at 367): ordinary false alarms against the new limits, and nobody was paged.
- INC-0008's lots at risk keep growing as new lots start on T-01: LOT-0043, LOT-0049, LOT-0052 and LOT-0058 are marked at_risk. Only the two lots at risk when the hold was confirmed are held. A person would confirm the newer ones separately.

## With the real model

These parts can differ when the dashboard runs with Claude instead of the fake client:
- **The diagnosis at tick 198 (DX-00004):** its wording, confidence, and which IDs it cites. The real model (run 1 in `eval_results/`) also cited readings next to M-0001, and it could abstain. The ✅/❌ marks still come from code.
- **Calls take a few seconds each.** A **+50** press waits for every diagnosis in those ticks, including the three medium false alarms in step 2.
- **Unchanged:** every tick, incident ID, rule, severity, lot, notification and hold above, and steps 6–7, where the model is killed anyway.
