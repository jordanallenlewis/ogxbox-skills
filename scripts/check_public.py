#!/usr/bin/env python3
"""Fail if the tree or the history holds something a public repo should not:
private LAN addresses, co-author trailers, or commits authored by a bot or an
AI tool. Standard library only. Run from the repository root.

The patterns are built from pieces so this file does not match itself.
"""

import re
import subprocess
import sys

PRIVATE_IP = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})(?![\d.])"
)
COAUTHOR = re.compile("Co-" + "authored-by", re.I)
GENERATED = re.compile("Generated " + "with", re.I)
TOOL_NAMES = re.compile(r"\b(?:" + "|".join(["Cla" + "ude", "Anthro" + "pic", "Open" + "AI",
                                              "Co" + "pilot", "Chat" + "GPT"]) + r")\b", re.I)


def git(*args):
    return subprocess.run(("git",) + args, check=True, capture_output=True, text=True).stdout


def main():
    problems = []
    for path in git("ls-files", "-z").split("\0"):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PRIVATE_IP.search(line):
                problems.append("%s:%d: private network address" % (path, n))
            if COAUTHOR.search(line):
                problems.append("%s:%d: co-author trailer" % (path, n))
            if GENERATED.search(line):
                problems.append("%s:%d: generated-with line" % (path, n))
    log = git("log", "--all", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e")
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, an, ae, cn, ce, body = rec.split("\x1f", 5)
        who = " ".join([an, ae, cn, ce])
        if TOOL_NAMES.search(who) or "[bot]" in who:
            problems.append("commit %s: author or committer is a tool or bot (%s)" % (sha[:12], an))
        if COAUTHOR.search(body):
            problems.append("commit %s: co-author trailer in the message" % sha[:12])
        if GENERATED.search(body) or TOOL_NAMES.search(body):
            problems.append("commit %s: tool attribution in the message" % sha[:12])
        if PRIVATE_IP.search(body):
            problems.append("commit %s: private network address in the message" % sha[:12])
    for p in problems:
        print(p)
    if problems:
        print("%d problem(s)" % len(problems))
        return 1
    print("public tree check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
