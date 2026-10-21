-- Supports idempotent upserts for each user's Instagram account.
-- The current project has no integration rows; reconcile duplicates before applying elsewhere.
create unique index if not exists integrations_user_provider_external_id_uidx
  on public.integrations (user_id, provider, external_id);

-- OAuth state nonces are persisted and atomically consumed to prevent callback replay.
create table if not exists public.instagram_oauth_states (
  nonce text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists instagram_oauth_states_expires_at_idx
  on public.instagram_oauth_states (expires_at);

alter table public.instagram_oauth_states enable row level security;
revoke all on table public.instagram_oauth_states from anon, authenticated;
grant all on table public.instagram_oauth_states to service_role;
