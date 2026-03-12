from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from xul_slackbot.lancedb import connect_lancedb


LOGGER = logging.getLogger(__name__)
DEFAULT_LANCEDB_DIR = Path("data/lancedb")

if TYPE_CHECKING:
    from slack_bolt import App


def build_mention_reply(message_text: str) -> str:
    return f"Received: {message_text}"


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
    return parser


def create_app(bot_token: str | None = None, lancedb_dir: str | Path | None = None) -> Any:
    from slack_bolt import App

    token = bot_token or os.environ["SLACK_BOT_TOKEN"]
    app = App(token=token)
    db_dir = Path(lancedb_dir) if lancedb_dir is not None else DEFAULT_LANCEDB_DIR
    app.lancedb = connect_lancedb(db_dir)
    app.lancedb_dir = str(db_dir)

    @app.event("app_mention")
    def handle_app_mention(event: dict, say) -> None:
        message_text = event.get("text", "")
        say(build_mention_reply(message_text))

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
    app = create_app(lancedb_dir=args.lancedb_dir)
    handler = SocketModeHandler(app, app_token)

    LOGGER.info("Slack bot running with LanceDB at %s", app.lancedb_dir)
    handler.start()


if __name__ == "__main__":
    main()
