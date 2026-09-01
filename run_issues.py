#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import termios
import time
import tty
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


COMPLETED_STATUSES = {"complete", "completed", "done"}
RUNNABLE_STATUS = "ready-for-agent"
DEFAULT_EFFORT = "default"
DEFAULT_MODEL = "OpenCode default"
COMPLETION_INSTRUCTION = (
    "Implement the issue and keep its Status field unchanged while working; do not mark "
    "it completed. Before exiting, update its Markdown file so every checklist item is "
    "checked (`- [x]`). The issue runner marks it completed after this process exits "
    "successfully. Do not finish while any checkbox remains unchecked."
)
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
RESET = "\033[0m"
CYAN = "\033[36m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"


@dataclass
class Issue:
    number: int
    path: Path
    relative_path: str
    status: str
    dependency_refs: list[str]
    dependencies: list["Issue"] = field(default_factory=list)
    name: str = ""


@dataclass
class OpenCodeSettings:
    model: str
    effort: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run ready issue files through OpenCode in dependency order."
    )
    parser.add_argument(
        "issues_dir",
        nargs="?",
        default="issues",
        help="directory containing issue Markdown files (default: issues)",
    )
    parser.add_argument(
        "--opencode",
        default="opencode",
        help="OpenCode executable (default: opencode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the preflight details without running issues",
    )
    return parser.parse_args()


def read_issue(path: Path, root: Path, number: int) -> Issue:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    frontmatter_status = frontmatter.get("status")
    status = (
        frontmatter_status
        if isinstance(frontmatter_status, str) and frontmatter_status
        else find_field(body, "Status") or "ready-for-agent"
    )
    frontmatter_dependencies = frontmatter.get("blocked_by", [])
    dependency_values = (
        frontmatter_dependencies if isinstance(frontmatter_dependencies, list) else []
    )
    if not dependency_values:
        dependency_values = parse_body_dependencies(find_field(body, "Blocked by"))
    return Issue(
        number=number,
        path=path,
        relative_path=path.relative_to(root).as_posix(),
        status=normalise_status(status),
        dependency_refs=dependency_values,
        name=issue_name(body, path),
    )


def split_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    fields: dict[str, str | list[str]] = {}
    collecting: str | None = None
    for line in lines[1:end]:
        match = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if match:
            key, value = match.groups()
            collecting = None
            if key == "blocked_by":
                values = parse_inline_list(value)
                fields[key] = values
                collecting = key if not value else None
            else:
                fields[key] = value.strip("'\"")
            continue
        if collecting == "blocked_by":
            item = re.match(r"^\s*-\s+(.+?)\s*$", line)
            if item:
                current = fields.setdefault("blocked_by", [])
                if isinstance(current, list):
                    current.append(item.group(1).strip("'\""))
    return fields, "".join(lines[end + 1 :])


def parse_inline_list(value: str) -> list[str]:
    value = value.strip()
    if (
        not value
        or value == "[]"
        or re.match(r"^none(?:\s|\(|$)", value, re.IGNORECASE)
    ):
        return []
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip().strip("'\"") for item in value.split(",") if item.strip()]


def find_field(text: str, name: str) -> str | None:
    pattern = re.compile(
        rf"^[ \t]*(?:\*\*)?{re.escape(name)}:\*{{0,2}}[ \t]*(.*?)[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def parse_body_dependencies(value: str | None) -> list[str]:
    if not value or re.match(r"^none(?:\s|\(|$)", value, re.IGNORECASE):
        return []
    references = [dependency_ref(part) for part in value.split(";")]
    return [reference for reference in references if reference]


def dependency_ref(value: str) -> str:
    value = value.strip().strip("`*_")
    match = re.match(r"^(\d+)(?:\s*[:.)-]|\s|$)", value)
    return match.group(1) if match else value


def normalise_status(value: str) -> str:
    return value.strip().lower().rstrip(".")


def issue_name(body: str, path: Path) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    name = re.sub(r"^#{1,6}\s+", "", first_line)
    name = re.sub(r"^\d+\s*[:.)-]\s*", "", name)
    return name or path.stem


def display_name(issue: Issue) -> str:
    return issue.name or issue.relative_path


def load_issues(issues_dir: Path) -> list[Issue]:
    root = issues_dir.resolve()
    paths = sorted(
        path
        for path in root.rglob("*.md")
        if path.name not in {"README.md", "PRD.md"} and path.is_file()
    )
    return [
        read_issue(path, root, number) for number, path in enumerate(paths, start=1)
    ]


def aliases_for(issue: Issue) -> set[str]:
    aliases = {
        issue.relative_path,
        Path(issue.relative_path).name,
        Path(issue.relative_path).stem,
    }
    stem_match = re.match(r"^(\d+)", Path(issue.relative_path).stem)
    if stem_match:
        aliases.add(str(int(stem_match.group(1))))
    aliases.add(str(issue.number))
    return aliases


def connect_dependencies(issues: list[Issue]) -> None:
    aliases: dict[str, list[Issue]] = {}
    for issue in issues:
        for alias in aliases_for(issue):
            aliases.setdefault(alias, []).append(issue)
    for issue in issues:
        for reference in issue.dependency_refs:
            matches = aliases.get(reference)
            if not matches and reference.isdigit():
                matches = aliases.get(str(int(reference)))
            if not matches:
                raise ValueError(
                    f"issue {issue.number} has unknown dependency: {reference}"
                )
            if len(matches) != 1:
                paths = ", ".join(item.relative_path for item in matches)
                raise ValueError(
                    f"issue {issue.number} has ambiguous dependency {reference}: {paths}"
                )
            if matches[0] is issue:
                raise ValueError(f"issue {issue.number} depends on itself")
            issue.dependencies.append(matches[0])
    detect_cycles(issues)


def detect_cycles(issues: list[Issue]) -> None:
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(issue: Issue) -> None:
        if issue.number in visiting:
            raise ValueError(f"dependency cycle includes issue {issue.number}")
        if issue.number in visited:
            return
        visiting.add(issue.number)
        for dependency in issue.dependencies:
            visit(dependency)
        visiting.remove(issue.number)
        visited.add(issue.number)

    for issue in issues:
        visit(issue)


def print_startup(issues: list[Issue]) -> None:
    print(paint("Issues\n", BOLD + CYAN))
    for issue in issues:
        print(
            f"  {paint(str(issue.number) + '.', BLUE)} "
            f"{paint(display_name(issue), DIM)} "
            f"[{status_color(issue.status)}]"
        )
    print(
        paint(
            "Dependency graph\n",
            BOLD + CYAN,
        )
    )
    print_dependency_tree(issues)
    print()


def print_dependency_tree(issues: list[Issue]) -> None:
    depended_on = {
        dependency.number for issue in issues for dependency in issue.dependencies
    }
    roots = [issue for issue in issues if issue.number not in depended_on]
    if not roots:
        roots = issues
    rendered: set[int] = set()
    for issue in roots:
        if issue.number in rendered:
            continue
        rendered.add(issue.number)
        print(f"  {dependency_label(issue)}")
        print_dependency_branches(issue.dependencies, "  ", rendered)


def print_dependency_branches(
    dependencies: list[Issue], prefix: str, rendered: set[int]
) -> None:
    unique_dependencies = {dependency.number: dependency for dependency in dependencies}
    dependencies = list(unique_dependencies.values())
    for index, dependency in enumerate(dependencies):
        last = index == len(dependencies) - 1
        branch = "`-- " if last else "|-- "
        if dependency.number in rendered:
            print(
                f"{prefix}{branch}"
                f"{paint(display_name(dependency), DIM)} "
                f"{paint('(shared prerequisite; shown above)', DIM)}"
            )
            continue
        rendered.add(dependency.number)
        print(f"{prefix}{branch}{dependency_label(dependency)}")
        child_prefix = prefix + ("    " if last else "|   ")
        print_dependency_branches(dependency.dependencies, child_prefix, rendered)


def dependency_label(issue: Issue) -> str:
    return (
        f"{paint(str(issue.number), BLUE)} "
        f"{paint(display_name(issue), DIM)} "
        f"[{status_color(issue.status)}]"
    )


def print_opencode_settings(settings: OpenCodeSettings, title: str = "OpenCode") -> None:
    print(paint(title, BOLD + CYAN))
    print(f"  {paint('model'.ljust(20), DIM)}{paint(settings.model, GREEN)}")
    print(
        f"  {paint('effort / variant'.ljust(20), DIM)}{paint(settings.effort, YELLOW)}"
    )
    print()


def configured_model_catalog(
    config: dict[str, object], current_model: str
) -> tuple[list[str], dict[str, list[str]]]:
    providers = config.get("provider")
    catalog: dict[str, list[str]] = {}
    if isinstance(providers, dict):
        for family, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            models = provider.get("models")
            if not isinstance(models, dict):
                continue
            for model, details in models.items():
                full_model = f"{family}/{model}"
                variants = details.get("variants") if isinstance(details, dict) else None
                catalog[full_model] = list(variants) if isinstance(variants, dict) else []
    families = sorted({model.split("/", 1)[0] for model in catalog})
    current_family = model_family(current_model)
    if current_family in families:
        families.remove(current_family)
        families.insert(0, current_family)
    if not families:
        families = [DEFAULT_MODEL]
    return families, catalog


def models_for_family(
    family: str, catalog: dict[str, list[str]], current_model: str
) -> list[str]:
    if family == DEFAULT_MODEL:
        return [DEFAULT_MODEL]
    models = [model for model in catalog if model.startswith(f"{family}/")]
    if current_model in models:
        models.remove(current_model)
        models.insert(0, current_model)
    return models or [DEFAULT_MODEL]


def model_family(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else DEFAULT_MODEL


def effort_options(
    model: str, catalog: dict[str, list[str]], current_effort: str
) -> list[str]:
    if model == DEFAULT_MODEL or model not in catalog:
        return [DEFAULT_EFFORT]
    return catalog[model] or [DEFAULT_EFFORT]


def select_option(
    title: str,
    options: list[str],
    current: str | None = None,
    labels: dict[str, str] | None = None,
) -> str | None:
    if not options:
        return None
    selected = options.index(current) if current in options else 0
    labels = labels or {}
    print(paint(title, BOLD + CYAN))
    if not sys.stdin.isatty():
        choice = options[selected]
        print(f"  selected: {labels.get(choice, choice)}")
        return choice
    print("  Use Up/Down and Enter to select, or q to cancel.")
    rendered_lines = 0
    terminal = sys.stdin.fileno()
    terminal_settings = termios.tcgetattr(terminal)
    try:
        tty.setcbreak(terminal)
        while True:
            if rendered_lines:
                sys.stdout.write(f"\033[{rendered_lines}F")
            for index, option in enumerate(options):
                marker = ">" if index == selected else " "
                sys.stdout.write(
                    f"\033[2K\r  [{marker}] {labels.get(option, option)}\n"
                )
            sys.stdout.flush()
            rendered_lines = len(options)
            key = sys.stdin.read(1)
            if key in {"\r", "\n"}:
                print()
                return options[selected]
            if key in {"q", "Q", "\x03"}:
                print()
                return None
            if key in {"k", "K", "\x1b[A"}:
                selected = (selected - 1) % len(options)
            elif key in {"j", "J", "\x1b[B"}:
                selected = (selected + 1) % len(options)
            elif key == "\x1b":
                sequence = sys.stdin.read(2)
                if sequence == "[A":
                    selected = (selected - 1) % len(options)
                elif sequence == "[B":
                    selected = (selected + 1) % len(options)
    finally:
        termios.tcsetattr(terminal, termios.TCSADRAIN, terminal_settings)


def confirm_run() -> bool:
    print(paint("Confirmation", BOLD + CYAN))
    try:
        response = input("Press Enter to run the issues, or type 'q' to cancel: ")
    except EOFError:
        print()
        print("issue run cancelled")
        return False
    if response.strip().lower() in {"q", "quit", "n", "no"}:
        print("issue run cancelled")
        return False
    return True


def paint(value: str, code: str) -> str:
    return f"{code}{value}{RESET}" if USE_COLOR else value


def status_color(status: str) -> str:
    if status in COMPLETED_STATUSES:
        return paint(status, GREEN)
    if status == RUNNABLE_STATUS:
        return paint(status, BLUE)
    if status == "ready-for-human":
        return paint(status, MAGENTA)
    return paint(status, YELLOW)


def print_issue_event(
    action: str, issue: Issue, timestamp: datetime, elapsed: float | None = None
) -> None:
    colors = {"started": BLUE, "finished": GREEN, "failed": RED}
    suffix = f" ({paint(format_duration(elapsed), DIM)})" if elapsed is not None else ""
    print(
        f"{paint('●', colors[action])} "
        f"{paint(f'issue {action}', colors[action] + BOLD)} "
        f"{paint(str(issue.number), BOLD)} "
        f"{paint('at', DIM)} {format_timestamp(timestamp)}{suffix}",
        flush=True,
    )


def format_timestamp(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s elapsed"
    total_seconds = max(0, int(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02} elapsed"


def print_issue_failure(
    issue: Issue,
    timestamp: datetime,
    elapsed: float,
    return_code: int,
    detail: str | None = None,
) -> None:
    print_issue_event("failed", issue, timestamp, elapsed)
    if not detail:
        detail = f"OpenCode exited with status {return_code}"
    print("  failure details:")
    print("\n".join(f"    {line}" for line in detail.splitlines()), flush=True)


def read_failure_output(output: BinaryIO) -> str:
    output.seek(0)
    detail = output.read().decode("utf-8", errors="replace").strip()
    if len(detail) <= 4000:
        return detail
    return "... output truncated ...\n" + detail[-4000:]


def load_opencode_settings(opencode: str) -> OpenCodeSettings:
    settings, _ = load_opencode_configuration(opencode)
    return settings


def load_opencode_configuration(
    opencode: str,
) -> tuple[OpenCodeSettings, dict[str, object]]:
    try:
        result = subprocess.run(
            [opencode, "debug", "config"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return OpenCodeSettings("unavailable", "unavailable"), {}
    config = parse_json_output(result.stdout)
    if not config:
        return OpenCodeSettings("OpenCode default", "OpenCode default"), {}
    return configured_settings(config), config


def parse_json_output(output: str) -> dict[str, object]:
    start = output.find("{")
    if start == -1:
        return {}
    try:
        value = json.JSONDecoder().raw_decode(output[start:])[0]
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def configured_settings(config: dict[str, object]) -> OpenCodeSettings:
    command = nested_dict(config, "command", "implement")
    agent_name = (
        string_value(command, "agent")
        or string_value(config, "default_agent")
        or "build"
    )
    agent = nested_dict(config, "agent", agent_name)
    model = (
        string_value(command, "model")
        or string_value(agent, "model")
        or string_value(config, "model")
    )
    effort = string_value(command, "variant") or string_value(agent, "variant")
    if not model:
        model = "OpenCode default"
    else:
        model, model_effort = split_model_variant(model)
        effort = effort or model_effort
    if not effort:
        effort = (
            reasoning_effort(command) or reasoning_effort(agent) or "OpenCode default"
        )
    return OpenCodeSettings(model, effort)


def nested_dict(value: dict[str, object], *keys: str) -> dict[str, object]:
    current: object = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def string_value(value: dict[str, object], key: str) -> str | None:
    item = value.get(key)
    return item.strip() if isinstance(item, str) and item.strip() else None


def reasoning_effort(value: dict[str, object]) -> str | None:
    options = value.get("options")
    if not isinstance(options, dict):
        options = value.get("settings")
    if not isinstance(options, dict):
        return None
    return string_value(options, "reasoningEffort") or string_value(
        options, "reasoning_effort"
    )


def split_model_variant(model: str) -> tuple[str, str | None]:
    if "#" not in model:
        return model, None
    model_name, effort = model.rsplit("#", 1)
    return model_name, effort or None


def mark_completed(issue: Issue) -> None:
    text = issue.path.read_text(encoding="utf-8")
    if has_unchecked_checklist(text):
        raise ValueError(f"issue {issue.number} has unchecked checklist items")
    updated = replace_status(text)
    if updated == text:
        raise ValueError(f"issue {issue.number} has no status field")
    mode = issue.path.stat().st_mode
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=issue.path.parent, delete=False
        ) as temporary:
            temporary.write(updated)
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, issue.path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    issue.status = "completed"


def has_unchecked_checklist(text: str) -> bool:
    return re.search(r"^[ \t]*[-*+]\s+\[[ \t]*\]", text, re.MULTILINE) is not None


def replace_status(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            for index in range(1, end):
                if re.match(r"^\s*status\s*:", lines[index], re.IGNORECASE):
                    lines[index] = re.sub(
                        r"(^[ \t]*status[ \t]*:[ \t]*).*?([ \t]*\r?\n?)$",
                        r"\1completed\2",
                        lines[index],
                        flags=re.IGNORECASE,
                    )
                    return "".join(lines)
    pattern = re.compile(
        r"^([ \t]*(?:\*\*)?Status:\*{0,2}[ \t]*).*?([ \t]*\r?\n?)$",
        re.IGNORECASE | re.MULTILINE,
    )
    return pattern.sub(r"\1completed\2", text, count=1)


def can_start(issue: Issue) -> bool:
    return issue.status == RUNNABLE_STATUS and all(
        dependency.status in COMPLETED_STATUSES for dependency in issue.dependencies
    )


def run_issues(
    issues: list[Issue],
    opencode: str,
    model: str | None = None,
    effort: str | None = None,
) -> int:
    running: dict[subprocess.Popen[bytes], tuple[Issue, datetime, float, BinaryIO]] = {}
    failed: set[int] = set()

    try:
        while True:
            for issue in issues:
                if issue.number in failed or issue.status in COMPLETED_STATUSES:
                    continue
                if all(
                    running_issue is not issue
                    for running_issue, _, _, _ in running.values()
                ) and can_start(issue):
                    started_at = datetime.now().astimezone()
                    print_issue_event("started", issue, started_at)
                    output = tempfile.TemporaryFile()
                    command = [
                        opencode,
                        "run",
                        "--command",
                        "implement",
                    ]
                    if model:
                        command.extend(["--model", model])
                    if effort and effort != DEFAULT_EFFORT:
                        command.extend(["--variant", effort])
                    command.extend([str(issue.path), COMPLETION_INSTRUCTION])
                    try:
                        process = subprocess.Popen(
                            command,
                            stdout=output,
                            stderr=subprocess.STDOUT,
                        )
                    except OSError as error:
                        output.close()
                        failed.add(issue.number)
                        print_issue_failure(
                            issue, datetime.now().astimezone(), 0, 0, str(error)
                        )
                    else:
                        running[process] = (
                            issue,
                            started_at,
                            time.monotonic(),
                            output,
                        )

            if not running:
                unfinished = [
                    issue
                    for issue in issues
                    if issue.status not in COMPLETED_STATUSES
                    and issue.number not in failed
                ]
                if not unfinished:
                    return 1 if failed else 0
                for issue in unfinished:
                    if issue.status != RUNNABLE_STATUS:
                        print(f"issue waiting: {issue.number} [{issue.status}]")
                    else:
                        print(f"issue blocked: {issue.number}")
                return 1 if failed else 0

            time.sleep(0.2)
            for process, (issue, _, started_clock, output) in list(running.items()):
                return_code = process.poll()
                if return_code is None:
                    continue
                del running[process]
                finished_at = datetime.now().astimezone()
                elapsed = time.monotonic() - started_clock
                if return_code != 0:
                    failed.add(issue.number)
                    print_issue_failure(
                        issue,
                        finished_at,
                        elapsed,
                        return_code,
                        read_failure_output(output),
                    )
                    output.close()
                    continue
                try:
                    mark_completed(issue)
                except (OSError, ValueError) as error:
                    failed.add(issue.number)
                    print_issue_failure(
                        issue,
                        datetime.now().astimezone(),
                        elapsed,
                        0,
                        str(error),
                    )
                    output.close()
                    continue
                output.close()
                print_issue_event("finished", issue, finished_at, elapsed)
    except KeyboardInterrupt:
        for process, (_, _, _, output) in running.items():
            process.terminate()
            output.close()
        raise


def main() -> int:
    arguments = parse_args()
    issues_dir = Path(arguments.issues_dir)
    if not issues_dir.is_dir():
        print(f"issues directory not found: {issues_dir}", file=sys.stderr)
        return 2
    try:
        issues = load_issues(issues_dir)
        if not issues:
            print(f"no issue files found in {issues_dir}")
            return 0
        connect_dependencies(issues)
        print_startup(issues)
        settings, config = load_opencode_configuration(arguments.opencode)
        if arguments.dry_run:
            print_opencode_settings(settings)
            return 0
        families, catalog = configured_model_catalog(config, settings.model)
        selected_family = select_option(
            "Select provider family", families, model_family(settings.model)
        )
        if selected_family is None:
            print("issue run cancelled")
            return 0
        models = models_for_family(selected_family, catalog, settings.model)
        selected_model = select_option("Select model", models, settings.model)
        if selected_model is None:
            print("issue run cancelled")
            return 0
        selected_effort = select_option(
            "Select effort",
            effort_options(selected_model, catalog, settings.effort),
            settings.effort,
            {DEFAULT_EFFORT: "default (model default)"},
        )
        if selected_effort is None:
            print("issue run cancelled")
            return 0
        print_opencode_settings(
            OpenCodeSettings(
                selected_model,
                "OpenCode default" if selected_effort == DEFAULT_EFFORT else selected_effort,
            ),
            "OpenCode",
        )
        if not confirm_run():
            return 0
        model_arg = None if selected_model == DEFAULT_MODEL else selected_model
        return run_issues(issues, arguments.opencode, model_arg, selected_effort)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
