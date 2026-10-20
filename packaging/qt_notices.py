"""Collect the third-party notices for the Qt modules Cold Archive ships.

Qt publishes, per module, every third-party component compiled into it, each
with its copyright lines and full licence text:
https://doc.qt.io/qt-6/licenses-used-in-qt.html. This script takes the
modules in QT_MODULES (the same allow-list build_app.sh enforces on the
bundle), fetches each component's attribution page, and writes one Markdown
file. Rerun it when the Qt version or the allow-list changes:

    python3 packaging/qt_notices.py > licenses/Qt-third-party-notices.md

The page describes the current Qt 6 release, which can be newer than the
bundled one; a component list that is a superset of what is bundled is the
safe direction for notices.
"""
from __future__ import annotations

import html
import re
import sys
import urllib.request

BASE = "https://doc.qt.io/qt-6/"
# Must match QT_ALLOWED in packaging/build_app.sh (headings as Qt's page spells them).
QT_MODULES = ["Qt Core", "Qt GUI", "Qt Widgets", "Qt D-Bus", "Qt SVG"]
# Modules Qt's page has no section for, because they contain no third-party
# code (checked 2026-10-19). A module missing from the page and not listed
# here stops the script, so a new module cannot slip through unexamined.
NO_THIRD_PARTY = {"Qt Widgets", "Qt SVG"}


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"</p>|</h\d>|</li>", "\n", fragment)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def main() -> None:
    index = fetch(BASE + "licenses-used-in-qt.html")
    sections = re.split(r'<h2 id="[^"]*">', index)
    rows = []
    found = set()
    for sec in sections[1:]:
        heading = text(sec.split("</h2>", 1)[0]).strip()
        if heading not in QT_MODULES:
            continue
        found.add(heading)
        for href, name, lic in re.findall(
                r'<td class="tblName"[^>]*><p><a href="([^"]+)">(.*?)</a></p></td><td class="tblDescr"><p>(.*?)</p>',
                sec, flags=re.S):
            rows.append((heading, html.unescape(name).strip(), text(lic).strip(), href))
    missing = [m for m in QT_MODULES if m not in found and m not in NO_THIRD_PARTY]
    if missing:
        sys.exit(f"Qt's licence page has no section for {missing}; check the headings")
    unexpected = sorted(found & NO_THIRD_PARTY)
    if unexpected:
        sys.exit(f"Qt's licence page now lists third-party code for {unexpected}; remove them from NO_THIRD_PARTY")

    out = ["# Third-party code inside the bundled Qt libraries", "",
           "Cold Archive bundles these Qt 6 modules, unmodified, as separate dynamic libraries: "
           + ", ".join(QT_MODULES) + ". Qt itself is under the GNU LGPL v3 (see LGPL-3.0.txt "
           "and GPL-3.0.txt). The components below are compiled into those modules; each "
           "entry repeats the copyright lines and licence text that Qt publishes for it at "
           "https://doc.qt.io/qt-6/licenses-used-in-qt.html. Some apply only on other "
           "platforms; they are kept so the list errs on the side of completeness. "
           + " and ".join(sorted(NO_THIRD_PARTY)) + " contain no third-party code.", ""]
    seen = set()
    for module, name, lic, href in rows:
        if href in seen:
            continue
        seen.add(href)
        page = fetch(BASE + href)
        start = page.find("<h1")
        end = page.find("</main>") if "</main>" in page else len(page)
        body = re.sub(r"\n\s*\n+", "\n\n", text(page[start:end])).strip()
        lines = body.splitlines()[1:]            # the first line repeats the name
        out += [f"## {name}", "", f"*{module}. Licence: {lic}.*", "", "```", "\n".join(lines).strip(), "```", ""]
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
