CREATE TABLE IF NOT EXISTS heart_rate (
    date TEXT PRIMARY KEY,
    resting_hr INTEGER,
    zones TEXT,
    provider TEXT,
    calculation_method TEXT,
    data_source TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    calories_out INTEGER,
    active_minutes INTEGER,
    very_active_minutes INTEGER,
    fairly_active_minutes INTEGER,
    lightly_active_minutes INTEGER,
    sedentary_minutes INTEGER,
    floors INTEGER,
    distance_km REAL,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS exercises (
    log_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    name TEXT,
    duration_min INTEGER,
    calories INTEGER,
    avg_hr INTEGER,
    steps INTEGER,
    distance_km REAL,
    distance_unit TEXT,
    start_time TEXT,
    source TEXT,
    log_type TEXT,
    provider TEXT,
    end_time TEXT,
    start_utc_offset TEXT,
    end_utc_offset TEXT,
    active_seconds REAL,
    notes TEXT,
    create_time TEXT,
    update_time TEXT,
    metrics_summary TEXT,
    exercise_metadata TEXT,
    exercise_events TEXT,
    splits TEXT,
    split_summaries TEXT,
    data_source TEXT
);

CREATE INDEX IF NOT EXISTS idx_exercises_date ON exercises(date);

CREATE TABLE IF NOT EXISTS sleep (
    date TEXT PRIMARY KEY,
    total_minutes INTEGER,
    efficiency INTEGER,
    start_time TEXT,
    end_time TEXT,
    deep_minutes INTEGER,
    light_minutes INTEGER,
    rem_minutes INTEGER,
    wake_minutes INTEGER,
    sessions INTEGER,
    sleep_period_minutes INTEGER,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS weight (
    date TEXT PRIMARY KEY,
    weight_kg REAL,
    bmi REAL,
    fat_pct REAL,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS spo2 (
    date TEXT PRIMARY KEY,
    avg REAL,
    min REAL,
    max REAL,
    avg_ci_low REAL,
    avg_ci_high REAL,
    provider TEXT,
    std_dev REAL,
    data_source TEXT
);

CREATE TABLE IF NOT EXISTS hrv (
    date TEXT PRIMARY KEY,
    daily_rmssd REAL,
    deep_rmssd REAL,
    provider TEXT,
    entropy REAL,
    non_rem_hr INTEGER,
    data_source TEXT
);

CREATE TABLE IF NOT EXISTS azm (
    date TEXT PRIMARY KEY,
    total_minutes INTEGER,
    fat_burn_minutes INTEGER,
    cardio_minutes INTEGER,
    peak_minutes INTEGER,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS breathing_rate (
    date TEXT PRIMARY KEY,
    breaths_per_min REAL,
    provider TEXT,
    data_source TEXT
);

CREATE TABLE IF NOT EXISTS skin_temperature (
    date TEXT PRIMARY KEY,
    nightly_relative REAL,
    log_type TEXT,
    nightly_absolute REAL,
    baseline REAL,
    provider TEXT,
    nightly_stddev_30d REAL,
    data_source TEXT
);

CREATE TABLE IF NOT EXISTS core_temperature (
    datetime TEXT NOT NULL,
    date TEXT NOT NULL,
    temp_celsius REAL,
    provider TEXT,
    reading_id TEXT,
    measurement_location TEXT,
    data_source TEXT,
    PRIMARY KEY (datetime, temp_celsius)
);

CREATE INDEX IF NOT EXISTS idx_core_temperature_date ON core_temperature(date);

CREATE TABLE IF NOT EXISTS cardio_fitness (
    date TEXT PRIMARY KEY,
    vo2_max_low REAL,
    vo2_max_high REAL,
    vo2_max REAL,
    provider TEXT,
    cardio_fitness_level TEXT,
    estimated INTEGER,
    vo2_max_covariance REAL,
    data_source TEXT
);

CREATE TABLE IF NOT EXISTS food_log (
    date TEXT PRIMARY KEY,
    calories_in INTEGER,
    water_ml REAL,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS ecg (
    reading_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    start_time TEXT,
    avg_bpm INTEGER,
    classification TEXT,
    lead_number INTEGER,
    sampling_hz INTEGER,
    scaling_factor INTEGER,
    duration_sec REAL,
    waveform TEXT,
    device_model TEXT,
    provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_ecg_date ON ecg(date);

CREATE TABLE IF NOT EXISTS irn (
    alert_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    alert_windows TEXT,
    device_model TEXT,
    provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_irn_date ON irn(date);

CREATE TABLE IF NOT EXISTS account (
    resource TEXT PRIMARY KEY,
    body TEXT,
    fetched_at TEXT,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS height (
    datetime TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    height_mm INTEGER,
    provider TEXT
);

CREATE TABLE IF NOT EXISTS exercise_routes (
    log_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    tcx TEXT,
    provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_exercise_routes_date ON exercise_routes(date);

CREATE TABLE IF NOT EXISTS sleep_sessions (
    session_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    record TEXT,
    provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_sleep_sessions_date ON sleep_sessions(date);

CREATE TABLE IF NOT EXISTS weight_readings (
    reading_id TEXT PRIMARY KEY,
    datetime TEXT,
    date TEXT NOT NULL,
    record TEXT,
    provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_weight_readings_date ON weight_readings(date);

CREATE TABLE IF NOT EXISTS body_fat_readings (
    reading_id TEXT PRIMARY KEY,
    datetime TEXT,
    date TEXT NOT NULL,
    record TEXT,
    provider TEXT
);

CREATE INDEX IF NOT EXISTS idx_body_fat_readings_date ON body_fat_readings(date);

CREATE TABLE IF NOT EXISTS authorisation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    missing_scopes TEXT,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT NOT NULL,
    data_type TEXT NOT NULL,
    status TEXT NOT NULL,
    records_added INTEGER,
    notes TEXT,
    last_date_attempted TEXT
);
