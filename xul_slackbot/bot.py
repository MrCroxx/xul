from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from xul_slackbot.lancedb import connect_lancedb
from xul_slackbot.necromancy import connect_necromancy_db, handle_mecromancy_command


LOGGER = logging.getLogger(__name__)
DEFAULT_LANCEDB_DIR = Path("data/lancedb")
DEFAULT_NECROMANCY_SQLITE = Path("data/necromancy.sqlite")

if TYPE_CHECKING:
    from slack_bolt import App


MENTION_PREFIX_RE = re.compile(r"^\s*<@[^>]+>\s*")
DIRECT_COMMAND_PREFIXES = ("/slack", "/github", "/link", "/links")


def build_mention_reply(message_text: str) -> str:
    return f"Received: {message_text}"


def extract_mention_command(message_text: str) -> str:
    return MENTION_PREFIX_RE.sub("", message_text, count=1).strip()


def should_handle_mecromancy_mention(message_text: str) -> bool:
    normalized = extract_mention_command(message_text).lower()
    return normalized.startswith(DIRECT_COMMAND_PREFIXES)


def build_app_mention_reply(necromancy_sqlite: str | Path, message_text: str) -> str:
    command_text = extract_mention_command(message_text)
    if command_text.lower().startswith(DIRECT_COMMAND_PREFIXES):
        command_text = command_text[1:].strip()
        return handle_mecromancy_command(necromancy_sqlite, command_text)
    return build_mention_reply(message_text)


def resolve_thread_reply_ts(event: dict) -> str | None:
    thread_ts = event.get("thread_ts")
    if isinstance(thread_ts, str) and thread_ts:
        return thread_ts
    ts = event.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    return None


def should_ignore_message_event(event: dict) -> bool:
    subtype = event.get("subtype")
    if subtype in {"bot_message", "message_changed", "message_deleted"}:
        return True

    return "bot_id" in event


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Xul Slack bot.")
    parser.add_argument(
        "--lancedb-dir",
        default=os.getenv("LANCEDB_DIR", str(DEFAULT_LANCEDB_DIR)),
        help="Local directory used by LanceDB.",
    )
    parser.add_argument(
        "--necromancy-sqlite",
        default=os.getenv("NECROMANCY_SQLITE", str(DEFAULT_NECROMANCY_SQLITE)),
        help="Local sqlite file used by mecromancy queries.",
    )
    return parser


def create_app(
    bot_token: str | None = None,
    lancedb_dir: str | Path | None = None,
    necromancy_sqlite: str | Path | None = None,
) -> Any:
    from slack_bolt import App

    token = bot_token or os.environ["SLACK_BOT_TOKEN"]
    app = App(token=token)
    db_dir = Path(lancedb_dir) if lancedb_dir is not None else DEFAULT_LANCEDB_DIR
    necromancy_path = (
        Path(necromancy_sqlite)
        if necromancy_sqlite is not None
        else DEFAULT_NECROMANCY_SQLITE
    )
    app.lancedb = connect_lancedb(db_dir)
    app.lancedb_dir = str(db_dir)
    init_conn = connect_necromancy_db(necromancy_path)
    init_conn.close()
    app.necromancy_sqlite = str(necromancy_path)

    @app.event("app_mention")
    def handle_app_mention(event: dict, say) -> None:
        message_text = event.get("text", "")
        say(
            text=build_app_mention_reply(app.necromancy_sqlite, message_text),
            thread_ts=resolve_thread_reply_ts(event),
        )

    def handle_direct_slash_command(subcommand: str, ack, respond, command) -> None:
        ack()
        response_text = handle_mecromancy_command(app.necromancy_sqlite, subcommand)
        text = command.get("text", "")
        if text:
            response_text = handle_mecromancy_command(
                app.necromancy_sqlite, f"{subcommand} {text}"
            )
        respond(response_text)

    @app.command("/slack")
    def handle_slack_slash_command(ack, respond, command) -> None:
        handle_direct_slash_command("slack", ack, respond, command)

    @app.command("/github")
    def handle_github_slash_command(ack, respond, command) -> None:
        handle_direct_slash_command("github", ack, respond, command)

    @app.command("/link")
    def handle_link_slash_command(ack, respond, command) -> None:
        handle_direct_slash_command("link", ack, respond, command)

    @app.command("/links")
    def handle_links_slash_command(ack, respond, command) -> None:
        handle_direct_slash_command("links", ack, respond, command)

    @app.event("message")
    def handle_message_event(event: dict, logger) -> None:
        if should_ignore_message_event(event):
            return

        logger.debug("Ignoring non-mention message event: %s", event.get("text", ""))

    return app


def main(argv: Sequence[str] | None = None) -> None:
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    args = build_arg_parser().parse_args(argv)
    app_token = os.environ["SLACK_APP_TOKEN"]
    app = create_app(
        lancedb_dir=args.lancedb_dir,
        necromancy_sqlite=args.necromancy_sqlite,
    )
    handler = SocketModeHandler(app, app_token)

    LOGGER.info(
        "Slack bot running with LanceDB at %s and mecromancy sqlite at %s",
        app.lancedb_dir,
        app.necromancy_sqlite,
    )
    handler.start()


if __name__ == "__main__":
    main()
