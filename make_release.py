#!/usr/bin/env python3
"""make_release.py <version>  — bump, tag, and prepare a release commit on master."""

import os
import re
import subprocess
import sys


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: make_release.py <version>")
    version = sys.argv[1]
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        sys.exit(f"version must be X.Y.Z, got '{version}'")
    if git("status", "--porcelain"):
        sys.exit("Uncommitted changes present")
    if git("branch", "--show-current") != "develop":
        sys.exit("Must run from the develop branch")

    # Bump version on develop, commit, tag
    toml = "pyproject.toml"
    with open(toml, encoding="utf-8") as f:
        text = f.read()
    text, n = re.subn(r'^version = ".*"', f'version = "{version}"', text, flags=re.MULTILINE)
    if not n:
        sys.exit("version field not found in pyproject.toml")
    with open(toml, "w", encoding="utf-8") as f:
        f.write(text)
    git("add", toml)
    git("commit", "-m", f"Set version to {version}")
    git("tag", f"v{version}dev")
    print(f"develop: tagged v{version}dev")

    # Bring master up to develop, then collapse everything into one release commit
    git("checkout", "master")
    git("reset", "--hard", "develop")
    git("reset", "--soft", "origin/master")
    subprocess.run(["git", "restore", "--staged", "CLAUDE.md"], capture_output=True, check=False)
    if os.path.exists("CLAUDE.md"):
        os.remove("CLAUDE.md")
    git("commit", "-m", f"Release v{version}")
    git("tag", f"v{version}")
    print(f"master:  tagged v{version}")

    # Find previous release tag on master
    all_tags = git("tag", "--list", "v*", "--sort=-version:refname").splitlines()
    prev = next((t for t in all_tags if not t.endswith("dev") and t != f"v{version}"), None)
    since_clause = f"since tag {prev}" if prev else "(first release)"

    prompt = (
        f"Write a markdown changelog for git-history v{version} {since_clause}. "
        f"Save it as CHANGELOG.md."
    )
    subprocess.run(["claude", "-p", prompt, "--allowedTools", "Write,Bash"], check=True)
    print("CHANGELOG.md written. Review before pushing.")


if __name__ == "__main__":
    main()
