CREATE TABLE IF NOT EXISTS jobs (
  id text PRIMARY KEY,
  source text NOT NULL,
  source_job_id text NOT NULL,
  url text NOT NULL,
  title text NOT NULL,
  company text,
  company_logo_url text,
  location text NOT NULL,
  work_type text,
  salary_min numeric,
  salary_max numeric,
  salary_period text,
  posted_at timestamptz,
  description text NOT NULL,
  canonical_job_id text,
  content_hash text NOT NULL,
  first_seen_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  removed_at timestamptz,
  last_detail_fetched_at timestamptz,
  next_detail_due_at timestamptz,
  source_signal text
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_signal text;
CREATE INDEX IF NOT EXISTS jobs_source_idx ON jobs (source);
CREATE INDEX IF NOT EXISTS jobs_canonical_idx ON jobs (canonical_job_id);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id serial PRIMARY KEY,
  started_at timestamptz NOT NULL,
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS requests (
  id bigserial PRIMARY KEY,
  run_id int,
  ts timestamptz NOT NULL DEFAULT now(),
  source text,
  url text,
  status int,
  bytes bigint,
  blocked boolean NOT NULL DEFAULT false,
  block_reason text,
  attempt int
);

CREATE TABLE IF NOT EXISTS skipped (
  source text NOT NULL,
  source_job_id text NOT NULL,
  reason text,
  at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source, source_job_id)
);
