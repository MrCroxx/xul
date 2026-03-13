from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from xul_slackbot.lancedb import connect_lancedb
from xul_slackbot.logging import configure_logging
from xul_slackbot.necromancy import connect_necromancy_db, handle_mecromancy_command
from xul_slackbot.summon import build_summoned_reply, handle_summon_command, init_summon_schema


LOGGER = logging.getLogger(__name__)
DEFAULT_LANCEDB_DIR = Path("data/lancedb")
DEFAULT_NECROMANCY_SQLITE = Path("data/necromancy.sqlite")

if TYPE_CHECKING:
    from slack_bolt import App


MENTION_PREFIX_RE = re.compile(r"^\s*<@[^>]+>\s*")
DIRECT_COMMAND_PREFIXES = ("/slack", "/github", "/link", "/links", "/summon")
RECEIVED_REACTION = "eyes"
REPLIED_REACTION = "smiling_imp"


def build_mention_reply(message_text: str) -> str:
    return f"The necromancer hears the invocation: {message_text}"


def format_xul_progress(message: str) -> str:
    templates = {
        "Resolving linked necromancy": "Xul traces the name through ash and old pacts, seeking the soul bound to your call.",
        "Checking local context dumps": "Xul parts the grave dust and inspects the relics already sealed in the catacombs.",
        "Context dumps already exist": "The relics are already laid upon the altar. No fresh exhumation is needed.",
        "Exporting Slack context dump": "Xul lowers a hook into the Slack catacombs and drags old voices toward the surface.",
        "Exporting GitHub context dump": "Xul scrapes the runes from GitHub's iron tablets and gathers them for the rite.",
        "Context dumps are ready": "The bones and fragments are assembled. The chamber is ready for deeper craft.",
        "Building soul profile": "Xul presses the stolen voices into a black grimoire, distilling cadence from memory.",
        "Building isolated LanceDB table": "Xul raises a private crypt of indices so the shade may hunt its own memories.",
        "Activating summoned necromancy": "Xul seals the rite with grave-salt and bids the shade take its seat upon the throne of whispers.",
        "Summon completed": "The candles gutter. The pact holds. The dead now answer when called.",
    }
    return templates.get(
        message,
        f"Xul murmurs over the ritual circle: {message}",
    )


def extract_mention_command(message_text: str) -> str:
    return MENTION_PREFIX_RE.sub("", message_text, count=1).strip()


def should_handle_mecromancy_mention(message_text: str) -> bool:
    normalized = extract_mention_command(message_text).lower()
    return normalized.startswith(DIRECT_COMMAND_PREFIXES)


def build_app_mention_reply(
    necromancy_sqlite: str | Path,
    lancedb: Any,
    message_text: str,
    thread_ts: str | None = None,
) -> str:
    command_text = extract_mention_command(message_text)
    if command_text.lower().startswith(DIRECT_COMMAND_PREFIXES):
        normalized = command_text[1:].strip()
        if normalized.lower().startswith("summon"):
            payload = normalized[len("summon") :].strip()
            return handle_summon_command(
                necromancy_sqlite,
                lancedb,
                payload,
                scope_key=thread_ts or None,
            )
        return handle_mecromancy_command(necromancy_sqlite, normalized)
    return build_summoned_reply(
        necromancy_sqlite,
        lancedb,
        command_text,
        scope_key=thread_ts or None,
    )


def emit_slash_progress(respond, percent: int, message: str) -> None:
    respond(format_xul_progress(message))


def emit_thread_progress(say, thread_ts: str | None, percent: int, message: str) -> None:
    say(text=format_xul_progress(message), thread_ts=thread_ts)


def add_message_reaction(client: Any, event: dict, reaction: str, logger: logging.Logger) -> None:
    channel = event.get("channel")
    timestamp = event.get("ts")
    if not isinstance(channel, str) or not channel:
        return
    if not isinstance(timestamp, str) or not timestamp:
        return
    try:
        client.reactions_add(channel=channel, timestamp=timestamp, name=reaction)
    except Exception as err:
        logger.warning("Failed to add Slack reaction %s: %s", reaction, err)


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
    init_summon_schema(init_conn)
    init_conn.close()
    app.necromancy_sqlite = str(necromancy_path)

    @app.event("app_mention")
    def handle_app_mention(event: dict, say, client, logger) -> None:
        message_text = event.get("text", "")
        add_message_reaction(client, event, RECEIVED_REACTION, logger)
        command_text = extract_mention_command(message_text)
        thread_ts = resolve_thread_reply_ts(event)
        if command_text.lower().startswith("/summon"):
            payload = command_text[len("/summon") :].strip()
            reply = handle_summon_command(
                app.necromancy_sqlite,
                app.lancedb,
                payload,
                scope_key=thread_ts or None,
                progress=lambda percent, msg: emit_thread_progress(
                    say, thread_ts, percent, msg
                ),
            )
            say(text=reply, thread_ts=thread_ts)
            add_message_reaction(client, event, REPLIED_REACTION, logger)
            return
        say(
            text=build_app_mention_reply(
                app.necromancy_sqlite,
                app.lancedb,
                message_text,
                thread_ts=thread_ts,
            ),
            thread_ts=thread_ts,
        )
        add_message_reaction(client, event, REPLIED_REACTION, logger)

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

    @app.command("/summon")
    def handle_summon_slash_command(ack, respond, command) -> None:
        ack()
        respond(
            handle_summon_command(
                app.necromancy_sqlite,
                app.lancedb,
                command.get("text", ""),
                scope_key=str(command.get("thread_ts") or ""),
                progress=lambda percent, msg: emit_slash_progress(
                    respond, percent, msg
                ),
            )
        )

    @app.event("message")
    def handle_message_event(event: dict, logger) -> None:
        if should_ignore_message_event(event):
            return

        logger.debug("Ignoring non-mention message event: %s", event.get("text", ""))

    return app


def main(argv: Sequence[str] | None = None) -> None:
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    configure_logging()
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
