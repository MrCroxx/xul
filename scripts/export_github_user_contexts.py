#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xul_slackbot.user_context_export import (  # noqa: E402
    export_github_user_contexts,
    load_github_dump_records,
    slugify,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export GitHub issue/PR/comment contexts into per-user sqlite databases."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("github_dump"),
        help="Directory containing issue_*.json and pr_*.json files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("user_context_exports/github"),
        help="Directory where per-user sqlite files will be written.",
    )
    parser.add_argument(
        "--user",
        action="append",
        required=True,
        help="Target GitHub login. Repeat this flag to export multiple users.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    records = load_github_dump_records(args.input_dir)

    for user in sorted({item.lower() for item in args.user}):
        output_path = args.output_dir / f"github_user_{slugify(user)}.sqlite"
        context_count = export_github_user_contexts(
            output_path=output_path,
            target_login=user,
            records=records,
            source_dir=args.input_dir,
        )
        print(f"exported {context_count} GitHub contexts for {user} -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
