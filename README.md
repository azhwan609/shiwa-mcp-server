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

The MCP tools `composio_search_social_tools` and `composio_execute_social_tool` discover and execute actions for the authenticated user's own Composio session. Use a Supabase user access token in the MCP Authorization header for these tools; the shared MCP key is not accepted as a substitute for a user's identity.

The connection callback is `https://deep-shiwa-ai-mcp.onrender.com/connect/callback`. The callback closes the popup and posts a status message to the configured frontend origin. The Telegram toolkit uses a bot-token connection flow hosted by Composio; it is not a personal Telegram-account OAuth login.

## Render environment

**Required new setting:** `COMPOSIO_API_KEY` — the Composio project API key, stored only on the server. Copy the existing key from your secret manager into Render's environment settings; never paste it into chat or frontend variables.

The server already has defaults for the current Supabase project, public publishable key, SHIWA Netlify origin, and Render URL. You can override `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `APP_ORIGIN`, and `MCP_PUBLIC_URL` if the deployment URLs change. Keep `MCP_API_KEY` for existing MCP clients.

Do not configure `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_SESSIONID`, or a global `TELEGRAM_BOT_TOKEN` for the Composio connection flow. The old global-account MCP actions are intentionally disabled.

## Local development

Copy `.env.example` to `.env`, install dependencies with `pip install -r requirements.txt`, then run:

```bash
uvicorn main:app --host 0.0.0.0 --port 10000
```

Health check: `/health`. MCP endpoint: `/mcp`.
