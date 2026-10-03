PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ===== Reference data =====
CREATE TABLE tools (
  tool_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL                 -- e.g. etch, deposition
);

CREATE TABLE sensors (
  sensor_id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  sensor_type TEXT NOT NULL,         -- temperature, pressure, rf_power
  unit TEXT NOT NULL,
  spec_lower REAL NOT NULL,          -- fixed process spec limits; NEVER change at runtime
  spec_upper REAL NOT NULL
);

CREATE TABLE people (
  person_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  available INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE tool_qualifications (
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  person_id TEXT NOT NULL REFERENCES people(person_id),
  PRIMARY KEY (tool_id, person_id)
);

-- ===== Live state (updated by merge only) =====
CREATE TABLE tool_state (
  tool_id TEXT PRIMARY KEY REFERENCES tools(tool_id),
  status TEXT NOT NULL DEFAULT 'healthy',   -- healthy | degraded | offline
  current_recipe_id TEXT
);

CREATE TABLE sensor_state (
  sensor_id TEXT PRIMARY KEY REFERENCES sensors(sensor_id),
  baseline_status TEXT NOT NULL,     -- learning | active | relearning
  baseline_window_start TEXT,        -- when the current learning window began
  baseline_locked_until TEXT,        -- learning window ends at this time
  baseline_activated_at TEXT,        -- when current control limits became active
  control_mean REAL,                 -- FROZEN while active
  control_stddev REAL,               -- FROZEN while active
  last_reading_ts TEXT
);

-- ===== Append-only history =====
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,              -- adapter name
  tool_id TEXT,
  payload TEXT NOT NULL,             -- JSON
  ts TEXT NOT NULL
);

CREATE TABLE readings (
  reading_id TEXT PRIMARY KEY,       -- e.g. RD-000123
  sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  value REAL NOT NULL,
  ts TEXT NOT NULL
);

CREATE TABLE maintenance_log (
  log_id TEXT PRIMARY KEY,           -- e.g. M-0042
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  description TEXT NOT NULL,         -- free text; treated as DATA in prompts
  technician TEXT,
  ts TEXT NOT NULL
);

CREATE TABLE recipe_changes (
  change_id TEXT PRIMARY KEY,        -- e.g. RC-0007
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  recipe_id TEXT NOT NULL,
  ts TEXT NOT NULL
);

CREATE TABLE dead_letter (
  id TEXT PRIMARY KEY,
  source TEXT,
  raw_payload TEXT NOT NULL,
  error_reason TEXT NOT NULL,
  ts TEXT NOT NULL
);

-- ===== Manufacturing context =====
CREATE TABLE lots (
  lot_id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  start_ts TEXT NOT NULL,
  end_ts TEXT,                       -- NULL while running
  status TEXT NOT NULL DEFAULT 'normal'   -- normal | at_risk | held | released
);

-- ===== Outputs =====
CREATE TABLE incidents (
  incident_id TEXT PRIMARY KEY,
  tool_id TEXT NOT NULL,
  sensor_id TEXT NOT NULL,
  rule_fired TEXT NOT NULL,          -- the worst rule seen so far
  severity TEXT NOT NULL,            -- low | medium | high
  onset_ts TEXT,                     -- estimated start of the problem, not detection time
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,              -- open | acknowledged | hold_confirmed | dismissed | resolved | expired
  owner_id TEXT,
  escalation_level INTEGER NOT NULL DEFAULT 0,
  recommend_hold INTEGER NOT NULL DEFAULT 0,
  lots_at_risk TEXT,                 -- JSON list of lot_ids
  latest_diagnosis_id TEXT,
  config_version TEXT NOT NULL,
  dismiss_reason TEXT                -- e.g. false_alarm
);

CREATE TABLE diagnoses (
  diagnosis_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
  status TEXT NOT NULL,              -- diagnosed | abstained | rejected | unavailable
  likely_factors TEXT,               -- JSON list
  cited_evidence TEXT,               -- JSON list of evidence IDs
  confidence TEXT,                   -- low | medium | high | NULL
  rejection_reason TEXT,             -- why: set when status = rejected or unavailable
  evidence_bundle TEXT NOT NULL,     -- JSON: exactly what the model saw
  raw_response TEXT,                 -- JSON list of every raw model response in this run (2 if retried)
  prompt_version TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE notifications (
  notification_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
  person_id TEXT NOT NULL REFERENCES people(person_id),
  reason TEXT NOT NULL,              -- new | upgraded | reactivated | persistent | escalated | reassigned
  escalation_level INTEGER NOT NULL,
  message TEXT NOT NULL,             -- templated, NOT LLM-written
  sent_at TEXT NOT NULL,
  acknowledged_at TEXT
);

CREATE TABLE config_versions (
  version_id TEXT PRIMARY KEY,
  config_json TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  is_active INTEGER NOT NULL,
  rejected_reason TEXT               -- set if validation failed
);

-- ===== Log-only rule firings (e.g. trending): recorded for the dashboard, never open incidents =====
CREATE TABLE logged_firings (
  firing_id TEXT PRIMARY KEY,        -- e.g. LF-000001
  sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
  tool_id TEXT NOT NULL REFERENCES tools(tool_id),
  rule_fired TEXT NOT NULL,
  onset_ts TEXT NOT NULL,
  triggering_reading_ids TEXT NOT NULL,  -- JSON list
  ts TEXT NOT NULL
);

-- ===== Ground truth (written ONLY by the simulator, read ONLY by eval) =====
CREATE TABLE fault_injections (
  fault_id TEXT PRIMARY KEY,
  fault_type TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  sensor_id TEXT,
  start_ts TEXT NOT NULL,
  planted_cause_id TEXT,             -- e.g. M-0042, or NULL if no real cause exists
  expected_outcome TEXT NOT NULL     -- e.g. incident:sustained_run, no_incident, abstain
);

-- ===== Indexes (talking point: same lesson as the Deloitte pipeline) =====
CREATE INDEX idx_readings_sensor_ts ON readings(sensor_id, ts);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_maint_tool_ts ON maintenance_log(tool_id, ts);
CREATE INDEX idx_recipe_tool_ts ON recipe_changes(tool_id, ts);
CREATE INDEX idx_lots_tool_time ON lots(tool_id, start_ts);
CREATE INDEX idx_incidents_sensor_status ON incidents(sensor_id, status);
CREATE INDEX idx_logged_firings_sensor_ts ON logged_firings(sensor_id, ts);
