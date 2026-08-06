#!/usr/bin/env python3
"""Package the typst-docs website into a Dash docset.

Usage: make_docset.py SITE_DIR OUT_DIR [--version X.Y.Z]

Takes the static site emitted by `cargo run -p typst-docs -- compile`
(docs/dist/site) and produces OUT_DIR/Typst.docset. The Dash search index is
built from the site's own search.json, which lists every page, function, type,
parameter, etc. with its route.
"""

import argparse
import html as html_mod
import json
import plistlib
import re
import shutil
import sqlite3
import sys
from pathlib import Path

# Dash entry types for whole-page items, keyed by search.json `kind`.
# Whole-page reference items (Function/Type) are indexed under their code name
# (the last route segment: `eval`, `int`); navigational pages under their title.
PAGE_KINDS = {
    "Function": ("code", "Function"),
    "Type": ("code", "Type"),
    "Category": ("title", "Category"),
    "Group": ("code", "Module"),
    "Symbols": ("title", "Section"),
    "Chapter": ("title", "Guide"),
}


def derive_entry(item: dict) -> tuple[str, str]:
    """Derive a (name, dash_type) for a search.json item.

    Anchored items get code-style dotted names derived from the page and the
    fragment, which encodes the item's code name (titles are human names like
    "Length" for `len`, so they can't be used):

      /reference/foundations/calc/#functions-abs      -> calc.abs   Function
      /reference/foundations/array/#definitions-at    -> array.at   Method
      /reference/text/text/#parameters-size           -> text.size  Parameter
      /reference/foundations/array/#constructor       -> array      Constructor
      ...array/#definitions-at-default (Param. of at) -> array.at.default
    """
    kind = item["kind"]
    route, _, frag = item["route"].partition("#")
    page = route.strip("/").rsplit("/", 1)[-1] or "index"

    if not frag:
        style, typ = PAGE_KINDS.get(kind, ("title", "Entry"))
        return (page if style == "code" else item["title"], typ)

    prefix, _, rest = frag.partition("-")
    if prefix == "functions":
        return f"{page}.{rest}", "Function"
    if prefix == "constructor":
        if not rest:
            return page, "Constructor"
        return f"{page}.{rest}", "Parameter"
    if prefix == "parameters":
        return f"{page}.{rest}", "Parameter"
    if prefix == "definitions":
        if kind.startswith("Parameter of "):
            parent = kind.removeprefix("Parameter of ")
            leaf = rest.removeprefix(f"{parent}-")
            return f"{page}.{parent}.{leaf}", "Parameter"
        typ = {"Function": "Method", "Type": "Type"}.get(kind, "Entry")
        return f"{page}.{rest}", typ
    return item["title"], "Entry"


def route_to_path(route: str) -> str:
    """Convert a site route like /reference/model/heading/#foo to a
    Documents-relative path like reference/model/heading/index.html#foo."""
    route, _, fragment = route.partition("#")
    path = route.strip("/")
    path = f"{path}/index.html" if path else "index.html"
    return f"{path}#{fragment}" if fragment else path


ABS_URL_HTML = re.compile(r'(href|src)="(/[^/"][^"]*|/)"')
ABS_URL_CSS = re.compile(r'url\((["\']?)(/[^/)"\'][^)"\']*)\1\)')
ABS_URL_SRCSET = re.compile(r'srcset="([^"]*)"')


def relativize(root: Path) -> None:
    """Rewrite absolute URLs (/foo/bar) in HTML and CSS to relative ones so
    the site works from file:// inside Dash."""
    for file in root.rglob("*"):
        if file.suffix not in (".html", ".css", ".js", ".svg"):
            continue
        depth = len(file.relative_to(root).parts) - 1
        prefix = "../" * depth if depth else "./"

        def rel(url: str) -> str:
            url = url.lstrip("/")
            # Routes end in "/" and are served as directories; point at the
            # index.html instead.
            if url == "" or url.endswith("/"):
                url += "index.html"
            elif "." not in url.rsplit("/", 1)[-1].split("#")[0]:
                # Extensionless route without trailing slash.
                base, _, frag = url.partition("#")
                url = f"{base}/index.html" + (f"#{frag}" if frag else "")
            return prefix + url

        text = file.read_text(encoding="utf-8", errors="surrogateescape")
        orig = text
        if file.suffix == ".html":
            # Dash prefers page titles without the docset name.
            text = text.replace(" - Typst Documentation</title>", "</title>", 1)
            text = ABS_URL_HTML.sub(lambda m: f'{m.group(1)}="{rel(m.group(2))}"', text)
            text = ABS_URL_SRCSET.sub(
                lambda m: 'srcset="'
                + ", ".join(
                    " ".join(
                        rel(part) if part.startswith("/") else part
                        for part in candidate.strip().split()
                    )
                    for candidate in m.group(1).split(",")
                )
                + '"',
                text,
            )
        text = ABS_URL_CSS.sub(lambda m: f"url({rel(m.group(2))})", text)
        if text != orig:
            file.write_text(text, encoding="utf-8", errors="surrogateescape")


def embed_search_index(documents: Path, search_json: Path) -> None:
    """Make the site's own search box work offline.

    The stock docs.js fetch()es /assets/search.json, which fails under file://
    (absolute path, and WebKit does not support fetch for file URLs), and it
    links results via absolute routes. Embed the index as a classic script,
    short-circuit the fetch, and relativize result links.
    """
    assets = search_json.parent
    index_js = assets / "search-index.js"
    index_js.write_text(
        "window.__searchIndex = " + search_json.read_text().strip() + ";\n"
    )

    docs_js = assets / "docs.js"
    js = docs_js.read_text()
    js = js.replace(
        "async function fetchSearchIndex() {",
        "async function fetchSearchIndex() {\n"
        "  if (window.__searchIndex) return window.__searchIndex;",
        1,
    )
    js = js.replace(
        "      a.href = url;",
        '      a.href = (window.__docRoot || "") + '
        'url.replace(/^\\//, "").replace(/\\/(\\?|#|$)/, "/index.html$1");',
        1,
    )
    docs_js.write_text(js)

    # Load the embedded index (and docs.js itself) as classic deferred
    # scripts; module scripts can be blocked for local files.
    tag_re = re.compile(r'<script type="module" src="([^"]*)assets/docs\.js">')
    for page in documents.rglob("*.html"):
        text = page.read_text(encoding="utf-8", errors="surrogateescape")
        new = tag_re.sub(
            lambda m: (
                f'<script>window.__docRoot="{m.group(1)}";</script>'
                f'<script defer src="{m.group(1)}assets/search-index.js"></script>'
                f'<script defer src="{m.group(1)}assets/docs.js">'
            ),
            text,
            count=1,
        )
        if new != text:
            page.write_text(new, encoding="utf-8", errors="surrogateescape")


SYMBOL_LI = re.compile(r'<li (id="symbol-[^"]*"[^>]*)>')
LI_ATTR = re.compile(r'([a-zA-Z-]+)="([^"]*)"')

# (module, source page dir, section heading) for the symbol grid pages.
SYMBOL_PAGES = [
    ("sym", "reference/symbols/sym", "General Symbols"),
    ("emoji", "reference/symbols/emoji", "Emoji"),
]

ALL_SYMBOLS_DIR = "reference/symbols/all"


def parse_symbol_grid(page_html: str) -> list[dict]:
    """Extract the symbol metadata that the grid page stores as data
    attributes on its <li> elements (the same data its click flyout shows)."""
    symbols = []
    for li in SYMBOL_LI.findall(page_html):
        attrs = {k: html_mod.unescape(v) for k, v in LI_ATTR.findall(li)}
        if attrs.get("data-codex-name"):
            symbols.append(attrs)
    return symbols


def esc(text: str) -> str:
    return html_mod.escape(text, quote=True)


def codeify(text: str) -> str:
    """HTML-escape, rendering `backticked` spans as <code>."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def unicode_escape(value: str) -> str:
    return " ".join(f"\\u{{{ord(ch):04X}}}" for ch in value)


def render_symbol_section(module: str, attrs: dict, glyphs: dict) -> str:
    name = attrs["data-codex-name"]
    qualified = f"{module}.{name}"
    value = attrs.get("data-value", "")
    title = attrs.get("data-unic-name") or qualified

    rows = [f"<dt>Name</dt><dd><code>{esc(qualified)}</code></dd>"]
    if value:
        rows.append(f"<dt>Escape</dt><dd><code>{esc(unicode_escape(value))}</code></dd>")
    if attrs.get("data-math-class"):
        rows.append(f"<dt>Math Class</dt><dd>{esc(attrs['data-math-class'])}</dd>")
    if attrs.get("data-accent") == "true":
        rows.append("<dt>Accent</dt><dd>yes</dd>")
    if attrs.get("data-markup-shorthand"):
        rows.append(
            f"<dt>Markup</dt><dd><code>{esc(attrs['data-markup-shorthand'])}</code></dd>"
        )
    if attrs.get("data-math-shorthand"):
        rows.append(
            f"<dt>Math</dt><dd><code>{esc(attrs['data-math-shorthand'])}</code></dd>"
        )
    if attrs.get("data-latex-name"):
        rows.append(f"<dt>LaTeX</dt><dd><code>{esc(attrs['data-latex-name'])}</code></dd>")

    deprecation = ""
    if attrs.get("data-deprecation"):
        deprecation = (
            f'<p class="sym-deprecation">&#9888; {codeify(attrs["data-deprecation"])}</p>'
        )

    variants = ""
    alternates = attrs.get("data-alternates", "").split()
    if alternates:
        chips = "".join(
            f'<a class="sym-variant" href="#{esc(module)}.{esc(alt)}">'
            f'<span class="sym">{esc(glyphs.get(alt, ""))}</span>'
            f"<code>{esc(alt)}</code></a>"
            for alt in alternates
        )
        variants = f'<div class="sym-variants">{chips}</div>'

    return (
        f'<section class="sym-card" id="{esc(qualified)}">'
        f'<div class="sym-glyph"><span>{esc(value)}</span></div>'
        f'<div class="sym-body"><h3><a href="#{esc(qualified)}">{esc(title)}</a></h3>'
        f"{deprecation}<dl>{''.join(rows)}</dl>{variants}</div>"
        "</section>"
    )


ALL_SYMBOLS_STYLE = """
main.all-symbols { max-width: 60rem; margin: 0 auto; padding: 1rem 1.5rem 4rem; }
main.all-symbols h2 { margin: 2.5rem 0 1rem; }
.sym-card { display: flex; gap: 1rem; padding: 0.9rem 0; border-top: 1px solid rgba(128,128,128,0.25); }
.sym-card:target { background: rgba(35,157,173,0.12); outline: 2px solid rgba(35,157,173,0.6); outline-offset: 4px; border-radius: 4px; }
.sym-glyph { flex: none; width: 3.6rem; height: 3.6rem; display: flex; align-items: center; justify-content: center; font-size: 2rem; border: 1px solid rgba(128,128,128,0.35); border-radius: 8px; }
.sym-body { min-width: 0; }
.sym-body h3 { margin: 0 0 0.35rem; font-size: 1.05rem; }
.sym-body h3 a { color: inherit; text-decoration: none; }
.sym-deprecation { margin: 0.2rem 0 0.4rem; color: #b45309; }
.sym-body dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.15rem 0.8rem; margin: 0; }
.sym-body dt { font-weight: 600; opacity: 0.75; }
.sym-body dd { margin: 0; overflow-wrap: anywhere; }
.sym-variants { margin-top: 0.55rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }
.sym-variant { display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.15rem 0.45rem; border: 1px solid rgba(128,128,128,0.35); border-radius: 6px; text-decoration: none; color: inherit; font-size: 0.85rem; }
.sym-variant .sym { font-size: 1.05rem; }
"""


def build_all_symbols_page(documents: Path) -> list[tuple[str, str, str]]:
    """Generate reference/symbols/all/index.html, a flat listing of every
    symbol with the metadata the grid pages show in their click flyout, and
    return Dash index entries pointing at its sections.

    Runs after relativize(), so all emitted asset/page URLs are relative.
    """
    entries = [("All Symbols", "Section", f"{ALL_SYMBOLS_DIR}/index.html")]
    body = []
    for module, page_dir, heading in SYMBOL_PAGES:
        page = documents / page_dir / "index.html"
        symbols = parse_symbol_grid(
            page.read_text(encoding="utf-8", errors="surrogateescape")
        )
        glyphs = {s["data-codex-name"]: s.get("data-value", "") for s in symbols}
        body.append(f'<h2 id="{esc(module)}">{esc(heading)} (<code>{esc(module)}</code>)</h2>')
        for attrs in symbols:
            body.append(render_symbol_section(module, attrs, glyphs))
            qualified = f"{module}.{attrs['data-codex-name']}"
            entries.append(
                (qualified, "Constant", f"{ALL_SYMBOLS_DIR}/index.html#{qualified}")
            )

    page_html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link href="../../../assets/base.css" rel="stylesheet">'
        '<link href="../../../assets/docs.css" rel="stylesheet">'
        f"<style>{ALL_SYMBOLS_STYLE}</style>"
        "<title>All Symbols</title></head><body>"
        '<main class="all-symbols"><h1>All Symbols</h1>'
        "<p>Every symbol in the <code>sym</code> and <code>emoji</code> modules "
        "with its escape sequence, math class, and variants.</p>"
        f"{''.join(body)}</main></body></html>"
    )
    out = documents / ALL_SYMBOLS_DIR / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page_html, encoding="utf-8")
    return entries


EMOJI_NAV_LINK = re.compile(
    r'(<li aria-expanded="false"><a href="([^"]*)reference/symbols/emoji/index\.html">'
    r"Emoji</a></li>)"
)


def add_all_symbols_nav(documents: Path) -> None:
    """Add an "All Symbols" entry to the sidebar nav next to General
    Symbols/Emoji on every page. Runs after relativize()."""
    for page in documents.rglob("*.html"):
        text = page.read_text(encoding="utf-8", errors="surrogateescape")
        new = EMOJI_NAV_LINK.sub(
            r'\1<li aria-expanded="false">'
            r'<a href="\g<2>reference/symbols/all/index.html">All Symbols</a></li>',
            text,
        )
        if new != text:
            page.write_text(new, encoding="utf-8", errors="surrogateescape")


def find_search_json(site: Path) -> Path:
    candidates = sorted(site.rglob("search.json"))
    if not candidates:
        sys.exit("error: no search.json found in site; cannot build index")
    return candidates[0]


def build_index(db_path: Path, entries: list[tuple[str, str, str]]) -> dict:
    """Write (name, type, path) entries into a Dash searchIndex database."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, "
        "type TEXT, path TEXT)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX anchor ON searchIndex (name, type, path)"
    )
    counts: dict[str, int] = {}
    for name, typ, path in entries:
        cur.execute(
            "INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?,?,?)",
            (name, typ, path),
        )
        counts[typ] = counts.get(typ, 0) + 1
    con.commit()
    con.close()
    return counts


def write_plist(
    path: Path,
    name: str = "Typst",
    identifier: str = "typst",
    index_file: str = "index.html",
    fallback_url: str | None = "https://typst.app/docs/",
) -> None:
    # No version key: the Dash-User-Contributions checklist requires version
    # info to live in docset.json only.
    plist = {
        "CFBundleIdentifier": identifier,
        "CFBundleName": name,
        "DocSetPlatformFamily": identifier,
        "isDashDocset": True,
        "isJavaScriptEnabled": True,
        "dashIndexFilePath": index_file,
    }
    if fallback_url:
        plist["DashDocSetFallbackURL"] = fallback_url
    with open(path, "wb") as f:
        plistlib.dump(plist, f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("site", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--version", default="unknown")
    args = ap.parse_args()

    site = args.site.resolve()
    if not site.is_dir():
        sys.exit(f"error: site directory {site} does not exist")

    docset = args.out / "Typst.docset"
    documents = docset / "Contents" / "Resources" / "Documents"
    if docset.exists():
        shutil.rmtree(docset)
    documents.parent.mkdir(parents=True)
    shutil.copytree(site, documents)

    relativize(documents)

    search_json = find_search_json(documents)
    embed_search_index(documents, search_json)

    search = json.loads(search_json.read_text())
    entries = [
        (*derive_entry(item), route_to_path(item["route"]))
        for item in search["items"]
    ]
    entries += build_all_symbols_page(documents)
    add_all_symbols_nav(documents)
    counts = build_index(docset / "Contents" / "Resources" / "docSet.dsidx", entries)
    write_plist(docset / "Contents" / "Info.plist")
    (args.out / "VERSION").write_text(args.version + "\n")

    # Docset icons (checked into this repo; derived from the Typst logo).
    here = Path(__file__).resolve().parent
    for name in ("icon.png", "icon@2x.png"):
        src = here / "icons" / name
        if src.exists():
            shutil.copy(src, docset / name)

    total = sum(counts.values())
    print(f"indexed {total} entries:")
    for typ, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {typ:12} {n}")
    print(f"docset: {docset}")


if __name__ == "__main__":
    main()
