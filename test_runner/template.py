"""
Creates the tester's .steps.txt file from a Jira test case JSON.

    python -m test_runner.template test_cases\\test_script_IAODTM-C22426_7905796.json
    python -m test_runner.template test_cases\\          (every JSON without a .steps.txt yet)
"""

import argparse
import json
import sys
from pathlib import Path

from .simple_steps import build_template, steps_file_for


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m test_runner.template",
                                     description="Create .steps.txt files for testers.")
    parser.add_argument("paths", nargs="+", help="Test case JSON files or folders")
    parser.add_argument("--force", action="store_true", help="Overwrite existing .steps.txt")
    args = parser.parse_args(argv)

    files = []
    for raw in args.paths:
        path = Path(raw)
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])

    for json_path in files:
        target = steps_file_for(json_path)
        if target.exists() and not args.force:
            print(f"Skipped (already exists): {target}")
            continue
        with open(json_path, encoding="utf-8") as f:
            raw = json.load(f)
        target.write_text(build_template(raw), encoding="utf-8")
        print(f"Created: {target}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
