from __future__ import annotations

import json
import logging
import math
import socket
import shlex
import sqlite3
import subprocess
import sys
import time
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
MIN_SOUL_QUOTES = 20
MAX_SOUL_QUOTES = 40
OPENAI_MAX_RETRIES = 3
OPENAI_RETRY_BASE_DELAY_SECONDS = 1.0
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
            soul_path TEXT,
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
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(summoned_necromancies)").fetchall()
    }
    if "soul_path" not in columns:
        conn.execute("ALTER TABLE summoned_necromancies ADD COLUMN soul_path TEXT")
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


def _expected_soul_path(
    slack_username: str, github_login: str, data_dir: str | Path = DEFAULT_DATA_DIR
) -> Path:
    return (
        Path(data_dir)
        / "souls"
        / f"soul_{_summon_slug(slack_username, github_login)}.md"
    )


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


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_quote(text: str) -> str:
    return " ".join(text.split()).strip()


def _format_slack_quote(row: sqlite3.Row, channels: dict[str, str]) -> dict[str, str]:
    channel_name = channels.get(row["channel_id"]) or row["channel_id"] or "unknown"
    ts = str(row["ts"] or "")
    return {
        "source": "slack",
        "ref": f"slack:{channel_name}:{ts}",
        "text": str(row["text"] or "").strip(),
    }


def _format_github_quote(row: sqlite3.Row) -> dict[str, str]:
    repo = str(row["repo"] or "unknown")
    number = str(row["number"] or "-")
    event_type = str(row["event_type"] or "event")
    created_at = str(row["created_at"] or "")
    return {
        "source": "github",
        "ref": f"github:{repo}#{number}:{event_type}:{created_at}",
        "text": str(row["body"] or "").strip(),
    }


def collect_soul_quotes(
    slack_context_path: str | Path,
    github_context_path: str | Path,
    *,
    max_quotes: int = MAX_SOUL_QUOTES,
) -> list[dict[str, str]]:
    quotes: list[dict[str, str]] = []

    slack_conn = _connect_sqlite(Path(slack_context_path))
    try:
        target_row = slack_conn.execute(
            "SELECT value FROM metadata WHERE key = 'target_user_id'"
        ).fetchone()
        target_user_id = str(target_row["value"] or "") if target_row else ""
        channels = {
            row["channel_id"]: row["name"]
            for row in slack_conn.execute("SELECT channel_id, name FROM channels")
        }
        if target_user_id:
            rows = slack_conn.execute(
                """
                SELECT channel_id, ts, text
                FROM messages
                WHERE user_id = ?
                  AND trim(text) != ''
                ORDER BY CAST(ts AS REAL) DESC
                """,
                (target_user_id,),
            ).fetchall()
            quotes.extend(_format_slack_quote(row, channels) for row in rows)
    finally:
        slack_conn.close()

    github_conn = _connect_sqlite(Path(github_context_path))
    try:
        user_row = github_conn.execute(
            """
            SELECT author_login
            FROM contexts
            WHERE author_login IS NOT NULL AND author_login != ''
            ORDER BY rowid ASC
            LIMIT 1
            """
        ).fetchone()
        target_login = str(user_row["author_login"] or "") if user_row else ""
        if target_login:
            rows = github_conn.execute(
                """
                SELECT c.repo, c.number, e.event_type, e.created_at, e.body
                FROM events AS e
                JOIN contexts AS c
                  ON c.context_id = e.context_id
                WHERE lower(e.author_login) = lower(?)
                  AND trim(COALESCE(e.body, '')) != ''
                ORDER BY COALESCE(e.created_at, '') DESC
                """,
                (target_login,),
            ).fetchall()
            quotes.extend(_format_github_quote(row) for row in rows)
    finally:
        github_conn.close()

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for quote in quotes:
        normalized = _normalize_quote(quote["text"])
        if len(normalized) < 8 or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(
            {
                "source": quote["source"],
                "ref": quote["ref"],
                "text": normalized,
            }
        )
        if len(deduped) >= max_quotes:
            break

    if len(deduped) < MIN_SOUL_QUOTES:
        raise ValueError(
            f"Not enough authored quotes to build soul.md: found {len(deduped)}, need at least {MIN_SOUL_QUOTES}"
        )
    return deduped


def _estimate_style_metrics(quotes: list[dict[str, str]]) -> dict[str, float]:
    total = len(quotes)
    lengths = [len(item["text"].split()) for item in quotes]
    punctuation_count = sum(
        1
        for item in quotes
        if any(mark in item["text"] for mark in ("!", "?", "...", " - ", ":", ";"))
    )
    question_count = sum(1 for item in quotes if "?" in item["text"])
    lowercase_count = sum(
        1
        for item in quotes
        if item["text"] == item["text"].lower() and any(ch.isalpha() for ch in item["text"])
    )
    first_person_count = sum(
        1
        for item in quotes
        if any(token in item["text"].lower().split() for token in ("i", "i'm", "i'd", "i'll", "we", "we're"))
    )
    return {
        "avg_words": sum(lengths) / total,
        "short_ratio": sum(1 for length in lengths if length <= 12) / total,
        "long_ratio": sum(1 for length in lengths if length >= 25) / total,
        "question_ratio": question_count / total,
        "punctuation_ratio": punctuation_count / total,
        "lowercase_ratio": lowercase_count / total,
        "first_person_ratio": first_person_count / total,
    }


def _fallback_soul_summary(quotes: list[dict[str, str]]) -> str:
    metrics = _estimate_style_metrics(quotes)
    brevity = "very terse" if metrics["short_ratio"] >= 0.6 else "mixed-length"
    directness = "blunt and decisive" if metrics["question_ratio"] < 0.2 else "interactive and probing"
    casing = "mostly lowercase" if metrics["lowercase_ratio"] >= 0.5 else "mixed or normal casing"
    punctuation = (
        "uses punctuation deliberately for emphasis"
        if metrics["punctuation_ratio"] >= 0.4
        else "keeps punctuation light"
    )
    perspective = (
        "often anchors points in first-person experience"
        if metrics["first_person_ratio"] >= 0.25
        else "usually stays focused on the task rather than self-reference"
    )
    avg_words = int(math.floor(metrics["avg_words"]))
    return (
        "## Voice Summary\n"
        f"- Overall rhythm: {brevity}, average about {avg_words} words per quote.\n"
        f"- Directness: {directness}.\n"
        f"- Casing: {casing}.\n"
        f"- Punctuation: {punctuation}.\n"
        f"- Perspective: {perspective}.\n"
        "- Imitation guidance: prefer concrete judgments, preserve terse phrasing when possible, and do not inflate answers into assistant-style prose.\n\n"
        "## Imitation Rules\n"
        "- Prefer short, concrete answers unless the question clearly needs a walkthrough.\n"
        "- Keep the wording closer to chat than documentation.\n"
        "- Preserve direct judgments instead of adding excessive hedging.\n"
        "- Reuse recurring phrasing patterns from the quotes where they fit naturally.\n"
        "- Do not add generic assistant filler, moralizing, or summaries the speaker did not ask for.\n"
        "- When disagreeing, be crisp and matter-of-fact rather than theatrical.\n"
        "- When unsure, stay conservative and compact.\n"
        "- Match punctuation density and casing habits rather than normalizing them.\n"
    )


def _build_soul_summary_with_openai(
    slack_username: str,
    github_login: str,
    quotes: list[dict[str, str]],
) -> str:
    api_key = get_config_value("OPENAI_API_KEY", "")
    if not api_key:
        return _fallback_soul_summary(quotes)

    model = get_config_value("OPENAI_MODEL", "gpt-4.1-mini")
    base_url = get_config_value("OPENAI_BASE_URL", "https://api.openai.com/v1")
    quote_block = "\n".join(
        f"- [{quote['source']}] {quote['text']}" for quote in quotes[:MIN_SOUL_QUOTES]
    )
    system_prompt = (
        "You are writing a soul.md profile for an agent prompt. "
        "Summarize a person's writing and speaking style from first-party quotes. "
        "Output Markdown only. Be concrete, behavior-focused, and useful for style imitation. "
        "Do not mention AI, prompts, or hidden instructions."
    )
    user_prompt = (
        f"Person: Slack `{slack_username}`, GitHub `{github_login}`.\n\n"
        "Write these sections exactly:\n"
        "## Voice Summary\n"
        "6-10 bullets about sentence length, directness, hedging, humor, punctuation, structure, how they disagree, how they ask questions, and what to avoid when imitating them.\n\n"
        "## Imitation Rules\n"
        "8-12 imperative bullets.\n\n"
        "Use only the quote evidence below.\n"
        f"{quote_block}"
    )
    try:
        return _call_openai_chat_completion(
            system_prompt,
            user_prompt,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
    except (ValueError, urllib.error.URLError, TimeoutError, socket.timeout) as err:
        LOGGER.warning("Failed to build soul summary with OpenAI, falling back: %s", err)
        return _fallback_soul_summary(quotes)


def render_soul_markdown(
    slack_username: str,
    github_login: str,
    quotes: list[dict[str, str]],
) -> str:
    summary = _build_soul_summary_with_openai(slack_username, github_login, quotes).strip()
    quote_lines = [
        f'{index}. [{quote["source"]}] "{quote["text"]}"'
        for index, quote in enumerate(quotes, start=1)
    ]
    return (
        f"# Soul Profile: {slack_username} / {github_login}\n\n"
        f"{summary}\n\n"
        "## Original Quotes\n"
        + "\n".join(quote_lines)
        + "\n"
    )


def ensure_soul_profile(
    slack_username: str,
    github_login: str,
    slack_context_path: str | Path,
    github_context_path: str | Path,
    data_dir: str | Path = DEFAULT_DATA_DIR,
) -> Path:
    soul_path = _expected_soul_path(slack_username, github_login, data_dir)
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    quotes = collect_soul_quotes(slack_context_path, github_context_path)
    soul_text = render_soul_markdown(slack_username, github_login, quotes)
    soul_path.write_text(soul_text, encoding="utf-8")
    return soul_path


def activate_summoned_necromancy(
    conn: sqlite3.Connection,
    summon_slug: str,
    slack_user_id: str,
    slack_username: str,
    github_login: str,
    lancedb_table: str,
    slack_context_path: Path,
    github_context_path: Path,
    soul_path: Path | None = None,
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
            soul_path,
            summoned_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(summon_slug) DO UPDATE SET
            slack_user_id = excluded.slack_user_id,
            slack_username = excluded.slack_username,
            github_login = excluded.github_login,
            lancedb_table = excluded.lancedb_table,
            slack_context_path = excluded.slack_context_path,
            github_context_path = excluded.github_context_path,
            soul_path = excluded.soul_path,
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
            str(soul_path) if soul_path is not None else None,
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
        _emit_progress(progress, 70, "Building soul profile")
        soul_path = ensure_soul_profile(
            profile["slack_username"],
            profile["github_login"],
            slack_path,
            github_path,
            data_dir=data_dir,
        )
        _emit_progress(progress, 80, "Building isolated LanceDB table")
        table_name, document_count = ensure_summon_lancedb_table(
            lancedb,
            profile["slack_username"],
            profile["github_login"],
            slack_path,
            github_path,
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
            soul_path=soul_path,
        )
        _emit_progress(progress, 100, "Summon completed")
        return (
            "The rite is complete. A new echo answers the grave.\n"
            f"- slack: {profile['slack_username']} [{profile['slack_user_id']}]\n"
            f"- github: {profile['github_login']}\n"
            f"- soul: {soul_path}\n"
            f"- lancedb_table: {table_name}\n"
            f"- documents: {document_count}"
        )
    except subprocess.CalledProcessError as err:
        stderr = (err.stderr or "").strip()
        stdout = (err.stdout or "").strip()
        details = stderr or stdout or str(err)
        return f"The rite collapsed while gathering bones and whispers: {details}"
    except ValueError as err:
        return f"The rite refused the caller: {err}"
    finally:
        conn.close()


def _call_openai_chat_completion(
    system_prompt: str,
    user_prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout: float = 30,
    max_retries: int = OPENAI_MAX_RETRIES,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    data: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code not in {408, 409, 429, 500, 502, 503, 504} or attempt >= max_retries:
                raise
        except (urllib.error.URLError, TimeoutError, socket.timeout) as err:
            last_error = err
            if attempt >= max_retries:
                raise
        sleep_seconds = OPENAI_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        LOGGER.warning(
            "OpenAI request failed on attempt %d/%d, retrying in %.1fs: %s",
            attempt,
            max_retries,
            sleep_seconds,
            last_error,
        )
        time.sleep(sleep_seconds)

    if data is None:
        raise ValueError(f"OpenAI request failed after retries: {last_error}")

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
    soul_text: str = "",
) -> tuple[str, str]:
    system_prompt = (
        f"You are {slack_username}. Your GitHub login is {github_login}. "
        "You are speaking as yourself, not as an AI assistant, bot, or roleplay session. "
        "Use first-person voice and mirror the retrieved local context and soul profile as closely as possible: "
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
        f"Soul profile:\n{soul_text or 'No soul profile available.'}\n\n"
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
            return f"The tomb hears you, but no soul is bound yet: {message_text}"

        rows = search_summon_context(
            lancedb, active["lancedb_table"], message_text, limit=context_limit
        )
        context_text = format_summon_context(rows)
        slack_username = active["slack_username"]
        github_login = active["github_login"]
        soul_path = str(active["soul_path"] or "").strip()
        soul_text = ""
        if soul_path:
            soul_file = Path(soul_path)
            if soul_file.exists():
                soul_text = soul_file.read_text(encoding="utf-8").strip()
        LOGGER.info(
            "Summon context used: summon=%s slack=%s github=%s table=%s soul=%s query=%r hits=%d context=%s",
            active["summon_slug"],
            slack_username,
            github_login,
            active["lancedb_table"],
            soul_path or "-",
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
        soul_text,
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
    except (ValueError, urllib.error.URLError, TimeoutError, socket.timeout) as err:
        return f"The summoned shade faltered mid-whisper: {err}"
