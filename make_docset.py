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


# Attribute values may contain unescaped ">" (e.g. data-value=">"), so the
# tag cannot be bounded by [^>]*; the grid <li> always opens with <button>.
SYMBOL_LI = re.compile(r'<li (id="symbol-.*?)><button', re.S)
LI_ATTR = re.compile(r'([a-zA-Z-]+)="([^"]*)"')

# Only the `sym` module is indexed; emoji are deliberately excluded.
SYMBOL_PAGE_DIR = "reference/symbols/sym"

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


def render_symbol_section(attrs: dict, glyphs: dict, show_variants: bool, link_for) -> str:
    name = attrs["data-codex-name"]
    qualified = f"sym.{name}"
    value = attrs.get("data-value", "")
    title = attrs.get("data-unic-name") or qualified

    rows = [("Name", f"<code>{esc(qualified)}</code>")]
    if value:
        rows.append(("Escape", f"<code>{esc(unicode_escape(value))}</code>"))
    if attrs.get("data-math-class"):
        rows.append(("Math Class", esc(attrs["data-math-class"])))
    if attrs.get("data-accent") == "true":
        rows.append(("Accent", "yes"))
    if attrs.get("data-markup-shorthand"):
        rows.append(("Markup", f"<code>{esc(attrs['data-markup-shorthand'])}</code>"))
    if attrs.get("data-math-shorthand"):
        rows.append(("Math", f"<code>{esc(attrs['data-math-shorthand'])}</code>"))
    if attrs.get("data-latex-name"):
        rows.append(("LaTeX", f"<code>{esc(attrs['data-latex-name'])}</code>"))

    # Only the family head lists its variants: a compact flat row of glyphs,
    # each linking to its own section (hover for the variant's name).
    if show_variants:
        row = "".join(
            f'<a class="sym-alt" title="{esc(alt)}" href="{esc(link_for(alt))}">'
            f"{esc(glyphs[alt])}</a>"
            for alt in attrs.get("data-alternates", "").split()
            if glyphs.get(alt)
        )
        if row:
            rows.append(("Variants", f'<span class="sym-alts">{row}</span>'))

    details = "".join(
        f'<div class="sym-row"><span class="sym-key">{key}</span>{val}</div>'
        for key, val in rows
    )

    deprecation = ""
    if attrs.get("data-deprecation"):
        deprecation = (
            f'<p class="sym-deprecation">&#9888; {codeify(attrs["data-deprecation"])}</p>'
        )

    return (
        f'<section class="sym-card" id="{esc(qualified)}">'
        f'<div class="sym-glyph">{esc(value)}</div>'
        f'<div class="sym-body"><h3><a href="#{esc(qualified)}">{esc(title)}</a></h3>'
        f"{deprecation}{details}</div>"
        "</section>"
    )


ALL_SYMBOLS_STYLE = """
main.all-symbols { max-width: 46rem; margin: 0 auto; padding: 1rem 1.5rem 4rem; }
.sym-card { display: flex; gap: 0.9rem; padding: 0.55rem 0; border-top: 1px solid rgba(128,128,128,0.2); }
.sym-card:target { background: rgba(35,157,173,0.12); outline: 2px solid rgba(35,157,173,0.6); outline-offset: 3px; border-radius: 4px; }
.sym-glyph { flex: none; width: 3rem; height: 3rem; display: flex; align-items: center; justify-content: center; font-size: 1.9rem; }
.sym-body { min-width: 0; line-height: 1.45; }
.sym-body h3 { margin: 0 0 0.15rem; font-size: 1.1em; }
.sym-body h3 a { color: inherit; text-decoration: none; }
.sym-deprecation { margin: 0 0 0.15rem; color: #b45309; }
.sym-row { display: flex; gap: 0.5rem; }
.sym-key { flex: none; width: 5.5rem; font-weight: 500; opacity: 0.6; }
.sym-alts { display: inline-flex; flex-wrap: wrap; gap: 0.4em 0.55em; font-size: 1.15rem; line-height: 1.4; }
.sym-alt { text-decoration: none; color: inherit; }
.sym-alt:hover { color: #239dad; }
.class-list { list-style: none; padding: 0; }
.class-list li { margin: 0.25rem 0; }
.class-list .count { opacity: 0.55; margin-left: 0.5rem; font-size: 0.9rem; }
"""


def write_page(path: Path, title: str, body: str, asset_prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link href="{asset_prefix}assets/base.css" rel="stylesheet">'
        f'<link href="{asset_prefix}assets/docs.css" rel="stylesheet">'
        f"<style>{ALL_SYMBOLS_STYLE}</style>"
        f"<title>{esc(title)}</title></head><body>"
        f'<main class="all-symbols">{body}</main></body></html>',
        encoding="utf-8",
    )


def build_all_symbols_pages(documents: Path) -> list[tuple[str, str, str]]:
    """Generate reference/symbols/all/: an index page plus one page per math
    class listing every `sym` symbol with the metadata the grid page shows in
    its click flyout. Returns Dash index entries pointing at the sections.

    Runs after relativize(), so all emitted asset/page URLs are relative.
    """
    page = documents / SYMBOL_PAGE_DIR / "index.html"
    symbols = parse_symbol_grid(
        page.read_text(encoding="utf-8", errors="surrogateescape")
    )
    glyphs = {s["data-codex-name"]: s.get("data-value", "") for s in symbols}

    # The variants row goes on the head of each dotted family: the first
    # member in page order (usually the dot-free name, but e.g. the arrow
    # and triangle families have no dot-free member).
    heads = set()
    seen_families = set()
    for attrs in symbols:
        family = attrs["data-codex-name"].split(".", 1)[0]
        if family not in seen_families:
            seen_families.add(family)
            heads.add(attrs["data-codex-name"])

    by_class: dict[str, list[dict]] = {}
    class_slug_of: dict[str, str] = {}
    for attrs in symbols:
        cls = attrs.get("data-math-class") or "Other"
        by_class.setdefault(cls, []).append(attrs)
        class_slug_of[attrs["data-codex-name"]] = cls.lower()

    entries = [("All Symbols", "Section", f"{ALL_SYMBOLS_DIR}/index.html")]
    toc = []
    for cls in sorted(by_class):
        slug = cls.lower()
        page_path = f"{ALL_SYMBOLS_DIR}/{slug}/index.html"
        members = by_class[cls]
        toc.append(
            f'<li><a href="{esc(slug)}/index.html">{esc(cls)}</a>'
            f'<span class="count">{len(members)}</span></li>'
        )
        entries.append((f"{cls} Symbols", "Section", page_path))

        def link_for(alt: str, here: str = slug) -> str:
            target = class_slug_of[alt]
            anchor = f"#sym.{alt}"
            return anchor if target == here else f"../{target}/index.html{anchor}"

        body = [f"<h1>{esc(cls)} Symbols</h1>"]
        for attrs in members:
            body.append(
                render_symbol_section(
                    attrs, glyphs, attrs["data-codex-name"] in heads, link_for
                )
            )
            qualified = f"sym.{attrs['data-codex-name']}"
            entries.append((qualified, "Constant", f"{page_path}#{qualified}"))
        write_page(
            documents / page_path,
            f"{cls} Symbols",
            "".join(body),
            asset_prefix="../../../../",
        )

    write_page(
        documents / ALL_SYMBOLS_DIR / "index.html",
        "All Symbols",
        "<h1>All Symbols</h1>"
        "<p>Every symbol in the <code>sym</code> module, by math class.</p>"
        f'<ul class="class-list">{"".join(toc)}</ul>',
        asset_prefix="../../../",
    )
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
    entries += build_all_symbols_pages(documents)
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
