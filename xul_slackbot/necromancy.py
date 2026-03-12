from __future__ import annotations

import shlex
import sqlite3
from pathlib import Path
from typing import Optional, Sequence


DEFAULT_RESULT_LIMIT = 10


def connect_necromancy_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_necromancy_schema(conn)
    return conn


def init_necromancy_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slack_users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            real_name TEXT NOT NULL,
            email TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS github_users (
            login TEXT PRIMARY KEY,
            issue_or_pr_authored INTEGER NOT NULL DEFAULT 0,
            issue_comments_authored INTEGER NOT NULL DEFAULT 0,
            pr_reviews_authored INTEGER NOT NULL DEFAULT 0,
            pr_review_comments_authored INTEGER NOT NULL DEFAULT 0,
            mentions INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS necromancy_links (
            slack_user_id TEXT NOT NULL UNIQUE,
            github_login TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (slack_user_id, github_login),
            FOREIGN KEY (slack_user_id) REFERENCES slack_users(user_id),
            FOREIGN KEY (github_login) REFERENCES github_users(login)
        );
        CREATE INDEX IF NOT EXISTS necromancy_links_github_idx
        ON necromancy_links (github_login);
        """
    )
    conn.commit()


def search_slack_users(
    conn: sqlite3.Connection, query: str, limit: int = DEFAULT_RESULT_LIMIT
) -> list[sqlite3.Row]:
    normalized = query.strip().lower()
    wildcard = f"%{normalized}%"
    return list(
        conn.execute(
            """
            SELECT
                su.user_id,
                su.username,
                su.display_name,
                su.real_name,
                su.email,
                nl.github_login
            FROM slack_users AS su
            LEFT JOIN necromancy_links AS nl
              ON nl.slack_user_id = su.user_id
            WHERE
                lower(su.user_id) = ?
                OR lower(su.username) = ?
                OR lower(su.email) = ?
                OR lower(su.real_name) = ?
                OR lower(su.display_name) = ?
                OR lower(su.user_id) LIKE ?
                OR lower(su.username) LIKE ?
                OR lower(su.email) LIKE ?
                OR lower(su.real_name) LIKE ?
                OR lower(su.display_name) LIKE ?
            ORDER BY
                CASE
                    WHEN lower(su.user_id) = ? THEN 0
                    WHEN lower(su.username) = ? THEN 1
                    WHEN lower(su.email) = ? THEN 2
                    WHEN lower(su.display_name) = ? THEN 3
                    WHEN lower(su.real_name) = ? THEN 4
                    ELSE 5
                END,
                lower(su.username),
                su.user_id
            LIMIT ?
            """,
            (
                normalized,
                normalized,
                normalized,
                normalized,
                normalized,
                wildcard,
                wildcard,
                wildcard,
                wildcard,
                wildcard,
                normalized,
                normalized,
                normalized,
                normalized,
                normalized,
                limit,
            ),
        )
    )


def search_github_users(
    conn: sqlite3.Connection, query: str, limit: int = DEFAULT_RESULT_LIMIT
) -> list[sqlite3.Row]:
    normalized = query.strip().lower()
    wildcard = f"%{normalized}%"
    return list(
        conn.execute(
            """
            SELECT
                gu.login,
                gu.issue_or_pr_authored,
                gu.issue_comments_authored,
                gu.pr_reviews_authored,
                gu.pr_review_comments_authored,
                gu.mentions,
                nl.slack_user_id
            FROM github_users AS gu
            LEFT JOIN necromancy_links AS nl
              ON nl.github_login = gu.login
            WHERE
                lower(gu.login) = ?
                OR lower(gu.login) LIKE ?
            ORDER BY
                CASE WHEN lower(gu.login) = ? THEN 0 ELSE 1 END,
                lower(gu.login)
            LIMIT ?
            """,
            (normalized, wildcard, normalized, limit),
        )
    )


def resolve_unique_slack_user(conn: sqlite3.Connection, selector: str) -> sqlite3.Row:
    normalized = selector.strip().lower()
    rows = list(
        conn.execute(
            """
            SELECT
                su.user_id,
                su.username,
                su.display_name,
                su.real_name,
                su.email,
                nl.github_login
            FROM slack_users AS su
            LEFT JOIN necromancy_links AS nl
              ON nl.slack_user_id = su.user_id
            WHERE
                lower(su.user_id) = ?
                OR lower(su.username) = ?
                OR lower(su.email) = ?
                OR lower(su.real_name) = ?
                OR lower(su.display_name) = ?
            ORDER BY lower(su.username), su.user_id
            """,
            (normalized, normalized, normalized, normalized, normalized),
        )
    )
    if not rows:
        raise ValueError(f"Slack mecromancy not found: {selector}")
    unique_rows = {row["user_id"]: row for row in rows}
    if len(unique_rows) > 1:
        matches = ", ".join(f"{row['username']}[{row['user_id']}]" for row in unique_rows.values())
        raise ValueError(f"Slack selector is ambiguous: {selector} -> {matches}")
    return next(iter(unique_rows.values()))


def resolve_unique_github_user(conn: sqlite3.Connection, selector: str) -> sqlite3.Row:
    normalized = selector.strip().lower()
    row = conn.execute(
        """
        SELECT
            gu.login,
            gu.issue_or_pr_authored,
            gu.issue_comments_authored,
            gu.pr_reviews_authored,
            gu.pr_review_comments_authored,
            gu.mentions,
            nl.slack_user_id
        FROM github_users AS gu
        LEFT JOIN necromancy_links AS nl
          ON nl.github_login = gu.login
        WHERE lower(gu.login) = ?
        """,
        (normalized,),
    ).fetchone()
    if row is None:
        raise ValueError(f"GitHub mecromancy not found: {selector}")
    return row


def upsert_mecromancy_link(
    conn: sqlite3.Connection, slack_selector: str, github_selector: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    slack_user = resolve_unique_slack_user(conn, slack_selector)
    github_user = resolve_unique_github_user(conn, github_selector)
    conn.execute(
        "DELETE FROM necromancy_links WHERE slack_user_id = ? OR github_login = ?",
        (slack_user["user_id"], github_user["login"]),
    )
    conn.execute(
        """
        INSERT INTO necromancy_links(slack_user_id, github_login, created_at, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (slack_user["user_id"], github_user["login"]),
    )
    conn.commit()
    return slack_user, github_user


def list_mecromancy_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                nl.slack_user_id,
                su.username,
                su.display_name,
                nl.github_login,
                nl.created_at,
                nl.updated_at
            FROM necromancy_links AS nl
            JOIN slack_users AS su
              ON su.user_id = nl.slack_user_id
            JOIN github_users AS gu
              ON gu.login = nl.github_login
            ORDER BY lower(su.username), lower(gu.login)
            """
        )
    )


def format_slack_results(rows: Sequence[sqlite3.Row], query: str) -> str:
    if not rows:
        return f"No Slack mecromancy found for query: {query}"
    lines = [f"Slack mecromancy results for `{query}`:"]
    for row in rows:
        linked = row["github_login"] or "-"
        lines.append(
            f"- {row['username']} [{row['user_id']}] display={row['display_name'] or '-'} "
            f"real={row['real_name'] or '-'} email={row['email'] or '-'} linked_github={linked}"
        )
    return "\n".join(lines)


def format_github_results(rows: Sequence[sqlite3.Row], query: str) -> str:
    if not rows:
        return f"No GitHub mecromancy found for query: {query}"
    lines = [f"GitHub mecromancy results for `{query}`:"]
    for row in rows:
        linked = row["slack_user_id"] or "-"
        activity = (
            f"issues/prs={row['issue_or_pr_authored']} "
            f"issue_comments={row['issue_comments_authored']} "
            f"reviews={row['pr_reviews_authored']} "
            f"review_comments={row['pr_review_comments_authored']} "
            f"mentions={row['mentions']}"
        )
        lines.append(f"- {row['login']} linked_slack={linked} {activity}")
    return "\n".join(lines)


def format_link_results(rows: Sequence[sqlite3.Row]) -> str:
    if not rows:
        return "No mecromancy links found."
    lines = ["Mecromancy links:"]
    for row in rows:
        display = row["display_name"] or "-"
        lines.append(
            f"- slack: {row['username']} [{row['slack_user_id']}] display={display} "
            f"<-> github: {row['github_login']}"
        )
    return "\n".join(lines)


def build_mecromancy_usage() -> str:
    return (
        "Usage:\n"
        "/mecromancy slack <query>\n"
        "/mecromancy github <query>\n"
        "/mecromancy links\n"
        '/mecromancy link "<slack selector>" <github_login>'
    )


def handle_mecromancy_command(db_path: str | Path, text: str) -> str:
    try:
        tokens = shlex.split(text)
    except ValueError as err:
        return f"Invalid command syntax: {err}"

    if not tokens:
        return build_mecromancy_usage()

    subcommand = tokens[0].lower()
    conn = connect_necromancy_db(db_path)
    try:
        if subcommand == "slack":
            query = " ".join(tokens[1:]).strip()
            if not query:
                return "Missing Slack query.\n" + build_mecromancy_usage()
            return format_slack_results(search_slack_users(conn, query), query)
        if subcommand == "github":
            query = " ".join(tokens[1:]).strip()
            if not query:
                return "Missing GitHub query.\n" + build_mecromancy_usage()
            return format_github_results(search_github_users(conn, query), query)
        if subcommand == "links":
            if len(tokens) != 1:
                return "Invalid links syntax.\n" + build_mecromancy_usage()
            return format_link_results(list_mecromancy_links(conn))
        if subcommand == "link":
            if len(tokens) != 3:
                return "Invalid link syntax.\n" + build_mecromancy_usage()
            slack_user, github_user = upsert_mecromancy_link(conn, tokens[1], tokens[2])
            return (
                "Linked mecromancy:\n"
                f"- slack: {slack_user['username']} [{slack_user['user_id']}]\n"
                f"- github: {github_user['login']}"
            )
        return "Unknown subcommand.\n" + build_mecromancy_usage()
    except ValueError as err:
        return str(err)
    finally:
        conn.close()
