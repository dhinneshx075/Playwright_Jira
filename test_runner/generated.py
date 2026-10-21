"""
Saved AI-generated actions for Jira cases are regenerated from freshly fetched Jira fields
on each run; previous files are kept only as backups. Local JSON plans can still be reused.
An AI repair can correct a step in place during a run.

    test_cases\\<id>.generated.json        the saved test case with "actions"
    test_cases\\<id>.generated.json.<time>.bak   every earlier version (before a regeneration or a repair)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import config


def fingerprint(raw: dict) -> str:
    """Hash of what the AI reads: each step's text, test data and expected result."""
    parts = [[str(s.get("step", "")), str(s.get("test_data", "")), str(s.get("expected_result", ""))]
             for s in raw.get("steps", [])]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def stamp(result: dict, source: dict) -> dict:
    """Mark a model reply as generated from `source` (call before saving)."""
    result["ai_generated"] = True
    result["source_hash"] = fingerprint(source)
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    result["verified_at"] = None      # set once a full run of this plan passes
    return result


def backup(path: Path) -> Path | None:
    """Keep the current file as <name>.<time>.bak before it is replaced; returns the backup."""
    path = Path(path)
    if not path.exists():
        return None
    stamp_text = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = path.with_name(f"{path.name}.{stamp_text}.bak")
    counter = 1
    while target.exists():
        counter += 1
        target = path.with_name(f"{path.name}.{stamp_text}-{counter}.bak")
    shutil.copyfile(path, target)
    return target


def counts(data: dict | None) -> tuple[int, int]:
    """(actions, checks) in a saved plan."""
    actions = [a for s in (data or {}).get("steps", []) for a in s.get("actions", []) if isinstance(a, dict)]
    return len(actions), sum(1 for a in actions if str(a.get("action", "")).startswith("verify"))


def compare(old: dict | None, new: dict) -> str:
    """One line for the console when a plan is replaced; warns if the checks dropped sharply."""
    if old is None:
        return ""
    (old_actions, old_checks), (new_actions, new_checks) = counts(old), counts(new)
    text = (f"Old plan: {old_actions} actions, {old_checks} checks  ->  "
            f"new plan: {new_actions} actions, {new_checks} checks.")
    if old_checks >= 2 and new_checks * 2 < old_checks:
        text += "  WARNING: far fewer checks than before - review the new plan."
    return text


def is_unverified(raw: dict) -> bool:
    """An AI plan that has never passed a full run."""
    return bool(raw.get("ai_generated")) and not raw.get("verified_at")


def mark_verified(test_case) -> bool:
    """After a passing run of an AI plan: record it in the file. True if newly marked."""
    path = test_case.generated_file
    if path is None or test_case.status != "PASS" or not Path(path).exists():
        return False
    path = Path(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not saved.get("ai_generated") or saved.get("verified_at"):
        return False
    saved["verified_at"] = datetime.now().isoformat(timespec="seconds")
    save(saved, path)
    test_case.raw["verified_at"] = saved["verified_at"]
    return True


def path_for(test_case) -> Path:
    """Where the generated actions for this test case are kept."""
    if test_case.path.suffix == ".json":
        return test_case.path.with_suffix(".generated.json")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_case.id)
    return config.BASE_DIR / "test_cases" / f"{safe}.generated.json"


def save(result: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_current(path: Path, source: dict) -> dict | None:
    """The saved actions if they exist and still match the current Jira steps, else None."""
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(saved, dict) or not saved.get("ai_generated"):
        return None
    if saved.get("source_hash") != fingerprint(source):
        return None
    if len(saved.get("steps", [])) != len(source.get("steps", [])):
        return None
    return saved


def save_repairs(test_case) -> int:
    """Write the actions of steps that were repaired by the AI and then passed back to the
    generated file (the previous version is kept as .bak). Returns the number of steps saved."""
    path = test_case.generated_file
    repaired = [s for s in test_case.steps if s.repaired and s.status == "PASS"]
    if path is None or not repaired or not Path(path).exists():
        return 0
    path = Path(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    by_sequence = {int(s.get("sequence", i)): s for i, s in enumerate(saved.get("steps", []), start=1)}
    count = 0
    for step in repaired:
        target = by_sequence.get(step.sequence)
        if target is not None:
            target["actions"] = step.actions
            count += 1
    if count:
        backup(path)
        saved["last_repair_at"] = datetime.now().isoformat(timespec="seconds")
        save(saved, path)
    return count
