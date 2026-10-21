"""
Self-repair for AI-generated test cases: when an interaction action (click, type, select ...)
fails because the model guessed the wrong label, the LLM is shown the real page (visible text,
buttons, links, fields) and proposes a replacement for that one action.

Verification actions (verify_*, db, excel ...) are never repaired - a failed check is a real
result, not a locator problem. Repairs are logged to <run folder>\\ai_repairs.json.
"""

from __future__ import annotations

import json
import re

from . import config
from .actions import ACTIONS
from .locators import has_target

# Only these can be repaired. Everything else fails as written.
REPAIRABLE = {"click", "hover", "menu", "fill", "press", "select", "show_options",
              "check", "uncheck", "wait_for", "switch_tab"}

# A failed check of these is retried unchanged after the AI adds interaction steps in front
# (e.g. expanding a collapsed panel). The check itself is never altered.
PRECONDITION_CHECKS = {"verify_visible", "verify_text"}

_SNAPSHOT_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; };
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const pick = (sel) => [...new Set([...document.querySelectorAll(sel)].filter(vis).map(txt).filter(Boolean))].slice(0, 60);
  const fields = [...document.querySelectorAll('input,select,textarea,p-multiselect,p-dropdown')].filter(vis).map(e => {
      const id = e.id && document.querySelector('label[for="' + e.id + '"]');
      return [id && id.innerText.trim(), e.getAttribute('placeholder'), e.getAttribute('name'), e.getAttribute('aria-label')]
             .filter(Boolean).join(' | ');
  }).filter(Boolean).slice(0, 40);
  return {
    buttons: pick('button,[role=button],input[type=button],input[type=submit]'),
    links: pick('a,[role=link]'),
    tabs_menus: pick('[role=tab],[role=menuitem],.nav-link,.dropdown-item,li'),
    labels: pick('label'),
    fields,
    text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 2500),
  };
}
"""


def page_snapshot(page) -> dict:
    """What a person would see: url, title and the visible controls of the page and its frames."""
    snapshot = {"url": page.url, "title": ""}
    try:
        snapshot["title"] = page.title()
    except Exception:
        pass
    frames = []
    for frame in page.frames:
        try:
            info = frame.evaluate(_SNAPSHOT_JS)
        except Exception:
            continue
        if any(info.values()):
            info["frame_url"] = frame.url
            frames.append(info)
    snapshot["frames"] = frames
    return snapshot


def _prompt(step, params: dict, error: str, snapshot: dict, previous: list[str]) -> str:
    from .prompt import _action_reference
    tried = "\n".join(f"- {p}" for p in previous) or "- (none)"
    if params.get("action") in PRECONDITION_CHECKS:
        task = ("A check failed because the element is not visible yet. Propose the interaction "
                "action(s) to run BEFORE the check so that it becomes visible (for example expand a "
                "collapsed panel or open a tab). The check itself is kept exactly as written and is "
                "re-run afterwards - do not include it, and do not change what is being checked.")
    else:
        task = ("A browser test step failed because an action could not find what it was told to "
                "use. Propose replacement action(s) for ONLY the failed action, using what is "
                "really on the page.")
    return f"""{task}

Reply with ONLY a JSON array of action objects - no explanation, no markdown fences.
Use only these actions: click, hover, menu, fill, press, select, show_options, check, uncheck,
wait_for, switch_tab. Never add verify_* or other checking actions.

## Available actions
{_action_reference()}

## Targets - use exactly one: "text", "role"+"name", "label", "placeholder", "css", "xpath",
"field", "option". Use text exactly as it appears on the page.

## Test step (what a human would do)
{step.step}
Expected: {step.expected_result}

## Failed action
{json.dumps(params, ensure_ascii=False)}
Error: {error}

## Already tried (do not repeat)
{tried}

## What the page shows now
{json.dumps(snapshot, indent=1, ensure_ascii=False)}
"""


def _parse(reply: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise ValueError("not a list of actions")
    for item in data:
        name = item.get("action") if isinstance(item, dict) else None
        if name not in REPAIRABLE:
            raise ValueError(f"action '{name}' is not allowed in a repair")
        spec = ACTIONS[name]
        if any(key not in item for key in spec.required) or (spec.needs_target and not has_target(item)):
            raise ValueError(f"action '{name}' is missing required keys or a target")
    return data


def propose(step, params: dict, error: str, page, previous: list[str]) -> list[dict] | None:
    """Ask the LLM for replacement actions; None if it can't help."""
    from .llm import LLMError, ask
    try:
        reply = ask(_prompt(step, params, error, page_snapshot(page), previous))
        return _parse(reply)
    except (LLMError, ValueError, json.JSONDecodeError) as e:
        print(f"        AI repair: no usable suggestion ({e})", flush=True)
        return None


def is_repairable(ctx, params: dict) -> bool:
    return (config.LLM_REPAIR_ATTEMPTS > 0
            and bool(ctx.test_case.raw.get("ai_generated"))
            and (params.get("action") in REPAIRABLE or params.get("action") in PRECONDITION_CHECKS)
            and not params.get("optional"))


def log_repair(ctx, step, original: dict, replacement: list[dict], error: str) -> None:
    path = ctx.run_dir / "ai_repairs.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        entries = []
    entries.append({"step": step.sequence, "failed_action": original, "error": error,
                    "replacement": replacement})
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
