from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from xul_slackbot.user_context_export import slugify


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def sanitize_table_name(value: str) -> str:
    return slugify(value).replace(".", "_")


def build_summon_table_name(slack_name: str, github_login: str) -> str:
    return f"summon_{sanitize_table_name(slack_name)}_{sanitize_table_name(github_login)}"


def load_slack_context_documents(path: str | Path) -> list[dict[str, str]]:
    conn = _connect_sqlite(Path(path))
    try:
        target = conn.execute(
            "SELECT value FROM metadata WHERE key = 'target_user_id'"
        ).fetchone()
        target_user_id = target["value"] if target is not None else ""
        channels = {
            row["channel_id"]: row["name"]
            for row in conn.execute("SELECT channel_id, name FROM channels")
        }
        rows = conn.execute(
            """
            SELECT
                c.context_key,
                c.context_type,
                c.channel_id,
                c.anchor_ts,
                c.match_reason,
                m.ts,
                m.user_id,
                m.text,
                m.is_direct_match
            FROM contexts AS c
            JOIN messages AS m
              ON m.context_key = c.context_key
            ORDER BY c.context_key, CAST(m.ts AS REAL)
            """
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[sqlite3.Row]] = {}
    metadata: dict[str, sqlite3.Row] = {}
    for row in rows:
        grouped.setdefault(row["context_key"], []).append(row)
        metadata[row["context_key"]] = row

    documents: list[dict[str, str]] = []
    for context_key, messages in grouped.items():
        head = metadata[context_key]
        channel_name = channels.get(head["channel_id"]) or head["channel_id"]
        lines = [
            f"Slack context for user {target_user_id}",
            f"channel={channel_name}",
            f"context_type={head['context_type']}",
            f"match_reason={head['match_reason']}",
        ]
        for message in messages:
            marker = "target" if message["is_direct_match"] else "context"
            author = message["user_id"] or "unknown"
            lines.append(f"[{message['ts']}] {marker} {author}: {message['text']}")
        documents.append(
            {
                "doc_id": f"slack:{context_key}",
                "source": "slack",
                "context_ref": context_key,
                "title": f"Slack {channel_name} {head['match_reason']}",
                "text": "\n".join(lines),
                "searchable_text": f"Slack {channel_name} {head['match_reason']}\n" + "\n".join(lines),
                "url": "",
                "timestamp": str(head["anchor_ts"] or ""),
            }
        )
    return documents


def load_github_context_documents(path: str | Path) -> list[dict[str, str]]:
    conn = _connect_sqlite(Path(path))
    try:
        rows = conn.execute(
            """
            SELECT
                c.context_id,
                c.repo,
                c.kind,
                c.number,
                c.title,
                c.state,
                c.html_url,
                c.author_login,
                c.match_reasons,
                e.event_type,
                e.author_login AS event_author_login,
                e.created_at,
                e.body,
                e.matched
            FROM contexts AS c
            JOIN events AS e
              ON e.context_id = c.context_id
            ORDER BY c.context_id, COALESCE(e.created_at, '')
            """
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[sqlite3.Row]] = {}
    metadata: dict[str, sqlite3.Row] = {}
    for row in rows:
        grouped.setdefault(row["context_id"], []).append(row)
        metadata[row["context_id"]] = row

    documents: list[dict[str, str]] = []
    for context_id, events in grouped.items():
        head = metadata[context_id]
        lines = [
            f"GitHub context for repo {head['repo']}",
            f"{head['kind']} #{head['number']} state={head['state'] or '-'}",
            f"title={head['title']}",
            f"author={head['author_login'] or '-'}",
            f"match_reasons={head['match_reasons']}",
        ]
        for event in events:
            marker = "target" if event["matched"] else "context"
            author = event["event_author_login"] or "unknown"
            lines.append(
                f"[{event['created_at'] or '-'}] {marker} {event['event_type']} {author}: {event['body'] or ''}"
            )
        documents.append(
            {
                "doc_id": f"github:{context_id}",
                "source": "github",
                "context_ref": context_id,
                "title": f"GitHub {head['repo']} #{head['number']} {head['title']}",
                "text": "\n".join(lines),
                "searchable_text": (
                    f"GitHub {head['repo']} #{head['number']} {head['title']}\n"
                    + "\n".join(lines)
                ),
                "url": str(head["html_url"] or ""),
                "timestamp": str(events[-1]["created_at"] or ""),
            }
        )
    return documents


def ensure_summon_lancedb_table(
    db: Any,
    slack_name: str,
    github_login: str,
    slack_context_path: str | Path,
    github_context_path: str | Path,
) -> tuple[str, int]:
    table_name = build_summon_table_name(slack_name, github_login)
    documents = load_slack_context_documents(slack_context_path)
    documents.extend(load_github_context_documents(github_context_path))
    if not documents:
        raise ValueError("No context documents found for summoned necromancy.")

    table = db.create_table(table_name, data=documents, mode="overwrite")
    table.create_fts_index("searchable_text", replace=True)
    return table_name, len(documents)


def search_summon_context(
    db: Any, table_name: str, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    table = db.open_table(table_name)
    return table.search(
        query,
        query_type="fts",
        fts_columns="searchable_text",
    ).limit(limit).to_list()


def format_summon_context(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No relevant local context found."
    snippets: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:277] + "..."
        snippets.append(f"[{row.get('source', '-')}] {row.get('title', '-')}: {text}")
    return "\n".join(snippets)


def connect_lancedb(uri: str | Path) -> Any:
    db_path = Path(uri).expanduser().resolve()
    db_path.mkdir(parents=True, exist_ok=True)

    import lancedb

    return lancedb.connect(str(db_path))
