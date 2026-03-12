#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xul_slackbot.user_context_export import (  # noqa: E402
    connect_sqlite,
    export_slack_user_context,
    load_latest_slack_channels,
    load_latest_slack_users,
    load_slack_messages,
    resolve_slack_users,
    slugify,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Slack user-related contexts into per-user sqlite databases."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("slackdump_archive_20260311/slackdump.sqlite"),
        help="Source slackdump sqlite path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("user_context_exports/slack"),
        help="Directory where per-user sqlite files will be written.",
    )
    parser.add_argument(
        "--user",
        action="append",
        required=True,
        help="Target Slack user selector. Supports user id, username, email, real name, or display name.",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=3,
        help="For non-thread messages, include N messages before and after the matched message.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_sqlite(args.input)
    users = load_latest_slack_users(conn)
    channels = load_latest_slack_channels(conn)
    messages = load_slack_messages(conn)
    conn.close()

    targets = resolve_slack_users(users, args.user)
    for user in sorted(targets.values(), key=lambda item: item.username.lower()):
        file_name = f"slack_user_{slugify(user.username or user.user_id)}.sqlite"
        output_path = args.output_dir / file_name
        context_count = export_slack_user_context(
            output_path=output_path,
            user=user,
            channels=channels,
            messages=messages,
            context_window=args.context_window,
            source_path=args.input,
        )
        print(
            f"exported {context_count} Slack contexts for {user.username} ({user.user_id}) -> {output_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
