#!/bin/sh
# Build a release zip for handover (docs/HANDOVER.md §4).
#
#   sh scripts/release.sh <version>      e.g. 0.2.0
#
# Runs the tests, tags v<version> on HEAD and writes dist/DSARClassifier-<version>.zip
# from that tag (tracked files only, so no case data or per-case configs), then
# prints its SHA-256. Pushing the tag and publishing the release are left to you.
set -eu
VERSION=${1:?usage: sh scripts/release.sh <version>}
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Uncommitted changes; commit or stash them first." >&2
    exit 1
fi
grep -q "^version = \"$VERSION\"" pyproject.toml || { echo "pyproject.toml version is not $VERSION" >&2; exit 1; }
grep -q "__version__ = \"$VERSION\"" src/dsar/__init__.py || { echo "src/dsar/__init__.py version is not $VERSION" >&2; exit 1; }

python -m pytest -q

git tag -a "v$VERSION" -m "DSARClassifier $VERSION"
mkdir -p dist
ZIP="dist/DSARClassifier-$VERSION.zip"
git archive --format=zip --prefix="DSARClassifier-$VERSION/" -o "$ZIP" "v$VERSION"
shasum -a 256 "$ZIP"
echo "Next: git push origin v$VERSION && gh release create v$VERSION $ZIP --notes-file CHANGELOG.md"
