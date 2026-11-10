CREATE TABLE IF NOT EXISTS recall_components (
    campaign_id TEXT NOT NULL REFERENCES recalls(campaign_id) ON DELETE CASCADE,
    component   TEXT NOT NULL,
    PRIMARY KEY (campaign_id, component)
);
