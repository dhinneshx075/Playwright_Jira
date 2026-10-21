"""
Benchmark for the AI that turns plain-English Jira steps into runner actions.

    python -m test_runner.benchmark                         # the configured deployment
    python -m test_runner.benchmark --repeat 3              # same prompt 3 times: how stable is it?
    python -m test_runner.benchmark --deployment gpt-5.4-mini --deployment gpt-5.4 --effort low --effort high
    python -m test_runner.benchmark --cases T1214,T1197     # only some cases
    python -m test_runner.benchmark --oracle                # sanity check: the golden plan must score 100%

Cases live in benchmark\\cases\\*.json: the Jira steps (the input), plus a "golden" plan written by the
testers (their command-form steps) - or, for "smoke" cases, no golden plan. There is no single correct
plan (menu vs open_report, check order ...), so the plan is not compared action by action. It is scored on:

    checks        the verifications the golden plan has (verify X / not X), by polarity and wording
    do            the interactions: open the report, run it, tick/untick, select, switch tab ...
    order         how much of the golden interaction order is kept (longest common subsequence)
    score         the mean of those three  -  what to compare between models
    precision     how many of the plan's checks are in the golden plan (extra checks are not wrong)

A case the pipeline could not produce at all (invalid after its retry) scores 0.
Every generated plan is saved under benchmark\\results\\<time>\\plans\\ so it can be run with
    python -m test_runner <plan file> --check
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config

CASES_DIR = config.BASE_DIR / "benchmark" / "cases"
RESULTS_DIR = config.BASE_DIR / "benchmark" / "results"


# ----------------------------------------------------------------------------- normalising

def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _report_name(value) -> str:
    return re.sub(r"report$", "", _norm(value))


def _field(action: dict) -> str:
    """The box/dropdown a select or fill is about - the model may name it field, label, placeholder ..."""
    for key in ("field", "label", "placeholder", "name", "text"):
        if action.get(key):
            return str(action[key])
    return ""


def _value(action: dict) -> str:
    for key in ("text", "option", "column", "field", "label", "name", "placeholder"):
        if action.get(key):
            return str(action[key])
    return ""


def summarise(actions: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """(checks, steps): checks are ("+"/"-", wording); steps are the interactions in order."""
    checks, steps = [], []
    for a in actions:
        name = a.get("action", "")
        if name in ("verify_text", "verify_visible"):
            checks.append(("+", _norm(_value(a))))
        elif name in ("verify_not_text", "verify_not_visible"):
            checks.append(("-", _norm(_value(a))))
        elif name == "verify_column_header":
            checks.append(("-" if a.get("absent") else "+", _norm(a.get("column"))))
        elif name == "verify_column_contains":
            checks.append(("+", _norm(a.get("column")) + _norm(a.get("text"))))
        elif name == "verify_column_not_contains":
            checks.append(("-", _norm(a.get("column")) + _norm(a.get("text"))))
        elif name in ("open_report", "menu"):
            target = a["path"][-1] if a.get("path") else a.get("name", "")
            steps.append(("open", _report_name(target)))
        elif name == "goto":
            if a.get("url"):
                steps.append(("goto", _norm(a["url"])))
        elif name == "run_report":
            steps.append(("run", ""))
        elif name == "switch_tab":
            steps.append(("tab", ""))
        elif name == "click":
            text = _norm(a.get("text") or _value(a))
            steps.append(("panel", "") if text == "reportcolumns" else ("click", text))
        elif name in ("check", "uncheck"):
            steps.append((name, _norm(a.get("option") or _value(a))))
        elif name in ("select", "fill"):
            steps.append((name, _norm(_field(a)) + _norm(a.get("value"))))
        elif name == "upload":
            steps.append(("upload", _norm(a.get("file"))))
        # screenshot / wait / waits for page load are not scored
    return checks, steps


def _same_wording(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 4 and shorter in longer


def _match_checks(golden: list[tuple], plan: list[tuple]) -> int:
    used, matched = set(), 0
    for polarity, wording in golden:
        for index, (p_polarity, p_wording) in enumerate(plan):
            if index not in used and polarity == p_polarity and _same_wording(wording, p_wording):
                used.add(index)
                matched += 1
                break
    return matched


def _same_step(g: tuple, p: tuple) -> bool:
    # "LineOfBusiness" vs "LineOfBusinessName": the Jira text itself uses both, so close wording counts
    loose = g[0] in ("click", "open", "check", "uncheck")
    return g[0] == p[0] and (g[1] == p[1] or (loose and _same_wording(g[1], p[1])))


def _lcs(golden: list[tuple], plan: list[tuple]) -> int:
    rows = [[0] * (len(plan) + 1) for _ in range(len(golden) + 1)]
    for i, g in enumerate(golden, start=1):
        for j, p in enumerate(plan, start=1):
            rows[i][j] = rows[i - 1][j - 1] + 1 if _same_step(g, p) else max(rows[i - 1][j], rows[i][j - 1])
    return rows[-1][-1]


def _ratio(part: int, whole: int) -> float:
    return 1.0 if whole == 0 else part / whole


def score(golden: list[dict], plan: list[dict]) -> dict:
    """How close `plan` is to `golden` (0..1 each) - see the module docstring."""
    g_checks, g_steps = summarise(golden)
    p_checks, p_steps = summarise(plan)
    matched_checks = _match_checks(g_checks, p_checks)
    order = _lcs(g_steps, p_steps)
    # "do" counts each golden interaction that the plan also has, in any order
    remaining, found = list(p_steps), 0
    for g in g_steps:
        for index, p in enumerate(remaining):
            if _same_step(g, p):
                found += 1
                del remaining[index]
                break
    result = {
        "checks": _ratio(matched_checks, len(g_checks)),
        "do": _ratio(found, len(g_steps)),
        "order": _ratio(order, len(g_steps)),
        "precision": _ratio(matched_checks, len(p_checks)) if p_checks else (1.0 if not g_checks else 0.0),
        "golden_checks": len(g_checks), "plan_checks": len(p_checks),
        "golden_steps": len(g_steps), "plan_steps": len(p_steps),
    }
    result["score"] = statistics.mean((result["checks"], result["do"], result["order"]))
    return result


def smoke(raw_steps: list[dict], plan: list[dict]) -> dict:
    """For cases with no golden plan: obvious expectations only."""
    text = " ".join(f"{s.get('step', '')} {s.get('expected_result', '')}" for s in raw_steps).lower()
    names = [a.get("action") for a in plan]
    wants_run = "run report" in text
    wants_check = bool(re.search(r"\b(verify|confirm|ensure|should)\b", text))
    result = {
        "runs_report": (not wants_run) or "run_report" in names,
        "has_checks": (not wants_check) or any(str(n).startswith("verify") for n in names),
        "plan_checks": sum(1 for n in names if str(n).startswith("verify")),
    }
    result["score"] = statistics.mean((float(result["runs_report"]), float(result["has_checks"])))
    return result


# ----------------------------------------------------------------------------- running

def load_cases(wanted: list[str] | None = None) -> list[dict]:
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]
    if wanted:
        cases = [c for c in cases if any(w.lower() in c["id"].lower() for w in wanted)]
    return cases


def run_case(case: dict, oracle: bool = False) -> dict:
    """Generate a plan for one case through the real pipeline; return the plan and its score."""
    from . import llm
    from .prompt import generate_actions

    calls, started = [], time.time()
    real_ask = llm.ask

    def timed_ask(prompt_text):
        began = time.time()
        try:
            return real_ask(prompt_text)
        finally:
            calls.append(round(time.time() - began, 1))

    def oracle_ask(prompt_text):
        calls.append(0.0)
        plan = json.loads(json.dumps(case["raw"]))
        plan["steps"][0]["actions"] = case["golden"]
        for extra in plan["steps"][1:]:
            extra["actions"] = []
        return json.dumps(plan)

    llm.ask = oracle_ask if oracle else timed_ask
    try:
        result = generate_actions(case["raw"], Path(case["id"] + ".json"))
    except Exception as e:                       # a crashed call counts as a failed generation
        print(f"      error: {e}")
        result = None
    finally:
        llm.ask = real_ask

    plan = [a for s in result["steps"] for a in s["actions"]] if result else []
    row = {"case": case["id"], "kind": "scored" if case.get("golden") else "smoke", "generated": result is not None, "attempts": len(calls),
           "seconds": round(time.time() - started, 1), "plan": plan, "result": result}
    if case.get("golden"):
        row.update(score(case["golden"], plan) if result else {"score": 0.0, "checks": 0.0, "do": 0.0, "order": 0.0})
    else:
        row.update(smoke(case["raw"]["steps"], plan) if result else {"score": 0.0})
    return row


def run_matrix(cases, deployments, efforts, repeat, oracle, out_dir: Path) -> list[dict]:
    rows = []
    plans_dir = out_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for deployment in deployments:
        for effort in efforts:
            config.LLM_DEPLOYMENT = deployment or config.LLM_DEPLOYMENT
            config.LLM_REASONING_EFFORT = effort if effort is not None else config.LLM_REASONING_EFFORT
            label = ("oracle" if oracle
                     else f"{config.LLM_DEPLOYMENT}/{config.LLM_REASONING_EFFORT or 'default'}")
            for run in range(1, repeat + 1):
                for case in cases:
                    print(f"[{label} run {run}/{repeat}] {case['id']} ...", flush=True)
                    row = run_case(case, oracle)
                    row.update(config=label, run=run)
                    print(f"      {'OK' if row['generated'] else 'NO PLAN'}  score {row['score']:.0%}  "
                          f"({row['attempts']} call(s), {row['seconds']}s)", flush=True)
                    if row["result"]:
                        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{case['id']}__{label}__run{run}") + ".json"
                        (plans_dir / name).write_text(json.dumps(row["result"], indent=1, ensure_ascii=False),
                                                      encoding="utf-8")
                    rows.append(row)
    return rows


# ----------------------------------------------------------------------------- report

def _pct(values) -> str:
    return f"{statistics.mean(values):.0%}" if values else "-"


def report(rows: list[dict], cases: list[dict]) -> str:
    configs = list(dict.fromkeys(r["config"] for r in rows))
    lines = []
    for cfg in configs:
        mine = [r for r in rows if r["config"] == cfg]
        scored = [r for r in mine if r["kind"] == "scored"]
        smoked = [r for r in mine if r["kind"] == "smoke"]
        by_case = {}
        for r in mine:
            by_case.setdefault(r["case"], []).append(r)
        lines += [f"## {cfg}", "",
                  f"- plans produced: {sum(r['generated'] for r in mine)}/{len(mine)}",
                  f"- scored cases - mean score {_pct([r['score'] for r in scored])} "
                  f"(checks {_pct([r['checks'] for r in scored])}, do {_pct([r['do'] for r in scored])}, "
                  f"order {_pct([r['order'] for r in scored])}, precision {_pct([r['precision'] for r in scored if 'precision' in r])})",
                  f"- smoke cases (no golden plan) - mean {_pct([r['score'] for r in smoked])}",
                  f"- calls per plan: {statistics.mean(r['attempts'] for r in mine):.2f}  "
                  f"(1.00 = always accepted first time)",
                  f"- seconds per plan: {statistics.mean(r['seconds'] for r in mine):.0f}", "",
                  "| case | origin | runs | score (mean) | min-max | checks | do | order | calls |",
                  "|---|---|---|---|---|---|---|---|---|"]
        origin = {c["id"]: c["origin"].split(" ")[0] for c in cases}
        for case_id, runs in by_case.items():
            scores = [r["score"] for r in runs]
            has = runs[0]["kind"] == "scored"
            lines.append(
                f"| {case_id} | {origin.get(case_id, '')} | {len(runs)} | {statistics.mean(scores):.0%} | "
                f"{min(scores):.0%}-{max(scores):.0%} | "
                f"{_pct([r['checks'] for r in runs]) if has else '-'} | {_pct([r['do'] for r in runs]) if has else '-'} | "
                f"{_pct([r['order'] for r in runs]) if has else '-'} | "
                f"{statistics.mean(r['attempts'] for r in runs):.1f} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m test_runner.benchmark",
                                     description="Score the AI's plain-English -> actions conversion.")
    parser.add_argument("--cases", help="Comma separated parts of case ids, e.g. T1214,T1197")
    parser.add_argument("--deployment", action="append", help="Deployment to test (repeatable)")
    parser.add_argument("--effort", action="append", help='Reasoning effort: low / medium / high / "" (repeatable)')
    parser.add_argument("--repeat", type=int, default=1, help="Run each case this many times (stability)")
    parser.add_argument("--oracle", action="store_true", help="Answer with the golden plan (checks the scoring)")
    parser.add_argument("--label", default="", help="Added to the results folder name")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases.split(",") if args.cases else None)
    if args.oracle:
        cases = [c for c in cases if c.get("golden")]      # smoke cases have no plan to replay
    if not cases:
        print(f"No benchmark cases found in {CASES_DIR}")
        return 1
    deployments = args.deployment or [config.LLM_DEPLOYMENT]
    efforts = args.effort if args.effort is not None else [config.LLM_REASONING_EFFORT]
    calls = len(cases) * len(deployments) * len(efforts) * args.repeat
    print(f"{len(cases)} case(s) x {len(deployments)} deployment(s) x {len(efforts)} effort(s) x "
          f"{args.repeat} run(s) = about {calls} model call(s)" + (" (oracle: no model calls)" if args.oracle else ""))

    out_dir = RESULTS_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{args.label}" if args.label else ""))
    rows = run_matrix(cases, deployments, efforts, args.repeat, args.oracle, out_dir)

    text = report(rows, cases)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    slim = [{k: v for k, v in r.items() if k != "result"} for r in rows]
    (out_dir / "results.json").write_text(json.dumps(slim, indent=1, ensure_ascii=False), encoding="utf-8")
    print()
    print(text)
    print(f"Saved: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
