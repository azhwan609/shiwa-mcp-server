-- Supports idempotent upserts for each user's Instagram account.
-- Existing duplicate rows, if any, must be reconciled before this index can be created.
create unique index if not exists integrations_user_provider_external_id_uidx
  on public.integrations (user_id, provider, external_id)
  where external_id is not null;