"""Bake README / SCORING_GUIDE / COMMANDS markdown into the viewer.

The viewer is opened directly as ``file://``, where browsers block
``fetch()`` to sibling files. So the doc tabs read their content from
``<script type="text/markdown">`` blocks inline in ``index.html``, and
this script keeps those blocks in sync with the source ``.md`` files.

Run after editing any of the bundled docs::

    python -m chemtreat_water_leads_viewer.bake_docs

Idempotent — safe to run repeatedly.
"""

from __future__ import annotations

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"
SRC_DIR = HERE.parent / "chemtreat_water_leads"

# (script id, source filename). Order is the order they're written into
# the sentinel block; not significant for rendering.
DOCS = [
    ("readme", "README.md"),
    ("scoring", "SCORING_GUIDE.md"),
    ("commands", "COMMANDS.md"),
]

START = "<!-- BAKED_DOCS_START -->"
END = "<!-- BAKED_DOCS_END -->"


def _build_block() -> str:
    """Build the replacement block of <script> tags."""
    parts = [START]
    for doc_id, filename in DOCS:
        src = SRC_DIR / filename
        if not src.is_file():
            raise SystemExit(f"missing source doc: {src}")
        # </script> in the body would close the inline script early. None
        # of our docs include it, but guard anyway so a future addition
        # can't quietly break rendering.
        text = src.read_text(encoding="utf-8")
        if "</script>" in text.lower():
            raise SystemExit(
                f"{src.name} contains </script>; that would close the inline "
                "block. Refusing to bake — work around it before re-running."
            )
        parts.append(f'<script type="text/markdown" id="doc-{doc_id}">')
        parts.append(text.rstrip("\n"))
        parts.append("</script>")
    parts.append(END)
    return "\n".join(parts)


def bake() -> None:
    if not INDEX.is_file():
        raise SystemExit(f"viewer not found: {INDEX}")
    html = INDEX.read_text(encoding="utf-8")
    if START not in html or END not in html:
        raise SystemExit(
            f"sentinel markers not found in {INDEX.name}; expected\n"
            f"  {START}\n  ...\n  {END}\n"
            "around the embedded doc <script> tags."
        )
    # String-only split/join — no regex backref interpretation footguns.
    before, _, rest = html.partition(START)
    _, _, after = rest.partition(END)
    new_html = before + _build_block() + after
    if new_html == html:
        print(f"{INDEX.name}: already up to date.")
        return
    INDEX.write_text(new_html, encoding="utf-8")
    sizes = ", ".join(
        f"{doc_id}={len((SRC_DIR / filename).read_text())}B"
        for doc_id, filename in DOCS
    )
    print(f"baked {len(DOCS)} docs into {INDEX.name} ({sizes})")


if __name__ == "__main__":
    sys.exit(bake() or 0)
