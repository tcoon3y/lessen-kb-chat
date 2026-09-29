# Lessen Pro Knowledge Bot

A private web chat that answers Lessen Pro questions from Confluence. Each question goes to Claude, which searches and reads pages from the allowed Confluence spaces (read-only) and replies with a short answer plus linked source pages. People can request missing docs or report wrong answers; both add a row to the Documentation Requests table in Confluence, the only page the bot can write to. Chat history lives only in the browser tab. It runs on Railway behind a shared passcode.

## Runbook

All settings live in Railway: open the project → the service → **Variables**. Saving a variable redeploys the app (about 2 minutes). Usage counts reset on each redeploy.

### Change which Confluence spaces it reads
1. Find the space key in a page URL: `…/wiki/spaces/<KEY>/pages/…`
2. Edit `ALLOWED_SPACES` to a comma-separated list of keys, e.g. `VendorSaaS,TCN`.
3. Anything outside these spaces is treated as not found, even when asked for by name.

### Rotate keys
- **Anthropic:** console.anthropic.com → API Keys → create a key → paste into `ANTHROPIC_API_KEY` → delete the old key.
- **Atlassian:** id.atlassian.com → Security → API tokens → **Create API token** (not "with scopes") → paste into `CONFLUENCE_API_TOKEN` → revoke the old token. Atlassian tokens expire; if the bot says "Can't connect to Confluence", this is the fix. `CONFLUENCE_EMAIL` must be the email of the token's account.
- **Passcode:** change `APP_PASSCODE` and tell the team the new one.

### Edit how the bot answers
Edit `app/prompts/system.md` on GitHub (pencil icon → Commit). Railway redeploys automatically. Keep the "I couldn't find this documented in Confluence." sentence exactly as written; the Request docs button depends on it.

### Change the starter questions
Set `SUGGESTED_QUESTIONS` to up to three questions separated by `|`. Once a question has been asked at least twice with a cited answer, it replaces a starter automatically.

### Check costs
- **Quick view:** the **Usage** page in the app (estimates, since the last restart).
- **Exact billing:** console.anthropic.com → Usage / Billing. Keep a monthly spend limit set there; when it's hit, the bot shows "Monthly usage limit reached".
- **Railway:** railway.com → project → Usage (Hobby plan, about $5/month).
- Estimates assume $3 / $15 per million input/output tokens. Override with `PRICE_INPUT_PER_MTOK` and `PRICE_OUTPUT_PER_MTOK`.

### Read the logs
Railway → service → **Deployments** → Active → **View logs**. One line per question with outcome, latency, tool calls and token counts. Question and answer text are never logged. Error lines name the problem (`confluence_login`, `claude_limit`, `rate_limited`, …).

### Limits
- 20 questions per 10 minutes per person (per IP address, or per email when behind Cloudflare Access).
- At most 6 Confluence searches/page reads per question; pages are capped at 20,000 characters.

## Settings

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (secret) |
| `CLAUDE_MODEL` | Model name, default `claude-sonnet-5-5` |
| `CONFLUENCE_BASE_URL` | `https://lessen.atlassian.net/wiki` |
| `CONFLUENCE_EMAIL` | Email of the Atlassian account the token belongs to |
| `CONFLUENCE_API_TOKEN` | Atlassian API token (secret) |
| `ALLOWED_SPACES` | Space keys the bot may read |
| `AUTH_MODE` | `passcode` or `cloudflare` |
| `APP_PASSCODE` | Team passcode (secret) |
| `DOCS_REQUEST_PAGE_ID` | Page ID of the Documentation Requests table |
| `SUGGESTED_QUESTIONS` | Optional starter questions, separated by `|` |
| `PRICE_INPUT_PER_MTOK`, `PRICE_OUTPUT_PER_MTOK` | Optional cost-estimate prices |
| `ANTHROPIC_MONTHLY_BUDGET` | Optional monthly Claude budget (USD) for the Usage page |

## Run locally

```
copy .env.example .env    # then fill in values yourself
uv run uvicorn app.main:app --port 8000
uv run pytest             # automated tests
uv run python -m tests.run_eval   # live test questions
```
