# CreateBench v2 spike: one brief end to end (QUA-2851, 2026-09-30)

**Status: PARTIAL. The creation half ran live; the two journey-runner episodes did not.**
One real creation episode ran: Codex CLI with GPT-6 Astra, following the
`qualgent-test-creator` template, with no source code. It explored the clean medtimer
build and saved a case through the real QualGent-MCP `create_test_case`, which was
backed by a local fake API. The captured case was converted by hand into a journey
case, and the frozen journey runner accepted it: the plan, the held-out loading, the
DevLoop checks and the server stamp all passed. The first runner episode was still in
staging when the host ran out of memory and restarted. By owner instruction, live
device work then stopped until an emulator slot is free again. Section 7 lists what
remains to run.

**GO/NO-GO: GO for the creation side (QUA-2852, QUA-2856). The runner/grader side
(QUA-2857) is a provisional GO until the clean and target episodes in section 7 have
run.** Nothing in the creation chain failed. Every gap found is a scoping item, and
section 8 assigns each one to an existing sibling ticket.

Throwaway harness (not merged, per the ticket): branch `spike/qua-2851-harness`,
`scratch/qua-2851/` (`fake_api.py`, `probe_qgmcp.py`, `multi_server_check.py`,
`create_episode.py`, `convert_to_journey.py`, `briefs/medtimer-add-medicine.md`).
Private text never enters this repository. The creator template is read at run time
from a private DevLoop-MCP checkout and written only into the episode's gitignored
`codex_home/config.toml`, under `~/.qualgentbench/`.

## Setup

| | |
| --- | --- |
| harness | qualgentbench-oss `5ba16d4` (epic branch `epic/qua-2850-createbench-v2`) |
| QualGent-MCP | `8fb4ce7` (main), a separate detached worktree, `.venv/bin/qualgent-mcp` over stdio |
| DevLoop-MCP | `52a20c6` (main), a separate detached worktree: `uv run devloop-mcp --transport streamable-http --port 51871 --app-source none`. The DevLoop desktop app kept `127.0.0.1:51821` the whole time |
| agent | codex-cli 0.156.1, `gpt-6-astra` (codex default reasoning, `low`) |
| device | `qgbench_root` (android-35, arm64), `-read-only`, `emulator-5554` |
| app | journey build `medtimer-buggy.apk` sha256 `0d1578a2…6ed3` (the pinned hash), clean episode: flags file empty |
| fake API | `scratch/qua-2851/fake_api.py` on `127.0.0.1:18731` |
| runs | `~/.qualgentbench/runs-qua2851/` (outside every directory that holds CLAUDE.md or AGENTS.md; see QUA-2778) |

**Brief** (`scratch/qua-2851/briefs/medtimer-add-medicine.md`, written by hand, neutral):

> Feature: adding a medicine. MedTimer keeps the user's medicines in a list on its
> Medicine tab. From that tab a user can add a new medicine by giving it a name. Once it
> is added, the user expects to find the new medicine in the Medicine tab's list, next to
> the medicines that were already there. Write one QualGent test case that checks this
> feature the way a user would use it.

**Target defect:** `medicine-list-empty-reminders-crash` (L4 crash, canary-covered: it
fires its own marker on the seeded arm only; `data/test-cases/medtimer.yaml`). A
medicine with no reminders crashes the Medicine list when the list draws it, with
`NoSuchElementException` from `MedicineCard`. A newly added medicine has no reminders,
so any case that goes back to the list after adding one crashes on the target build.
The brief never mentions the list crashing. A case that stops on the new medicine's
own screen never reaches the defect.

## 1. How the creator template behaves under headless Codex

The spike delivers the template the same way the desktop app installs it for Codex
(QualGent3 `desktop/src/main/agents/subagent-renderer.ts::renderCodexToml`): the
frontmatter tool list is dropped and the body becomes `developer_instructions`. It
goes into the episode's own `config.toml` (`create_episode.py`). The episode's codex
rollout confirms the instructions arrived. The user prompt is our own text: the device
and server note, "no source code", the brief, and an approval note.

What the agent did (transcript
`create/medtimer-add-medicine-back-to-list/2026-09-30T21-12-27Z_…_create_trial-1_ep-e95992f1c8c9/agent/transcript.txt`,
20 tool calls):

1. `list_mcp_resources` and `read_mcp_resource qualgent://test-case-guide`. Codex's
   built-in resource tools work against the stdio server. The template points to this
   resource, and the agent read it.
2. `list_categories` and `list_test_cases` (template step 2). It noted that neither
   returned anything, left `category_id` unset and recommended a category in its
   review message, as the template asks.
3. It skipped the code-reading step (step 1) because no source exists and the prompt
   said so. It skipped `list_credentials`, correctly, because the flow has no login.
   It never called `check_credits`, `list_apps` or `upload_test_file`.
4. It skipped device acquisition (step 4) because the prompt said the device was
   reserved. It never looked for `qg_acquire_device`.
5. It explored with 11 device calls: `mobile_dismiss_dialogs`, `mobile_observe_screen`
   ×4, `mobile_tap_and_observe` ×5 (`Medicine`, `Add medicine`, `Medicine name`, `OK`,
   `Navigate up`) and `mobile_type_text`. The meter charged 16 interactions (6 tap,
   1 type, 9 observe). The agent found that OK opens the new medicine's own screen,
   and it used "Navigate up" to go back to the list.
6. **The approval step** (template `subagent-templates/qualgent-test-creator.md:257-275`, the review and
   submit steps). The agent posted the full draft as a message, as the review
   step asks, then wrote "I'll submit using your advance approval" and called
   `create_test_case` in the same turn. The explicit note ("approval is granted…
   nobody will answer… do not wait") was enough. `codex exec` is single-turn, so an
   agent that asked a question here would end the episode with no case. That outcome
   has to be classified (section 8, P3).
7. It never called `qg_release_device`, which the standalone server does not serve.
   It finished with the created id.

**Tool-surface mismatch.** The template's tool list names
`qg_list_devices`, `qg_acquire_device`, `qg_release_device` and
`mobile_take_screenshot`. None of these exist on the standalone DevLoop server; the
first three are desktop-bridge pseudo-tools (see `tests/fixtures/devloop_tools.json`).
The agent was never hurt by this, because the prompt told it to skip lock handling.
Without that note, the template's steps 4 and 11 point at tools that are missing.

## 2. Can the codex adapter carry DevLoop and QualGent-MCP together?

**Yes, with no adapter change. The runner is what carries only one server.**

- `adapters/codex_cli.py::_mcp_servers` iterates `mcpServers`, and `_config_toml` emits
  one `[mcp_servers.<name>]` table per entry. It keeps `env` for a stdio server and
  drops it for an http one. `scratch/qua-2851/multi_server_check.py` renders the
  adapter's own config from a two-server `mcp_config.json`: `device` (http, DevLoop)
  plus `qualgent` (stdio, QualGent-MCP with `QUALGENT_API_URL` and `QUALGENT_API_KEY`
  in `env`). `codex mcp list --json` under that `CODEX_HOME` lists both, enabled, with
  the right transports.
- In the live episode the agent used both servers (`server: device` and
  `server: qualgent` on each `mcp_tool_call`).
- The one-server limit is upstream of the adapter. `episode_runner._generate_mcp_config`
  emits a single `device` entry from the single `--mcp-server` (`cli.py`), and the MCP
  meter fronts only that one. The spike monkeypatched `_generate_mcp_config` to append
  the `qualgent` entry.
- `disabled_tools` (from `QGB_DISALLOWED_TOOLS`) is written into **every** server's
  table, not just the device server's.
- QualGent-MCP calls do not go through the MCP meter, and they should not: they are
  not device interactions. `interactions.mcp_effective_rule` returns `None` for all 28
  of its tool names, because none contains `mobile_`. The creation-side record is the
  fake API's request log plus the transcript.

## 3. Which QualGent API routes the creator calls

**Live episode** (`~/.qualgentbench/runs-qua2851/create-api/requests.jsonl`, 3 requests,
each with an `x-api-key`):

| # | method | path | status |
| --- | --- | --- | --- |
| 1 | GET | `/v1/categories/list` | 200 `[]` |
| 2 | GET | `/v1/test-cases/list` | 200 `[]` |
| 3 | POST | `/v1/test-cases` | 201 |

**Every QualGent tool in the template's tool list**, called directly over stdio against
the fake (`probe_qgmcp.py`, log `scratch/qua-2851/out/offline/requests.jsonl`):

| tool | route | spike fake answers |
| --- | --- | --- |
| `list_apps` | GET `/v1/apps/list` | `[]` |
| `list_categories` | GET `/v1/categories/list` (client-cached 60 s) | `[]` |
| `list_credentials` | GET `/v1/credentials/list` (cached) | `{}`, which the tool renders as "No credentials found for your organization." |
| `list_test_cases` | GET `/v1/test-cases/list[?category=]` | the cases created so far |
| `get_test_case` | GET `/v1/test-cases/{id}[?version_id=]` | the stored body |
| `create_test_case` | POST `/v1/test-cases` | 201 `{id, name, version_id, files: []}` (the real `CreateTestCaseResponse` shape, api-service `app/routers/test_cases.py:570`) |
| `check_credits` | POST `/v1/credits/validate` `{required_credits}` | `{has_sufficient_credits: true, …}` |
| `upload_test_file` | POST `/v1/apps/upload-test-file` (multipart) | not exercised; would 404 |
| (`list_devices`, not in the template) | GET `/v1/devices` | `{devices: []}` |

Notes for the real fake (QUA-2852):

- The server needs `QUALGENT_API_KEY` to start with `qg_`. Otherwise `config.get_api_key`
  raises and the client quietly falls back to an empty key.
- `QUALGENT_API_URL` from the environment wins over the checkout's `.env`. The
  docstring in `config.get_api_url` says the opposite; the code is what counts. No
  QualGent-MCP code change was needed.
- An empty list comes back to the agent as **no content blocks**, and the agent
  handled that correctly.
- The Codex rendering drops the template's tool list, so the creator is offered all
  28 QualGent tools. That includes `run_tests`, `delete_apps`, `upload_app`,
  `save_bug`, `update_test_case` and `record_routine`. The fake answers 404 for
  anything unknown and logs it. The arm spec decides whether to narrow the list
  (`enabled_tools` per server is already supported by the adapter).

## 4. The captured artifact

`~/.qualgentbench/runs-qua2851/create-api/captures/01.json`, the full request body
(the agent's own words; public-safe):

```json
{
  "name": "Add medicine and return to the medicine list",
  "steps": [
    {"description": "Open the app", "kind": "setup"},
    {"description": "Tap the \"Medicine\" tab", "kind": "setup"},
    {"description": "Wait for the medicine list to load", "kind": "setup"},
    {"description": "Record the names of the existing medicines for comparison", "kind": "setup"},
    {"description": "Tap \"Add medicine\"", "kind": "act"},
    {"description": "Tap the \"Medicine name\" field", "kind": "act"},
    {"description": "Enter \"QG Test Medicine\" followed by a unique suffix in the \"Medicine name\" field", "kind": "act"},
    {"description": "Tap \"OK\"", "kind": "act"},
    {"description": "Wait for the new medicine's detail screen to load", "kind": "act"},
    {"description": "Tap \"Navigate up\"", "kind": "act"},
    {"description": "Wait for the medicine list to load", "kind": "act"},
    {"description": "Verify a medicine entry with the full name entered in this run is present in the \"Medicine\" list", "kind": "verify"},
    {"description": "Verify the previously recorded medicines remain in the list", "kind": "verify"}
  ],
  "expected_result": "The Medicine list contains the newly added medicine alongside the medicines present before adding it.",
  "priority": "High",
  "change_source": "mcp"
}
```

The shape: `name`, `steps[]` of `{description, kind?, credential_id?}`, `expected_result`,
`priority`, `change_source: "mcp"`, and optionally `description`, `category_id` and
`file_attachments`. `kind` is one of setup, act or verify, and this agent tagged every
step.

As a test, the case is good. It goes back to the list after adding, so on the target
build it walks into the crash. It follows the template's advice to create its own test data by
giving the medicine **a unique suffix per run**. It also adds a "record the existing
medicines" step so it can compare before and after. Those two choices are exactly what
makes the case hard to grade with a fixed oracle (section 8, P6).

**Hand conversion** (`convert_to_journey.py`): `name` becomes `name`,
`steps[].description` becomes `steps`, and `expected_result` becomes
`expected_outcome`. Everything else comes from the harness, taken from the corpus case
the brief targets: `bugs: [medicine-list-empty-reminders-crash]`, the `crash:
NoSuchElementException` gate, and the `db:` oracle. The oracle's query was rewritten by
hand to `medicineName like 'QG Test Medicine%'` because the authored name is not
`Lisinopril`. The step budget was set to 60 by hand (13 authored steps, where the corpus
case has 5 steps and a budget of 45). The case is served through the held-out loader
(`QGB_HELDOUT_DIR`, a scratch copy of `test-cases/medtimer.yaml` holding only this case,
id `medtimer-createspike-add-medicine`), so `run --mode journey` runs it with no code
change. `journey.journey_tasks` builds both versions: clean expects PASS, and seeded
expects FAIL, blocking on the target, with `crash_texts: [NoSuchElementException]`.
The journey brief lists the 13 authored steps and the authored expected outcome
verbatim.

## 5. The journey runner on clean and on the target

**NOT RUN (section 7).** Run `20260930-211636-036d`
(`~/.qualgentbench/runs-qua2851/journey/`) passed plan and preflight: held-out
`90eabf370319` (medtimer), corpus `897f0f94a1b6`, brief v3, server stamp `devloop-mcp`
1.27.0 with `app_source: none`. It started `…~clean`, and after 3 minutes the episode
was still in staging (snapshot and precondition) when the host ran out of memory and
restarted. The agent never launched: the episode dir has no `agent/` and no
`result.json`, so it is an orphan of that run. No verdicts exist yet.

The questions these two episodes answer:

- Does Astra-as-runner produce a unique name, satisfy the `LIKE` oracle and report
  `pass` on clean?
- Does it hit the crash at "Navigate up" (step 10) on target, report `fail`, and name
  the crash?

## 6. Wall time and cost of one creation episode

| | |
| --- | --- |
| agent wall time | **104.6 s** (`result.json` `wall_time_sec`) |
| episode wall time incl. install and staging | **145.3 s** (`create_episode.py`, `run_episode`) |
| tokens | 394,086 input (345,344 cached), 1,447 output (61 reasoning) |
| cost | **$0.91** (`cost_source: estimated`, the pricing table's `gpt-6-astra` row) |
| device interactions | 16 (6 tap, 1 type, 9 observe); 20 tool calls in total |

Almost all of the input is screenshots returned by `mobile_observe_screen` and
`mobile_tap_and_observe`, re-sent every turn and mostly cached. This is in line with
the $0.85-per-episode Astra journey average on the DevLoop arm
(`docs/comparison-fable-astra-2026-09-24.md`).

## 7. What remains to run (live)

The prerequisites are an android-35 emulator and memory headroom.
**Port 51821 is not needed**: the standalone DevLoop ran on 51871 next to the desktop
app, and every bench check passed against it. All inputs are on disk:

1. Boot an emulator (read-only `qgbench_root`) and start
   `devloop-mcp --transport streamable-http --port 51871 --app-source none` from the
   DevLoop-MCP `52a20c6` worktree.
2. Regenerate the held-out copy if it is gone:
   `uv run python scratch/qua-2851/convert_to_journey.py ~/.qualgentbench/runs-qua2851/create-api/captures/01.json ~/.qualgentbench/runs-qua2851/heldout`.
3. Run the journey once on clean and once on target:
   `QGB_HELDOUT_DIR=~/.qualgentbench/runs-qua2851/heldout uv run qualgent-bench run --agent codex-cli --models gpt-6-astra --app medtimer --case medtimer-createspike-add-medicine --mode journey --trials 1 --device <serial> --mcp-server http://127.0.0.1:51871 --runs-dir ~/.qualgentbench/runs-qua2851/journey -y --plain`.
   Use a new run, not `--resume` of the orphaned one.
4. Record both verdicts, the wall time and the cost in section 5, and settle the
   runner-side GO/NO-GO.

## 8. Problems found, with owners

| # | problem | evidence | owner |
| --- | --- | --- | --- |
| P1 | The runner emits one MCP server (`device`) from one `--mcp-server`, so creation needs a second, `qualgent` stdio entry (command + env → fake API). The adapter already handles it | §2 | QUA-2856 |
| P2 | The template lists tools the standalone server lacks (`qg_list_devices`/`qg_acquire_device`/`qg_release_device`, `mobile_take_screenshot`). Some creation-surface note has to say the device is already reserved. The spike's prompt did, and it worked | §1 | QUA-2852 (arm spec / surface injection) |
| P3 | The approval step: an explicit advance-approval note made the agent submit in the same turn. Single-turn `codex exec` means an agent that asks instead ends with no case, and that has to be a first-class outcome (`no_case_created`), not a crash or an empty grade | §1 step 6 | QUA-2852 (note text), QUA-2856 (outcome) |
| P4 | Codex drops the template's tool list, so the creator sees all 28 QualGent tools, including `run_tests`/`delete_apps`/`upload_app`/`save_bug`. The fake must 404 and log unknown routes, and the arm spec should decide on `enabled_tools` | §3 | QUA-2852 |
| P5 | `QGB_DISALLOWED_TOOLS` → `disabled_tools` is applied to every server | §2 | QUA-2856 |
| P6 | **Oracle binding.** The author picks the data (here a per-run unique suffix, chosen by the *runner* at run time), so the corpus case's fixed-string `db:` oracle cannot be reused. The spike hand-wrote `LIKE 'QG Test Medicine%'`. The grader needs oracles that do not depend on authored literals (a count delta on `Medicine`, liveness/crash gates), or it grades on verdict plus report alone and is honest about it | §4 | QUA-2857 |
| P7 | Budget for an authored case: 13 authored steps against the corpus route's 5. The spike set 60 by hand. The budget must come from the authored case or be one fixed creation-mode number, never the corpus case's | §4 | QUA-2857 |
| P8 | The creation episode's `result.json` comes from the journey verdict path (`passed: false`, "agent never called mobile_report_result"), which is meaningless for creation. `--mode create` needs its own result (case captured? request log path, capture path) | `create/…/result.json` | QUA-2856 |
| P9 | The runner has no way to take an authored case except a hand-made held-out tree. The frozen runner needs a real case source (captured JSON → journey task) that keeps harness-owned fields apart from authored ones | §4 | QUA-2857 |
| P10 | `QUALGENT_API_URL` precedence: the code says env wins over `.env`, and the docstring says the opposite. Harmless for the fake (env wins); worth a docstring fix in QualGent-MCP | §3 | no ticket (private repo, cosmetic) |

The split in QUA-2850 does not need to change: every problem falls inside an existing
sibling's scope.
