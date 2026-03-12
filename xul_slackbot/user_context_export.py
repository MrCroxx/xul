from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


GITHUB_MENTION_RE = re.compile(r"(?<![A-Za-z0-9-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")


@dataclass(frozen=True)
class SlackUser:
    user_id: str
    username: str
    real_name: str
    display_name: str
    email: str
    raw_json: str


@dataclass(frozen=True)
class SlackChannel:
    channel_id: str
    name: str
    raw_json: str


@dataclass(frozen=True)
class SlackMessage:
    channel_id: str
    ts: str
    thread_ts: Optional[str]
    text: str
    user_id: Optional[str]
    subtype: Optional[str]
    raw_json: str

    @property
    def context_key(self) -> str:
        if self.thread_ts:
            return f"thread:{self.channel_id}:{self.thread_ts}"
        return f"message:{self.channel_id}:{self.ts}"

    @property
    def is_thread_context(self) -> bool:
        return self.thread_ts is not None


@dataclass(frozen=True)
class SlackContextMatch:
    context_key: str
    channel_id: str
    thread_ts: Optional[str]
    anchor_ts: str
    match_reason: str
    matched_message_ts: str


@dataclass(frozen=True)
class GitHubEvent:
    event_id: str
    event_type: str
    author_login: Optional[str]
    created_at: Optional[str]
    body: str
    html_url: Optional[str]
    matched: bool
    match_reason: Optional[str]
    raw_json: str


@dataclass(frozen=True)
class GitHubContext:
    context_id: str
    repo: str
    kind: str
    number: int
    title: str
    state: Optional[str]
    html_url: Optional[str]
    author_login: Optional[str]
    matched: bool
    match_reasons: Tuple[str, ...]
    raw_json: str
    events: Tuple[GitHubEvent, ...]


@dataclass(frozen=True)
class GitHubUserSummary:
    login: str
    issue_or_pr_authored: int
    issue_comments_authored: int
    pull_request_reviews_authored: int
    pull_request_review_comments_authored: int
    mentions: int


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "_", lowered)
    return slug.strip("._-") or "unknown"


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def extract_slack_user_fields(raw: Dict[str, object]) -> Tuple[str, str, str]:
    profile = raw.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    real_name = str(profile.get("real_name") or raw.get("real_name") or "")
    display_name = str(profile.get("display_name") or "")
    email = str(profile.get("email") or "")
    return real_name, display_name, email


def connect_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def load_latest_slack_users(conn: sqlite3.Connection) -> Dict[str, SlackUser]:
    query = """
    WITH latest AS (
        SELECT id, MAX(chunk_id) AS max_chunk_id
        FROM S_USER
        GROUP BY id
    )
    SELECT u.id, u.username, u.data
    FROM S_USER AS u
    JOIN latest
      ON latest.id = u.id
     AND latest.max_chunk_id = u.chunk_id
    """
    users: Dict[str, SlackUser] = {}
    for row in conn.execute(query):
        raw = json.loads(row["data"])
        real_name, display_name, email = extract_slack_user_fields(raw)
        users[row["id"]] = SlackUser(
            user_id=row["id"],
            username=row["username"],
            real_name=real_name,
            display_name=display_name,
            email=email,
            raw_json=row["data"],
        )
    return users


def load_latest_slack_channels(conn: sqlite3.Connection) -> Dict[str, SlackChannel]:
    query = """
    WITH latest AS (
        SELECT id, MAX(chunk_id) AS max_chunk_id
        FROM CHANNEL
        GROUP BY id
    )
    SELECT c.id, COALESCE(c.name, ''), c.data
    FROM CHANNEL AS c
    JOIN latest
      ON latest.id = c.id
     AND latest.max_chunk_id = c.chunk_id
    """
    channels: Dict[str, SlackChannel] = {}
    for row in conn.execute(query):
        channels[row["id"]] = SlackChannel(
            channel_id=row["id"],
            name=row[1],
            raw_json=row["data"],
        )
    return channels


def load_slack_messages(conn: sqlite3.Connection) -> List[SlackMessage]:
    query = """
    SELECT channel_id, ts, thread_ts, COALESCE(txt, ''), data, chunk_id
    FROM MESSAGE
    """
    latest_rows: Dict[Tuple[str, str], Tuple[int, SlackMessage]] = {}
    for row in conn.execute(query):
        raw = json.loads(row["data"])
        thread_ts = row["thread_ts"]
        if not thread_ts and raw.get("thread_ts"):
            thread_ts = str(raw["thread_ts"])
        message = SlackMessage(
            channel_id=row["channel_id"],
            ts=row["ts"],
            thread_ts=thread_ts,
            text=str(raw.get("text") or row[3] or ""),
            user_id=raw.get("user"),
            subtype=raw.get("subtype"),
            raw_json=row["data"],
        )
        key = (message.channel_id, message.ts)
        existing = latest_rows.get(key)
        if existing is None or row["chunk_id"] > existing[0]:
            latest_rows[key] = (row["chunk_id"], message)
    messages = [item[1] for item in latest_rows.values()]
    messages.sort(key=lambda item: (item.channel_id, float(item.ts)))
    return messages


def resolve_slack_users(
    users: Dict[str, SlackUser], selectors: Sequence[str]
) -> Dict[str, SlackUser]:
    by_lookup: Dict[str, SlackUser] = {}
    for user in users.values():
        keys = {
            user.user_id,
            user.username.lower(),
            user.email.lower(),
            user.real_name.lower(),
            user.display_name.lower(),
        }
        for key in keys:
            if key:
                by_lookup[key] = user

    resolved: Dict[str, SlackUser] = {}
    for selector in selectors:
        key = selector if selector in users else selector.lower()
        user = by_lookup.get(key)
        if user is None:
            raise ValueError(f"Slack user not found: {selector}")
        resolved[user.user_id] = user
    return resolved


def build_slack_context_matches(
    messages: Sequence[SlackMessage], target_user_id: str
) -> List[SlackContextMatch]:
    matches: Dict[str, SlackContextMatch] = {}
    mention_token = f"<@{target_user_id}>"
    for message in messages:
        reasons: List[str] = []
        if message.user_id == target_user_id:
            reasons.append("authored")
        if mention_token in message.text:
            reasons.append("mentioned")
        if not reasons:
            continue
        reason = ",".join(reasons)
        if message.context_key in matches:
            existing = matches[message.context_key]
            if reason not in existing.match_reason.split(","):
                merged = ",".join(
                    sorted(set(existing.match_reason.split(",")) | set(reasons))
                )
                matches[message.context_key] = SlackContextMatch(
                    context_key=existing.context_key,
                    channel_id=existing.channel_id,
                    thread_ts=existing.thread_ts,
                    anchor_ts=existing.anchor_ts,
                    match_reason=merged,
                    matched_message_ts=existing.matched_message_ts,
                )
            continue
        matches[message.context_key] = SlackContextMatch(
            context_key=message.context_key,
            channel_id=message.channel_id,
            thread_ts=message.thread_ts,
            anchor_ts=message.thread_ts or message.ts,
            match_reason=reason,
            matched_message_ts=message.ts,
        )
    return sorted(matches.values(), key=lambda item: (item.channel_id, item.anchor_ts))


def collect_slack_context_messages(
    messages: Sequence[SlackMessage],
    matches: Sequence[SlackContextMatch],
    context_window: int,
) -> Dict[str, List[SlackMessage]]:
    by_channel: Dict[str, List[SlackMessage]] = {}
    thread_members: Dict[Tuple[str, str], List[SlackMessage]] = {}
    for message in messages:
        by_channel.setdefault(message.channel_id, []).append(message)
        if message.thread_ts:
            thread_members.setdefault((message.channel_id, message.thread_ts), []).append(
                message
            )

    for channel_messages in by_channel.values():
        channel_messages.sort(key=lambda item: float(item.ts))
    for members in thread_members.values():
        members.sort(key=lambda item: float(item.ts))

    contexts: Dict[str, List[SlackMessage]] = {}
    for match in matches:
        if match.thread_ts:
            contexts[match.context_key] = list(
                thread_members.get((match.channel_id, match.thread_ts), [])
            )
            continue

        channel_messages = by_channel.get(match.channel_id, [])
        anchor_index = next(
            index
            for index, message in enumerate(channel_messages)
            if message.ts == match.anchor_ts
        )
        start = max(0, anchor_index - context_window)
        end = min(len(channel_messages), anchor_index + context_window + 1)
        contexts[match.context_key] = channel_messages[start:end]
    return contexts


def init_slack_output_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            real_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            email TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE channels (
            channel_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE contexts (
            context_key TEXT PRIMARY KEY,
            context_type TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT,
            anchor_ts TEXT NOT NULL,
            match_reason TEXT NOT NULL,
            matched_message_ts TEXT NOT NULL
        );
        CREATE TABLE messages (
            context_key TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            thread_ts TEXT,
            user_id TEXT,
            subtype TEXT,
            text TEXT NOT NULL,
            is_direct_match INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (context_key, channel_id, ts)
        );
        CREATE INDEX messages_channel_ts_idx ON messages (channel_id, ts);
        """
    )
    return conn


def export_slack_user_context(
    output_path: Path,
    user: SlackUser,
    channels: Dict[str, SlackChannel],
    messages: Sequence[SlackMessage],
    context_window: int,
    source_path: Path,
) -> int:
    matches = build_slack_context_matches(messages, user.user_id)
    contexts = collect_slack_context_messages(messages, matches, context_window)

    conn = init_slack_output_db(output_path)
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("source_path", str(source_path)),
    )
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("target_user_id", user.user_id),
    )
    conn.execute(
        "INSERT INTO users(user_id, username, real_name, display_name, email, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            user.user_id,
            user.username,
            user.real_name,
            user.display_name,
            user.email,
            user.raw_json,
        ),
    )

    used_channels: Set[str] = set()
    for match in matches:
        used_channels.add(match.channel_id)
        conn.execute(
            """
            INSERT INTO contexts(
                context_key, context_type, channel_id, thread_ts, anchor_ts, match_reason, matched_message_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match.context_key,
                "thread" if match.thread_ts else "message_window",
                match.channel_id,
                match.thread_ts,
                match.anchor_ts,
                match.match_reason,
                match.matched_message_ts,
            ),
        )

        direct_match_ts = {
            item.matched_message_ts
            for item in matches
            if item.context_key == match.context_key
        }
        for message in contexts.get(match.context_key, []):
            conn.execute(
                """
                INSERT INTO messages(
                    context_key, channel_id, ts, thread_ts, user_id, subtype, text, is_direct_match, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match.context_key,
                    message.channel_id,
                    message.ts,
                    message.thread_ts,
                    message.user_id,
                    message.subtype,
                    message.text,
                    1 if message.ts in direct_match_ts else 0,
                    message.raw_json,
                ),
            )

    for channel_id in sorted(used_channels):
        channel = channels.get(channel_id)
        if channel is None:
            continue
        conn.execute(
            "INSERT INTO channels(channel_id, name, raw_json) VALUES (?, ?, ?)",
            (channel.channel_id, channel.name, channel.raw_json),
        )

    conn.commit()
    conn.close()
    return len(matches)


def extract_github_mentions(text: str) -> Set[str]:
    if not text:
        return set()
    return {match.group(1).lower() for match in GITHUB_MENTION_RE.finditer(text)}


def as_login(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    login = payload.get("login")
    if isinstance(login, str) and login:
        return login
    return None


def build_github_context(record: Dict[str, object], target_login: str) -> GitHubContext:
    issue = record["issue_or_pr"]
    if not isinstance(issue, dict):
        raise ValueError("issue_or_pr must be a JSON object")

    normalized_login = target_login.lower()
    reasons: Set[str] = set()
    events: List[GitHubEvent] = []

    issue_author = as_login(issue.get("user"))
    issue_body = str(issue.get("body") or "")
    if issue_author and issue_author.lower() == normalized_login:
        reasons.add("author")
    if normalized_login in extract_github_mentions(issue_body):
        reasons.add("mentioned_in_body")

    issue_number = int(record["number"])
    base_event = GitHubEvent(
        event_id=f"{record['kind']}:{issue_number}:body",
        event_type="issue_or_pr",
        author_login=issue_author,
        created_at=issue.get("created_at"),
        body=issue_body,
        html_url=issue.get("html_url"),
        matched=bool(reasons),
        match_reason=",".join(sorted(reasons)) if reasons else None,
        raw_json=json_dumps(issue),
    )
    events.append(base_event)

    def append_events(items: Iterable[object], prefix: str, event_type: str) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            local_reasons: Set[str] = set()
            author_login = as_login(item.get("user"))
            body = str(item.get("body") or "")
            if author_login and author_login.lower() == normalized_login:
                local_reasons.add(f"{event_type}_author")
            if normalized_login in extract_github_mentions(body):
                local_reasons.add(f"{event_type}_mention")
            if local_reasons:
                reasons.update(local_reasons)
            event_id = item.get("id")
            events.append(
                GitHubEvent(
                    event_id=f"{prefix}:{event_id}",
                    event_type=event_type,
                    author_login=author_login,
                    created_at=item.get("created_at") or item.get("submitted_at"),
                    body=body,
                    html_url=item.get("html_url"),
                    matched=bool(local_reasons),
                    match_reason=",".join(sorted(local_reasons)) if local_reasons else None,
                    raw_json=json_dumps(item),
                )
            )

    append_events(record.get("issue_comments", []), "issue_comment", "issue_comment")
    append_events(
        record.get("pull_request_reviews", []),
        "pull_request_review",
        "pull_request_review",
    )
    append_events(
        record.get("pull_request_review_comments", []),
        "pull_request_review_comment",
        "pull_request_review_comment",
    )

    return GitHubContext(
        context_id=f"{record['kind']}:{record['repo']}:{issue_number}",
        repo=str(record["repo"]),
        kind=str(record["kind"]),
        number=issue_number,
        title=str(issue.get("title") or ""),
        state=issue.get("state"),
        html_url=issue.get("html_url"),
        author_login=issue_author,
        matched=bool(reasons),
        match_reasons=tuple(sorted(reasons)),
        raw_json=json_dumps(record),
        events=tuple(events),
    )


def init_github_output_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE contexts (
            context_id TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            kind TEXT NOT NULL,
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            state TEXT,
            html_url TEXT,
            author_login TEXT,
            matched INTEGER NOT NULL,
            match_reasons TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            context_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            author_login TEXT,
            created_at TEXT,
            body TEXT NOT NULL,
            html_url TEXT,
            matched INTEGER NOT NULL,
            match_reason TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX events_context_idx ON events (context_id, created_at);
        """
    )
    return conn


def export_github_user_contexts(
    output_path: Path,
    target_login: str,
    records: Sequence[Dict[str, object]],
    source_dir: Path,
) -> int:
    conn = init_github_output_db(output_path)
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("source_dir", str(source_dir)),
    )
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("target_login", target_login),
    )

    matched_count = 0
    for record in records:
        context = build_github_context(record, target_login)
        if not context.matched:
            continue
        matched_count += 1
        conn.execute(
            """
            INSERT INTO contexts(
                context_id, repo, kind, number, title, state, html_url, author_login, matched, match_reasons, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context.context_id,
                context.repo,
                context.kind,
                context.number,
                context.title,
                context.state,
                context.html_url,
                context.author_login,
                1,
                ",".join(context.match_reasons),
                context.raw_json,
            ),
        )
        for event in context.events:
            conn.execute(
                """
                INSERT INTO events(
                    event_id, context_id, event_type, author_login, created_at, body, html_url, matched, match_reason, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    context.context_id,
                    event.event_type,
                    event.author_login,
                    event.created_at,
                    event.body,
                    event.html_url,
                    1 if event.matched else 0,
                    event.match_reason,
                    event.raw_json,
                ),
            )

    conn.commit()
    conn.close()
    return matched_count


def load_github_dump_records(source_dir: Path) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for path in sorted(source_dir.glob("*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def list_slack_users(users: Dict[str, SlackUser]) -> List[SlackUser]:
    return sorted(users.values(), key=lambda item: (item.username.lower(), item.user_id))


def summarize_github_users(records: Sequence[Dict[str, object]]) -> List[GitHubUserSummary]:
    authored_issue_or_pr: Dict[str, int] = {}
    authored_issue_comments: Dict[str, int] = {}
    authored_pr_reviews: Dict[str, int] = {}
    authored_pr_review_comments: Dict[str, int] = {}
    mentions: Dict[str, int] = {}

    def bump(counter: Dict[str, int], login: Optional[str]) -> None:
        if not login:
            return
        normalized = login.lower()
        counter[normalized] = counter.get(normalized, 0) + 1

    def bump_mentions(text: str) -> None:
        for login in extract_github_mentions(text):
            mentions[login] = mentions.get(login, 0) + 1

    for record in records:
        issue = record.get("issue_or_pr")
        if isinstance(issue, dict):
            bump(authored_issue_or_pr, as_login(issue.get("user")))
            bump_mentions(str(issue.get("body") or ""))

        for item in record.get("issue_comments", []):
            if not isinstance(item, dict):
                continue
            bump(authored_issue_comments, as_login(item.get("user")))
            bump_mentions(str(item.get("body") or ""))

        for item in record.get("pull_request_reviews", []):
            if not isinstance(item, dict):
                continue
            bump(authored_pr_reviews, as_login(item.get("user")))
            bump_mentions(str(item.get("body") or ""))

        for item in record.get("pull_request_review_comments", []):
            if not isinstance(item, dict):
                continue
            bump(authored_pr_review_comments, as_login(item.get("user")))
            bump_mentions(str(item.get("body") or ""))

    logins = sorted(
        set(authored_issue_or_pr)
        | set(authored_issue_comments)
        | set(authored_pr_reviews)
        | set(authored_pr_review_comments)
        | set(mentions)
    )

    return [
        GitHubUserSummary(
            login=login,
            issue_or_pr_authored=authored_issue_or_pr.get(login, 0),
            issue_comments_authored=authored_issue_comments.get(login, 0),
            pull_request_reviews_authored=authored_pr_reviews.get(login, 0),
            pull_request_review_comments_authored=authored_pr_review_comments.get(
                login, 0
            ),
            mentions=mentions.get(login, 0),
        )
        for login in logins
    ]
