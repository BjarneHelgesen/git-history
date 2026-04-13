#!/usr/bin/env python3
"""
Helper used as GIT_EDITOR / GIT_SEQUENCE_EDITOR during git rebase.

Git invokes this with a single file path (git-rebase-todo, COMMIT_EDITMSG,
.git/rebase-merge/message, ...). We replace that file with the contents of a
source file named by an environment variable:

    git-rebase-todo   <- $GIT_HISTORY_TODO
    anything else     <- $GIT_HISTORY_MSG

If the relevant env var is unset, the target file is left alone so git uses
whatever it prepared itself (default concatenated squash message, etc).
"""
import os
import sys


def main():
    if len(sys.argv) < 2:
        return
    target = sys.argv[1]
    name = os.path.basename(target)
    if name == "git-rebase-todo":
        src = os.environ.get("GIT_HISTORY_TODO")
    else:
        src = os.environ.get("GIT_HISTORY_MSG")
    if not src:
        return
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError:
        return
    with open(target, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    main()
