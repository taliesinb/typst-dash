# typst-dash

Workflow for building a [Dash](https://kapeli.com/dash) docset for
[Typst](https://typst.app) from the official documentation.

There is no user-contributed Typst docset in Dash, but the typst repo contains
everything needed to make a high-quality one: the `typst-docs` crate builds the
complete official documentation website (the same one served at
typst.app/docs) as a static site, including a `search.json` that enumerates
every page, function, type, parameter, symbol, etc. with its route. This repo
compiles that site and packages it as a docset, using `search.json` to build
the Dash search index.

## Requirements

- A clone of [typst/typst](https://github.com/typst/typst) (default location:
  `~/github/typst`; override with `TYPST_REPO=...`)
- Rust toolchain
- Python 3 (stdlib only)

## Usage

```sh
./build.sh            # builds for the default pinned tag
./build.sh v0.15.1    # or an explicit tag
```

This:

1. Creates a git worktree of the typst repo at the requested tag under
   `build/typst-src`.
2. Runs `cargo run -p typst-docs --release -- compile`, producing the static
   docs site at `build/typst-src/docs/dist/site`.
3. Runs `make_docset.py`, which copies the site into
   `dist/Typst.docset/Contents/Resources/Documents`, rewrites absolute URLs to
   relative ones so pages work offline from `file://`, builds the SQLite
   search index (`docSet.dsidx`) from the site's `search.json`, and writes
   `Info.plist` and the icon.

Then install by double-clicking `dist/Typst.docset` or via Dash → Settings →
Docsets → `+` → Add Local Docset.

## Notes

- Index entries use code-style names derived from routes and anchors
  (`heading`, `array.at`, `array.at.default`, `calc.abs`, `text.size`), typed
  as Function / Method / Parameter / Type / Module / Category / Guide.
- When previewing pages in Chrome via `file://`, the SVG icon sprites and
  `docs.js` (the site's own search UI) are blocked by Chrome's unique-origin
  policy for local files. This doesn't affect content, and Dash's WebKit view
  grants the docset folder read access.

- The mapping from `search.json` item kinds to Dash entry types lives in
  `KIND_MAP` / `KIND_PREFIXES` in `make_docset.py`.
- To upgrade to a new Typst release: `./build.sh vX.Y.Z` (the docs CLI has
  been stable across recent releases; verify `docs/src/args.rs` if it fails).
- `~/github/tinymist` (local fork of the tinymist LSP) could be used in the
  future to index additional symbols/doc-comments not present in the official
  docs, but the official site already covers the full public API.
