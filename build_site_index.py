"""Rebuilds index.html (the homepage) from site_index_template.html.

Split out purely so the homepage's nav stays in sync with every other
page automatically - it used to be a fully static, hand-maintained file
with its own literal copy of the <nav> markup, which is exactly the kind
of duplication that let the nav drift out of sync across the site (see
dashboard_template.py's render_nav() docstring / the site-wide nav audit
2026-09-20). Nothing else on this page is templated; it's still a static
page, just with one placeholder.

Run before "Build Pages site" in every deploy workflow, so index.html is
always freshly regenerated with whatever render_nav() currently outputs
regardless of which workflow last touched it.
"""
from dashboard_template import render_nav

TEMPLATE = "site_index_template.html"
OUTPUT = "index.html"


def main():
    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<NAV_HTML>>>" in template, "template placeholder missing"

    html = template.replace("<<<NAV_HTML>>>", render_nav(OUTPUT))

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
