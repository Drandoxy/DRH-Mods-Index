#!/usr/bin/env python3
# Copyright (c) Tutez. GPLv3: see .github/LICENSE.
"""Checks a pull request to the index.

It may only add mods/<id>/<version>.json files, each checked by the
launcher (--check-entry), or add `yanked` to an indexed one. Writes what the
reviewer reads, the sources' diff against the last indexed version included,
to the job summary, and with the diffs cut shorter to the file $REPORT, which
the Comment workflow posts on the pull request.

After a push, <before> is the branch's head before it: what the push changed
in each entry's sources, against the entry the pull request held then, goes to
the job summary too and to the file $PUSH_REPORT, posted as a comment of its
own. A reviewer who read that entry reads this diff rather than the whole one.

Usage: check_pull_request.py <launcher> <base> [<before>]
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ENTRY_PATH = re.compile(r"mods/[a-z0-9_]+/[0-9]+\.[0-9]+\.[0-9]+\.json")
MAX_DIFF_CHARS = 200_000
# A comment holds 65,536 characters; the diffs share this much of it.
COMMENT_DIFF_CHARS = 50_000


class Sources(str):
    """The sources' diff of an entry in the report, cut to fit where the report goes."""

    title = "Sources against the last indexed version"


def sources(text, title):
    block = Sources(text)
    block.title = title
    return block


def fenced(text, info=""):
    """Text from the pull request as a code block it cannot close: a mod's sources must not
    turn the rest of the report into Markdown."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + info, text, fence]


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def labels():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return set()
    event = json.loads(Path(event_path).read_text())
    return {label["name"] for label in event.get("pull_request", {}).get("labels", [])}


def check_yank(base, path):
    """A merged entry only ever gets `yanked`."""
    before = json.loads(git("show", f"{base}:{path}"))
    after = json.loads(Path(path).read_text())
    reason = after.pop("yanked", None)
    if "yanked" in before:
        return f"{path}: already yanked"
    if before != after:
        return f"{path}: a merged entry only gets yanked, nothing else changes"
    if not isinstance(reason, str) or not reason.strip():
        return f"{path}: yanked must say why"
    return None


def diff(work):
    """The sources against the last indexed version, or all of them."""
    previous = work / "previous"
    if not previous.is_dir():
        previous = work / "empty"
        previous.mkdir(exist_ok=True)
    return diff_folders(previous, work / "new")


def diff_folders(previous, new):
    result = subprocess.run(
        ["git", "diff", "--no-index", "--no-color", "--", str(previous), str(new)],
        capture_output=True,
        text=True,
    )
    # git writes each path under its temporary folder, after a/ or b/, bare in a rename's lines:
    # keep it relative to its version.
    text = result.stdout
    for root in (previous, new):
        text = text.replace(str(root) + "/", "/")
    return re.sub(r"^(rename|copy) (from|to) /", r"\1 \2 ", text, flags=re.MULTILINE)


def readable(commit):
    """Whether a commit is there to read, fetched if need be: a forced push leaves the old head
    out of the branch."""
    def known():
        return subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode == 0

    if not known():
        subprocess.run(["git", "fetch", "--quiet", "origin", commit], capture_output=True)
    return known()


def entries_at(commit, folder):
    listed = git("ls-tree", "-r", "--name-only", commit, "--", folder + "/").splitlines()
    return {path for path in listed if ENTRY_PATH.fullmatch(path)}


def held_before(before, base, path):
    """The entry of this mod the pull request held at `before`: this path again when its content
    changed, else its highest other version. None when the push left this entry as it was, or
    when the pull request had none for this mod."""
    folder = str(Path(path).parent)
    held = entries_at(before, folder) - entries_at(base, folder)
    if path in held:
        return None if git("show", f"{before}:{path}") == Path(path).read_text() else path
    return max(held, key=lambda entry: tuple(int(part) for part in Path(entry).stem.split(".")), default=None)


def since_push(launcher, before, base, base_dir, before_dir, path, new, work):
    """What the push changed in an entry's sources, as report lines; none when it changed nothing."""
    old = held_before(before, base, path)
    if old is None:
        return []
    lines = [f"## {path}", ""]
    # Its zip is checked against the size and sha256 that entry gave, as any entry's.
    subprocess.run([launcher, "--check-entry", old, str(base_dir), str(work)], cwd=before_dir, capture_output=True)
    version = Path(old).stem
    if not (work / "new").is_dir():
        redone = " A release redone under the same version replaces its zip." if old == path else ""
        return lines + [
            f"Before the push this pull request held `{old}`, whose zip is no longer the one it named: "
            f"there is nothing to compare with.{redone} The diff against the last indexed version stands.",
            "",
        ]
    text = diff_folders(work / "new", new)
    if not text.strip():
        return lines + [f"Its sources are those of `{old}`, which this pull request held before the push: only the entry changed.", ""]
    again = " as it was" if old == path else ""
    return lines + [
        f"Against `{old}`{again}, which this pull request held before the push.",
        "",
        sources(text, f"Sources against {version}{again}, before the push"),
    ]


def render(summary, diff_chars, cut):
    """The report, each diff cut past `diff_chars`, saying `cut` where it is."""
    lines = []
    for line in summary:
        if isinstance(line, Sources):
            text = line if len(line) <= diff_chars else line[:diff_chars] + f"\n… cut: {cut}.\n"
            lines += [
                f"<details><summary>{line.title}</summary>",
                "",
                *fenced(text, "diff"),
                "</details>",
                "",
            ]
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def main():
    launcher, base = os.path.abspath(sys.argv[1]), sys.argv[2]
    before = sys.argv[3] if len(sys.argv) > 3 else ""
    takeover = "takeover" in labels()
    problems, summary, pushed = [], ["# Index check", ""], []
    if before and not readable(before):
        pushed += [f"The head before the push, `{before[:12]}`, can no longer be read: the diff against the last indexed version stands.", ""]
        before = ""

    added = []
    for line in git("diff", "--name-status", "--no-renames", base, "HEAD").splitlines():
        status, path = line.split("\t", 1)
        if not ENTRY_PATH.fullmatch(path):
            problems.append(f"{path}: a pull request only touches mods/<id>/<version>.json")
        elif status == "A":
            added.append(path)
        elif status == "M":
            problem = check_yank(base, path)
            if problem:
                problems.append(problem)
            else:
                summary += [f"## Yanks {path}", "", *fenced(json.loads(Path(path).read_text())["yanked"]), ""]
        else:
            problems.append(f"{path}: an indexed entry is never removed")

    with tempfile.TemporaryDirectory() as temp:
        base_dir = Path(temp) / "base"
        git("worktree", "add", "--detach", str(base_dir), base)
        before_dir = Path(temp) / "before"
        if before:
            git("worktree", "add", "--detach", str(before_dir), before)
        try:
            for number, path in enumerate(added):
                work = Path(temp) / f"work{number}"
                work.mkdir()
                command = [launcher, "--check-entry", path, str(base_dir), str(work)]
                if takeover:
                    command.append("--takeover")
                result = subprocess.run(command, capture_output=True, text=True)
                output = (result.stdout + result.stderr).strip()
                print(output)
                if result.returncode != 0:
                    problems.append(f"{path}: the launcher's checks failed")
                summary += [f"## {path}", "", *fenced(output), ""]
                if (work / "new").is_dir():
                    summary.append(Sources(diff(work)))
                    if before:
                        held = Path(temp) / f"held{number}"
                        held.mkdir()
                        pushed += since_push(launcher, before, base, base_dir, before_dir, path, work / "new", held)
        finally:
            git("worktree", "remove", "--force", str(base_dir))
            if before:
                git("worktree", "remove", "--force", str(before_dir))

    if not added and not problems and len(summary) == 2:
        problems.append("the pull request changes no entry")
    summary += ["## Result", ""] + ([f"- {problem}" for problem in problems] or ["Every check passed."])
    if pushed:
        pushed = ["# Since the last push", ""] + pushed
    report = render(summary + pushed, MAX_DIFF_CHARS, "read the zips themselves")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as file:
            file.write(report)
    else:
        print(report)
    comment = os.environ.get("REPORT")
    if comment:
        diffs = sum(isinstance(line, Sources) for line in summary)
        Path(comment).parent.mkdir(parents=True, exist_ok=True)
        Path(comment).write_text(render(summary, COMMENT_DIFF_CHARS // max(1, diffs), "the whole diff is in the run's summary"))
    push_comment = os.environ.get("PUSH_REPORT")
    if push_comment and pushed:
        diffs = sum(isinstance(line, Sources) for line in pushed)
        Path(push_comment).parent.mkdir(parents=True, exist_ok=True)
        Path(push_comment).write_text(render(pushed, COMMENT_DIFF_CHARS // max(1, diffs), "the whole diff is in the run's summary"))
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
