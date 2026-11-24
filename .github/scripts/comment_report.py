#!/usr/bin/env python3
# Copyright (c) Tutez. GPLv3: see .github/LICENSE.
"""Posts the Check workflow's report on its pull request, in one comment kept
up to date, and what a push changed in a comment of its own, one for each push.

The reports come from the pull request's own run, which runs the workflow and
scripts of the pull request: one that changes them can write any report. Such
reports are not posted, only a warning. Reads its run from the environment, as
the Comment workflow sets it.

A run is posted on the pull request it names (meta.json, beside the report),
when that one is to the default branch and still at the run's commit, and
nowhere else: two pull requests may share a commit, and one to another branch
is checked by that branch's workflow and scripts, against that branch's
entries. A run that names none shows no report.

The comment kept up to date records the commit whose report it shows. A push
whose report was never posted here, replaced by another before its run ended,
is then said with the next one: what that one changed is not all that changed
since the reviewer last read.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ENTRY_PATH = re.compile(r"mods/[a-z0-9_]+/[0-9]+\.[0-9]+\.[0-9]+\.json")
MARKER = "<!-- index-check -->"
SHOWN_MARKER = "<!-- index-check-shown {sha} -->"
SHOWN_PATTERN = re.compile(r"<!-- index-check-shown ([0-9a-f]{40}) -->")
PUSH_MARKER = "<!-- index-check-push {sha} -->"
BOT = "github-actions[bot]"
MAX_REPORT_CHARS = 60_000
SHA = re.compile(r"[0-9a-f]{40}")


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
    files = pages(f"pulls/{pull}/files")
    # GitHub lists 3,000 files at most: past that, some were not read.
    if len(files) != api("GET", f"pulls/{pull}").get("changed_files"):
        return False
    # A rename counts by both names.
    names = [name for file in files for name in (file["filename"], file.get("previous_filename"))]
    return all(name is None or ENTRY_PATH.fullmatch(name) for name in names)


def read_meta():
    """What the run said of itself, {} when it said nothing that reads."""
    try:
        meta = json.loads((Path(os.environ["REPORT"]).parent / "meta.json").read_text())
    except (OSError, ValueError, KeyError):
        return {}
    return meta if isinstance(meta, dict) else {}


def to_the_index(pull):
    """Whether a pull request is to this repository's default branch: the index."""
    base = pull["base"]
    return base["repo"]["full_name"] == os.environ["GITHUB_REPOSITORY"] and base["ref"] == base["repo"]["default_branch"]


def mine(comments, marker):
    return [comment for comment in comments if comment["user"]["login"] == BOT and comment["body"].startswith(marker)]


def shown_before(comments):
    """The commit whose report the comment kept up to date shows, None when it says none."""
    for comment in mine(comments, MARKER):
        found = SHOWN_PATTERN.search(comment["body"])
        return found.group(1) if found else None
    return None


def body(trusted, conclusion, run_url):
    """The comment kept up to date, and whether it gives the reviewer the run's report to read."""
    head = [f"**Index check: {conclusion}.** [Its run]({run_url}) has the whole report.", ""]
    if not trusted:
        return "\n".join(head + [
            "This pull request changes files outside `mods/<id>/<version>.json`, so its run may not be the index's check: "
            "its report is not shown here.",
        ]), False
    report = Path(os.environ["REPORT"])
    if not report.is_file():
        return "\n".join(head + ["The run wrote no report."]), False
    text = report.read_text()
    if len(text) > MAX_REPORT_CHARS:
        return "\n".join(head + ["The report is too long for a comment."]), True
    return "\n".join(head + [text]), True


def unbound_body(conclusion, run_url):
    """For a run that does not name its pull request: nothing of it is shown but that it ran."""
    return "\n".join([
        f"**Index check: no report.** [A run]({run_url}) on this commit ended ({conclusion}) without saying which pull request "
        "it checked, so nothing of it is shown here, and what this comment showed before is out of date. "
        "Run the check again to have its report.",
    ])


def push_body(sha, run_url, skipped):
    """What the push to `sha` changed, None when its run wrote nothing of it. `skipped`: the
    head before it and the commit of the last report shown, when that head's was never posted."""
    report = Path(os.environ.get("PUSH_REPORT", ""))
    if not report.is_file() and not skipped:
        return None
    head = [PUSH_MARKER.format(sha=sha), f"**What the push to `{sha[:7]}` changed.** [Its run]({run_url}) has the whole report.", ""]
    if skipped:
        before, shown = skipped
        head += [
            f"**This is not all that changed since the last report shown here.** The head before this push, `{before[:7]}`, "
            f"was replaced before its report could be posted: the last one shown was for `{shown[:7]}`. "
            "Read the whole diff again, in the comment above, not only what this push changed.",
            "",
        ]
    if not report.is_file():
        return "\n".join(head + ["This push changed no entry's sources."])
    text = report.read_text()
    if len(text) > MAX_REPORT_CHARS:
        return "\n".join(head + ["The report is too long for a comment."])
    return "\n".join(head + [text])


def post(pull, comments, marker, text):
    """One comment for a marker: a run made again writes over its own."""
    held = mine(comments, marker)
    if held:
        api("PATCH", f"issues/comments/{held[0]['id']}", {"body": text})
    else:
        api("POST", f"issues/{pull}/comments", {"body": text})


def with_markers(text, shown):
    return "\n".join([MARKER] + ([SHOWN_MARKER.format(sha=shown)] if shown else []) + [text])


def main():
    sha = os.environ["HEAD_SHA"]
    conclusion, run_url = os.environ["CONCLUSION"], os.environ["RUN_URL"]
    meta = read_meta()
    pulls = [pull["number"] for pull in pages("pulls?state=open") if pull["head"]["sha"] == sha and to_the_index(pull)]
    if not pulls:
        print(f"No open pull request to the index is at {sha} any more.")
        return 0
    named = meta.get("pull")
    if not isinstance(named, int) or isinstance(named, bool):
        for pull in pulls:
            comments = pages(f"issues/{pull}/comments")
            post(pull, comments, MARKER, with_markers(unbound_body(conclusion, run_url), shown_before(comments)))
            print(f"The run names no pull request: said so on #{pull}.")
        return 0
    if named not in pulls:
        print(f"The run is for #{named}, which is not an open pull request to the index at {sha}.")
        return 0

    pull = named
    trusted = only_entries(pull)
    comments = pages(f"issues/{pull}/comments")
    shown = shown_before(comments)
    text, read = body(trusted, conclusion, run_url)
    post(pull, comments, MARKER, with_markers(text, sha if read else shown))
    if trusted:
        before = meta.get("before")
        before = before if isinstance(before, str) and SHA.fullmatch(before) else None
        skipped = (before, shown) if before and shown and shown not in (before, sha) else None
        pushed = push_body(sha, run_url, skipped)
        if pushed is not None:
            post(pull, comments, PUSH_MARKER.format(sha=sha), pushed)
    print(f"Commented on #{pull}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
