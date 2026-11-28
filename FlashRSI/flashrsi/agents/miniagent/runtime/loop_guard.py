"""Bound repeated identical tool observations in editing runs."""

import hashlib
import json
import os
from pathlib import Path


def workspace_state(root):
    """Cheap file metadata fingerprint; this is a progress heuristic."""
    rows = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in {".git", "__pycache__", ".venv"}
            and not name.startswith(("history-", "meta-feedback-", "probe-evidence-"))
        )
        for name in sorted(files):
            path = Path(directory) / name
            try:
                stat = path.lstat()
                rows.append(
                    (str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns)
                )
            except FileNotFoundError:
                continue
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


class ToolLoopGuard:
    def __init__(self, limit, root):
        if type(limit) is not int or limit < 0 or limit == 1:
            raise ValueError("tool_repeat_limit must be zero or at least two")
        self.limit = limit
        self.root = root
        self.state = None
        self.seen = {}
        self.stopped = False
        self.event = None

    def observe(self, name, args, result):
        if not self.limit:
            return False
        state = workspace_state(self.root)
        if state != self.state:
            self.seen.clear()
            self.state = state
        signature = hashlib.sha256(
            json.dumps(
                [name, args, result], sort_keys=True, ensure_ascii=False, default=str
            ).encode()
        ).hexdigest()
        count = self.seen.get(signature, 0) + 1
        self.seen[signature] = count
        if count >= self.limit:
            self.stopped = True
            self.event = {
                "reason": "repeated_identical_tool_result_without_workspace_change",
                "tool": name,
                "repetitions": count,
                "signature_sha256": signature,
                "workspace_metadata_sha256": state,
            }
        return self.stopped
