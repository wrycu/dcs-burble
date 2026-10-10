"""The hub's look, from the custom CSS its admins set (on /admin): the website gets the CSS as it is; images drawn
for Discord (the greenie board, the landing posts' colour) can't use CSS rules, so they take the theme variables
set in it (`:root { --bg: ...; --grade-ok: ...; }`), the same variables the website's own styles use.

Discord's images are dark, so variables set for dark mode (`@media (prefers-color-scheme: dark) { :root { ... } }`)
win over ones set for both; ones set only for light mode are ignored here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# Each grade's variable (the website's .g-N classes use them too), by grade as stored.
GRADE_VARS = {"_OK_": "--grade-perfect", "OK": "--grade-ok", "(OK)": "--grade-fair", "---": "--grade-no-grade",
              "B": "--grade-bolter", "WO": "--grade-wave-off", "C": "--grade-cut"}
# The other variables a theme takes: Theme field -> variable.
COLOR_VARS = {"bg": "--bg", "panel": "--card", "row_alt": "--row-alt", "empty": "--empty", "text": "--text",
              "muted": "--muted", "border": "--border"}
FONT_VAR = "--font"

MAX_CSS = 20_000  # characters
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,30}|(?:rgb|rgba|hsl|hsla)\([0-9.,%\s/]+\)")
_FONT = re.compile(r"""[\w\s,"'-]{1,200}""")
DEFAULT_FONT = "DejaVu Sans, Verdana, Arial, sans-serif"


@dataclass(frozen=True, slots=True)
class Theme:
    """Colours and font for the images (defaults: the dark board as it always was)."""
    bg: str = "#0d1117"
    panel: str = "#161b22"
    row_alt: str = "#1c2129"
    empty: str = "#21262d"
    text: str = "#e6edf3"
    muted: str = "#8d96a0"
    border: str = "#30363d"
    font: str = DEFAULT_FONT
    grades: dict[str, str] = field(default_factory=dict)  # grade -> colour, where the CSS sets one


def theme_from_css(css: str | None) -> Theme:
    """The theme the CSS's variables give (anything not set, or not a plain colour or font, keeps its default)."""
    values = css_variables(css or "")
    theme = Theme()
    changes = {name: values[var] for name, var in COLOR_VARS.items()
               if var in values and _COLOR.fullmatch(values[var])}
    if FONT_VAR in values and _FONT.fullmatch(values[FONT_VAR]):
        changes["font"] = f"{values[FONT_VAR]}, {DEFAULT_FONT}"
    grades = {g: values[var] for g, var in GRADE_VARS.items() if var in values and _COLOR.fullmatch(values[var])}
    return replace(theme, **changes, grades=grades)


def css_variables(css: str) -> dict[str, str]:
    """Custom properties set on `:root` at the top level, then (overriding them) in dark-mode media blocks."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    both: dict[str, str] = {}
    dark: dict[str, str] = {}
    for media, selector, body in _rules(css):
        if ":root" not in [s.strip() for s in selector.split(",")]:
            continue
        if media is None:
            target = both
        elif "prefers-color-scheme" in media and "dark" in media:
            target = dark
        else:
            continue
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+)", body):
            target[name] = value.strip()
    return both | dark


def _rules(css: str):
    """(media query or None, selector, declarations) for each rule, one level of @-blocks deep."""
    i, n = 0, len(css)
    media: str | None = None
    depth_media = False
    start = 0
    while i < n:
        c = css[i]
        if c == "{":
            head = css[start:i].strip()
            if head.startswith("@"):
                media, depth_media = head, True
                start = i + 1
            else:
                end = css.find("}", i)
                if end < 0:
                    return
                yield (media if depth_media else None), head, css[i + 1:end]
                i = end
                start = i + 1
        elif c == "}":
            media, depth_media = None, False
            start = i + 1
        i += 1


def safe_css(css: str) -> str:
    """The CSS as it can go in a page's <style> (no way out of it: `<` only appears escaped)."""
    return css.replace("<", "\\3c ")


def grade_color(theme: Theme, grade: str, default: str) -> str:
    return theme.grades.get(grade, default)
