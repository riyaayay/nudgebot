"""Tests the generic HTML scraper's parsing logic against a fake HTML page
(no network calls) by monkeypatching _get_soup.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from scrapers.generic_html import GenericHTMLScraper

FAKE_ASSIGNMENTS_HTML = """
<table class="assignments">
  <tr class="assignment-row" id="row-1">
    <td class="course-code">CS 101</td>
    <td class="assign-title">Problem Set 4</td>
    <td class="due-date">2026-10-03</td>
    <td class="points">20</td>
  </tr>
  <tr class="assignment-row" id="row-2">
    <td class="course-code">ECON 201</td>
    <td class="assign-title">Essay 2</td>
    <td class="due-date">2026-10-10</td>
    <td class="points">50</td>
  </tr>
</table>
"""

CONFIG = {
    "assignments_url": "https://fake.portal/assignments",
    "assignment_row_selector": "table.assignments tr.assignment-row",
    "row_id_attr": "id",
    "fields": {
        "course": {"selector": ".course-code", "attr": "text"},
        "title": {"selector": ".assign-title", "attr": "text"},
        "due_date": {"selector": ".due-date", "attr": "text"},
        "points": {"selector": ".points", "attr": "text"},
    },
}


class TestGenericHTMLScraper(unittest.TestCase):
    def test_parses_assignment_rows(self) -> None:
        scraper = GenericHTMLScraper(
            base_url=None,
            username="u",
            secret="p",
            config=CONFIG,
            allow_insecure_http=True,
            default_tz="UTC",
        )
        with patch.object(scraper, "_get_soup", return_value=BeautifulSoup(FAKE_ASSIGNMENTS_HTML, "html.parser")):
            assignments = scraper.fetch_assignments()

        self.assertEqual(len(assignments), 2)
        self.assertEqual(assignments[0].course, "CS 101")
        self.assertEqual(assignments[0].title, "Problem Set 4")
        self.assertIn("2026-10-03", assignments[0].mutable_fields["due_date"])
        self.assertEqual(assignments[0].key, "row-1")

    def test_no_assignments_url_returns_empty(self) -> None:
        scraper = GenericHTMLScraper(
            base_url=None,
            username="u",
            secret="p",
            config={"grades_url": "https://fake.portal/grades", "grade_row_selector": "y"},
            allow_insecure_http=True,
        )
        self.assertEqual(scraper.fetch_assignments(), [])


if __name__ == "__main__":
    unittest.main()
