#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xul_slackbot.user_context_export import (  # noqa: E402
    connect_sqlite,
    list_slack_users,
    load_github_dump_records,
    load_latest_slack_users,
    summarize_github_users,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List available Slack and GitHub users found in local dump files."
    )
    parser.add_argument(
        "--source",
        choices=["slack", "github", "all"],
        default="all",
        help="Which dump source to inspect.",
    )
    parser.add_argument(
        "--slack-input",
        type=Path,
        default=Path("slackdump_archive_20260311/slackdump.sqlite"),
        help="Source slackdump sqlite path.",
    )
    parser.add_argument(
        "--github-input-dir",
        type=Path,
        default=Path("github_dump"),
        help="Directory containing issue_*.json and pr_*.json files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("user_context_exports/dump_users.sqlite"),
        help="Output sqlite path.",
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv"],
        default="table",
        help="Output format.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of rows to print per source. 0 means no limit.",
    )
    parser.add_argument(
        "--contains",
        default="",
        help="Only print rows whose primary identifier contains this substring.",
    )
    parser.add_argument(
        "--no-stdout",
        action="store_true",
        help="Do not print rows to stdout; only write sqlite output.",
    )
    return parser.parse_args()


def write_rows(headers: list[str], rows: Iterable[list[str]], output_format: str) -> None:
    materialized = list(rows)
    if output_format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        writer.writerows(materialized)
        return

    widths = [len(header) for header in headers]
    for row in materialized:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * widths[index] for index in range(len(headers))))
    for row in materialized:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def apply_filters(rows: list[list[str]], contains: str, limit: int) -> list[list[str]]:
    filtered = rows
    if contains:
        needle = contains.lower()
        filtered = [row for row in filtered if needle in row[0].lower()]
    if limit > 0:
        filtered = filtered[:limit]
    return filtered


def init_output_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE slack_users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            real_name TEXT NOT NULL,
            email TEXT NOT NULL
        );
        CREATE TABLE github_users (
            login TEXT PRIMARY KEY,
            issue_or_pr_authored INTEGER NOT NULL,
            issue_comments_authored INTEGER NOT NULL,
            pr_reviews_authored INTEGER NOT NULL,
            pr_review_comments_authored INTEGER NOT NULL,
            mentions INTEGER NOT NULL
        );
        """
    )
    return conn


def emit_slack_users(args: argparse.Namespace, output_conn: sqlite3.Connection) -> int:
    conn = connect_sqlite(args.slack_input)
    users = list_slack_users(load_latest_slack_users(conn))
    conn.close()

    rows = [
        [
            user.user_id,
            user.username,
            user.display_name,
            user.real_name,
            user.email,
        ]
        for user in users
    ]
    rows = apply_filters(rows, args.contains, args.limit)
    output_conn.executemany(
        """
        INSERT INTO slack_users(user_id, username, display_name, real_name, email)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    if not args.no_stdout:
        print("[slack]")
        write_rows(
            ["user_id", "username", "display_name", "real_name", "email"],
            rows,
            args.format,
        )
        print(f"\nrows={len(rows)}")
    return len(rows)


def emit_github_users(args: argparse.Namespace, output_conn: sqlite3.Connection) -> int:
    records = load_github_dump_records(args.github_input_dir)
    summaries = summarize_github_users(records)

    rows = [
        [
            summary.login,
            str(summary.issue_or_pr_authored),
            str(summary.issue_comments_authored),
            str(summary.pull_request_reviews_authored),
            str(summary.pull_request_review_comments_authored),
            str(summary.mentions),
        ]
        for summary in summaries
    ]
    rows = apply_filters(rows, args.contains, args.limit)
    output_conn.executemany(
        """
        INSERT INTO github_users(
            login,
            issue_or_pr_authored,
            issue_comments_authored,
            pr_reviews_authored,
            pr_review_comments_authored,
            mentions
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    if not args.no_stdout:
        print("[github]")
        write_rows(
            [
                "login",
                "issue_or_pr_authored",
                "issue_comments_authored",
                "pr_reviews_authored",
                "pr_review_comments_authored",
                "mentions",
            ],
            rows,
            args.format,
        )
        print(f"\nrows={len(rows)}")
    return len(rows)


def main() -> int:
    args = parse_args()
    output_conn = init_output_db(args.output)
    output_conn.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            ("source", args.source),
            ("slack_input", str(args.slack_input)),
            ("github_input_dir", str(args.github_input_dir)),
            ("contains", args.contains),
            ("limit", str(args.limit)),
        ],
    )
    slack_count = 0
    github_count = 0
    if args.source in {"slack", "all"}:
        slack_count = emit_slack_users(args, output_conn)
        if args.source == "all" and not args.no_stdout:
            print()
    if args.source in {"github", "all"}:
        github_count = emit_github_users(args, output_conn)
    output_conn.commit()
    output_conn.close()
    if args.no_stdout:
        print(
            f"wrote sqlite to {args.output} (slack_rows={slack_count}, github_rows={github_count})"
        )
    else:
        print(f"\nsqlite={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
