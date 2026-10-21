# shiwa-mcp-server

DEEP-SHIWA-AI — FastAPI and MCP server for SHIWA Creator Hub.

## Social account connections through Composio

Instagram and Telegram connection links are generated through Composio's hosted connection flow. Each connection uses the authenticated Supabase user's stable UUID as the Composio user ID, so connections are scoped per SHIWA user. The server does not need Meta app credentials, Instagram passwords, or Telegram bot tokens.

The frontend uses:
- `GET /connect/instagram/start`
- `GET /connect/instagram/accounts`
- `DELETE /connect/instagram/accounts/{connected_account_id}`
- `GET /connect/telegram/start`
- `GET /connect/telegram/accounts`
- `DELETE /connect/telegram/accounts/{connected_account_id}`
- `POST /connect/execute` for user-authenticated, provider-scoped tool execution

The connection callback is `https://deep-shiwa-ai-mcp.onrender.com/connect/callback`. The callback closes the popup and posts a status message to the configured frontend origin. The Telegram toolkit uses a bot-token connection flow hosted by Composio; it is not a personal Telegram-account OAuth login.

## Render environment

Set these server-side environment variables:
- `MCP_API_KEY` — existing key for the MCP transport.
- `SUPABASE_URL` — your Supabase project URL.
- `SUPABASE_ANON_KEY` — Supabase publishable/anon key used only to validate the signed-in user's session.
- `COMPOSIO_API_KEY` — the Composio project API key. Keep it server-side; never put it in the frontend.
- `APP_ORIGIN` — exact deployed SHIWA frontend origin.
- `MCP_PUBLIC_URL` — public Render URL, normally `https://deep-shiwa-ai-mcp.onrender.com`.

Do not configure `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_SESSIONID`, or a global `TELEGRAM_BOT_TOKEN` for the Composio connection flow. The old global-account MCP actions are intentionally disabled.

## Local development

Copy `.env.example` to `.env`, install dependencies with `pip install -r requirements.txt`, then run:

```bash
uvicorn main:app --host 0.0.0.0 --port 10000
```

Health check: `/health`. MCP endpoint: `/mcp`.
