# Demo script: Fab Tool Health Monitor dashboard

**All data in this demo is synthetic.** Say so at the start: every tool, sensor, person, lot and reading is simulated.

Every minute and ID below was produced by running this exact sequence of button presses headless, with the scripted fake LLM client (`scripts/demo_walkthrough.py`). `tests/test_demo_script.py` replays it and fails if any of these moments moves. Detection, incidents, lots and notifications don't depend on the LLM, so they are the same with the real model. Only the diagnosis contents can differ (see [With the real model](#with-the-real-model) at the end).

## Before you start

```
cd ~/fab-monitor
.venv/bin/streamlit run dashboard/app.py
```

The dashboard uses Claude if `ANTHROPIC_API_KEY` is set, otherwise scripted responses; the sidebar's "AI:" line says which. Time is simulated: one step (tick) is one minute, shown as **Minute**. Every label shows its technical term in parentheses or on hover. Raw IDs that the story doesn't need (diagnosis IDs, evidence record IDs, escalation levels, the settings version, raw rejected data) are in collapsed **Technical details** sections; open them when a step below points there.

Status colors are the same everywhere: 🟢 healthy, 🟠 watch or alert, 🔴 urgent, ⚪ closed. The sidebar opens with a status line (minute, AI on or off, open alerts) and four numbered sections: **1 · Run the simulation**, **2 · Try a scenario**, **3 · Do it yourself** and **4 · People**. Every sidebar action confirms itself with a short pop-up in the corner of the screen, so you can see it worked even when the sidebar covers the page. **Load demo scenario** also turns the AI outage off.

## 1. Open the page (demo scenario loads)

The demo loads by itself, and **Load demo scenario** resets to the same state at any time.

**You see:**
- The page title **Fab Tool Health Monitor** with a one-line description, the **Simulated data** banner, and the **What you're looking at** box (open) explaining the page in four sentences.
- **Minute 150**, **Bad data 0**. The settings version, **2026-10-05-a**, is under the **Technical details** section below the machine cards.
- Machines: every card shows **🟢 Healthy** and "T-0x · monitoring". The 150-minute warm-up went through the event bus, and each sensor learned its normal range from its first 120 readings.
- The **Activity** tab (first tab) lists notable events in plain English, newest first.

**Say:** a drift is planted on Etch Tool 1 (T-01)'s temperature sensor. A maintenance entry, **M-0001** "Replaced chamber heater controller board; thermocouple recalibrated.", is logged at **minute 179**, and the drift starts at **minute 180**. S-04-TEMP is noisy but healthy, and T-03 and T-05 have no faults.

## 2. Press **+50** (sidebar) → minute 200: the drift is caught

**You see:**
- Etch Tool 1's card shows **🟠 1 alert** ("T-01 · monitoring").
- **Activity** tab: "**Minute 198:** Etch Tool 1 (T-01) temperature has been above normal for 9 readings in a row. Alert sent to Avery Lin (P-01). (INC-0008)"
- **Incidents** tab → *Alerts sent to people*: **🟠 Alert**, **INC-0008**, T-01 temperature, **Sustained shift**, detected at minute **198**.
- Select **INC-0008** under *Show details for*:
  - The one-line summary at the top: "Etch Tool 1's temperature has been running high for 10 minutes. Waiting for Avery Lin to respond. 1 product batch may be affected."
  - **Sustained shift: 9 readings in a row on one side of normal (sustained_run)** · level **Alert** · status **Waiting for response (open)**.
  - Started around minute **190** (onset), detected at minute **198**, owner **Avery Lin (P-01)**.
  - **Product batches (lots) at risk:** LOT-0030 (at risk). No hold is recommended yet (it's only an Alert).
  - **What the AI thinks: ✅ AI suggested likely causes (diagnosed)**. On the hosted copy (scripted responses), it also shows "AI revised its answer after the checker rejected it": the scripted first answer cites a made-up record, M-9999, the checker rejects it, and the revised answer cites M-0001. Both attempts are shown. *Evidence it pointed to*: the maintenance note at minute 179, "✓ checked against the records".
  - **Technical details** (open it): AI diagnosis **DX-00004**, and the evidence by record ID: **M-0001** · minute 179 · "✓ checked against the records (verified)".
- **Inboxes** tab → Avery Lin (P-01): **🟠 Alert · Minute 198 · New alert · INC-0008**. Open it for the message; escalation level 0 is in its first line.
- **Sensor chart** (Etch Tool 1 (T-01) temperature (S-01-TEMP)): readings climbing toward the top of the shaded **normal range**, the dark **allowed range** lines, and an amber "problem detected" marker at minute 198.

**Also on screen (be honest about these):** six false alarms on healthy sensors appeared during these 50 minutes.
- **Three Alerts** were sent to a person: INC-0003 S-03-TEMP (167), INC-0004 S-02-RF (172) and INC-0005 S-02-TEMP (179).
- **Three** went on the *Watch list* and nobody was paged: INC-0002 S-02-TEMP (152), INC-0006 S-04-RF (180) and INC-0007 S-04-PRES (185).

**Say:** SPC caught the drift 18 minutes after it began, 56 minutes before any product went out of the allowed range. The AI's suggestion cites real evidence, and code checked every citation.

## 3. Acknowledge INC-0008

In the INC-0008 details, **Acting as** shows **Avery Lin (P-01)**. Press **Acknowledge**.

**You see:** "INC-0008: Waiting for response → Someone is on it (open → acknowledged)". The page stays on the Incidents tab with INC-0008 selected. Escalation for this incident stops.

## 4. Live recipe change on T-05

In the sidebar under **3 · Do it yourself → Switch a recipe**: Machine **Etch Tool 5 (T-05)**, New recipe name **R-05-B** (the defaults). Press **Change recipe**.

**You see:**
- "Etch Tool 5 (T-05) switched to recipe R-05-B: its sensors are learning the new normal (relearning)."
- Etch Tool 5's card: **T-05 · learning new normal** (all three sensors). The change is recorded as **RC-0001** at minute 200.
- **Sensor chart** → pick Etch Tool 5 (T-05) temperature (S-05-TEMP): a grey "learning the new normal" band starts at minute 200.

**Say:** the alarms that compare against normal are paused while the new normal is learned. The allowed-range check still runs.

## 5. Send bad data

Press **Send bad data** (sidebar, *3 · Do it yourself → Garble a message*).

**You see:**
- "Bad data rejected and set aside (dead letter); monitoring kept going."
- **Bad data 1**.
- **Rejected bad data** tab: **DL-000001** · minute 200 · from the `sensor` feed · failed the format check. Its **Technical details** show why it was rejected, "bad payload: value: Input should be a valid number", and the raw message.

## 6. Simulate an AI outage, then press **+50** → minute 250

Turn on **Simulate AI outage** in the sidebar. A pop-up says "AI outage on: new alerts will go out without an AI diagnosis", and the line below changes to "AI: off (simulated outage)". Then press **+50**.

**You see** (INC-0008 detail):
- **Minute 213:** INC-0008 is acknowledged but still showing problems 15 minutes after its last alert, so Avery Lin (P-01) gets a **still active** reminder (persistent). The AI's answer is now **⚠️ AI unavailable: alert sent without it (unavailable)**; its **Technical details** show diagnosis **DX-00005** and the reason "LLM unavailable: LLM disabled (kill switch)". The reminder went out anyway.
- **Product batches (lots) at risk:** LOT-0030 and **LOT-0037**. LOT-0037 started on T-01 at minute 233, during the incident, and was marked on that minute with no new alert.

**Say:** the AI is out, and the alerts still go out.

## 7. Press **+1** four times → minute 254: the drift leaves the allowed range

**You see** (INC-0008):
- **🔴 Urgent** · **Outside allowed range (beyond_spec)**, level **Urgent** (got worse from Alert, still the same incident).
- **Hold recommended:** urgent problem with product batches at risk: LOT-0030, LOT-0037.
- Avery Lin (P-01) gets a **got worse** alert (upgraded) at minute 254.
- AI diagnosis **DX-00006** (under Technical details): **AI unavailable** (outage still on). The level and the hold recommendation came from the rule, not from the AI.
- Sensor chart: the reading at minute 254 crosses the dark upper **allowed range** line.

## 8. Confirm the hold

On INC-0008, press **Confirm hold** (acting as Avery Lin (P-01)).

**You see:** "INC-0008: Someone is on it → Product on hold (acknowledged → hold_confirmed)". The page stays on INC-0008. **Batches held: 2** (LOT-0030, LOT-0037), and the Activity tab says "Avery Lin (P-01) put 2 product batches (lots) on hold for INC-0008: LOT-0030, LOT-0037."

**Say:** the system recommended the hold; a person confirmed it.

## 9. Turn the AI outage off, then press **+50** three times → minute 404

**You see:**
- Etch Tool 5's card: **T-05 · monitoring** again. On the S-05-TEMP chart, the grey band runs **exactly from minute 200 to minute 320**, the real length of that learning period. The Activity tab says T-05 finished learning the new normal at minute 320.
- No incident on T-05 during relearning. Between minutes 320 and 404, three watch-list items opened and closed automatically on T-05 against the new normal: INC-0012 S-05-PRES (opened at 337), INC-0014 S-05-RF (339) and INC-0015 S-05-RF (367). Nobody was paged. By minute 404 they have closed, so the watch list no longer shows them and T-05's card reads **🟢 Healthy** (they're still in the Activity tab).
- INC-0008's product batches at risk keep growing as new batches start on T-01: LOT-0043, LOT-0049, LOT-0052 and LOT-0058 are marked at risk. Only the two batches at risk when the hold was confirmed are on hold. A person would confirm the newer ones separately.

## After the demo: one-click scenarios

These aren't part of the timed demo above (they add to whatever is running, so the minutes and IDs above no longer apply once you press one; **Load demo scenario** starts over). Each one sets up a situation, moves time forward, and pops up what happened and where to look:

| Scenario | What it sets up | Where to look |
|---|---|---|
| Sudden jump | T-03 pressure jumps 2σ (inside the allowed range); 30 minutes | Incidents: caught as a sustained shift |
| Sensor goes silent | T-02 RF power stops reporting; 12 minutes | Incidents: "Sensor stopped reporting", no batches marked |
| Misleading maintenance notes | Drift on T-03 temperature, real cause plus three decoy notes; 60 minutes | Incident details: the AI cites the real note, not the decoys |
| Note that tries to trick the AI | Drift on T-02 pressure; its note contains instructions aimed at the AI; 40 minutes | Incident details: the alert goes out and the note is treated as data |
| Drift with no known cause | Drift on T-04 pressure with nothing in the records; 40 minutes | Incident details: the AI abstains instead of guessing |
| Recipe change + bad reading | T-05 switches recipe, one reading leaves the allowed range while it relearns; 15 minutes | Incidents: an Urgent alert even while learning |
| Someone calls out mid-alert | The owner of an unanswered alert becomes unavailable (starts a problem first if there's none) | Inboxes: the "reassigned" notice |
| Burst of bad data | Four garbled records of different kinds | Rejected bad data: each with its reason |

## With the real model

These parts can differ when the dashboard runs with Claude instead of scripted responses:
- **The diagnosis at minute 198 (DX-00004):** its wording, confidence, and which IDs it cites. The real model (run 1 in `eval_results/`) also cited readings next to M-0001, and it could abstain. The ✅/❌ marks still come from code.
- **Calls take a few seconds each.** A **+50** press waits for every diagnosis in those minutes, including the three false-alarm Alerts in step 2.
- **Unchanged:** every minute, incident ID, rule, level, batch, alert and hold above, and steps 6–7, where the AI is off anyway.
