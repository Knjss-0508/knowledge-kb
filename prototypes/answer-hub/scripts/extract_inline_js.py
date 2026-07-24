from __future__ import annotations

import argparse
from pathlib import Path
import re


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    html = Path(args.html).read_text(encoding="utf-8")
    scripts = [
        match.group("code")
        for match in re.finditer(
            r"<script(?P<attrs>[^>]*)>(?P<code>.*?)</script>",
            html,
            re.S,
        )
        if not re.search(r"\bsrc\s*=", match.group("attrs"))
    ]
    if not scripts:
        raise SystemExit("No inline JavaScript found.")
    Path(args.output).write_text("\n".join(scripts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
