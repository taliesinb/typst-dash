#!/usr/bin/env python3
"""Package a tinymist-generated package docs bundle into a Dash docset.

Usage: make_package_docset.py BUNDLE_DIR DOCS_JSON OUT_DIR [--name N] [--version V]

BUNDLE_DIR is the HTML bundle produced by `tinymist package docs`, and
DOCS_JSON the PackageDoc JSON it writes to its work directory
(target/package-docs/<ns>-<name>-<ver>/<ns>-<name>-<ver>.json). The docset
search index is built by walking the JSON's definition tree; each definition
carries a name, kind, and bundle_link. Use package_docset.sh for the full
pipeline from a package checkout.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from make_docset import build_index, relativize, write_plist

DEF_KIND_MAP = {
    "function": "Function",
    "constant": "Constant",
    "variable": "Variable",
    "module": "Module",
    "struct": "Struct",
    "reference": "Reference",
}


def walk_defs(def_info: dict, entries: list, prefix: str = "") -> None:
    for child in def_info.get("children", []):
        name = child.get("name", "")
        link = child.get("bundle_link")
        kind = child.get("kind", "")
        qualified = f"{prefix}{name}" if name else prefix
        if name and link:
            entries.append(
                (qualified, DEF_KIND_MAP.get(kind, "Entry"), link.lstrip("/"))
            )
        if child.get("children"):
            sub = f"{qualified}." if kind == "module" and name else prefix
            walk_defs(child, entries, sub)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    ap.add_argument("docs_json", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--name", default=None)
    ap.add_argument("--version", default="unknown")
    args = ap.parse_args()

    bundle = args.bundle.resolve()
    if not (bundle / "index.html").is_file():
        sys.exit(f"error: {bundle} does not look like a docs bundle (no index.html)")
    doc = json.loads(args.docs_json.read_text())

    name = args.name or doc["meta"]["name"]
    docset = args.out / f"{name}.docset"
    documents = docset / "Contents" / "Resources" / "Documents"
    if docset.exists():
        shutil.rmtree(docset)
    documents.parent.mkdir(parents=True)
    shutil.copytree(bundle, documents)

    relativize(documents)

    # Clean up page titles: "@preview/fletcher 0.5.9 - Package Exports - node"
    # -> "node".
    title_re = re.compile(r"<title>@[^<]* - Package Exports(?: - )?([^<]*)</title>")
    for page in documents.rglob("*.html"):
        text = page.read_text(encoding="utf-8", errors="surrogateescape")
        new = title_re.sub(
            lambda m: f"<title>{m.group(1) or name}</title>", text, count=1
        )
        if new != text:
            page.write_text(new, encoding="utf-8", errors="surrogateescape")

    # The first module is the package's exports view; later modules are
    # per-source-file views that repeat the same symbols. Keep the first
    # occurrence of each (name, kind) so exports win.
    entries = [(name, "Package", "index.html")]
    seen = set()
    for _module_name, def_info, _module_info in doc["modules"]:
        module_entries: list[tuple[str, str, str]] = []
        walk_defs(def_info, module_entries)
        for entry in module_entries:
            key = entry[:2]
            if key not in seen:
                seen.add(key)
                entries.append(entry)

    counts = build_index(docset / "Contents" / "Resources" / "docSet.dsidx", entries)
    write_plist(
        docset / "Contents" / "Info.plist",
        name=name,
        identifier=name.lower(),
        fallback_url=None,
    )
    (args.out / "VERSION").write_text(args.version + "\n")

    total = sum(counts.values())
    print(f"indexed {total} entries:")
    for typ, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {typ:12} {n}")
    print(f"docset: {docset}")


if __name__ == "__main__":
    main()
