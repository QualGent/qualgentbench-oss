# QualGentBench — common commands. See README.md.
#
#   make setup                 deps
#   make doctor                check device + agent + APKs
#   make test                  unit tests + undefined-name check
#   make preflight             check bench.config.yaml and print the plan/ETA
#   make run                   run bench.config.yaml on this machine (asks first)
#   make image                 build the Docker image (harness + APKs + agent CLIs)
#   make launch                run bench.config.yaml from the image, booting its AVDs
#
CONFIG ?= bench.config.yaml
IMAGE  ?= qualgentbench:local

.PHONY: help setup doctor test preflight run image launch

help:
	@grep -E '^#( |   )' Makefile | sed 's/^# //'

setup:
	uv sync

doctor:
	uv run qualgent-bench doctor

test:
	uv run pytest -q
	uvx ruff check --select F821 src tests scripts

preflight:
	uv run qualgent-bench preflight $(CONFIG) --plan

run:
	uv run qualgent-bench run --config $(CONFIG)

image:
	docker build -t $(IMAGE) .

launch:
	python3 scripts/launch.py $(CONFIG) --image $(IMAGE)
