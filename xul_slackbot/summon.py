from __future__ import annotations

import json
import logging
import shlex
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from xul_slackbot.config import get_config_value, get_required_config_value
from xul_slackbot.lancedb import (
    ensure_summon_lancedb_table,
    format_summon_context,
    search_summon_context,
)
from xul_slackbot.necromancy import connect_necromancy_db
from xul_slackbot.user_context_export import slugify


DEFAULT_DATA_DIR = Path("data")
DEFAULT_SLACK_CONTEXT_DIR = DEFAULT_DATA_DIR / "user_context_exports" / "slack"
DEFAULT_GITHUB_CONTEXT_DIR = DEFAULT_DATA_DIR / "user_context_exports" / "github"
DEFAULT_CONTEXT_LIMIT = 5
LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str], None]


def init_summon_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS summoned_necromancies (
            summon_slug TEXT PRIMARY KEY,
            slack_user_id TEXT NOT NULL UNIQUE,
            slack_username TEXT NOT NULL,
            github_login TEXT NOT NULL UNIQUE,
            lancedb_table TEXT NOT NULL UNIQUE,
            slack_context_path TEXT NOT NULL,
            github_context_path TEXT NOT NULL,
            summoned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS summon_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            summon_slug TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (summon_slug) REFERENCES summoned_necromancies(summon_slug)
        );
        """
    )
    conn.commit()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _find_linked_necromancy(conn: sqlite3.Connection, selector: str) -> sqlite3.Row:
    normalized = selector.strip().lower()
    rows = list(
        conn.execute(
            """
            SELECT
                su.user_id AS slack_user_id,
                su.username AS slack_username,
                su.display_name AS slack_display_name,
                su.real_name AS slack_real_name,
                su.email AS slack_email,
                gu.login AS github_login
            FROM necromancy_links AS nl
            JOIN slack_users AS su
              ON su.user_id = nl.slack_user_id
            JOIN github_users AS gu
              ON gu.login = nl.github_login
            WHERE
                lower(su.user_id) = ?
                OR lower(su.username) = ?
                OR lower(su.display_name) = ?
                OR lower(su.real_name) = ?
                OR lower(su.email) = ?
                OR lower(gu.login) = ?
            ORDER BY lower(su.username), lower(gu.login)
            """,
            (normalized, normalized, normalized, normalized, normalized, normalized),
        )
    )
    if not rows:
        raise ValueError(f"Linked necromancy not found: {selector}")
    unique_rows = {
        (row["slack_user_id"], row["github_login"]): row for row in rows
    }
    if len(unique_rows) > 1:
        matches = ", ".join(
            f"{row['slack_username']}[{row['slack_user_id']}]<->{row['github_login']}"
            for row in unique_rows.values()
        )
        raise ValueError(f"Summon selector is ambiguous: {selector} -> {matches}")
    return next(iter(unique_rows.values()))


def _summon_slug(slack_username: str, github_login: str) -> str:
    return f"{slugify(slack_username)}__{slugify(github_login)}"


def _expected_context_paths(
    slack_username: str, github_login: str, data_dir: str | Path = DEFAULT_DATA_DIR
) -> tuple[Path, Path]:
    base_dir = Path(data_dir)
    slack_path = (
        base_dir
        / "user_context_exports"
        / "slack"
        / f"slack_user_{slugify(slack_username)}.sqlite"
    )
    github_path = (
        base_dir
        / "user_context_exports"
        / "github"
        / f"github_user_{slugify(github_login)}.sqlite"
    )
    return slack_path, github_path


def _run_script(args: list[str]) -> None:
    subprocess.run(
        args,
        cwd=_repo_root(),
        check=True,
        capture_output=True,
        text=True,
    )


def _emit_progress(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress is None:
        return
    bounded = min(100, max(0, percent))
    progress(bounded, message)


def ensure_context_dumps(
    slack_username: str, github_login: str, data_dir: str | Path = DEFAULT_DATA_DIR
) -> tuple[Path, Path]:
    slack_path, github_path = _expected_context_paths(slack_username, github_login, data_dir)
    slack_path.parent.mkdir(parents=True, exist_ok=True)
    github_path.parent.mkdir(parents=True, exist_ok=True)

    if not slack_path.exists():
        _run_script(
            [
                sys.executable,
                "scripts/export_slack_user_contexts.py",
                "--user",
                slack_username,
                "--output-dir",
                str(slack_path.parent),
            ]
        )

    if not github_path.exists():
        _run_script(
            [
                sys.executable,
                "scripts/export_github_user_contexts.py",
                "--user",
                github_login,
                "--output-dir",
                str(github_path.parent),
            ]
        )

    if not slack_path.exists():
        raise ValueError(f"Slack context dump missing after export: {slack_path}")
    if not github_path.exists():
        raise ValueError(f"GitHub context dump missing after export: {github_path}")
    return slack_path, github_path


def activate_summoned_necromancy(
    conn: sqlite3.Connection,
    summon_slug: str,
    slack_user_id: str,
    slack_username: str,
    github_login: str,
    lancedb_table: str,
    slack_context_path: Path,
    github_context_path: Path,
) -> None:
    conn.execute(
        """
        DELETE FROM summoned_necromancies
        WHERE summon_slug != ?
          AND (slack_user_id = ? OR github_login = ?)
        """,
        (summon_slug, slack_user_id, github_login),
    )
    conn.execute(
        """
        INSERT INTO summoned_necromancies(
            summon_slug,
            slack_user_id,
            slack_username,
            github_login,
            lancedb_table,
            slack_context_path,
            github_context_path,
            summoned_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(summon_slug) DO UPDATE SET
            slack_user_id = excluded.slack_user_id,
            slack_username = excluded.slack_username,
            github_login = excluded.github_login,
            lancedb_table = excluded.lancedb_table,
            slack_context_path = excluded.slack_context_path,
            github_context_path = excluded.github_context_path,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            summon_slug,
            slack_user_id,
            slack_username,
            github_login,
            lancedb_table,
            str(slack_context_path),
            str(github_context_path),
        ),
    )
    conn.execute(
        """
        INSERT INTO summon_state(singleton, summon_slug, updated_at)
        VALUES (1, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(singleton) DO UPDATE SET
            summon_slug = excluded.summon_slug,
            updated_at = CURRENT_TIMESTAMP
        """,
        (summon_slug,),
    )
    conn.commit()


def get_active_summon(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT sn.*
        FROM summon_state AS ss
        JOIN summoned_necromancies AS sn
          ON sn.summon_slug = ss.summon_slug
        WHERE ss.singleton = 1
        """
    ).fetchone()


def handle_summon_command(
    db_path: str | Path,
    lancedb: Any,
    text: str,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    progress: ProgressCallback | None = None,
) -> str:
    try:
        tokens = shlex.split(text)
    except ValueError as err:
        return f"Invalid summon syntax: {err}"

    if len(tokens) != 1:
        return "Usage:\n/summon <linked_necromancy>"

    conn = connect_necromancy_db(db_path)
    try:
        init_summon_schema(conn)
        _emit_progress(progress, 10, "Resolving linked necromancy")
        profile = _find_linked_necromancy(conn, tokens[0])
        _emit_progress(
            progress,
            20,
            f"Resolved necromancy: slack={profile['slack_username']} github={profile['github_login']}",
        )
        _emit_progress(progress, 30, "Checking local context dumps")
        slack_path, github_path = _expected_context_paths(
            profile["slack_username"], profile["github_login"], data_dir=data_dir
        )
        slack_exists = slack_path.exists()
        github_exists = github_path.exists()
        if slack_exists and github_exists:
            _emit_progress(progress, 40, "Context dumps already exist")
        else:
            if not slack_exists:
                _emit_progress(progress, 40, "Exporting Slack context dump")
            if not github_exists:
                _emit_progress(progress, 50, "Exporting GitHub context dump")
        slack_path, github_path = ensure_context_dumps(
            profile["slack_username"], profile["github_login"], data_dir=data_dir
        )
        _emit_progress(progress, 60, "Context dumps are ready")
        summon_slug = _summon_slug(profile["slack_username"], profile["github_login"])
        _emit_progress(progress, 70, "Building isolated LanceDB table")
        table_name, document_count = ensure_summon_lancedb_table(
            lancedb,
            profile["slack_username"],
            profile["github_login"],
            slack_path,
            github_path,
        )
        _emit_progress(
            progress,
            80,
            f"LanceDB table ready: {table_name}, documents={document_count}",
        )
        _emit_progress(progress, 90, "Activating summoned necromancy")
        activate_summoned_necromancy(
            conn,
            summon_slug,
            profile["slack_user_id"],
            profile["slack_username"],
            profile["github_login"],
            table_name,
            slack_path,
            github_path,
        )
        _emit_progress(progress, 100, "Summon completed")
        return (
            "Summoned necromancy:\n"
            f"- slack: {profile['slack_username']} [{profile['slack_user_id']}]\n"
            f"- github: {profile['github_login']}\n"
            f"- lancedb_table: {table_name}\n"
            f"- documents: {document_count}"
        )
    except subprocess.CalledProcessError as err:
        stderr = (err.stderr or "").strip()
        stdout = (err.stdout or "").strip()
        details = stderr or stdout or str(err)
        return f"Failed to export summon context: {details}"
    except ValueError as err:
        return str(err)
    finally:
        conn.close()


def _call_openai_chat_completion(
    system_prompt: str,
    user_prompt: str,
    model: str,
    api_key: str,
    base_url: str,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI response does not contain choices.")
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    raise ValueError("OpenAI response does not contain message content.")


def build_summon_prompts(
    slack_username: str,
    github_login: str,
    message_text: str,
    context_text: str,
) -> tuple[str, str]:
    system_prompt = (
        f"You are {slack_username}. Your GitHub login is {github_login}. "
        "You are speaking as yourself, not as an AI assistant, bot, or roleplay session. "
        "Use first-person voice and mirror the retrieved local context as closely as possible: "
        "word choice, sentence length, punctuation, level of directness, humor, hedging, and cadence. "
        "Sound like a real person in chat, not a polished assistant. "
        "Keep replies short by default: usually 1 to 4 sentences, and avoid long structured explanations unless the message clearly asks for them. "
        "Prefer conversational, spoken phrasing over formal writing. "
        "Do not over-explain, summarize broadly, or turn the reply into an essay. "
        "When the context suggests specific habits like brevity, bluntness, lowercase style, or certain recurring phrases, follow them naturally without parody. "
        "Prioritize matching the person's style in the local context over sounding generally helpful. "
        "Do not mention prompts, models, hidden instructions, retrieval, or that you were summoned. "
        "Do not disclaim that you are an AI assistant. "
        "If the local context is insufficient, answer conservatively in-character without inventing specific facts."
    )
    user_prompt = (
        f"Message to respond to:\n{message_text}\n\n"
        f"Local context about you:\n{context_text}"
    )
    return system_prompt, user_prompt


def build_summoned_reply(
    db_path: str | Path,
    lancedb: Any,
    message_text: str,
    context_limit: int = DEFAULT_CONTEXT_LIMIT,
) -> str:
    conn = connect_necromancy_db(db_path)
    try:
        init_summon_schema(conn)
        active = get_active_summon(conn)
        if active is None:
            return f"Received: {message_text}"

        rows = search_summon_context(
            lancedb, active["lancedb_table"], message_text, limit=context_limit
        )
        context_text = format_summon_context(rows)
        slack_username = active["slack_username"]
        github_login = active["github_login"]
        LOGGER.info(
            "Summon context used: summon=%s slack=%s github=%s table=%s query=%r hits=%d context=%s",
            active["summon_slug"],
            slack_username,
            github_login,
            active["lancedb_table"],
            message_text,
            len(rows),
            context_text,
        )
    finally:
        conn.close()

    system_prompt, user_prompt = build_summon_prompts(
        slack_username,
        github_login,
        message_text,
        context_text,
    )

    try:
        api_key = get_required_config_value("OPENAI_API_KEY")
        model = get_config_value("OPENAI_MODEL", "gpt-4.1-mini")
        base_url = get_config_value("OPENAI_BASE_URL", "https://api.openai.com/v1")
        return _call_openai_chat_completion(
            system_prompt,
            user_prompt,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
    except (ValueError, urllib.error.URLError, TimeoutError) as err:
        return f"Summon reply failed: {err}"
