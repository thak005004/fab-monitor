# Demo script: Fab Tool Health Monitor dashboard

**All data in this demo is synthetic.** Say so at the start: every tool, sensor, person, lot and reading is simulated.

Every minute and ID below was produced by running this exact sequence of button presses headless, with the scripted fake LLM client (`scripts/demo_walkthrough.py`). `tests/test_demo_script.py` replays it and fails if any of these moments moves. Detection, incidents, lots and notifications don't depend on the LLM, so they are the same with the real model. Only the diagnosis contents can differ (see [With the real model](#with-the-real-model) at the end).

## Before you start

```
cd ~/fab-monitor
.venv/bin/streamlit run dashboard/app.py
```

The dashboard uses Claude if `ANTHROPIC_API_KEY` is set, otherwise scripted responses; the sidebar's "AI:" line says which. Time is simulated: one step (tick) is one minute, shown as **Simulated minute**. Every label shows its technical term in parentheses or on hover.

## 1. Open the page (demo scenario loads)

The demo loads by itself, and **Load demo scenario** resets to the same state at any time.

**You see:**
- The red **Simulated data** banner, and the **What you're looking at** box (open) explaining the page in four sentences.
- **Simulated minute 150**, settings version **2026-10-05-a**, rejected bad data **0**.
- Machines (tools): every card shows **All sensors monitoring**. The 150-minute warm-up went through the event bus, and each sensor learned its normal range from its first 120 readings.
- The **Activity** tab (first tab) lists notable events in plain English, newest first.

**Say:** a drift is planted on Etch Tool 1 (T-01)'s temperature sensor. A maintenance entry, **M-0001** "Replaced chamber heater controller board; thermocouple recalibrated.", is logged at **minute 179**, and the drift starts at **minute 180**. S-04-TEMP is noisy but healthy, and T-03 and T-05 have no faults.

## 2. Press **+50** (sidebar) → minute 200: the drift is caught

**You see:**
- Etch Tool 1 (T-01)'s card turns orange: **Open: 1 alert**.
- **Activity** tab: "**Minute 198:** Etch Tool 1 (T-01) temperature has been above normal for 9 readings in a row. Alert sent to Avery Lin (P-01). (INC-0008)"
- **Incidents** tab → *Alerts sent to people*: **INC-0008**, T-01 temperature, **Sustained shift**, level **Alert**, detected at minute **198**.
- Select **INC-0008** under *Incident detail*:
  - **Sustained shift: 9 readings in a row on one side of normal (sustained_run)** · level **Alert** · status **Waiting for response (open)**.
  - Started around minute **190** (onset), detected at minute **198**, owner **Avery Lin (P-01)**.
  - **Product batches (lots) at risk:** LOT-0030 (at risk). No hold is recommended yet (it's only an Alert).
  - **AI diagnosis DX-00004: ✅ AI suggested likely causes (diagnosed)**. On the hosted copy (scripted responses), it also shows "AI revised its answer after the checker rejected it": the scripted first answer cites a made-up record, M-9999, the checker rejects it, and the revised answer cites M-0001. Both attempts are shown. The evidence it cites, **M-0001**, shows "✓ checked against the records (verified)", with its maintenance text.
- **Inboxes** tab → Avery Lin (P-01): a **new alert** at minute 198, escalation level 0.
- **Sensor chart** (Etch Tool 1 (T-01) temperature (S-01-TEMP)): readings climbing toward the dashed **normal range** line, the red **allowed range** lines, and an orange "problem detected" marker at minute 198.

**Also on screen (be honest about these):** six false alarms on healthy sensors appeared during these 50 minutes.
- **Three Alerts** were sent to a person: INC-0003 S-03-TEMP (167), INC-0004 S-02-RF (172) and INC-0005 S-02-TEMP (179).
- **Three** went on the *Watch list* and nobody was paged: INC-0002 S-02-TEMP (152), INC-0006 S-04-RF (180) and INC-0007 S-04-PRES (185).

**Say:** SPC caught the drift 18 minutes after it began, 56 minutes before any product went out of the allowed range. The AI's suggestion cites real evidence, and code checked every citation.

## 3. Acknowledge INC-0008

On *Incident detail* for INC-0008, **Acting as** shows **Avery Lin (P-01)**. Press **Acknowledge**.

**You see:** "INC-0008: Waiting for response → Someone is on it (open → acknowledged)". The page stays on the Incidents tab with INC-0008 selected. Escalation for this incident stops.

## 4. Live recipe change on T-05

In the sidebar under **Recipe change**: Tool **Etch Tool 5 (T-05)**, New recipe ID **R-05-B** (the defaults). Press **Change recipe**.

**You see:**
- "Etch Tool 5 (T-05) switched to recipe R-05-B: its sensors are learning the new normal (relearning)."
- Etch Tool 5 (T-05)'s card: **Learning new normal: temperature, pressure, RF power**. The change is recorded as **RC-0001** at minute 200.
- **Sensor chart** → pick Etch Tool 5 (T-05) temperature (S-05-TEMP): a grey "learning the new normal" band starts at minute 200.

**Say:** the alarms that compare against normal are paused while the new normal is learned. The allowed-range check still runs.

## 5. Send bad data

Press **Send bad data** (sidebar, *Bad data*).

**You see:**
- "Bad data rejected and set aside (dead letter); monitoring kept going."
- Rejected bad data **1**.
- **Rejected bad data** tab: **DL-000001**, from the `sensor` feed, why rejected: "bad payload: value: Input should be a valid number", with the raw message.

## 6. Simulate an AI outage, then press **+50** → minute 250

Turn on **Simulate AI outage** in the sidebar. The line below changes to "AI: off: simulated outage (FailingClient)". Then press **+50**.

**You see** (INC-0008 detail):
- **Minute 213:** INC-0008 is acknowledged but still showing problems 15 minutes after its last alert, so Avery Lin (P-01) gets a **still active** reminder (persistent). Its new AI diagnosis **DX-00005** is **⚠️ AI unavailable: alert sent without it (unavailable)**, reason "LLM unavailable: LLM disabled (kill switch)". The reminder went out anyway.
- **Product batches (lots) at risk:** LOT-0030 and **LOT-0037**. LOT-0037 started on T-01 at minute 233, during the incident, and was marked on that minute with no new alert.

**Say:** the AI is out, and the alerts still go out.

## 7. Press **+1** four times → minute 254: the drift leaves the allowed range

**You see** (INC-0008):
- **Outside allowed range (beyond_spec)**, level **Urgent** (got worse from Alert, still the same incident).
- **Hold recommended:** urgent problem with product batches at risk: LOT-0030, LOT-0037.
- Avery Lin (P-01) gets a **got worse** alert (upgraded) at minute 254.
- AI diagnosis **DX-00006**: **AI unavailable** (outage still on). The level and the hold recommendation came from the rule, not from the AI.
- Sensor chart: the reading at minute 254 crosses the red upper **allowed range** line.

## 8. Confirm the hold

On INC-0008, press **Confirm hold** (acting as Avery Lin (P-01)).

**You see:** "INC-0008: Someone is on it → Product on hold (acknowledged → hold_confirmed)". The page stays on INC-0008. **Product batches on hold: 2** (LOT-0030, LOT-0037), and the Activity tab says "Avery Lin (P-01) put 2 product batches (lots) on hold for INC-0008: LOT-0030, LOT-0037."

**Say:** the system recommended the hold; a person confirmed it.

## 9. Turn the AI outage off, then press **+50** three times → minute 404

**You see:**
- Etch Tool 5 (T-05): **All sensors monitoring** again. On the S-05-TEMP chart, the grey band runs **exactly from minute 200 to minute 320**, the real length of that learning period. The Activity tab says T-05 finished learning the new normal at minute 320.
- No incident on T-05 during relearning. Between minutes 320 and 404, three watch-list items opened and closed automatically on T-05 against the new normal: INC-0012 S-05-PRES (opened at 337), INC-0014 S-05-RF (339) and INC-0015 S-05-RF (367). Nobody was paged. By minute 404 they have closed, so the watch list no longer shows them and T-05's card reads **No open problems** (they're still in the Activity tab).
- INC-0008's product batches at risk keep growing as new batches start on T-01: LOT-0043, LOT-0049, LOT-0052 and LOT-0058 are marked at risk. Only the two batches at risk when the hold was confirmed are on hold. A person would confirm the newer ones separately.

## With the real model

These parts can differ when the dashboard runs with Claude instead of scripted responses:
- **The diagnosis at minute 198 (DX-00004):** its wording, confidence, and which IDs it cites. The real model (run 1 in `eval_results/`) also cited readings next to M-0001, and it could abstain. The ✅/❌ marks still come from code.
- **Calls take a few seconds each.** A **+50** press waits for every diagnosis in those minutes, including the three false-alarm Alerts in step 2.
- **Unchanged:** every minute, incident ID, rule, level, batch, alert and hold above, and steps 6–7, where the AI is off anyway.
