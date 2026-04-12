#!/usr/bin/env python3
"""
Generate MkDocs documentation from db/*.yaml.

Each class (BackgroundFetch, BackgroundFetchConfig, TaskConfig, HeadlessEvent)
gets its own Markdown page per platform tab.  Class members become ## sections.
Code examples are rendered as Material "tabbed" blocks.

Usage:
    python3 scripts/generate_mkdocs.py
"""

import re
import shutil
import yaml
from pathlib import Path
from collections import defaultdict

ROOT     = Path(__file__).parent.parent
DB_DIR   = ROOT / "db"
SRC_DIR  = ROOT / "src"
STATIC   = SRC_DIR / "static"
CSS_SRC  = SRC_DIR / "assets" / "css" / "site.css"
OUT_DIR  = ROOT / "mkdocs-site"
DOCS_DIR = OUT_DIR / "docs"

# ── Platform definitions ─────────────────────────────────────────────────────

PLATFORMS = [
    {"id": "react-native", "label": "React Native", "code_lang": "ts",   "code_label": "TypeScript"},
    {"id": "cordova",      "label": "Cordova",       "code_lang": "ts",   "code_label": "JavaScript"},
    {"id": "capacitor",    "label": "Capacitor",     "code_lang": "ts",   "code_label": "TypeScript"},
    {"id": "flutter",      "label": "Flutter",       "code_lang": "dart", "code_label": "Dart"},
]

# ── Page definitions: which db/ classes appear on each page ──────────────────
#
# 'classes' lists the id prefixes included on this page (order matters for rendering).
# AbstractConfig members appear on both BackgroundFetchConfig and TaskConfig pages.

PAGES = [
    {
        "id":      "BackgroundFetch",
        "title":   "BackgroundFetch",
        "classes": ["BackgroundFetch"],
    },
    {
        "id":      "BackgroundFetchConfig",
        "title":   "BackgroundFetchConfig",
        "classes": ["BackgroundFetchConfig", "AbstractConfig"],
        "intro":   "Configuration object passed to [[BackgroundFetch.configure]].",
    },
    {
        "id":      "TaskConfig",
        "title":   "TaskConfig",
        "classes": ["TaskConfig", "AbstractConfig"],
        "intro":   "Configuration object passed to [[BackgroundFetch.scheduleTask]].",
    },
    {
        "id":      "HeadlessEvent",
        "title":   "HeadlessEvent",
        "classes": ["HeadlessEvent"],
        "intro":   "Event object delivered to [[BackgroundFetch.registerHeadlessTask]] handlers.",
    },
]

# ── Regex helpers ─────────────────────────────────────────────────────────────

EXAMPLE_REF_RE = re.compile(r"^\s*@example\s+(\S+)$", re.MULTILINE)
LINK_RE        = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]*?))?\]\]")


# ── Link conversion ───────────────────────────────────────────────────────────

def _convert_links(text: str, platform_id: str) -> str:
    """Convert [[ClassName.member]] cross-references to Markdown links."""
    def _replace(m):
        target  = m.group(1).strip().rstrip("\\")
        display = (m.group(2) or target).strip()
        if "." in target:
            cls, member = target.split(".", 1)
            # Find page that contains this class
            page_id = next(
                (p["id"] for p in PAGES if cls in p["classes"]),
                cls,
            )
            return f"[{display}](../{page_id}/#{member.lower()})"
        else:
            # Class-level link — find matching page
            page_id = next(
                (p["id"] for p in PAGES if target == p["id"] or target in p["classes"]),
                target,
            )
            return f"[{display}](../{page_id}/)"
    return LINK_RE.sub(_replace, text)


# ── Code example rendering ────────────────────────────────────────────────────

def _render_example(example: dict, code_lang: str, code_label: str) -> str:
    """Render a single example as a fenced code block (or tabbed if multiple langs)."""
    code = example.get("code", {})
    if not code:
        return ""

    title = example.get("title", "")
    lines = []
    if title:
        lines.append(f"**{title}**\n")

    # If the example has the target language, just render it.
    if code_lang in code:
        lines.append(f"```{code_lang}")
        lines.append(code[code_lang])
        lines.append("```")
    elif "ts" in code:
        # Fallback to ts for Cordova/Capacitor if no separate entry.
        lines.append(f"```ts")
        lines.append(code["ts"])
        lines.append("```")

    return "\n".join(lines)


def _render_description(desc: str, examples: dict, code_lang: str, code_label: str, platform_id: str) -> str:
    """Expand @example references and convert links in a description string."""
    if not desc:
        return ""

    def _replace_example(m):
        key = m.group(1)
        ex  = examples.get(key, {})
        return _render_example(ex, code_lang, code_label) if ex else ""

    text = EXAMPLE_REF_RE.sub(_replace_example, desc)
    text = _convert_links(text, platform_id)
    return text


# ── Member section ────────────────────────────────────────────────────────────

def _member_section(entry: dict, code_lang: str, code_label: str, platform_id: str) -> str:
    """Render one API member as a ## Markdown section."""
    eid   = entry["id"]
    name  = eid.split(".", 1)[-1] if "." in eid else eid
    kind  = entry.get("kind", "property")
    desc  = entry.get("description", "")
    examples = entry.get("examples", {}) or {}
    sigs  = entry.get("signatures", {}) or {}

    lines = [f"## {name}\n"]

    # Signature
    sig = sigs.get(code_lang) or sigs.get("ts", "")
    if sig:
        lines.append(f"```{code_lang}")
        lines.append(sig)
        lines.append("```\n")

    # Description with embedded examples
    if desc:
        lines.append(_render_description(desc, examples, code_lang, code_label, platform_id))
        lines.append("")

    return "\n".join(lines)


# ── Page builder ──────────────────────────────────────────────────────────────

def _build_page(page_def: dict, entries_by_class: dict, platform: dict) -> str:
    """Build a full Markdown page for one page definition and one platform."""
    code_lang  = platform["code_lang"]
    code_label = platform["code_label"]
    platform_id = platform["id"]
    title      = page_def["title"]
    intro      = page_def.get("intro", "")

    lines = [f"# {title}\n"]
    if intro:
        lines.append(_convert_links(intro, platform_id))
        lines.append("")

    for cls in page_def["classes"]:
        members = entries_by_class.get(cls, [])
        if not members:
            continue
        # For multi-class pages, add a section header for the secondary class
        if cls == "AbstractConfig" and len(page_def["classes"]) > 1:
            lines.append("\n---\n")
            lines.append("## Android-only constraints\n")
            lines.append("*The following options apply to Android only.*\n")
        for entry in members:
            lines.append(_member_section(entry, code_lang, code_label, platform_id))

    return "\n".join(lines)


# ── MkDocs nav + yml ──────────────────────────────────────────────────────────

def _build_nav() -> list:
    """Build the MkDocs nav list for all platforms."""
    nav = []
    for plat in PLATFORMS:
        pid = plat["id"]
        label = plat["label"]
        entries = [{"Home": f"{pid}/index.md"}]
        entries.append({"Setup": f"{pid}/setup.md"})
        api = [
            {page["title"]: f"{pid}/{page['id']}.md"}
            for page in PAGES
        ]
        entries.append({"API Reference": api})
        nav.append({label: entries})
    return nav


def _write_mkdocs_yml(nav: list):
    """Write mkdocs.yml from the nav structure."""
    config = {
        "site_name": "Background Fetch — SDK Reference",
        "site_url":  "https://fetch.transistorsoft.com/",
        "docs_dir":  "docs",
        "site_dir":  "site",
        "copyright": "Transistor Software",
        "repo_url":  "https://github.com/transistorsoft/transistor-background-fetch",

        "theme": {
            "name":       "material",
            "palette":    {"scheme": "slate", "primary": "black", "accent": "amber"},
            "features": [
                "navigation.tabs",
                "navigation.top",
                "content.code.copy",
                "content.tabs.link",
            ],
        },

        "markdown_extensions": [
            "tables",
            "admonition",
            "attr_list",
            {"toc": {"permalink": True, "toc_depth": 3}},
            "pymdownx.highlight",
            "pymdownx.superfences",
            {"pymdownx.tabbed": {"alternate_style": True}},
        ],

        "nav": nav,
    }

    # PyYAML doesn't know about None values in extension dicts — clean them.
    def _clean(obj):
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [_clean(i) for i in obj]
        return obj

    out = OUT_DIR / "mkdocs.yml"
    with out.open("w") as f:
        yaml.dump(_clean(config), f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"  wrote {out.relative_to(ROOT)}")


# ── Static pages ──────────────────────────────────────────────────────────────

def _copy_static_pages():
    """Copy static markdown pages from src/static/<platform>/ into docs/<platform>/."""
    for plat in PLATFORMS:
        pid = plat["id"]
        src_dir = STATIC / pid
        dst_dir = DOCS_DIR / pid
        dst_dir.mkdir(parents=True, exist_ok=True)

        if src_dir.is_dir():
            for md in src_dir.glob("*.md"):
                shutil.copy(md, dst_dir / md.name)
                print(f"  copied {md.relative_to(ROOT)} → docs/{pid}/{md.name}")
        else:
            # Generate minimal stubs if static dir doesn't exist yet.
            _write_stub(dst_dir / "index.md",
                        f"# {plat['label']} — Background Fetch\n\n"
                        f"Select a topic from the navigation.\n")
            _write_stub(dst_dir / "setup.md",
                        f"# {plat['label']} — Setup\n\n"
                        f"*Setup guide coming soon.*\n")


def _write_stub(path: Path, content: str):
    if not path.exists():
        path.write_text(content)
        print(f"  stub    docs/{path.relative_to(DOCS_DIR)}")


# ── CSS ───────────────────────────────────────────────────────────────────────

def _copy_css():
    css_dst = DOCS_DIR / "assets" / "css"
    css_dst.mkdir(parents=True, exist_ok=True)
    if CSS_SRC.exists():
        shutil.copy(CSS_SRC, css_dst / "site.css")


# ── Main entry ────────────────────────────────────────────────────────────────

def generate():
    """Load db/*.yaml, generate docs/ content, and write mkdocs.yml."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Load all YAML entries ────────────────────────────────────────
    entries_by_class: dict[str, list] = defaultdict(list)
    for path in sorted(DB_DIR.glob("*.yaml")):
        with path.open() as f:
            entry = yaml.safe_load(f)
        if not entry or "id" not in entry:
            continue
        eid = entry["id"]
        cls = eid.split(".", 1)[0] if "." in eid else eid
        entries_by_class[cls].append(entry)

    print(f"  loaded {sum(len(v) for v in entries_by_class.values())} entries "
          f"from {len(list(DB_DIR.glob('*.yaml')))} YAML files")

    # ── Generate per-platform pages ──────────────────────────────────
    for plat in PLATFORMS:
        pid = plat["id"]
        plat_dir = DOCS_DIR / pid
        plat_dir.mkdir(parents=True, exist_ok=True)

        for page_def in PAGES:
            content = _build_page(page_def, entries_by_class, plat)
            out = plat_dir / f"{page_def['id']}.md"
            out.write_text(content)
            print(f"  wrote  docs/{pid}/{page_def['id']}.md")

    # ── Static pages ─────────────────────────────────────────────────
    _copy_static_pages()

    # ── CSS ──────────────────────────────────────────────────────────
    _copy_css()

    # ── mkdocs.yml ───────────────────────────────────────────────────
    nav = _build_nav()
    _write_mkdocs_yml(nav)


if __name__ == "__main__":
    generate()
