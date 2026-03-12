from __future__ import annotations

from xul_slackbot.user_context_export import (
    SlackContextMatch,
    SlackMessage,
    build_github_context,
    build_slack_context_matches,
    collect_slack_context_messages,
)


def test_slack_thread_context_includes_full_thread() -> None:
    messages = [
        SlackMessage(
            channel_id="C1",
            ts="100.0",
            thread_ts=None,
            text="before",
            user_id="U9",
            subtype=None,
            raw_json="{}",
        ),
        SlackMessage(
            channel_id="C1",
            ts="101.0",
            thread_ts="101.0",
            text="root message",
            user_id="U1",
            subtype=None,
            raw_json="{}",
        ),
        SlackMessage(
            channel_id="C1",
            ts="102.0",
            thread_ts="101.0",
            text="reply",
            user_id="U2",
            subtype=None,
            raw_json="{}",
        ),
        SlackMessage(
            channel_id="C1",
            ts="103.0",
            thread_ts=None,
            text="after",
            user_id="U9",
            subtype=None,
            raw_json="{}",
        ),
    ]

    matches = build_slack_context_matches(messages, "U1")
    contexts = collect_slack_context_messages(messages, matches, context_window=1)

    assert len(matches) == 1
    assert matches[0].context_key == "thread:C1:101.0"
    assert [message.ts for message in contexts[matches[0].context_key]] == ["101.0", "102.0"]


def test_slack_message_window_includes_neighbors() -> None:
    messages = [
        SlackMessage("C1", "100.0", None, "m0", "U0", None, "{}"),
        SlackMessage("C1", "101.0", None, "m1", "U1", None, "{}"),
        SlackMessage("C1", "102.0", None, "m2", "U0", None, "{}"),
        SlackMessage("C1", "103.0", None, "m3", "U0", None, "{}"),
    ]

    matches = [SlackContextMatch("message:C1:101.0", "C1", None, "101.0", "authored", "101.0")]
    contexts = collect_slack_context_messages(messages, matches, context_window=1)

    assert [message.ts for message in contexts["message:C1:101.0"]] == ["100.0", "101.0", "102.0"]


def test_github_context_matches_author_and_mentions() -> None:
    record = {
        "repo": "acme/project",
        "kind": "pr",
        "number": 42,
        "issue_or_pr": {
            "title": "Improve exports",
            "state": "open",
            "html_url": "https://example.invalid/pr/42",
            "created_at": "2026-03-01T00:00:00Z",
            "user": {"login": "alice"},
            "body": "Need review from @bob",
        },
        "issue_comments": [
            {
                "id": 1,
                "user": {"login": "carol"},
                "created_at": "2026-03-01T01:00:00Z",
                "body": "Looping in @bob here too",
                "html_url": "https://example.invalid/pr/42#issuecomment-1",
            }
        ],
        "pull_request_reviews": [
            {
                "id": 2,
                "user": {"login": "bob"},
                "submitted_at": "2026-03-01T02:00:00Z",
                "body": "Looks good",
                "html_url": "https://example.invalid/pr/42#pullrequestreview-2",
            }
        ],
        "pull_request_review_comments": [],
    }

    context = build_github_context(record, "bob")

    assert context.matched is True
    assert set(context.match_reasons) == {
        "mentioned_in_body",
        "issue_comment_mention",
        "pull_request_review_author",
    }
    assert len(context.events) == 3
    assert context.events[0].matched is True
