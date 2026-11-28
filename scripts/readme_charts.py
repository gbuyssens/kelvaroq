"""Draws the README's measurement charts as SVG, light and dark.

python scripts/readme_charts.py

The numbers are heavy frames (3,000 draws or more) of the prologue at
3840 x 2160 on an RTX 5080; documentation.md gives each measurement and its
protocol. Edit MILESTONES or AB and run again.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "charts"

# Median frame time of heavy frames at each milestone, in milliseconds.
MILESTONES = [
    (("Experimental", "branch"), "before the port", 254.9, "254.9 ms"),
    (("Ported to", "victory-fork"), "patches 0001–0037", 235.0, "~235 ms"),
    (("Memory and", "submission"), "patches 0038–0040", 198.3, "198.3 ms"),
    (("Mesh launch,", "wide hash"), "patches 0041–0042", 119.8, "119.8 ms"),
    (("Copies, pages,", "async present"), "patches 0044–0048", 108.5, "108.5 ms"),
]
TARGET = 33.3

# The A/B of patches 0041-0042: same save, same warm pipeline cache, 300 s each.
AB = [("Median", 198.3, 119.8), ("p95", 312.4, 207.8), ("Mean", 221.1, 137.0)]

# GitHub's surfaces and text inks; the series hues are the validated
# categorical slots 1 and 2 (blue, orange) stepped for each surface.
THEMES = {
    "light": {
        "surface": "#ffffff",
        "text": "#1f2328",
        "muted": "#59636e",
        "grid": "#d1d9e0",
        "s1": "#257de0",
        "s2": "#f47230",
    },
    "dark": {
        "surface": "#0d1117",
        "text": "#e6edf3",
        "muted": "#9198a1",
        "grid": "#3d444d",
        "s1": "#338ced",
        "s2": "#e76624",
    },
}
FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
)


def bar(x, y, w, base):
    """A bar from the baseline up to y, its data end rounded 4 px."""
    r = min(4, (base - y) / 2, w / 2)
    return (
        f"M{x:.1f},{base:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
        f"H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{base:.1f}Z"
    )


def text(x, y, s, fill, size=13, anchor="start", weight=400):
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{s}</text>'
    )


def frame(width, height, t, body, title):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}" font-family="{FONT}">'
        f'<title>{title}</title><rect width="{width}" height="{height}" rx="8" fill="{t["surface"]}"/>'
        + "".join(body)
        + "</svg>\n"
    )


def milestones(t):
    width, height = 760, 380
    left, right, top, base = 72, 644, 92, 300
    most = 300.0
    y = lambda v: base - (base - top) * v / most
    body = [
        text(24, 34, "Median frame time of heavy frames", t["text"], 17, weight=600),
        text(
            24,
            56,
            "Prologue, 3840×2160, RTX 5080, frames with 3,000 draws or more · lower is better",
            t["muted"],
            12,
        ),
    ]
    for v in (0, 100, 200, 300):
        body.append(
            f'<line x1="{left}" x2="{right}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{t["grid"]}" stroke-width="1"/>'
        )
        body.append(text(left - 10, y(v) + 4, f"{v} ms", t["muted"], 11, "end"))
    slot = (right - left) / len(MILESTONES)
    w = 64
    for i, ((l1, l2), patches, value, label) in enumerate(MILESTONES):
        cx = left + slot * (i + 0.5)
        body.append(
            f'<path d="{bar(cx - w / 2, y(value), w, base)}" fill="{t["s1"]}"/>'
        )
        body.append(text(cx, y(value) - 8, label, t["text"], 12, "middle", 600))
        body.append(text(cx, base + 20, l1, t["text"], 12, "middle"))
        body.append(text(cx, base + 36, l2, t["text"], 12, "middle"))
        body.append(text(cx, base + 52, patches, t["muted"], 11, "middle"))
    ty = y(TARGET)
    body.append(
        f'<line x1="{left}" x2="{right}" y1="{ty:.1f}" y2="{ty:.1f}" stroke="{t["muted"]}" '
        f'stroke-width="1.5" stroke-dasharray="5 4"/>'
    )
    body.append(text(right + 12, ty - 2, "30 fps target", t["muted"], 11))
    body.append(text(right + 12, ty + 13, "33.3 ms", t["muted"], 11))
    return frame(
        width, height, t, body, "Median frame time of heavy frames by milestone"
    )


def ab(t):
    width, height = 760, 330
    left, right, top, base = 72, 736, 96, 286
    most = 350.0
    y = lambda v: base - (base - top) * v / most
    body = [
        text(
            24,
            34,
            "Patches 0041–0042, A/B in the heavy scene",
            t["text"],
            17,
            weight=600,
        ),
        text(
            24,
            56,
            "Same save and warm pipeline cache, 300 s each · 959 heavy frames before, 1,564 after",
            t["muted"],
            12,
        ),
    ]
    # Legend, top right: swatch then name, in text ink.
    lx = right - 300
    for k, (name, color) in enumerate(
        (("Before (0038–0040)", t["s2"]), ("After (0041–0042)", t["s1"]))
    ):
        x = lx + k * 156
        body.append(
            f'<rect x="{x}" y="74" width="12" height="12" rx="3" fill="{color}"/>'
        )
        body.append(text(x + 18, 84, name, t["text"], 12))
    for v in (0, 100, 200, 300):
        body.append(
            f'<line x1="{left}" x2="{right}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{t["grid"]}" stroke-width="1"/>'
        )
        body.append(text(left - 10, y(v) + 4, f"{v} ms", t["muted"], 11, "end"))
    slot = (right - left) / len(AB)
    w, gap = 56, 2
    for i, (name, before, after) in enumerate(AB):
        cx = left + slot * (i + 0.5)
        for value, color, x in (
            (before, t["s2"], cx - gap / 2 - w),
            (after, t["s1"], cx + gap / 2),
        ):
            body.append(f'<path d="{bar(x, y(value), w, base)}" fill="{color}"/>')
            body.append(
                text(
                    x + w / 2,
                    y(value) - 8,
                    f"{value:.1f}",
                    t["text"],
                    12,
                    "middle",
                    600,
                )
            )
        body.append(text(cx, base + 22, name, t["text"], 12, "middle"))
    return frame(
        width, height, t, body, "Heavy-frame times before and after patches 0041-0042"
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for mode, t in THEMES.items():
        (OUT / f"milestones-{mode}.svg").write_text(milestones(t), "utf8", newline="\n")
        (OUT / f"ab-0041-{mode}.svg").write_text(ab(t), "utf8", newline="\n")
    print("charts written to", OUT)


main()
