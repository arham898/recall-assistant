CREATE TABLE IF NOT EXISTS recalls (
    campaign_id             TEXT PRIMARY KEY,
    manufacturer            TEXT NOT NULL,
    manufacturer_campaign_number TEXT NOT NULL DEFAULT '',
    component               TEXT NOT NULL,
    recall_type             CHAR(1) NOT NULL,
    potential_units_affected INTEGER,
    defect_summary          TEXT NOT NULL DEFAULT '',
    consequence_summary     TEXT NOT NULL DEFAULT '',
    corrective_action       TEXT NOT NULL DEFAULT '',
    notes                   TEXT NOT NULL DEFAULT '',
    report_received_date    DATE,
    record_creation_date    DATE,
    manufacturing_begin_date DATE,
    manufacturing_end_date  DATE,
    owner_notification_date DATE,
    influenced_by           TEXT NOT NULL DEFAULT '',
    do_not_drive            BOOLEAN NOT NULL DEFAULT FALSE,
    park_outside            BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS recall_vehicles (
    id          SERIAL PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES recalls(campaign_id) ON DELETE CASCADE,
    make        TEXT NOT NULL,
    model       TEXT NOT NULL,
    year        INTEGER,
    UNIQUE (campaign_id, make, model, year)
);

CREATE INDEX IF NOT EXISTS idx_recall_vehicles_make ON recall_vehicles (make);
CREATE INDEX IF NOT EXISTS idx_recall_vehicles_model ON recall_vehicles (model);
CREATE INDEX IF NOT EXISTS idx_recall_vehicles_year ON recall_vehicles (year);
CREATE INDEX IF NOT EXISTS idx_recall_vehicles_make_model ON recall_vehicles (make, model);
