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

ROOT       = Path(__file__).parent.parent
FORGE_ROOT = ROOT.parent                       # fetch-forge repo root
DB_DIR     = ROOT / "db"
SRC_DIR    = ROOT / "src"
STATIC     = SRC_DIR / "static"
CSS_SRC    = SRC_DIR / "assets" / "css" / "site.css"
JS_SRC     = SRC_DIR / "assets" / "js"
IMAGES_SRC = SRC_DIR / "assets" / "images"
OVERRIDES  = SRC_DIR / "overrides"
PARTIALS   = SRC_DIR / "partials"
ASSETS_SUB = FORGE_ROOT / "assets"             # transistorsoft/assets submodule
OUT_DIR    = ROOT / "mkdocs-site"
DOCS_DIR   = OUT_DIR / "docs"

# ── Platform definitions ─────────────────────────────────────────────────────

PLATFORMS = [
    {"id": "react-native", "label": "React Native", "code_lang": "ts",   "code_label": "TypeScript"},
    {"id": "cordova",      "label": "Cordova",       "code_lang": "ts",   "code_label": "JavaScript"},
    {"id": "capacitor",    "label": "Capacitor",     "code_lang": "ts",   "code_label": "TypeScript"},
    {"id": "flutter",      "label": "Flutter",       "code_lang": "dart", "code_label": "Dart"},
    # Native platforms — grouped under a single "Native" tab via parent="native".
    # Native pages render single-language and only include entries that actually have
    # signatures.<swift|kotlin> (so Swift omits Android-only types, and unauthored
    # entries don't silently fall back to TypeScript).
    {"id": "ios",     "label": "iOS",     "code_lang": "swift",  "code_label": "Swift",  "native": True, "parent": "native", "nav_label": "Swift / iOS"},
    {"id": "android", "label": "Android", "code_lang": "kotlin", "code_label": "Kotlin", "native": True, "parent": "native", "nav_label": "Kotlin / Android"},
]

# The parent "Native" tab that groups the iOS/Android native platforms in the nav.
NATIVE_PARENT = {"id": "native", "label": "Native"}

# id → platform dict, for quick lookup (e.g. during link conversion).
PLATFORM_BY_ID = {p["id"]: p for p in PLATFORMS}

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

# Enum / type pages — rendered identically to PAGES but placed under "Types" in nav.
ENUM_PAGES = [
    {
        "id":      "BackgroundFetchStatus",
        "title":   "BackgroundFetchStatus",
        "classes": ["BackgroundFetchStatus"],
        "intro":   "Authorization status returned by [[BackgroundFetch.status]] and [[BackgroundFetch.configure]].",
    },
    {
        "id":      "NetworkType",
        "title":   "NetworkType",
        "classes": ["NetworkType"],
        "intro":   "Network connectivity constraint for use with [[AbstractConfig.requiredNetworkType]].",
    },
]

# Combined list for link resolution
ALL_PAGES = PAGES + ENUM_PAGES


# ── Native platform helpers ──────────────────────────────────────────────────
#
# Native (Swift/Kotlin) pages render single-language and ONLY include entries that
# carry a signature for that language. This both omits Android-only entities from
# the Swift nav and prevents the TypeScript fallback from rendering on entries whose
# native content has not been authored yet.

def _entry_has_native_sig(entry: dict, code_lang: str) -> bool:
    sigs = entry.get("signatures", {}) or {}
    return bool(sigs.get(code_lang))


def _page_has_native_content(page_def: dict, entries_by_class: dict, code_lang: str) -> bool:
    for cls in page_def["classes"]:
        for entry in entries_by_class.get(cls, []):
            if "." in entry["id"] and _entry_has_native_sig(entry, code_lang):
                return True
    return False

# ── Regex helpers ─────────────────────────────────────────────────────────────

EXAMPLE_REF_RE = re.compile(r"^\s*@example\s+(\S+)$", re.MULTILINE)
LINK_RE        = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]*?))?\]\]")
INCLUDE_RE     = re.compile(r"^([ \t]*){{>\s*(.+?)\s*}}", re.MULTILINE)

ANDROID_ONLY_RE = re.compile(r"\[Android only\]")
IOS_ONLY_RE     = re.compile(r"\[iOS only\]")
_PLATFORM_BADGE_PREFIX_RE = re.compile(r"^\s*(\[(?:Android|iOS) only\])\s*")

_HEADING_BADGES = {
    "[Android only]": '<span class="platform-badge platform-badge--android">Android only</span>',
    "[iOS only]":     '<span class="platform-badge platform-badge--ios">iOS only</span>',
}


def _extract_heading_badge(description: str) -> tuple[str, str]:
    """Return (badge_html, stripped_description) if description starts with a platform marker."""
    m = _PLATFORM_BADGE_PREFIX_RE.match(description)
    if m:
        return _HEADING_BADGES.get(m.group(1), ""), description[m.end():]
    return "", description


# ── Partial expansion ────────────────────────────────────────────────────────

def _expand_includes(text: str) -> str:
    """Expand {{> partial-name.md }} directives, preserving indentation."""
    def _replace(match: re.Match) -> str:
        indent = match.group(1)
        filename = match.group(2).strip()
        partial_path = PARTIALS / filename
        if not partial_path.exists():
            raise FileNotFoundError(f"Partial not found: {partial_path}")
        content = partial_path.read_text(encoding="utf-8").rstrip("\n")
        lines = content.split("\n")
        return "\n".join(indent + line if line else "" for line in lines)
    return INCLUDE_RE.sub(_replace, text)


# ── Link conversion ───────────────────────────────────────────────────────────

# Set of all known entry IDs (e.g. "BackgroundFetch.configure", "TaskConfig.taskId").
# Populated by generate() before any rendering occurs.
KNOWN_ENTRY_IDS: set[str] = set()

# Per native language (swift/kotlin): the set of page ids that actually have native
# content. Used to downgrade cross-references to omitted pages (Android-only types not
# shown in Swift, or entries whose native content isn't authored yet) into inline code
# on native pages — instead of emitting broken links.
_NATIVE_PAGES_WITH_CONTENT: dict[str, set] = {}

def _convert_links(text: str, platform_id: str, current_page_id: str = "") -> str:
    """Convert [[ClassName.member]] cross-references to Markdown links."""
    plat = PLATFORM_BY_ID.get(platform_id, {})
    native_pages = _NATIVE_PAGES_WITH_CONTENT.get(plat.get("code_lang")) if plat.get("native") else None

    def _unavailable_on_native(page_id):
        # On a native page, a reference to a page not rendered for this platform
        # (omitted Android-only type, or not-yet-authored) becomes inline code.
        return native_pages is not None and page_id != current_page_id and page_id not in native_pages

    def _replace(m):
        target  = m.group(1).strip().rstrip("\\")
        explicit_display = m.group(2)
        display = (explicit_display or target).strip()
        if "." in target:
            cls, member = target.split(".", 1)
            # AbstractConfig is an internal base class — display just the member name
            if cls == "AbstractConfig" and not explicit_display:
                display = member
            # If no matching DB entry exists, render as inline code instead of a broken link
            if target not in KNOWN_ENTRY_IDS:
                return f"`{display}`"
            # Find page that contains this class
            page_id = next(
                (p["id"] for p in ALL_PAGES if cls in p["classes"]),
                cls,
            )
            if _unavailable_on_native(page_id):
                return f"`{display}`"
            if page_id == current_page_id:
                return f"[{display}](#{member.lower()})"
            return f"[{display}]({page_id}.md#{member.lower()})"
        else:
            # Class-level link — find matching page
            page_id = next(
                (p["id"] for p in ALL_PAGES if target == p["id"] or target in p["classes"]),
                target,
            )
            if _unavailable_on_native(page_id):
                return f"`{display}`"
            if page_id == current_page_id:
                return f"[{display}](#)"
            return f"[{display}]({page_id}.md)"
    return LINK_RE.sub(_replace, text)


# ── Code example rendering ────────────────────────────────────────────────────

def _render_example(example: dict, code_lang: str, code_label: str, native: bool = False) -> str:
    """Render a single example as a fenced code block."""
    code = example.get("code", {})
    if not code:
        return ""

    # If the example has the target language, render it.
    if code_lang in code:
        body = code[code_lang]
    elif not native and "ts" in code:
        # Fallback to ts for Cordova/Capacitor if no separate entry.
        # Never fall back on native pages — a Swift/Kotlin page must not show TypeScript.
        code_lang, body = "ts", code["ts"]
    else:
        return ""

    lines = []
    title = example.get("title", "")
    if title:
        lines.append(f"**{title}**\n")
    lines.append(f"```{code_lang}")
    lines.append(body)
    lines.append("```")
    return "\n".join(lines)


def _render_description(desc: str, examples: dict, code_lang: str, code_label: str, platform_id: str, current_page_id: str = "", native: bool = False) -> str:
    """Expand @example references and convert links in a description string."""
    if not desc:
        return ""

    def _replace_example(m):
        key = m.group(1)
        ex  = examples.get(key, {})
        return _render_example(ex, code_lang, code_label, native) if ex else ""

    text = EXAMPLE_REF_RE.sub(_replace_example, desc)
    text = _convert_links(text, platform_id, current_page_id)
    # Convert inline [Android only] / [iOS only] to styled badges
    text = ANDROID_ONLY_RE.sub(_HEADING_BADGES["[Android only]"], text)
    text = IOS_ONLY_RE.sub(_HEADING_BADGES["[iOS only]"], text)
    return text


# ── Member section ────────────────────────────────────────────────────────────

def _member_section(entry: dict, code_lang: str, code_label: str, platform_id: str, current_page_id: str = "", native: bool = False) -> str:
    """Render one API member as a ## Markdown section.

    On native (Swift/Kotlin) pages, members without a signature for that language are
    skipped entirely (returns "") — never falling back to TypeScript."""
    eid   = entry["id"]
    name  = eid.split(".", 1)[-1] if "." in eid else eid
    kind  = entry.get("kind", "property")
    desc  = entry.get("description", "")
    examples = entry.get("examples", {}) or {}
    sigs  = entry.get("signatures", {}) or {}

    # Native pages only show entries that actually have native signatures.
    if native and not sigs.get(code_lang):
        return ""

    # Use platform-specific description if available, fall back to shared description
    platform_descs = entry.get("platform_description", {}) or {}
    if platform_id in platform_descs:
        desc = platform_descs[platform_id]

    # Extract platform badge from description start (e.g. "[Android only]")
    badge, desc = _extract_heading_badge(desc)
    if badge:
        # Use data-toc-label to keep badge text out of the TOC sidebar
        # Use explicit #id to prevent MkDocs from slugifying the badge text into the anchor
        lines = [f'## {name} {badge} {{ data-toc-label="{name}" #{name.lower()} }}\n']
    else:
        lines = [f"## {name}\n"]

    # Signature — native never falls back to TypeScript.
    sig = sigs.get(code_lang) if native else (sigs.get(code_lang) or sigs.get("ts", ""))
    if sig:
        lines.append(f"```{code_lang}")
        lines.append(sig)
        lines.append("```\n")

    # Description with embedded examples
    if desc:
        lines.append(_render_description(desc, examples, code_lang, code_label, platform_id, current_page_id, native))
        lines.append("")

    return "\n".join(lines)


# ── Page builder ──────────────────────────────────────────────────────────────

def _build_page(page_def: dict, entries_by_class: dict, platform: dict) -> str:
    """Build a full Markdown page for one page definition and one platform."""
    code_lang  = platform["code_lang"]
    code_label = platform["code_label"]
    platform_id = platform["id"]
    native      = platform.get("native", False)
    title      = page_def["title"]
    intro      = page_def.get("intro", "")

    page_id    = page_def["id"]
    lines = [f"# {title}\n"]

    # Try to pull the intro from the class-level entry's description/platform_description,
    # falling back to the static intro in the page definition.
    intro_rendered = False
    for cls in page_def["classes"]:
        for entry in entries_by_class.get(cls, []):
            if "." not in entry["id"]:
                platform_descs = entry.get("platform_description", {}) or {}
                desc = platform_descs.get(platform_id, entry.get("description", ""))
                examples = entry.get("examples", {}) or {}
                if desc:
                    lines.append(_render_description(desc, examples, code_lang, code_label, platform_id, page_id, native))
                    lines.append("")
                    intro_rendered = True
                break
        if intro_rendered:
            break
    if not intro_rendered and intro:
        lines.append(_convert_links(intro, platform_id, page_id))
        lines.append("")

    for cls in page_def["classes"]:
        members = entries_by_class.get(cls, [])
        if not members:
            continue
        # Render member sections first so empty classes can be skipped (e.g. a native
        # page where no member in this class has native content).
        rendered = []
        for entry in members:
            # Skip class-level entries (e.g. enum parent like "NetworkType") —
            # they provide the page intro, not a member section.
            if "." not in entry["id"]:
                continue
            section = _member_section(entry, code_lang, code_label, platform_id, page_id, native)
            if section.strip():
                rendered.append(section)
        if not rendered:
            continue
        # The "Android-only constraints" banner only makes sense on the cross-platform
        # (TS/Dart) pages — a native Kotlin page is already entirely Android.
        if cls == "AbstractConfig" and len(page_def["classes"]) > 1 and not native:
            lines.append("\n---\n")
            lines.append("## Android-only constraints\n")
            lines.append("*The following options apply to Android only.*\n")
        lines.extend(rendered)

    return "\n".join(lines)


# ── Landing page ─────────────────────────────────────────────────────────────

# Map platform id → panel SVG filename.
# react-native/flutter/ios/android use the shared transistorsoft/assets panels;
# cordova/capacitor/native use per-SDK panels in src/assets/images/ (single-SDK icons
# instead of the multi-icon -ts / -all composites).
PLATFORM_PANELS = {
    "react-native": "transistor-logo-panel-react-native.svg",
    "cordova":      "transistor-logo-panel-cordova.svg",
    "capacitor":    "transistor-logo-panel-capacitor.svg",
    "flutter":      "transistor-logo-panel-dart.svg",
    "ios":          "transistor-logo-panel-swift.svg",
    "android":      "transistor-logo-panel-kotlin.svg",
    "native":       "transistor-logo-panel-native.svg",
}

def _build_landing_page() -> str:
    """Generate the root index.md with hero panel and platform cards."""
    lines = [
        '<div class="fetch-landing" markdown>\n',
        '<div class="fetch-hero" markdown>\n',
        '![Background Fetch](assets/images/transistor-logo-panel-all.svg){ .fetch-hero-img }\n',
        '# Background Fetch\n',
        'Cross-platform background fetch API for iOS & Android.\n',
        '</div>\n',
        '<div class="fetch-platform-cards" markdown>\n',
    ]
    def _card(href, panel, label):
        return (
            f'<a class="fetch-platform-card" href="{href}">\n'
            f'  <img src="assets/images/{panel}" alt="{label}">\n'
            f'  <span class="fetch-platform-card__label">{label}</span>\n'
            f'</a>\n'
        )

    for plat in PLATFORMS:
        # Native sub-platforms (iOS/Android) are reached via the single "Native" card.
        if plat.get("parent"):
            continue
        pid = plat["id"]
        panel = PLATFORM_PANELS.get(pid, "transistor-logo-panel-all.svg")
        lines.append(_card(f"{pid}/", panel, plat["label"]))

    # Single "Native" card → the Native hub (Swift / Kotlin).
    if any(p.get("parent") == "native" for p in PLATFORMS):
        lines.append(_card("native/", PLATFORM_PANELS.get("native", "transistor-logo-panel-all.svg"), "Native"))

    lines.append('</div>\n')
    lines.append('</div>\n')
    return "\n".join(lines)


# ── MkDocs nav + yml ──────────────────────────────────────────────────────────

def _build_nav(entries_by_class: dict) -> list:
    """Build the MkDocs nav list. Native platforms are grouped under a single
    "Native" tab, and only their pages that have native content are listed."""
    nav = [{"Home": "index.md"}]
    native_children = []
    for plat in PLATFORMS:
        pid = plat["id"]
        native = plat.get("native", False)
        code_lang = plat["code_lang"]
        label = plat.get("nav_label", plat["label"])

        entries = [
            {"Home": f"{pid}/index.md"},
            {"Setup": f"{pid}/setup.md"},
            {"Examples": f"{pid}/examples.md"},
            {"Debugging": f"{pid}/debugging.md"},
        ]
        if native:
            api = [{p["title"]: f"{pid}/{p['id']}.md"} for p in PAGES
                   if _page_has_native_content(p, entries_by_class, code_lang)]
            types = [{p["title"]: f"{pid}/{p['id']}.md"} for p in ENUM_PAGES
                     if _page_has_native_content(p, entries_by_class, code_lang)]
        else:
            api = [{p["title"]: f"{pid}/{p['id']}.md"} for p in PAGES]
            types = [{p["title"]: f"{pid}/{p['id']}.md"} for p in ENUM_PAGES]
        if api:
            entries.append({"API Reference": api})
        if types:
            entries.append({"Types": types})

        nav_item = {label: entries}
        if plat.get("parent") == "native":
            native_children.append(nav_item)
        else:
            nav.append(nav_item)

    if native_children:
        nav.append({NATIVE_PARENT["label"]: [{"Home": "native/index.md"}] + native_children})
    return nav


def _write_mkdocs_yml(nav: list):
    """Write mkdocs.yml from the nav structure."""
    config = {
        "site_name": "Background Fetch — SDK Reference",
        "site_url":  "https://transistorsoft.github.io/transistor-background-fetch/",
        "docs_dir":  "docs",
        "site_dir":  "site",
        "copyright": "Transistor Software",


        "theme": {
            "name":       "material",
            "custom_dir": "../src/overrides",
            "logo":       "assets/images/transistor-logo-clear.png",
            "favicon":    "assets/images/transistor-favicon.png",
            "palette":    {"scheme": "slate", "primary": "black", "accent": "amber"},
            "features": [
                "navigation.tabs",
                "navigation.top",
                "content.code.copy",
                "content.tabs.link",
            ],
        },

        "extra": {
            "analytics": {
                "provider": "google",
                "property": "G-4NNZKTE395",
            },
        },

        "extra_css": ["assets/css/site.css"],
        "extra_javascript": [
            "assets/js/lucide.min.js",
            "assets/js/toc-icons.js",
            "assets/js/method-groups.js",
            "assets/js/search-scope.js",
        ],

        "markdown_extensions": [
            "tables",
            "admonition",
            "attr_list",
            "md_in_html",
            {"toc": {"permalink": True, "toc_depth": 2}},
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
                content = _expand_includes(md.read_text(encoding="utf-8"))
                (dst_dir / md.name).write_text(content, encoding="utf-8")
                print(f"  copied {md.relative_to(ROOT)} → docs/{pid}/{md.name}")

        # Always ensure index + setup stubs exist
        _write_stub(dst_dir / "index.md",
                    f"# {plat['label']} — Background Fetch\n\n"
                    f"Select a topic from the navigation.\n")
        _write_stub(dst_dir / "setup.md",
                    f"# {plat['label']} — Setup\n\n"
                    f"*Setup guide coming soon.*\n")

    # Native hub page (the "Native" parent tab landing).
    native_src = STATIC / "native"
    native_dst = DOCS_DIR / "native"
    native_dst.mkdir(parents=True, exist_ok=True)
    if native_src.is_dir():
        for md in native_src.glob("*.md"):
            content = _expand_includes(md.read_text(encoding="utf-8"))
            (native_dst / md.name).write_text(content, encoding="utf-8")
            print(f"  copied {md.relative_to(ROOT)} → docs/native/{md.name}")
    _write_stub(native_dst / "index.md", "# Native\n\nSelect a language from the navigation.\n")


def _write_stub(path: Path, content: str):
    if not path.exists():
        path.write_text(content)
        print(f"  stub    docs/{path.relative_to(DOCS_DIR)}")


# ── CSS ───────────────────────────────────────────────────────────────────────

def _copy_assets():
    css_dst = DOCS_DIR / "assets" / "css"
    css_dst.mkdir(parents=True, exist_ok=True)
    if CSS_SRC.exists():
        shutil.copy(CSS_SRC, css_dst / "site.css")

    if JS_SRC.is_dir():
        js_dst = DOCS_DIR / "assets" / "js"
        if js_dst.exists():
            shutil.rmtree(js_dst)
        shutil.copytree(JS_SRC, js_dst)

    if IMAGES_SRC.is_dir():
        img_dst = DOCS_DIR / "assets" / "images"
        if img_dst.exists():
            shutil.rmtree(img_dst)
        shutil.copytree(IMAGES_SRC, img_dst)

    # Copy panel SVGs from transistorsoft/assets submodule
    panels_src = ASSETS_SUB / "images" / "logos" / "transistor"
    img_dst = DOCS_DIR / "assets" / "images"
    img_dst.mkdir(parents=True, exist_ok=True)
    if panels_src.is_dir():
        for svg in panels_src.glob("transistor-logo-panel-*.svg"):
            shutil.copy(svg, img_dst / svg.name)
        print(f"  copied panel SVGs from assets submodule")
    else:
        print(f"  warning: assets submodule not found at {panels_src}")

    print(f"  assets/images/ ({len(list(img_dst.rglob('*')))} files)")


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

    # ── Build set of known entry IDs for link validation ────────────
    KNOWN_ENTRY_IDS.clear()
    for cls_entries in entries_by_class.values():
        for entry in cls_entries:
            KNOWN_ENTRY_IDS.add(entry["id"])

    # ── Per-native-language: which pages actually have native content ─
    _NATIVE_PAGES_WITH_CONTENT.clear()
    for plat in PLATFORMS:
        if plat.get("native"):
            cl = plat["code_lang"]
            _NATIVE_PAGES_WITH_CONTENT[cl] = {
                p["id"] for p in ALL_PAGES if _page_has_native_content(p, entries_by_class, cl)
            }

    # ── Generate per-platform pages ──────────────────────────────────
    for plat in PLATFORMS:
        pid = plat["id"]
        native = plat.get("native", False)
        code_lang = plat["code_lang"]
        plat_dir = DOCS_DIR / pid
        plat_dir.mkdir(parents=True, exist_ok=True)

        for page_def in ALL_PAGES:
            # Native pages with no native content are omitted (and excluded from nav).
            if native and not _page_has_native_content(page_def, entries_by_class, code_lang):
                continue
            content = _build_page(page_def, entries_by_class, plat)
            out = plat_dir / f"{page_def['id']}.md"
            out.write_text(content)
            print(f"  wrote  docs/{pid}/{page_def['id']}.md")

    # ── Landing page ────────────────────────────────────────────────
    landing = _build_landing_page()
    (DOCS_DIR / "index.md").write_text(landing)
    print(f"  wrote  docs/index.md")

    # ── Static pages ─────────────────────────────────────────────────
    _copy_static_pages()

    # ── Assets (CSS + images) ────────────────────────────────────────
    _copy_assets()

    # ── CNAME (for GitHub Pages custom domain) ─────────────────────
    (DOCS_DIR / "CNAME").write_text("fetch.transistorsoft.com\n")

    # ── mkdocs.yml ───────────────────────────────────────────────────
    nav = _build_nav(entries_by_class)
    _write_mkdocs_yml(nav)


if __name__ == "__main__":
    generate()
