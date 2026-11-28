"""Fixed controller entry: replay an explicitly declared candidate rule module."""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

from flashrsi.agents.miniagent.runtime.rule_audit import audit_rules


def main():
    request = json.load(sys.stdin)
    root = Path(request["root"]).resolve()
    module_path = (root / request["module_path"]).resolve()
    if not module_path.is_relative_to(root) or not module_path.is_file():
        raise ValueError("Rule module must be inside the pinned snapshot")
    sys.path.insert(0, str(root))
    with contextlib.redirect_stdout(sys.stderr):
        spec = importlib.util.spec_from_file_location(
            "rsi_candidate_rules", module_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rules = module.AUDIT_RULES
        if [rule.rule_id for rule in rules] != request["rule_ids"]:
            raise ValueError(
                "Exported AUDIT_RULES differ from the recorded runtime order"
            )
        report = audit_rules(
            rules,
            request["cases"],
            hypotheses=request["hypotheses"],
            provenance=request["provenance"],
        )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
