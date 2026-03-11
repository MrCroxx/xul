#!/usr/bin/env python3
"""Export all issues/PRs and their comments from a GitHub repository.

Each issue/PR is written as one JSON file in the target output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_USER_AGENT = "github-issues-pr-exporter"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def parse_repo(repo: str) -> Tuple[str, str]:
    text = repo.strip().strip("/")
    if text.startswith("https://github.com/"):
        text = text[len("https://github.com/") :].strip("/")
    parts = text.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"invalid repo: {repo!r}, expected owner/repo")
    return parts[0], parts[1]


def parse_link_header(link_header: str) -> Dict[str, str]:
    links: Dict[str, str] = {}
    for part in link_header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"\s*', part)
        if match:
            url, rel = match.group(1), match.group(2)
            links[rel] = url
    return links


class GitHubClient:
    def __init__(
        self,
        token: str,
        timeout: int = 30,
        max_retries: int = 5,
        backoff_seconds: float = 1.5,
        max_backoff_seconds: float = 30.0,
        verbose: bool = False,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.verbose = verbose

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def _request(self, url: str) -> Tuple[object, urllib.response.addinfourl]:
        headers = self._build_headers()
        req = urllib.request.Request(url, headers=headers, method="GET")

        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
                    if payload:
                        data = json.loads(payload.decode("utf-8"))
                    else:
                        data = None
                    return data, resp
            except urllib.error.HTTPError as err:
                if self._should_rate_limit_wait(err):
                    wait_seconds = self._rate_limit_wait_seconds(err)
                    self._log(
                        f"rate limited (status={err.code}), sleep {wait_seconds:.1f}s: {url}"
                    )
                    time.sleep(wait_seconds)
                    continue
                if err.code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    wait_seconds = self._retry_delay_seconds(attempt, err.headers)
                    self._log(
                        f"retryable status={err.code}, retry in {wait_seconds:.1f}s: {url}"
                    )
                    time.sleep(wait_seconds)
                    continue

                body = err.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"GitHub API error status={err.code} url={url} body={body}"
                ) from err
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionResetError,
                socket.timeout,
            ) as err:
                if attempt < self.max_retries:
                    wait_seconds = self._retry_delay_seconds(attempt)
                    self._log(
                        f"network error ({err}), retry in {wait_seconds:.1f}s: {url}"
                    )
                    time.sleep(wait_seconds)
                    continue
                raise RuntimeError(f"network error url={url}: {err}") from err

        raise RuntimeError(f"exhausted retries for url={url}")

    def _should_rate_limit_wait(self, err: urllib.error.HTTPError) -> bool:
        if err.code != 403:
            return False
        remaining = err.headers.get("X-RateLimit-Remaining")
        return remaining == "0"

    def _rate_limit_wait_seconds(self, err: urllib.error.HTTPError) -> float:
        reset_str = err.headers.get("X-RateLimit-Reset")
        if not reset_str:
            return 60.0
        try:
            reset_ts = int(reset_str)
        except ValueError:
            return 60.0
        # Add a small safety margin to avoid immediate next 403.
        return max(1.0, reset_ts - time.time() + 1.0)

    def _retry_delay_seconds(
        self, attempt: int, headers: Optional[Dict[str, str]] = None
    ) -> float:
        if headers is not None:
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.1, min(float(retry_after), self.max_backoff_seconds))
                except ValueError:
                    pass

        base = self.backoff_seconds * (2**attempt)
        capped = min(base, self.max_backoff_seconds)
        jitter = random.uniform(0.8, 1.2)
        return max(0.1, capped * jitter)

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[INFO] {message}", file=sys.stderr)

    def get_json(self, url: str, params: Optional[Dict[str, object]] = None) -> object:
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{sep}{query}"
        data, _resp = self._request(url)
        return data

    def iter_paginated(
        self, url: str, params: Optional[Dict[str, object]] = None
    ) -> Iterator[object]:
        first_url = url
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            first_url = f"{url}{sep}{query}"

        next_url: Optional[str] = first_url
        while next_url is not None:
            data, resp = self._request(next_url)
            if not isinstance(data, list):
                raise RuntimeError(f"expected list payload for paginated API: {next_url}")
            for item in data:
                yield item
            link_header = resp.headers.get("Link", "")
            links = parse_link_header(link_header) if link_header else {}
            next_url = links.get("next")


def fetch_issue_comments(client: GitHubClient, comments_url: str) -> List[object]:
    return list(client.iter_paginated(comments_url, params={"per_page": 100}))


def fetch_pull_request_review_comments(
    client: GitHubClient, owner: str, repo: str, number: int
) -> List[object]:
    url = f"{API_ROOT}/repos/{owner}/{repo}/pulls/{number}/comments"
    return list(client.iter_paginated(url, params={"per_page": 100}))


def fetch_pull_request_reviews(
    client: GitHubClient, owner: str, repo: str, number: int
) -> List[object]:
    url = f"{API_ROOT}/repos/{owner}/{repo}/pulls/{number}/reviews"
    return list(client.iter_paginated(url, params={"per_page": 100}))


def safe_write_json(path: Path, obj: object) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    temp_path.replace(path)


def load_record_stats(path: Path) -> Optional[Tuple[int, int, int]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    issue_comments = data.get("issue_comments")
    review_comments = data.get("pull_request_review_comments")
    reviews = data.get("pull_request_reviews")

    issue_comment_count = len(issue_comments) if isinstance(issue_comments, list) else 0
    review_comment_count = len(review_comments) if isinstance(review_comments, list) else 0
    review_count = len(reviews) if isinstance(reviews, list) else 0
    return issue_comment_count, review_comment_count, review_count


def log(message: str) -> None:
    print(message, flush=True)


def format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def export_repo(
    repo: str,
    output_dir: Path,
    token: str,
    state: str,
    verbose: bool,
    resume: bool,
    max_retries: int,
    backoff_seconds: float,
    max_backoff_seconds: float,
) -> None:
    started_at = time.time()
    owner, name = parse_repo(repo)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()

    client = GitHubClient(
        token=token,
        verbose=verbose,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )
    issues_url = f"{API_ROOT}/repos/{owner}/{name}/issues"
    log(
        f"[START] repo={owner}/{name} state={state} output_dir={output_dir} "
        f"api_version={API_VERSION}"
    )
    log("[STEP 1/2] fetching issue/pr index ...")
    index_started_at = time.time()
    issues: List[object] = []
    for item in client.iter_paginated(
        issues_url,
        params={
            "state": state,
            "per_page": 100,
            "sort": "created",
            "direction": "asc",
        },
    ):
        issues.append(item)
        if len(issues) % 100 == 0:
            elapsed = time.time() - index_started_at
            log(
                f"[STEP 1/2] index progress fetched={len(issues)} "
                f"elapsed={format_duration(elapsed)}"
            )
    index_elapsed = time.time() - index_started_at
    log(
        f"[STEP 1/2] index completed total={len(issues)} "
        f"elapsed={format_duration(index_elapsed)}"
    )

    total_items = len(issues)
    total_prs = sum(1 for item in issues if isinstance(item, dict) and "pull_request" in item)
    total_issues = total_items - total_prs
    log(
        f"[STEP 2/2] fetched {total_items} items "
        f"(issues={total_issues}, prs={total_prs})"
    )
    log(
        f"[STEP 2/2] dump mode={'resume' if resume else 'overwrite'} "
        f"output_dir={output_dir}"
    )
    log("[PROGRESS] start dumping per-item JSON ...")

    total_issue_comments = 0
    total_review_comments = 0
    total_reviews = 0
    resumed_items = 0

    for index, issue in enumerate(issues, start=1):
        item_started_at = time.time()
        if not isinstance(issue, dict):
            raise RuntimeError(f"unexpected issue payload type: {type(issue)!r}")
        number = issue["number"]
        is_pr = "pull_request" in issue
        kind = "pr" if is_pr else "issue"
        out_path = output_dir / f"{kind}_{number:06d}.json"

        if resume and out_path.exists():
            cached_stats = load_record_stats(out_path)
            if cached_stats is not None:
                issue_comment_count, review_comment_count, review_count = cached_stats
                total_issue_comments += issue_comment_count
                total_review_comments += review_comment_count
                total_reviews += review_count
                resumed_items += 1

                elapsed = time.time() - started_at
                speed = index / elapsed if elapsed > 0 else 0.0
                remain = total_items - index
                eta_seconds = (remain / speed) if speed > 0 else 0.0
                item_elapsed = time.time() - item_started_at
                percent = (index / total_items * 100.0) if total_items > 0 else 100.0
                log(
                    f"[{index}/{total_items} {percent:6.2f}%] "
                    f"{kind}#{number} reused={out_path.name} "
                    f"issue_comments={issue_comment_count} "
                    f"pr_review_comments={review_comment_count} "
                    f"pr_reviews={review_count} "
                    f"item_elapsed={item_elapsed:.2f}s "
                    f"total_elapsed={format_duration(elapsed)} "
                    f"eta={format_duration(eta_seconds)}"
                )
                continue

            log(f"[WARN] invalid existing file, refetching: {out_path}")

        issue_comments = fetch_issue_comments(client, issue["comments_url"])

        record: Dict[str, object] = {
            "repo": f"{owner}/{name}",
            "kind": kind,
            "number": number,
            "issue_or_pr": issue,
            "issue_comments": issue_comments,
        }
        issue_comment_count = len(issue_comments)
        total_issue_comments += issue_comment_count

        review_comment_count = 0
        review_count = 0

        if is_pr:
            pr_url = issue["pull_request"]["url"]
            pr_detail = client.get_json(pr_url)
            review_comments = fetch_pull_request_review_comments(
                client, owner, name, number
            )
            reviews = fetch_pull_request_reviews(client, owner, name, number)
            review_comment_count = len(review_comments)
            review_count = len(reviews)
            total_review_comments += review_comment_count
            total_reviews += review_count
            record["pull_request"] = pr_detail
            record["pull_request_review_comments"] = review_comments
            record["pull_request_reviews"] = reviews

        safe_write_json(out_path, record)
        elapsed = time.time() - started_at
        speed = index / elapsed if elapsed > 0 else 0.0
        remain = total_items - index
        eta_seconds = (remain / speed) if speed > 0 else 0.0
        item_elapsed = time.time() - item_started_at
        percent = (index / total_items * 100.0) if total_items > 0 else 100.0
        log(
            f"[{index}/{total_items} {percent:6.2f}%] "
            f"{kind}#{number} wrote={out_path.name} "
            f"issue_comments={issue_comment_count} "
            f"pr_review_comments={review_comment_count} "
            f"pr_reviews={review_count} "
            f"item_elapsed={item_elapsed:.2f}s "
            f"total_elapsed={format_duration(elapsed)} "
            f"eta={format_duration(eta_seconds)}"
        )

    total_elapsed = time.time() - started_at
    speed = total_items / total_elapsed if total_elapsed > 0 else 0.0
    log(
        "[DONE] "
        f"items={total_items} issues={total_issues} prs={total_prs} "
        f"issue_comments={total_issue_comments} "
        f"pr_review_comments={total_review_comments} "
        f"pr_reviews={total_reviews} "
        f"resumed_items={resumed_items} "
        f"elapsed={format_duration(total_elapsed)} "
        f"avg_speed={speed:.2f} items/s "
        f"output_dir={output_dir}"
    )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export all GitHub issues/PRs with comments into per-item JSON files."
    )
    parser.add_argument(
        "repo",
        help="GitHub repo in owner/repo format, or full URL like https://github.com/owner/repo",
    )
    parser.add_argument("output_dir", help="Output directory for JSON files")
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN", ""),
        help="GitHub token (defaults to env GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--state",
        choices=["open", "closed", "all"],
        default="all",
        help="Issue/PR state filter (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print retry/rate-limit logs to stderr",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not reuse existing per-item JSON files; always refetch",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Max retry attempts for transient network/API errors (default: 5)",
    )
    parser.add_argument(
        "--backoff-seconds",
        type=float,
        default=1.5,
        help="Initial exponential backoff delay in seconds (default: 1.5)",
    )
    parser.add_argument(
        "--max-backoff-seconds",
        type=float,
        default=30.0,
        help="Maximum retry delay in seconds (default: 30.0)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    if not args.token:
        print(
            "missing GitHub token, pass --token or set GITHUB_TOKEN",
            file=sys.stderr,
        )
        return 2

    try:
        export_repo(
            repo=args.repo,
            output_dir=Path(args.output_dir),
            token=args.token,
            state=args.state,
            verbose=args.verbose,
            resume=not args.no_resume,
            max_retries=max(0, args.max_retries),
            backoff_seconds=max(0.1, args.backoff_seconds),
            max_backoff_seconds=max(0.1, args.max_backoff_seconds),
        )
    except Exception as err:  # noqa: BLE001
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
