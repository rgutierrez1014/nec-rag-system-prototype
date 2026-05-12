-- depends: 0001_initial

ALTER TABLE practices ADD COLUMN IF NOT EXISTS neighborhood VARCHAR(100) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS practices_neighborhood_idx ON practices (neighborhood);
