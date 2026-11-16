#!/usr/bin/env python3
# Copyright (c) Tutez. GPLv3: see .github/LICENSE.
"""Posts the Check workflow's report on its pull request, in one comment kept
up to date, and what a push changed in a comment of its own, one for each push.

The reports come from the pull request's own run, which runs the workflow and
scripts of the pull request: one that changes them can write any report. Such
reports are not posted, only a warning. Reads its run from the environment, as
the Comment workflow sets it.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ENTRY_PATH = re.compile(r"mods/[a-z0-9_]+/[0-9]+\.[0-9]+\.[0-9]+\.json")
MARKER = "<!-- index-check -->"
PUSH_MARKER = "<!-- index-check-push {sha} -->"
BOT = "github-actions[bot]"
MAX_REPORT_CHARS = 60_000


def api(method, path, body=None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def pages(path):
    """Every item of a listing, page by page."""
    items, page = [], 1
    while True:
        batch = api("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def only_entries(pull):
    """Whether the pull request changes nothing but entries: its run is then the index's check."""
    # A rename counts by both names.
    names = [name for file in pages(f"pulls/{pull}/files") for name in (file["filename"], file.get("previous_filename"))]
    return all(name is None or ENTRY_PATH.fullmatch(name) for name in names)


def body(trusted, conclusion, run_url):
    head = [MARKER, f"**Index check: {conclusion}.** [Its run]({run_url}) has the whole report.", ""]
    if not trusted:
        return "\n".join(head + [
            "This pull request changes files outside `mods/<id>/<version>.json`, so its run may not be the index's check: "
            "its report is not shown here.",
        ])
    report = Path(os.environ["REPORT"])
    if not report.is_file():
        return "\n".join(head + ["The run wrote no report."])
    text = report.read_text()
    if len(text) > MAX_REPORT_CHARS:
        return "\n".join(head + ["The report is too long for a comment."])
    return "\n".join(head + [text])


def push_body(sha, run_url):
    """What the push to `sha` changed, None when its run wrote nothing of it."""
    report = Path(os.environ.get("PUSH_REPORT", ""))
    if not report.is_file():
        return None
    head = [PUSH_MARKER.format(sha=sha), f"**What the push to `{sha[:7]}` changed.** [Its run]({run_url}) has the whole report.", ""]
    text = report.read_text()
    if len(text) > MAX_REPORT_CHARS:
        return "\n".join(head + ["The report is too long for a comment."])
    return "\n".join(head + [text])


def post(pull, comments, marker, text):
    """One comment for a marker: a run made again writes over its own."""
    mine = [comment for comment in comments if comment["user"]["login"] == BOT and comment["body"].startswith(marker)]
    if mine:
        api("PATCH", f"issues/comments/{mine[0]['id']}", {"body": text})
    else:
        api("POST", f"issues/{pull}/comments", {"body": text})


def main():
    sha = os.environ["HEAD_SHA"]
    pulls = [pull["number"] for pull in pages("pulls?state=open") if pull["head"]["sha"] == sha]
    if not pulls:
        print(f"No open pull request is at {sha} any more.")
        return 0
    for pull in pulls:
        trusted = only_entries(pull)
        comments = pages(f"issues/{pull}/comments")
        post(pull, comments, MARKER, body(trusted, os.environ["CONCLUSION"], os.environ["RUN_URL"]))
        pushed = push_body(sha, os.environ["RUN_URL"]) if trusted else None
        if pushed is not None:
            post(pull, comments, PUSH_MARKER.format(sha=sha), pushed)
        print(f"Commented on #{pull}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
