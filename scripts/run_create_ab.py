#!/usr/bin/env python3
"""CreateBench v2: run, resume or report a pre-registered creation-arm A/B (QUA-2858).

The driver lives in `qualgentbench.create.ab` (its docstring is the full contract:
the pre-registered prediction, how each expectation is judged, resume, the cost
ceiling). The exit code IS the verdict: 0 DETECTED, 1 MISSED, 3 INCONCLUSIVE,
4 INCOMPLETE, 2 refused (bad arguments, a changed registration, an unready gate).

    # plan only (nothing written, nothing spent)
    uv run python scripts/run_create_ab.py run --experiment pc-1 \\
        --a-qualgent-mcp ~/Work/QualGent-MCP@main --a-devloop ~/Work/DevLoop-MCP@main \\
        --b-qualgent-mcp ~/Work/QualGent-MCP@throwaway/createbench-v2-harmful-rule \\
        --b-devloop ~/Work/DevLoop-MCP@main
    # run / resume (same command; a bare resume needs only --experiment)
    uv run python scripts/run_create_ab.py run --experiment pc-1 ... --device emulator-5558 \\
        --mcp-server http://127.0.0.1:51871 --max-cost 700 --yes
    # read-out
    uv run python scripts/run_create_ab.py report --experiment pc-1
"""

import sys

from qualgentbench.create.ab import main

if __name__ == "__main__":
    sys.exit(main())
