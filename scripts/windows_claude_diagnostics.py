#!/usr/bin/env python3
"""Print read-only, redacted Windows Claude Desktop configuration diagnostics."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.windows_claude_diagnostics import collect_windows_claude_diagnostics  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(collect_windows_claude_diagnostics(), ensure_ascii=False, indent=2))
