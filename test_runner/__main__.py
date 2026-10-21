"""
Command line entry point.

    python -m test_runner test_cases\\test_script_IAODTM-C22426_7905796.json
    python -m test_runner test_cases\\                 (every test case in the folder)
    python -m test_runner test_cases\\ --check         (validate only, no browser)
    python -m test_runner suites\\ZGM1_regression.steps.txt   (a .steps.txt without Jira JSON)
    python -m test_runner IAODTM-T106608       (read from Jira: commands in each Step field)
    python -m test_runner IAODTM-C22426        (every test case in a Jira test cycle)
    python -m test_runner regression.txt       (a list file: one key / Jira link / file per line)
    python -m test_runner --jira-project TWC5958 --jira-folder "/Regression/ZGM1"
    python -m test_runner --jira-label automation
    python -m test_runner TWC5958-T1014 --url https://consiaoprice.intel.com/   (another site)
    python -m test_runner --commands            (what testers can write in .steps.txt)
    python -m test_runner --list-actions
"""

import argparse
import json
import sys
from pathlib import Path

from . import config
from .actions import ACTIONS, validate_actions
from .executor import run_test_case
from .jira import build_query, fetch as fetch_from_jira, is_jira_key, key_from_url
from .models import load_test_case, test_case_from_raw
from . import generated
from .prompt import generate_actions
from .report import create_evidence_document, write_results_json
from .simple_steps import CHEAT_SHEET, STEPS_SUFFIX, json_file_for
from .mail import email_run
from .summary import RunSummary, create_run_summary


def _is_list_file(path: Path) -> bool:
    return (path.is_file() and path.suffix.lower() == ".txt"
            and not path.name.lower().endswith(STEPS_SUFFIX))


def _expand_list_files(paths):
    """
    A .txt list file (not a .steps.txt) stands for its lines: one Jira key, Jira link,
    file or folder per line. Lines starting with # are notes.
    """
    expanded = []
    for raw in paths:
        path = Path(raw)
        if not _is_list_file(path):
            expanded.append(raw)
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            # Files are relative to the list file's folder.
            beside = path.parent / entry
            expanded.append(str(beside) if not is_jira_key(entry) and beside.exists() else entry)
    return expanded


def _is_group_run(args, given_paths) -> bool:
    """A folder, label, query, test cycle or list file - not just named test cases."""
    if args.jira_folder or args.jira_label or args.jira_query:
        return True
    for raw in given_paths:
        path = Path(raw)
        key = key_from_url(raw) or raw
        if path.is_dir() or _is_list_file(path):
            return True
        if is_jira_key(key) and key.strip().upper().split("-")[-1].startswith("C"):
            return True
    return False


def _describe_scope(args, given_paths) -> str:
    """What was run, in words, for the summary document."""
    parts = []
    if args.jira_folder:
        parts.append(f"Jira folder {args.jira_folder} (project {args.jira_project.upper()})")
    for label in args.jira_label or []:
        parts.append(f"Jira label '{label}'" + (f" (project {args.jira_project.upper()})"
                                                if args.jira_project and not args.jira_folder else ""))
    for query in args.jira_query or []:
        parts.append(f"Jira search: {query}")
    for raw in given_paths:
        path = Path(raw)
        key = key_from_url(raw) or raw
        if _is_list_file(path):
            parts.append(f"List file {path.name}")
        elif path.is_dir():
            parts.append(f"Folder {raw}")
        elif is_jira_key(key):
            kind = "Test cycle" if key.strip().upper().split("-")[-1].startswith("C") else "Test case"
            parts.append(f"{kind} {key.strip().upper()}")
        else:
            parts.append(f"File {path.name}")
    if args.url:
        parts.append(f"site override {args.url}")
    return "; ".join(parts) or "-"


def _collect_files(paths):
    """JSON test cases, plus .steps.txt files that have no JSON (regression suites)."""
    files = []
    for raw in paths:
        path = Path(raw)
        if is_jira_key(raw) and not path.exists():
            continue  # read from Jira in main()
        if not path.exists():
            print(f"{raw}: not found (no such file or folder, and not a Jira key)")
            continue
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
            files.extend(
                p for p in sorted(path.glob(f"*{STEPS_SUFFIX}"))
                if not json_file_for(p).exists()
            )
        else:
            files.append(path)
    return files


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _needs_ai(test_case) -> bool:
    """Plain-English steps: no tester .steps.txt and no ready-made "actions" in the JSON."""
    return test_case.steps_file is None and not any("actions" in s.raw for s in test_case.steps)


def _reusable_actions(test_case, saved_path: Path, regenerate: bool):
    """The saved plan if the steps, test data and expected results are unchanged since it was
    made. A Jira-key run reuses it only once it has passed a full run (repairs included)."""
    if regenerate:
        return None
    saved = generated.load_current(saved_path, test_case.raw)
    if saved is not None and not test_case.path.suffix and not saved.get("verified_at"):
        return None
    return saved


def _list_actions():
    for name, spec in ACTIONS.items():
        extras = []
        if spec.needs_target:
            extras.append("target")
        extras.extend(spec.required)
        print(f"{name:28} [{', '.join(extras)}]")
        for line in spec.doc.splitlines():
            print(f"{'':30}{line.strip()}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m test_runner",
                                     description="Run Jira test steps in the browser.")
    parser.add_argument("paths", nargs="*",
                        help="Test case JSON files or folders, .steps.txt files, Jira keys "
                             "(test case IAODTM-T106608 or test cycle IAODTM-C22426) or links, "
                             "or a .txt list file with one of these per line")
    parser.add_argument("--jira-project", metavar="KEY",
                        help="Jira project key for --jira-folder / --jira-label, e.g. TWC5958")
    parser.add_argument("--jira-folder", metavar="FOLDER",
                        help='Run every test case in this Zephyr folder, e.g. "/Regression/ZGM1" '
                             "(needs --jira-project)")
    parser.add_argument("--jira-label", metavar="LABEL", action="append",
                        help="Run every test case with this label (repeat for any of several)")
    parser.add_argument("--jira-query", metavar="TQL", action="append",
                        help='Any Zephyr Scale search (TQL), e.g. projectKey = "TWC5958" '
                             'AND status = "Approved" (repeatable)')
    parser.add_argument("--url", metavar="URL",
                        help="Application to test for this run, e.g. https://consiaoprice.intel.com/ "
                             "(instead of the test case's app_url / DEFAULT_APP_URL in config.py)")
    parser.add_argument("--fresh-browser", action="store_true",
                        help="Test in a clean browser (no saved logins) instead of the saved "
                            "selected browser profile (config.USE_SAVED_PROFILE)")
    parser.add_argument("--headless", action="store_true", help="Run without showing the browser")
    parser.add_argument("--check", action="store_true", help="Only validate the JSON files")
    parser.add_argument("--no-ai", action="store_true",
                        help="Don't use the AI to turn plain-English steps into actions")
    parser.add_argument("--no-email", action="store_true", help="Don't send the summary mail after the run")
    parser.add_argument("--email-to", metavar="ADDRESS",
                        help=f"Send the summary mail to this address (default {config.EMAIL_RECIPIENT})")
    parser.add_argument("--email-preview", action="store_true",
                        help="Write the summary mail as an .html file in the output folder; send nothing")
    parser.add_argument("--regenerate", action="store_true",
                        help="Force new AI actions even when the saved plan still matches the steps")
    parser.add_argument("--keep-going", action="store_true",
                        help="After a failed check keep running the rest, so one run lists every failure")
    parser.add_argument("--output", default=str(config.OUTPUT_DIR), help="Output folder")
    parser.add_argument("--list-actions", action="store_true", help="Show available actions")
    parser.add_argument("--commands", action="store_true",
                        help="Show the commands testers can write in a .steps.txt file")
    args = parser.parse_args(argv)
    given_paths = list(args.paths)
    # List files stand for their lines; a pasted Jira link counts as its key.
    args.paths = [key_from_url(p) or p for p in _expand_list_files(args.paths)]

    jira_queries = list(args.jira_query or [])
    if args.jira_folder and not args.jira_project:
        parser.error("--jira-folder needs --jira-project, e.g. --jira-project TWC5958")
    if args.jira_folder or args.jira_label:
        jira_queries.append(build_query(args.jira_project, args.jira_folder, args.jira_label))

    if args.commands:
        print(CHEAT_SHEET)
        return 0

    if args.list_actions:
        _list_actions()
        return 0

    files = _collect_files(args.paths)
    jira_keys = [p for p in args.paths if is_jira_key(p) and not Path(p).exists()]
    if not files and not jira_keys and not jira_queries:
        parser.error("give at least one test case JSON file or folder, a Jira key, a list file, "
                     "or --jira-folder / --jira-label")

    summary = []
    exit_code = 0
    run_summary = RunSummary(_describe_scope(args, given_paths))

    # (label, loader) - files are loaded here, Jira test cases are read up front.
    sources = [(str(file), lambda file=file: load_test_case(file)) for file in files]
    if jira_keys or jira_queries:
        try:
            fetched = fetch_from_jira(jira_keys, jira_queries, headless=args.headless)
        except Exception as e:    # JiraError (e.g. sign-in expired) or browser could not start
            print(f"Jira: {e}")
            # Recorded so the summary mail still goes out - a scheduled run must not fail silently.
            run_summary.add_not_run(run_summary.command, f"Could not read from Jira: {e}")
            fetched = []
            exit_code = 1
        for label, raw, error in fetched:
            if raw is None:
                print(f"{label}: could not read from Jira - {error}")
                run_summary.add_not_run(label, f"Could not read from Jira: {error}")
                exit_code = 1
                continue
            sources.append((f"Jira {label}",
                            lambda raw=raw, label=label: test_case_from_raw(raw, Path(label))))

    for position, (file, loader) in enumerate(sources):
        try:
            test_case = loader()
        except Exception as e:
            print(f"{file}: could not load - {e}")
            run_summary.add_not_run(file, f"Could not load: {e}")
            exit_code = 1
            continue

        if args.url:
            test_case.app_url = args.url

        problems = validate_actions(test_case)
        if problems and not args.no_ai and _needs_ai(test_case):
            saved_path = generated.path_for(test_case)
            result = _reusable_actions(test_case, saved_path, args.regenerate)
            if result is not None:
                print(f"{file}: steps unchanged - reusing saved AI actions ({saved_path.name}, generated "
                      f"{result.get('generated_at', 'earlier')}, passed {result.get('verified_at', 'earlier')})"
                      " - use --regenerate for new ones.")
            else:
                old_plan = _read_json(saved_path)
                if args.regenerate:
                    print(f"{file}: --regenerate - asking the AI for new actions...")
                elif old_plan is None:
                    print(f"{file}: steps are plain English - asking the AI to convert them to actions...")
                elif old_plan.get("source_hash") != generated.fingerprint(test_case.raw):
                    print(f"{file}: the steps / test data / expected results changed since the saved "
                          "actions were made - regenerating.")
                else:
                    print(f"{file}: the saved actions have not passed a run yet - regenerating.")
                result = generate_actions(test_case.raw, test_case.path)
                if result is not None:
                    kept = generated.backup(saved_path)
                    generated.save(result, saved_path)
                    print(f"Generated actions saved: {saved_path}")
                    if kept is not None:
                        print(f"Previous version kept: {kept.name}")
                    note = generated.compare(old_plan, result)
                    if note:
                        print(note)
            if result is not None:
                test_case = test_case_from_raw(result, test_case.path)
                test_case.generated_file = saved_path
                problems = validate_actions(test_case)
        elif test_case.raw.get("ai_generated") and test_case.path.suffix == ".json":
            test_case.generated_file = test_case.path     # a saved AI file run directly: repairs go back into it
        if args.keep_going:
            test_case.stop_on_failure = False
        if generated.is_unverified(test_case.raw) and not problems:
            print(f"{file}: this AI plan has not passed a full run yet - treat a failure as possibly "
                  "the plan's fault, not the application's.")
        if problems:
            print(f"{file}: INVALID")
            for problem in problems:
                print(f"  - {problem}")
            run_summary.add_not_run(test_case.id, "Invalid commands: " + "; ".join(problems))
            exit_code = 1
            continue

        if args.check:
            automated = sum(1 for s in test_case.steps if s.actions)
            print(f"{file}: OK ({automated}/{len(test_case.steps)} steps automated)")
            continue

        try:
            run_dir = run_test_case(test_case, headless=args.headless,
                                    output_root=Path(args.output),
                                    use_saved_profile=False if args.fresh_browser else None)
        except KeyboardInterrupt:
            print()
            print(f"Stopped by user (Ctrl+C) during {test_case.id}. Remaining tests not run.")
            run_summary.add_not_run(test_case.id, "Stopped by user (Ctrl+C) during this test.")
            for later_file, _ in sources[position + 1:]:
                run_summary.add_not_run(later_file, "Not run - the run was stopped by the user.")
            exit_code = 130
            break
        except Exception as e:    # e.g. browser profile in use - record it and go on to the next test
            print(f"{test_case.id}: could not run - {e}")
            run_summary.add_not_run(test_case.id, f"Could not run: {e}")
            exit_code = 1
            continue

        results_path = write_results_json(test_case, run_dir)
        document_path = create_evidence_document(test_case, run_dir)
        run_summary.add_run(test_case, document_path)

        summary.append((test_case.id, test_case.status, run_dir))
        print()
        print(f"Results JSON : {results_path}")
        print(f"Evidence doc : {document_path}")

        if test_case.status == "PASS":
            if generated.mark_verified(test_case):
                print(f"AI plan verified by this passing run: {test_case.generated_file}")
        else:
            exit_code = 1
            if generated.is_unverified(test_case.raw):
                where = f" ({test_case.generated_file})" if test_case.generated_file else ""
                print(f"NOTE: this AI plan had never passed before. Review it{where} - the failure may be "
                      "a wrong plan, not an application defect. Use --regenerate for a fresh plan.")

    # e.g. a Jira folder / label / cycle that matched no test cases: still worth a mail.
    if not args.check and not run_summary.entries:
        print("No test cases were found to run.")
        run_summary.add_not_run(run_summary.command, "No test cases were found to run.")
        exit_code = 1

    if summary:
        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)
        for test_id, status, run_dir in summary:
            print(f"{status:14} {test_id}   {run_dir}")

    # A summary document only for group runs (folder, label, cycle, list file, several tests).
    docx_path = None
    if not args.check and run_summary.entries and (
            _is_group_run(args, given_paths) or len(run_summary.entries) > 1):
        docx_path, _ = create_run_summary(run_summary, Path(args.output))
        print()
        print(f"Run summary  : {docx_path}")

    # Summary mail: only after real runs (never --check). A mail problem never changes the exit code.
    if not args.check and run_summary.entries and not args.no_email and (
            config.EMAIL_AFTER_RUN or args.email_to or args.email_preview):
        print()
        email_run(run_summary, Path(args.output), docx_path, to=args.email_to,
                  preview_only=args.email_preview)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
