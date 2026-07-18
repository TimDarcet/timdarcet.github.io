-- Backend for shared gameplay. Run once in the Supabase SQL editor (Project -> SQL editor -> New query).
-- One grow-only table: a room's discovered streets. Roster is Realtime Presence (no table).
-- The anon/public key ships in client JS on purpose; access is gated by Row-Level Security + the
-- CHECK constraints below (they cap what the public key can write), never by secrecy.

create table if not exists public.found (
  room  text not null check (char_length(room) between 1 and 12),
  key   text not null check (char_length(key)  between 1 and 200),
  name  text not null check (char_length(name) between 1 and 120),
  la    double precision not null check (la between -90  and 90),
  lo    double precision not null check (lo between -180 and 180),
  by    text  check (by is null or char_length(by) <= 64),
  color text  check (color is null or color ~ '^#[0-9a-fA-F]{6}$'),
  ts    timestamptz not null default now(),
  primary key (room, key)                 -- grow-only: one row per street per room
);
create index if not exists found_room_idx on public.found (room);   -- history read filters by room

alter table public.found enable row level security;
create policy "read"   on public.found for select using (true);
create policy "append" on public.found for insert with check (true);
-- no update/delete policy => discoveries can never be overwritten or removed

alter publication supabase_realtime add table public.found;         -- live INSERT push
