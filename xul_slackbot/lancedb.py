from __future__ import annotations

from pathlib import Path
from typing import Any


def connect_lancedb(uri: str | Path) -> Any:
    db_path = Path(uri).expanduser().resolve()
    db_path.mkdir(parents=True, exist_ok=True)

    import lancedb

    return lancedb.connect(str(db_path))
