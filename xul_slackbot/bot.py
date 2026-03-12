from __future__ import annotations

import logging
import os

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


LOGGER = logging.getLogger(__name__)


def build_mention_reply(message_text: str) -> str:
    return f"Received: {message_text}"


def should_ignore_message_event(event: dict) -> bool:
    subtype = event.get("subtype")
    if subtype in {"bot_message", "message_changed", "message_deleted"}:
        return True

    return "bot_id" in event


def create_app(bot_token: str | None = None) -> App:
    token = bot_token or os.environ["SLACK_BOT_TOKEN"]
    app = App(token=token)

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


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    app_token = os.environ["SLACK_APP_TOKEN"]
    handler = SocketModeHandler(create_app(), app_token)

    LOGGER.info("Slack bot running")
    handler.start()


if __name__ == "__main__":
    main()
