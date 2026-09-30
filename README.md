# Lessen Pro Knowledge Bot

A private web chat that answers Lessen Pro questions from Confluence. It has three chats: **Customer success** (Confluence only), **Product** (Confluence plus read-only Jira search in project LP) and **User feedback** (Notion user research, the CS feedback sheet, and Jira LP + LPH). Each question goes to Claude, which searches and reads pages from the allowed Confluence spaces (read-only) and replies with a short answer plus linked source pages. People can request missing docs or report wrong answers; both add a row to the Documentation Requests table in Confluence, the only page the bot can write to. With a Railway Postgres database attached, questions, answers, sources, timing, tokens and cost are saved for a year (then the text is cleared, counts are kept); without one, nothing is stored. It runs on Railway behind a shared passcode.

## Runbook

All settings live in Railway: open the project → the service → **Variables**. Saving a variable redeploys the app (about 2 minutes). Usage counts reset on each redeploy.

### Change which Confluence spaces it reads
1. Find the space key in a page URL: `…/wiki/spaces/<KEY>/pages/…`
2. Edit `ALLOWED_SPACES` to a comma-separated list of keys, e.g. `VendorSaaS,TCN`.
3. Anything outside these spaces is treated as not found, even when asked for by name.

### Change which Jira projects the Product chat reads
Edit `JIRA_PROJECTS` (default `LP`), e.g. `LP,LPH`. Tickets in other projects are treated as not found. Jira uses the same Atlassian email and token as Confluence. Test locally with `uv run python -m app.jira "autopay"`.

### User feedback chat
- **Notion:** create an internal integration at notion.so/profile/integrations (read content only), copy its secret into `NOTION_TOKEN`, then in Notion open **User Research** → ••• → **Connections** → add the integration. Only that page and its sub-pages are read (`NOTION_ROOT_PAGE_ID`). The bot re-reads them every 30 minutes. Test locally with `uv run python -m app.notion "scheduling"`.
- **Feedback sheet:** a daily scheduled Claude task (6:52am Central) copies `Lessen Pro Feedback.xlsx` into the Confluence page `FEEDBACK_PAGE_ID` ("Lessen Pro Feedback (synced from Excel)" in TCN). Edit the Excel file, not that page. The CS and Product chats don't search it.
- **Jira:** this chat searches `JIRA_PROJECTS_FEEDBACK` (default `LP,LPH`).
- Edit its instructions in `app/prompts/feedback.md`.

### Database (saved questions, answers and usage)
- **Set up:** in the Railway project click **+ Create → Database → PostgreSQL**. Then open the app service → **Variables** → **+ New Variable** → **Add Reference** → choose `DATABASE_URL` from Postgres. Deploy. Tables are created automatically on start.
- **What's saved:** `chats` (question, answer, sources, chat mode, outcome, time taken, tokens, estimated cost, a per-tab conversation id), `events` (doc requests, not-documented requests, incorrect-answer reports) and `api_calls` (daily Atlassian/Notion call counts).
- **Retention:** question/answer text is cleared after `RETENTION_DAYS` (default 365); counts and costs stay.
- **Browse it:** Railway → Postgres → **Data** tab, or connect any SQL client with the connection details shown there.
- If the database is down, the bot keeps answering; it just stops saving until it's back.

### Rotate keys
- **Anthropic:** console.anthropic.com → API Keys → create a key → paste into `ANTHROPIC_API_KEY` → delete the old key.
- **Atlassian:** id.atlassian.com → Security → API tokens → **Create API token** (not "with scopes") → paste into `CONFLUENCE_API_TOKEN` → revoke the old token. Atlassian tokens expire; if the bot says "Can't connect to Confluence", this is the fix. `CONFLUENCE_EMAIL` must be the email of the token's account.
- **Passcode:** change `APP_PASSCODE` and tell the team the new one.

### Edit how the bot answers
Edit `app/prompts/system.md` (both chats) or `app/prompts/product.md` (Product chat only) on GitHub (pencil icon → Commit). Railway redeploys automatically. Keep the "I couldn't find this documented in Confluence." sentence exactly as written; the Request docs button depends on it.

### Change the starter questions
Set `SUGGESTED_QUESTIONS` (Customer success) and `SUGGESTED_QUESTIONS_PRODUCT` (Product) to up to three questions each, separated by `|`. Once a question has been asked at least twice with a cited answer, it replaces a starter automatically.

### Check costs
- **Quick view:** the **Usage** page in the app (estimates; the whole month when the database is attached, otherwise since the last restart).
- **Exact billing:** console.anthropic.com → Usage / Billing. Keep a monthly spend limit set there; when it's hit, the bot shows "Monthly usage limit reached".
- **Railway:** railway.com → project → Usage (Hobby plan, about $5/month).
- Estimates assume $3 / $15 per million input/output tokens. Override with `PRICE_INPUT_PER_MTOK` and `PRICE_OUTPUT_PER_MTOK`.

### Read the logs
Railway → service → **Deployments** → Active → **View logs**. One line per question with outcome, latency, tool calls and token counts. Question and answer text never go to the logs (they're only in the database). Error lines name the problem (`confluence_login`, `claude_limit`, `rate_limited`, …).

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
| `SUGGESTED_QUESTIONS`, `SUGGESTED_QUESTIONS_PRODUCT`, `SUGGESTED_QUESTIONS_FEEDBACK` | Optional starter questions per chat, separated by `|` |
| `JIRA_PROJECTS` | Jira projects the Product chat may search, default `LP` |
| `NOTION_TOKEN` | Notion integration secret for the User feedback chat (secret) |
| `NOTION_ROOT_PAGE_ID` | Notion page tree to read, default User Research |
| `FEEDBACK_PAGE_ID` | Confluence page holding the synced feedback sheet |
| `JIRA_PROJECTS_FEEDBACK` | Jira projects the User feedback chat may search, default `LP,LPH` |
| `PRICE_INPUT_PER_MTOK`, `PRICE_OUTPUT_PER_MTOK` | Optional cost-estimate prices |
| `ANTHROPIC_MONTHLY_BUDGET` | Optional monthly Claude budget (USD) for the Usage page |
| `DATABASE_URL` | Postgres connection (Railway reference variable); empty = nothing stored |
| `RETENTION_DAYS` | Days to keep question/answer text, default 365 |

## Run locally

```
copy .env.example .env    # then fill in values yourself
uv run uvicorn app.main:app --port 8000
uv run pytest             # automated tests
uv run python -m tests.run_eval   # live test questions
```
