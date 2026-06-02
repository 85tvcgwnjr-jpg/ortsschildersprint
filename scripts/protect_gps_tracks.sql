-- ════════════════════════════════════════════════════════════════════════════
-- GPS-Tracks schützen (Option 2)
-- ════════════════════════════════════════════════════════════════════════════
-- Ziel:
--   • gps_track in der rides-Tabelle ist NUR noch für den Eigentümer lesbar.
--   • Fahrt-Metadaten (device_id, Zeiten) bleiben öffentlich über eine View,
--     damit der "Aus Fahrten auswählen"-Picker funktioniert.
--   • crossings + profiles bleiben öffentlich lesbar (für die Ranglisten).
--
-- Im Supabase SQL Editor ausführen.
-- ════════════════════════════════════════════════════════════════════════════

-- ── 1) profiles: öffentlich lesbar (anon + authenticated) ──────────────────────
-- Bisher nur "to anon" → eingeloggte Nutzer (Rolle authenticated) sahen nichts.
drop policy if exists "anon read profiles"   on profiles;
drop policy if exists "public read profiles" on profiles;
create policy "public read profiles"
    on profiles for select to public using (true);

-- ── 2) crossings: öffentlich lesbar (für Ranglisten über alle Geräte) ─────────
drop policy if exists "anon read crossings"   on crossings;
drop policy if exists "public read crossings" on crossings;
create policy "public read crossings"
    on crossings for select to public using (true);

-- ── 3) rides: ALLE bisherigen SELECT-Policies entfernen (versteckt gps_track) ──
-- Robust per Schleife, da der genaue Policy-Name unbekannt ist.
do $$
declare pol record;
begin
  for pol in
    select policyname from pg_policies
    where schemaname = 'public' and tablename = 'rides' and cmd = 'SELECT'
  loop
    execute format('drop policy %I on rides', pol.policyname);
  end loop;
end $$;

-- Nur der Eigentümer darf seine eigene Fahrt (inkl. gps_track) lesen.
-- Die device_id steht in den user_metadata des Login-Tokens (JWT).
create policy "owner reads own rides"
    on rides for select to authenticated
    using ( device_id = (auth.jwt() -> 'user_metadata' ->> 'device_id') );

-- ── 4) Öffentliche View mit Fahrt-Metadaten OHNE gps_track ────────────────────
-- Läuft als Definer (Standard) → umgeht die rides-RLS, gibt aber nur die
-- unkritischen Spalten preis. Für Picker + "andere finden ihre Fahrt".
create or replace view rides_public as
    select id, device_id, started_at, ended_at, created_at
    from rides;

grant select on rides_public to anon, authenticated;

-- ── 5) Löschen: nur eingeloggt + nur eigene Fahrten/Crossings ─────────────────
-- Alle bisherigen DELETE-Policies (z. B. offenes "to anon") entfernen …
do $$
declare pol record;
begin
  for pol in
    select policyname, tablename from pg_policies
    where schemaname = 'public' and tablename in ('rides','crossings') and cmd = 'DELETE'
  loop
    execute format('drop policy %I on %I', pol.policyname, pol.tablename);
  end loop;
end $$;

-- … und durch Eigentümer-Regeln ersetzen (device_id aus dem Login-Token).
create policy "owner deletes own rides"
    on rides for delete to authenticated
    using ( device_id = (auth.jwt() -> 'user_metadata' ->> 'device_id') );

create policy "owner deletes own crossings"
    on crossings for delete to authenticated
    using ( device_id = (auth.jwt() -> 'user_metadata' ->> 'device_id') );

-- ── 6) PostgREST-Schema-Cache neu laden, damit die View sofort verfügbar ist ──
notify pgrst, 'reload schema';
