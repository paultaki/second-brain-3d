#!/usr/bin/env python3
"""
Second Brain 3D: vault scanner + graph builder.

Walks the Obsidian vault, turns notes into nodes and [[wikilinks]] into edges,
color-codes by top-level folder, then injects the graph AND the vendored
three.js/3d-force-graph bundle inline into a single self-contained HTML file
(brain.html) so it always reflects the latest vault and opens instantly with
no network dependency.

SAFETY CONTRACT (see vault CLAUDE.md):
  - 90-Private/ is NEVER walked, read, counted, or emitted. Hard rule, no flag.
    A fail-closed assertion at the end re-verifies nothing private slipped in.
  - Faith / Family / Relationship are excluded by DEFAULT. Flip
    INCLUDE_SENSITIVE_AREAS to True only when YOU choose to include them.
  - _quarantine/ (imported third-party skills) is excluded: it is not your notes.

Usage:
    python3 generate.py                    # regenerate brain.html from the vault
    python3 generate.py --open             # regenerate, then open it
    python3 generate.py --vault ~/Notes    # point at any Obsidian vault

The vault is auto-detected by walking up from this script looking for an
.obsidian/ folder, so dropping this directory anywhere inside a vault works.

Stdlib only. No pip installs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR / "template.html"
VENDOR = SCRIPT_DIR / "vendor.min.js"
OUTPUT = SCRIPT_DIR / "brain.html"


def find_vault(explicit: Path | None) -> Path:
    if explicit:
        v = explicit.expanduser().resolve()
        if not v.is_dir():
            raise SystemExit(f"Vault not found: {v}")
        return v
    for cand in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
        if (cand / ".obsidian").is_dir():
            return cand
    # no fallback: guessing a root outside a vault could scan half the disk
    raise SystemExit(
        "No .obsidian/ folder found above this script.\n"
        "Pass --vault /path/to/your/vault, or place this folder inside one.")


def check_vault_root(vault: Path) -> Path:
    # A vault whose ROOT is itself a private-named folder would make the rails
    # meaningless (children carry no private path component). Refuse, fail-closed.
    if is_private_name(vault.name):
        raise SystemExit(
            f"REFUSING: vault root '{vault.name}' is a private-named folder "
            f"({', '.join(sorted(PRIVATE_DIR_NAMES))}).\n"
            "Point --vault at the folder ABOVE it, or rename the vault.")
    return vault

# --------------------------------------------------------------------------- #
# Safety / scope configuration
# --------------------------------------------------------------------------- #
# Folder names that are NEVER walked, read, counted, or emitted, at any depth.
# HARD RULE, no flag. A fail-closed check re-verifies after the scan.
# Matched case-insensitively: on the case-insensitive filesystems this mostly
# runs on (APFS, NTFS), "private/" IS "Private/" and must be caught as such.
PRIVATE_DIR_NAMES = {"90-Private", "Private", "_private"}
_PRIVATE_LOWER = {n.lower() for n in PRIVATE_DIR_NAMES}


def is_private_name(name: str) -> bool:
    return name.lower() in _PRIVATE_LOWER

# Directory names pruned at traversal level anywhere in the tree. The private
# set is non-negotiable; the rest are noise or third-party content.
EXCLUDE_DIR_NAMES = PRIVATE_DIR_NAMES | {
    "_quarantine",      # imported third-party skills, not the vault owner's notes
    ".git", ".obsidian", ".trash", ".smart-env", ".claude", ".stfolder",
    "node_modules", "Attachments",
}

# Sensitive personal areas. Excluded unless YOU explicitly opt in (CLAUDE.md #4).
SENSITIVE_SUBPATHS = (
    # "Areas/Family",   # example: hidden until you flip the flag below
)
INCLUDE_SENSITIVE_AREAS = False   # <-- flip to True to include Faith/Family/Relationship

# --------------------------------------------------------------------------- #
# Category taxonomy. Two modes:
#   FOLDER_TO_CAT = None   -> auto-derive: every top-level folder becomes a
#                             category, colored from AUTO_PALETTE by size, with
#                             daily/journal folders on tier 1 and archives on
#                             tier 2. Works on any vault, zero config.
#   FOLDER_TO_CAT = {...}  -> curated mapping (like below) plus CATEGORIES for
#                             labels/colors/tiers. Order in CATEGORIES = legend
#                             order.
# Palette is colorblind-aware and tuned to glow well on a black background.
# tier: 0 = Core, 1 = Extended, 2 = Deep (Archive). Used by the in-app scope dial.
# --------------------------------------------------------------------------- #
CATEGORIES = [
    # key,        label,        color,      tier
    ("hubs",      "Hubs",       "#EAF0FF",  0),
    ("projects",  "Projects",   "#FFB020",  0),
    ("areas",     "Areas",      "#2EC4B6",  0),
    ("resources", "Resources",  "#3A86FF",  0),
    ("agents",    "Agents",     "#FF4D9D",  0),
    ("meta",      "Meta",       "#B892FF",  0),
    ("dreams",    "Dreams",     "#E0AAFF",  0),
    ("mia",       "Mia",        "#FF7A5C",  1),
    ("daily",     "Daily",      "#FFD166",  1),
    ("archive",   "Archive",    "#5C6784",  2),
    ("other",     "Other",      "#8A94A6",  0),
]

# top-level folder name -> category key. Set to None to auto-derive instead.
FOLDER_TO_CAT = None

AUTO_PALETTE = ["#FFB020", "#2EC4B6", "#3A86FF", "#FF4D9D", "#B892FF",
                "#E0AAFF", "#FF7A5C", "#FFD166", "#4DEEA9", "#6FA9C4"]
_LABEL_STRIP_RE = re.compile(r"^[\d\-_. ]+")
_DAILY_RE = re.compile(r"(daily|weekly|monthly|journal|log)s?(\s+notes?)?", re.I)


def _tier_for(folder: str) -> int:
    """Whole-name matching on the cleaned folder name, so 'Blog', 'Catalog',
    and 'unarchived' don't false-positive the way substring checks did."""
    core = _LABEL_STRIP_RE.sub("", folder).strip().lower()
    if core.startswith("archive"):        # archive, archives, archived, archive 2023
        return 2
    if _DAILY_RE.fullmatch(core):
        return 1
    return 0


def derive_taxonomy(note_paths: list[Path], vault: Path):
    """Auto mode: one category per top-level folder, biggest folders get the
    palette first so the dominant regions read as distinct colors."""
    counts: dict[str, int] = {}
    for p in note_paths:
        parts = p.relative_to(vault).parts
        if len(parts) > 1:
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    cats = [("hubs", "Hubs", "#EAF0FF", 0)]
    folder_map: dict[str, str] = {}
    used_keys = {"hubs", "other"}
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    for i, (folder, _n) in enumerate(ordered):
        key = re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-") or f"cat{i}"
        while key in used_keys:
            key += "x"
        used_keys.add(key)
        label = _LABEL_STRIP_RE.sub("", folder).strip() or folder
        tier = _tier_for(folder)
        color = "#5C6784" if tier == 2 else AUTO_PALETTE[i % len(AUTO_PALETTE)]
        cats.append((key, label, color, tier))
        folder_map[folder] = key
    cats.append(("other", "Other", "#8A94A6", 0))
    return cats, folder_map

# --------------------------------------------------------------------------- #
# Wikilink parsing
# --------------------------------------------------------------------------- #
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+?)\]\]")
FM_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)


def is_sensitive(rel_posix: str) -> bool:
    """Root-relative match: the folder's subtree, the exact path, or a same-named
    single note (e.g. 'Areas/Family' also gates 'Areas/Family.md')."""
    for p in SENSITIVE_SUBPATHS:
        if rel_posix.startswith(p + "/") or rel_posix == p or rel_posix == p + ".md":
            return True
    return False


def resolve_target(raw: str) -> tuple[str | None, str]:
    """Normalize a raw wikilink body to (path_key, basename_key), lowercased.
    path_key is set only for path-style links ([[folder/Name]]) and is tried
    first against the full-relpath index; basename is the fallback."""
    # strip display alias, heading anchor, block ref
    raw = raw.split("|", 1)[0]
    raw = raw.split("#", 1)[0]
    raw = raw.split("^", 1)[0]
    raw = raw.strip()
    if raw.lower().endswith(".md"):
        raw = raw[:-3]
    path_key = raw.lower() if "/" in raw else None
    base = raw.rsplit("/", 1)[-1].strip().lower()
    return path_key, base


def title_for(text: str, stem: str) -> str:
    """Prefer a frontmatter `title:` if present, else the filename stem."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            m = FM_TITLE_RE.search(text[3:end])
            if m:
                t = m.group(1).strip().strip('"').strip("'")
                if t:
                    return t
    return stem


def creation_dates(vault: Path) -> dict[str, int]:
    """First-added git timestamp per current path, from one git log pass.

    core.quotePath=false keeps non-ASCII paths verbatim so they match the
    rel-posix node ids; --no-renames dates a moved file at its current path,
    which is the honest choice for a vault that was mass-reorganized twice.
    """
    dates: dict[str, int] = {}
    try:
        out = subprocess.run(
            ["git", "-C", str(vault), "-c", "core.quotePath=false",
             "log", "--reverse", "--diff-filter=A", "--no-renames",
             "--name-only", "--format=%x01%at"],
            capture_output=True, text=True, timeout=45,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return dates
    ts = 0
    for line in out.splitlines():
        if line.startswith("\x01"):
            try:
                ts = int(line[1:].strip())
            except ValueError:
                ts = 0
        elif line and ts:
            dates.setdefault(line, ts)
    return dates


def walk_notes(vault: Path) -> list[Path]:
    """Return all .md files, pruning excluded dirs at the traversal level."""
    import os
    notes: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dp = Path(dirpath)
        # prune IN PLACE so excluded dirs are never descended into. Private
        # names match case-insensitively; this tool's own folder is skipped so
        # cloning it into a vault doesn't index its README as a note.
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIR_NAMES
                       and not is_private_name(d)
                       and (dp / d).resolve() != SCRIPT_DIR]
        if dp.resolve() == SCRIPT_DIR:
            continue
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            p = dp / fn
            # SAFETY: a symlinked .md would read whatever it points at (possibly
            # a private note) while wearing a public path. Never follow.
            if p.is_symlink():
                continue
            notes.append(p)
    return notes


def build_graph(vault: Path) -> dict:
    note_paths = walk_notes(vault)
    if not note_paths:
        raise SystemExit(
            f"No .md notes found under {vault}\n"
            "Pass --vault /path/to/your/vault, or drop this folder anywhere "
            "inside an Obsidian vault (it finds the nearest .obsidian/).")
    created = creation_dates(vault)

    if FOLDER_TO_CAT is None:
        categories, folder_map = derive_taxonomy(note_paths, vault)
    else:
        categories, folder_map = CATEGORIES, FOLDER_TO_CAT
    cat_info = {key: {"label": label, "color": color, "tier": tier}
                for key, label, color, tier in categories}

    def category_for(rel_parts: tuple[str, ...]) -> str:
        if len(rel_parts) == 1:           # a file sitting at the vault root
            return "hubs"
        return folder_map.get(rel_parts[0], "other")

    # ---- pass 1: build nodes + resolution indexes ------------------------- #
    nodes: dict[str, dict] = {}            # id (rel posix) -> node
    by_basename: dict[str, str] = {}       # lower(stem) -> id
    by_relpath: dict[str, str] = {}        # lower(rel-no-ext) -> id
    raw_texts: dict[str, str] = {}

    for p in note_paths:
        rel = p.relative_to(vault)
        rel_posix = rel.as_posix()

        # SAFETY: never let a private path through, belt-and-suspenders.
        if any(is_private_name(part) for part in rel.parts):
            raise SystemExit(f"REFUSING: private path surfaced: {rel_posix}")
        if not INCLUDE_SENSITIVE_AREAS and is_sensitive(rel_posix):
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        stem = p.stem
        cat = category_for(rel.parts)
        # creation estimate: earliest evidence wins between git first-add and
        # filesystem birthtime (each can be later than the truth after a
        # reorg copy or a late first-commit); fall back to mtime.
        try:
            st = p.stat()
        except OSError:
            st = None
        git_ts = created.get(rel_posix, 0)
        birth = int(getattr(st, "st_birthtime", 0)) if st else 0
        candidates = [t for t in (git_ts, birth) if t > 0]
        mtime = int(st.st_mtime) if st else 0
        node = {
            "id": rel_posix,
            "name": title_for(text, stem),
            "cat": cat,
            "color": cat_info[cat]["color"],
            "tier": cat_info[cat]["tier"],
            "folder": rel.parts[0] if len(rel.parts) > 1 else "/",
            "deg": 0,
            "w": len(text.split()),   # whitespace word count of the note
            "c": min(candidates) if candidates else mtime,
            "m": mtime,
        }
        nodes[rel_posix] = node
        raw_texts[rel_posix] = text
        by_basename.setdefault(stem.lower(), rel_posix)
        by_relpath.setdefault(rel_posix[:-3].lower(), rel_posix)

    # ---- pass 2: extract + resolve links ---------------------------------- #
    pair_seen: set[tuple[str, str]] = set()
    links: list[dict] = []
    unresolved = 0

    for src_id, text in raw_texts.items():
        for _bang, body in WIKILINK_RE.findall(text):
            path_key, base_key = resolve_target(body)
            if not base_key:
                continue
            # full relpath match first (path-style links), then basename
            tgt_id = ((by_relpath.get(path_key) if path_key else None)
                      or by_basename.get(base_key))
            if tgt_id is None:
                unresolved += 1
                continue
            if tgt_id == src_id:
                continue
            pair = (src_id, tgt_id) if src_id < tgt_id else (tgt_id, src_id)
            if pair in pair_seen:
                continue
            pair_seen.add(pair)
            links.append({"source": src_id, "target": tgt_id})
            nodes[src_id]["deg"] += 1
            nodes[tgt_id]["deg"] += 1

    # ---- final safety check (real raises, not asserts: python -O strips
    # asserts and this line must survive any interpreter flag) --------------- #
    for nid in nodes:
        if any(is_private_name(part) for part in nid.split("/")):
            raise SystemExit(f"LEAK: {nid}")
        if not INCLUDE_SENSITIVE_AREAS and is_sensitive(nid):
            raise SystemExit(f"SENSITIVE LEAK: {nid}")

    node_list = list(nodes.values())
    counts: dict[str, int] = {}
    total_words = 0
    for n in node_list:
        counts[n["cat"]] = counts.get(n["cat"], 0) + 1
        total_words += n["w"]

    now = datetime.now(timezone.utc).astimezone()
    meta = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "generated_ts": int(now.timestamp()),
        # vault NAME only (no absolute path): deep links use
        # obsidian://open?vault=<name>&file=<relpath>, so a shared brain.html
        # reveals nothing about the local filesystem
        "vault_name": vault.name,
        "total_nodes": len(node_list),
        "total_links": len(links),
        "total_words": total_words,
        "unresolved_links": unresolved,
        "include_sensitive": INCLUDE_SENSITIVE_AREAS,
        "counts": counts,
        "categories": [
            {"key": k, "label": cat_info[k]["label"], "color": cat_info[k]["color"],
             "tier": cat_info[k]["tier"]}
            for k, *_ in categories if counts.get(k)
        ],
    }
    return {"meta": meta, "nodes": node_list, "links": links}


def inject(graph: dict, output: Path) -> None:
    if not TEMPLATE.exists():
        raise SystemExit(f"Template not found: {TEMPLATE}")
    if not VENDOR.exists():
        raise SystemExit(
            f"Vendor bundle missing: {VENDOR}\n"
            "Rebuild it: cd build-vendor && npm install && "
            "./node_modules/.bin/esbuild entry.js --bundle --minify "
            "--format=iife --outfile=../vendor.min.js"
        )
    html = TEMPLATE.read_text(encoding="utf-8")

    # </script inside inlined JS or JSON strings would terminate the script
    # tag early; escaping the slash is semantically identical in both JS and JSON.
    vendor_js = VENDOR.read_text(encoding="utf-8").replace("</script", "<\\/script")
    payload = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")

    for marker, replacement in (
        ("/*__VENDOR_JS__*/", vendor_js),
        ("/*__GRAPH_DATA__*/ null", payload),
    ):
        if marker not in html:
            raise SystemExit(f"Template missing marker: {marker}")
        html = html.replace(marker, replacement, 1)
    output.write_text(html, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the 3D second-brain graph.")
    ap.add_argument("--open", action="store_true", help="open brain.html after building")
    ap.add_argument("--vault", type=Path, default=None,
                    help="vault root (default: walk up from this script to the "
                         "nearest folder containing .obsidian/)")
    ap.add_argument("--output", type=Path, default=None,
                    help="output HTML path (default: brain.html next to this script)")
    args = ap.parse_args()

    vault = check_vault_root(find_vault(args.vault))
    output = (args.output or OUTPUT).expanduser().resolve()
    graph = build_graph(vault)
    inject(graph, output)

    m = graph["meta"]
    print(f"  vault: {vault}")
    print(f"  brain.html rebuilt  ->  {output}")
    print(f"  {m['total_nodes']} notes  ·  {m['total_links']} links  "
          f"·  {m['total_words']:,} words  ·  {m['unresolved_links']} unresolved dropped")
    print(f"  sensitive areas included: {m['include_sensitive']}   "
          f"(90-Private always excluded)")
    top = sorted(graph["nodes"], key=lambda n: n["deg"], reverse=True)[:5]
    print("  top hubs: " + ", ".join(f"{n['name']}({n['deg']})" for n in top))

    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(output)], check=False)


if __name__ == "__main__":
    main()
