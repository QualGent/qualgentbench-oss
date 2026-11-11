"""README's public journey corpus counts match `corpus-report` (QUA-3451).

The sentence went stale once ("41 cases … 82 episodes" while the corpus held 45); every
app added under the corpus expansion must update it, and this test says what to write."""

from __future__ import annotations

import re
from pathlib import Path

from qualgentbench import corpus_report

README = Path(__file__).parents[1] / "README.md"
SENTENCE = re.compile(
    r"(\d+) public apps carry (\d+) cases\s+between them \((\d+) to (\d+) each\) "
    r"× two versions = (\d+) episodes")


def test_readme_journey_counts_match_the_corpus_report():
    text = README.read_text()
    m = SENTENCE.search(text)
    assert m, "README.md lost the 'N public apps carry C cases between them' sentence"
    pub = corpus_report.build()["public"]
    per_app = [a["cases"] for a in pub["apps"].values()]
    want = (len(per_app), pub["cases"], min(per_app), max(per_app), 2 * pub["cases"])
    got = tuple(int(g) for g in m.groups())
    assert got == want, (
        f"README.md says {got}; corpus-report says {want}. Write: '{want[0]} public apps "
        f"carry {want[1]} cases between them ({want[2]} to {want[3]} each) × two versions "
        f"= {want[4]} episodes'")
