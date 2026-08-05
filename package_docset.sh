#!/usr/bin/env bash
# Build a Dash docset for any Typst package using tinymist.
#
# Usage: ./package_docset.sh <package-path-or-spec> [more packages...]
#   e.g. ./package_docset.sh ~/github/typst-cetz
#        ./package_docset.sh @preview/cetz:0.5.2
#
# Requires: a build of the tinymist fork at $TINYMIST_REPO (cargo build -p
# tinymist-cli --release, plus the tinymist-index wasm plugin — see README),
# and Python 3.
set -euo pipefail

TINYMIST_REPO="${TINYMIST_REPO:-$HOME/github/tinymist}"
TINYMIST="$TINYMIST_REPO/target/release/tinymist"
ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -x "$TINYMIST" ]]; then
    echo "error: $TINYMIST not found; build with:" >&2
    echo "  cd $TINYMIST_REPO && cargo build -p tinymist-cli --release" >&2
    exit 1
fi

for pkg in "$@"; do
    # Read the package name/version from its manifest (local path) or spec.
    if [[ "$pkg" == @* ]]; then
        spec="${pkg#@}"                    # ns/name:version
        ns="${spec%%/*}" rest="${spec#*/}"
        name="${rest%%:*}" version="${rest##*:}"
    else
        name=$(sed -n 's/^name *= *"\(.*\)"/\1/p' "$pkg/typst.toml" | head -1)
        version=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$pkg/typst.toml" | head -1)
        ns="preview"
    fi
    base="$ns-$name-$version"
    bundle="$ROOT/build/pkg-docs/$name"

    echo "==> generating docs for $name $version"
    (cd "$TINYMIST_REPO" && "$TINYMIST" package docs "$pkg" "$bundle")

    python3 "$ROOT/make_package_docset.py" \
        "$bundle" \
        "$TINYMIST_REPO/target/package-docs/$base/$base.json" \
        "$ROOT/dist" --name "$name" --version "$version"
done
