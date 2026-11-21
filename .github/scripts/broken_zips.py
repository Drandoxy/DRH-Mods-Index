#!/usr/bin/env python3
# Copyright (c) Tutez. GPLv3: see .github/LICENSE.
"""Opens an issue for each indexed version that no longer installs, its zip
gone from its release or changed since it was indexed, mentioning who can fix
it, and closes it once the version installs again or is withdrawn.

Reads in BROKEN what `DRH-Launcher --build-index` wrote of them: the broken
versions, and the repositories GitHub did not answer for, whose issues stay as
they are. Run from the index's checkout, by the Deploy workflow. DRY_RUN set:
says what it would do, changes nothing.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

LABEL = "broken-zip"
TITLE = "{id} {version}: its zip no longer installs"
TITLE_PATTERN = re.compile(r"([a-z0-9_]+) ([0-9]+\.[0-9]+\.[0-9]+): its zip no longer installs")
DRY_RUN = bool(os.environ.get("DRY_RUN"))


def api(method, path, body=None):
    """`path` from the API's root, or from the repository's without a leading slash."""
    if not path.startswith("/"):
        path = f"/repos/{os.environ['GITHUB_REPOSITORY']}/{path}"
    if DRY_RUN and method != "GET":
        print(f"would {method} {path}: {json.dumps(body)}")
        return {}
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        text = response.read()
        return json.loads(text) if text else {}


def pages(path):
    """Every item of a listing, page by page."""
    items, page = [], 1
    while True:
        batch = api("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def who_fixes(version):
    """The login to mention: the repository's owner, or, when it is an
    organization, whose mention notifies no one, who proposed the entry."""
    owner = version["source"].removeprefix("https://github.com/").split("/")[0]
    try:
        if api("GET", f"/users/{owner}").get("type") == "User":
            return owner
        commits = pages(f"commits?path=mods/{version['id']}/{version['version']}.json")
        pulls = api("GET", f"commits/{commits[-1]['sha']}/pulls") if commits else []
        return pulls[0]["user"]["login"] if pulls else None
    except urllib.error.HTTPError as error:
        print(f"::warning::No one to mention for {version['id']} {version['version']}: {error}")
        return None


def issue_body(version, login):
    id, number, source = version["id"], version["version"], version["source"]
    tag, zip_name, sha256 = version["tag"], version["zip"], version["sha256"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if version["problem"] == "changed":
        problem = (f"`{zip_name}` in release `{tag}` is not the file the index checked: "
                   f"its SHA-256 is `{version.get('found')}`, the entry's `{sha256}`.")
    else:
        problem = (f"`{zip_name}` is no longer in release `{tag}`: the release or the file "
                   "was deleted, or renamed.")
    lines = [
        f"{'@' + login + ': p' if login else 'P'}layers can no longer install {id} {number}. {problem}",
        "",
        f"- Entry: [mods/{id}/{number}.json](https://github.com/{repository}/blob/HEAD/mods/{id}/{number}.json)",
        f"- Release: {source}/releases/tag/{tag}",
        "",
        "The launcher refuses that zip: installing or updating to this version fails, and who has it keeps it.",
        "",
        "To fix it, either:",
        f"- put back in release `{tag}` the zip the index checked (SHA-256 `{sha256}`), or",
        "- publish a new version and submit it (`DRH-Launcher --submit-mod`). This one can then be withdrawn: "
        "a pull request adds `\"yanked\": \"<why>\"` to its entry.",
        "",
        "This issue closes by itself once the zip is found again or the version is withdrawn.",
    ]
    return "\n".join(lines)


def source_of(title):
    """The `source` of the entry an issue's title names, None if it names none."""
    match = TITLE_PATTERN.fullmatch(title)
    if not match:
        return None
    entry = Path("mods", match[1], f"{match[2]}.json")
    return json.loads(entry.read_text())["source"] if entry.is_file() else None


def main():
    report = json.loads(os.environ["BROKEN"])
    broken = {TITLE.format(**version): version for version in report["broken"]}
    unread = set(report["unread"])

    if broken and not any(label["name"] == LABEL for label in pages("labels")):
        api("POST", "labels", {"name": LABEL, "color": "d73a4a",
                               "description": "An indexed version whose zip no longer installs"})
    issues = [issue for issue in pages(f"issues?state=open&labels={LABEL}") if "pull_request" not in issue]
    opened = {issue["title"]: issue for issue in issues}

    for title, version in broken.items():
        if title in opened:
            print(f"#{opened[title]['number']} is already open: {title}")
            continue
        created = api("POST", "issues", {"title": title, "body": issue_body(version, who_fixes(version)),
                                         "labels": [LABEL]})
        if not DRY_RUN:
            print(f"Opened #{created['number']}: {title}")

    for title, issue in opened.items():
        if title in broken:
            continue
        if source_of(title) in unread:
            print(f"#{issue['number']} stays open: GitHub did not answer for its repository")
            continue
        api("POST", f"issues/{issue['number']}/comments",
            {"body": "The index finds this version's zip again, or the version is withdrawn: closing."})
        api("PATCH", f"issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
        print(f"Closed #{issue['number']}: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
