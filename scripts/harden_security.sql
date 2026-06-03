-- ════════════════════════════════════════════════════════════════════════════
-- Sicherheits-Härtung (Audit-Fixes #1, #4 + Cleanup)
-- Im Supabase SQL Editor ausführen.
-- ════════════════════════════════════════════════════════════════════════════

-- ── #1: Profile-Schreibrecht schließen ───────────────────────────────────────
-- Bisher durfte JEDER mit dem öffentlichen anon-Key beliebige Profile anlegen/
-- ändern (Namen-Vandalismus + XSS-Einschleusung). Schreiben jetzt nur noch
-- eingeloggt + nur das eigene Profil (device_id muss zur Login-Token-ID passen).
-- Garmin schreibt nie Profile; die Edge Functions nutzen Service-Role (umgeht RLS).
drop policy if exists "anon upsert profiles"  on profiles;
drop policy if exists "anon update profiles"  on profiles;
drop policy if exists "owner writes own profile"  on profiles;
drop policy if exists "owner updates own profile" on profiles;

create policy "owner writes own profile"
    on profiles for insert to authenticated
    with check ( device_id = (auth.jwt() -> 'user_metadata' ->> 'device_id') );

create policy "owner updates own profile"
    on profiles for update to authenticated
    using  ( device_id = (auth.jwt() -> 'user_metadata' ->> 'device_id') );

-- ── Cleanup: Test-Eintrag aus dem Audit entfernen ───────────────────────────
delete from profiles where device_id = 'XSS_TEST_PROBE';

-- ── #2: crossings direkte Schreibrechte schließen ────────────────────────────
-- Crossings kommen jetzt ausschließlich über die upload-ride Edge Function
-- (Service-Role). Der direkte anon-REST-Zugriff ermöglicht Fake-Ranglisten.
drop policy if exists "anon insert crossings" on crossings;
-- Service-Role (Edge Function) umgeht RLS automatisch → keine neue Policy nötig.

-- ── #4: Brute-Force-Schutz für claim-device ─────────────────────────────────
-- Speichert Fehlversuche pro Gerät. Keine RLS-Policy → nur die Edge Function
-- (Service-Role) kann lesen/schreiben. 6-stelliger Code + Sperre nach 5 Fehlern.
create table if not exists claim_attempts (
    device_id    text primary key,
    fails        int  not null default 0,
    locked_until timestamptz
);
alter table claim_attempts enable row level security;
-- absichtlich KEINE Policy: für anon/authenticated komplett gesperrt.

notify pgrst, 'reload schema';
