"""The drift guard every day module's payload test calls.

Not a test module -- it holds assertions, not tests, so that
`tests/test_report_<day>.py` can import it without importing someone else's
tests. `tests/test_report_payload.py` exercises the guards themselves; a
decorative guard is worse than none, since eight modules would be trusting it.

The failure this exists to catch: `render` and `payload` are two emitters over
one set of values, and a section added to one and forgotten in the other is
invisible in either function read on its own.
"""

import json


def markdown_headings(text):
    """`[(level, heading)]` for every `##`/`###`/`####` line, in order. The H1
    dateline is excluded -- it is the header block, not a section."""
    found = []
    for line in text.splitlines():
        stripped = line.lstrip("#")
        level = len(line) - len(stripped)
        if 2 <= level <= 6 and stripped.startswith(" "):
            found.append((level, stripped.strip()))
    return found


def payload_headings(sections):
    """`[(level, heading)]` for every section carrying a heading, depth-first
    in render order. Headingless blocks -- the second table under Wednesday's
    Watchlist -- contribute nothing, which is right: they contribute no
    heading to the markdown either."""
    found = []
    for section in sections:
        if section.get("heading") is not None:
            found.append((section["level"], section["heading"]))
        found.extend(payload_headings(section.get("blocks", [])))
    return found


def assert_payload_matches_markdown(text, sections):
    assert payload_headings(sections) == markdown_headings(text), (
        "payload() and render() disagree about this report's sections.\n"
        f"  markdown: {markdown_headings(text)}\n"
        f"  payload:  {payload_headings(sections)}"
    )


def assert_json_serializable(header, sections):
    """A single numpy scalar or NaT anywhere in the tree fails the real write
    -- at the last step of a scheduled run, after the markdown has already
    been committed. Cheaper to catch here."""
    json.dumps({"header": header, "sections": sections})


def assert_no_display_strings(sections):
    """No table cell may carry a "no reading" placeholder as a string.

    The authoritative set is `payload.PLACEHOLDERS`, imported rather than
    restated so a new placeholder added there is enforced here automatically.
    It spans three vocabularies -- render.num/availability.read's "insufficient
    data", render.table's `--`, and format_trajectory's em dashes -- and the
    JSON's way of saying any of them is `null`. A consumer handed the string
    will render it as data or try to parse it.
    """
    from espn_ff.report.payload import PLACEHOLDERS

    for section in sections:
        for row in section.get("rows", []):
            for key, value in row.items():
                assert not (isinstance(value, str) and value.strip() in PLACEHOLDERS), (
                    f"section {section['id']!r} column {key!r} carries the markdown "
                    f"placeholder {value!r} -- payload must emit null instead"
                )
        assert_no_display_strings(section.get("blocks", []))
