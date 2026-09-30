"""QUA-2851 spike: hand-convert ONE captured `POST /v1/test-cases` body into a journey
case, served through the held-out loader (QGB_HELDOUT_DIR) so the frozen journey runner
runs it with no code change. Throwaway — QUA-2857 owns the real conversion.

The authored fields (name, steps, expected_result) become the agent-facing brief. The
harness-owned fields (the target defect, the crash gate, the db oracle, the budget)
come from the corpus case the brief targets; the oracle QUERY is edited by hand to the
authored data (see --name-like), which is exactly the step the real grader must own.

    uv run python scratch/qua-2851/convert_to_journey.py <capture.json> <heldout_dir>
"""

import argparse
import json
import shutil
from pathlib import Path

import yaml

from qualgentbench import corpus

TARGET = "medtimer-add-medicine-back-to-list"
CASE_ID = "medtimer-createspike-add-medicine"

ap = argparse.ArgumentParser()
ap.add_argument("capture", type=Path)
ap.add_argument("heldout", type=Path)
ap.add_argument("--name-like", default="QG Test Medicine%")
ap.add_argument("--budget", type=int, default=60)
a = ap.parse_args()

body = json.loads(a.capture.read_text())["request"]
src = corpus.PACKAGED / "test-cases" / "medtimer.yaml"
text = src.read_text()
canary = text.splitlines()[0]           # keep the contamination canary line
doc = yaml.safe_load(text)
target = next(c for c in doc["test_cases"] if c["id"] == TARGET)

expect = dict(target["check"]["expect"])
expect["query"] = (f"select count(*) from Medicine where medicineName like "
                   f"'{a.name_like}';")
case = {
    "id": CASE_ID,
    "name": body["name"],
    "steps": [s["description"] for s in body["steps"]],
    "expected_outcome": body["expected_result"],
    "step_budget": a.budget,
    "check": {"steps": target["check"]["steps"], "expect": expect},
    "bugs": list(target["bugs"]),
}
out = {k: v for k, v in doc.items() if k != "test_cases"}
out["test_cases"] = [case]

for sub in ("test-cases", "benchmarks", "truth"):
    (a.heldout / sub).mkdir(parents=True, exist_ok=True)
(a.heldout / "test-cases" / "medtimer.yaml").write_text(
    canary + "\n# QUA-2851 spike: authored case converted by hand.\n"
    + yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=100))
shutil.copy(corpus.PACKAGED / "benchmarks" / "medtimer.yaml", a.heldout / "benchmarks")
shutil.copy(corpus.PACKAGED / "truth" / "journey-medtimer.json", a.heldout / "truth")
print(yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=100))
