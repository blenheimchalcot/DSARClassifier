#!/bin/sh
# Re-attach the synced (OneDrive) working copy to git on a new machine.
#
# Git metadata is kept OUTSIDE the synced folder (the folder holds only a `.git`
# pointer file), so a new laptop gets the files but not the history. This clones
# the history from GitHub into a local git dir and points the folder at it.
# It never changes the working files; `git status` afterwards shows any local
# edits that are not on GitHub yet.
#
#   sh scripts/attach_git.sh [git-dir]      (default: ~/Git/DSARClassifier.git)
set -eu
REPO_URL=${REPO_URL:-https://github.com/blenheimchalcot/DSARClassifier.git}
WT=$(cd "$(dirname "$0")/.." && pwd)
GITDIR=${1:-$HOME/Git/DSARClassifier.git}

if [ -e "$GITDIR" ]; then
    echo "Git dir $GITDIR already exists; nothing done." >&2
    exit 1
fi
mkdir -p "$(dirname "$GITDIR")"
git clone --bare "$REPO_URL" "$GITDIR"
git --git-dir="$GITDIR" config core.bare false
git --git-dir="$GITDIR" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
git --git-dir="$GITDIR" fetch -q origin

printf 'gitdir: %s\n' "$GITDIR" > "$WT/.git"
cd "$WT"
git reset -q                       # index from HEAD; working files untouched
git branch -q --set-upstream-to=origin/main main
echo "Attached $WT -> $GITDIR"
git status --short
