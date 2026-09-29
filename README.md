# Lessen Pro KB Chat

A private web chat that answers Lessen Pro questions from Confluence. A FastAPI app sends each question to Claude (Anthropic API), which searches and reads pages from an allow-listed set of Confluence spaces (read-only) and replies with a short answer plus linked source pages. Chat history lives only in the browser tab; there is no database. It is hosted on Railway behind a passcode or Cloudflare Access login gate.

## Run locally

```
cp .env.example .env   # then fill in values yourself
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
```
