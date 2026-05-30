-- Full database setup for Ortsschilder-Sprint.
-- Run once in the Supabase SQL Editor.
-- Safe to re-run (uses IF NOT EXISTS / IF NOT EXISTS guards).

-- ── signs ────────────────────────────────────────────────────────────────────
-- Pre-imported from OSM via scripts/import_signs.py (monthly GitHub Actions job).
-- Garmin reads via anon key with bbox filter.

create table if not exists signs (
    id         text        primary key,   -- "osm_12345678" or "place_N"
    name       text        not null,      -- town name, e.g. "Köln"
    lat        float8      not null,
    lon        float8      not null,
    updated_at timestamptz not null default now()
);

create index if not exists signs_lat_lon on signs (lat, lon);

alter table signs enable row level security;

create policy "anon read signs"
    on signs for select to anon using (true);


-- ── crossings ────────────────────────────────────────────────────────────────
-- One row per sign crossing per device.
-- Garmin inserts via anon key after each crossing.
-- Used for server-side ranking (query by sign_id + timestamp window).

create table if not exists crossings (
    id         bigserial   primary key,
    sign_id    text        not null,      -- matches signs.id
    sign_name  text        not null,
    timestamp  bigint      not null,      -- Unix epoch seconds (Time.now().value())
    device_id  text        not null,      -- Garmin device unique identifier
    created_at timestamptz not null default now()
);

-- Index for ranking query: sign_id + timestamp range
create index if not exists crossings_sign_ts on crossings (sign_id, timestamp);

alter table crossings enable row level security;

-- Garmin can insert and read (needed for ranking fetch)
create policy "anon insert crossings"
    on crossings for insert to anon with check (true);

create policy "anon read crossings"
    on crossings for select to anon using (true);


-- ── rides ────────────────────────────────────────────────────────────────────
-- One row per completed activity (uploaded after timer stop).
-- gps_track: array of [lat, lon, unix_ts] sampled every 30 s.

create table if not exists rides (
    id          bigserial   primary key,
    device_id   text        not null,
    started_at  bigint      not null,     -- Unix epoch seconds
    ended_at    bigint      not null,     -- Unix epoch seconds
    gps_track   jsonb       not null,     -- [[lat, lon, ts], ...]
    created_at  timestamptz not null default now()
);

alter table rides enable row level security;

create policy "anon insert rides"
    on rides for insert to anon with check (true);
