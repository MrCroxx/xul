from __future__ import annotations

import logging

from xul_slackbot.config import get_config_value


def configure_logging() -> None:
    level_name = get_config_value("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if root_logger.handlers:
        root_logger.handlers.clear()

    try:
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        formatter = logging.Formatter("%(name)s: %(message)s")
    except ImportError:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    handler.setLevel(level)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
