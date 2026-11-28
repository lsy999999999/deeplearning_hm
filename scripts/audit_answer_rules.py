#!/usr/bin/env python3
"""Offline audit of a harness module exporting an ordered AUDIT_RULES list.

Rules follow the pure (answer, evidence) -> string contract. This loads trusted
harness code with the same permissions as the caller; it is not a sandbox.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from flashrsi.agents.miniagent.runtime.rule_audit import audit_rules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        required=True,
        help="JSON list: id, answer, evidence, expected",
    )
    parser.add_argument(
        "--hypotheses", type=Path, help="JSON list of disable-rule output predictions"
    )
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Choose a new output path to preserve prior evidence")
    spec = importlib.util.spec_from_file_location(
        "audited_harness_rules", args.module.resolve()
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = json.loads(args.cases.read_text())
    hypotheses = json.loads(args.hypotheses.read_text()) if args.hypotheses else []
    result = audit_rules(
        module.AUDIT_RULES,
        cases,
        hypotheses=hypotheses,
        provenance={
            "snapshot_id": args.snapshot_id,
            "module": str(args.module.resolve()),
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Saved {len(result['variants'])} intervention variants to {args.output}")


if __name__ == "__main__":
    main()
