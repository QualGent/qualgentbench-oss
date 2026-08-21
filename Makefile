# QualGentBench — turnkey runner. See REPRODUCE.md for prerequisites.
# Usage examples:
#   make setup                      # deps + build the FOSS seeded-bug apps
#   make doctor                     # check DevLoop bridge + device + agent
#   make regression                 # Story 2: N-DL vs DL vs DL-R across all emulators
#   make hunt                       # Story 1: raw vs devloop hunt across all emulators
#   make push-hunt                  # push hunt results → 'bug_exploratory' tab
#   make push-regression            # push regression results → 'Regression' tab
#
# Overridable vars (defaults shown):
AGENT   ?= claude-code
MODEL   ?= claude-opus-4-8
TRIALS  ?= 3
APP     ?= easynotes
DEVICES ?= auto            # 'auto' = every online emulator; or 'cloud'/serial list
HUNT_TAB ?= bug_exploratory
REG_TAB  ?= Regression

.PHONY: help setup doctor regression hunt push-hunt push-regression all

help:
	@grep -E '^#( |   )' Makefile | sed 's/^# //'

setup:
	uv sync
	# Rebuild the seeded-bug apps (APK + buggy source).
	-uv run python scripts/build_app.py easynotes --emit-source
	-uv run python scripts/build_app.py opencalc  --emit-source
	-uv run python scripts/build_app.py pftodo    --emit-source
	@echo "Setup done. Start the DevLoop app (logged in) + boot emulator(s), then: make doctor"

doctor:
	uv run qualgent-bench doctor --agent $(AGENT) --lean

# ── Story 2: regression (N-DL / DL / DL-R), one setup per emulator, concurrent ──
# Interactive picks: app → pass cases → fail cases → model → credential.
# Provide QGB_RAW_CREDENTIAL in .env for the N-DL login.
regression:
	uv run qualgent-bench eval regression --devices $(DEVICES) --agent $(AGENT) --trials $(TRIALS)

# ── Story 1: hunt (raw vs devloop), spread across all emulators in parallel ─────
hunt:
	uv run python scripts/parallel_bugs.py --apps $(APP) --agent $(AGENT) \
	  --models $(MODEL) --conditions raw,devloop --mode hunt \
	  --devices $(DEVICES) --trials $(TRIALS)

push-hunt:
	uv run python scripts/push_ablation.py --sheet-name $(HUNT_TAB)

push-regression:
	uv run python scripts/push_regression.py --sheet-name $(REG_TAB)

all: hunt push-hunt
