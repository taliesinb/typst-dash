#!/usr/bin/env bash
# Build the Typst Dash docset from a pinned checkout of the typst repo.
#
# Usage: ./build.sh [TAG]
#   TAG defaults to the value of TYPST_TAG below.
#
# Requires: a clone of https://github.com/typst/typst at $TYPST_REPO,
# a Rust toolchain, and Python 3.
set -euo pipefail

TYPST_REPO="${TYPST_REPO:-$HOME/github/typst}"
TYPST_TAG="${1:-v0.15.1}"

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/build/typst-src"
SITE="$SRC/docs/dist/site"

# Pin a worktree of the typst repo to the requested tag.
if [[ ! -d "$SRC" ]]; then
    git -C "$TYPST_REPO" worktree add "$SRC" "$TYPST_TAG"
else
    git -C "$SRC" checkout --detach "$TYPST_TAG"
fi

# Build the official docs website.
(cd "$SRC" && CARGO_TARGET_DIR="$ROOT/build/target" \
    cargo run -p typst-docs --release -- compile)

# Package it into a docset.
python3 "$ROOT/make_docset.py" "$SITE" "$ROOT/dist" --version "${TYPST_TAG#v}"

echo "Docset written to $ROOT/dist/Typst.docset"
