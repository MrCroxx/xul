# Xul

The necromancer who can “revive” resigned employees.

![Xul](assets/xul.png)

## Slack Bot

Install dependencies with `uv sync`, then start the bot with `uv run slack-bot`.

You can set the LanceDB local storage directory when the bot starts:

```bash
uv run slack-bot --lancedb-dir ./data/lancedb
```

If the directory does not exist, the bot will create it before connecting to LanceDB.

Required environment variables:

- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`

Optional environment variables:

- `LANCEDB_DIR`
