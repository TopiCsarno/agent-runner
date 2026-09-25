#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import socket
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
    "Implement the issue. Before exiting, update the issue tracker in its Markdown file: "
    "mark every acceptance-criteria checklist item that is satisfied as checked (`- [x]`) "
    "and set its Status field (or frontmatter `status`) to `completed`. Do not merely "
    "describe this in your final response; edit the issue file. Re-read the file before "
    "exiting and do not finish while any checklist item remains unchecked. The runner "
    "validates the checklist and accepts an already-completed status."
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


@dataclass
class ProcessState:
    issue: Issue
    started_at: datetime
    started_clock: float
    output: BinaryIO
    log_path: Path
    display_buffer: str = ""
    session_id: str | None = None
    stream_closed: bool = False


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
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="run directory for logs and session metadata (default: user cache)",
    )
    parser.add_argument(
        "--follow",
        metavar="ISSUE",
        help="follow a running issue log instead of starting issues",
    )
    parser.add_argument(
        "--attach",
        metavar="ISSUE",
        help="open the OpenCode session for an issue instead of starting issues",
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


def default_run_root() -> Path:
    cache_home = Path(
        os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
    )
    return cache_home / "yapcap-agent-runner"


def create_run_dir(requested: Path | None) -> Path:
    if requested is None:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        requested = default_run_root() / f"run-{timestamp}-{os.getpid()}"
    requested.mkdir(parents=True, exist_ok=True)
    manifest = requested / "manifest.json"
    if manifest.exists():
        raise ValueError(f"run directory already contains a manifest: {requested}")
    return requested.resolve()


def latest_run_dir() -> Path:
    candidates = sorted(
        path
        for path in default_run_root().glob("run-*")
        if path.is_dir() and (path / "manifest.json").is_file()
    )
    if not candidates:
        raise ValueError(f"no runner runs found under {default_run_root()}")
    return candidates[-1]


def write_manifest(run_dir: Path, manifest: dict[str, object]) -> None:
    path = run_dir / "manifest.json"
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_path, path)


def read_manifest(run_dir: Path) -> dict[str, object]:
    path = run_dir / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read runner manifest {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"runner manifest is invalid: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"runner manifest is not an object: {path}")
    return value


def resolve_run_dir(requested: Path | None) -> Path:
    return requested if requested is not None else latest_run_dir()


def issue_record(
    manifest: dict[str, object], issue_ref: str
) -> tuple[str, dict[str, object]]:
    issues = manifest.get("issues")
    if not isinstance(issues, dict):
        raise ValueError("runner manifest has no issue records")
    if issue_ref in issues and isinstance(issues[issue_ref], dict):
        return issue_ref, issues[issue_ref]
    for key, value in issues.items():
        if isinstance(value, dict) and (
            value.get("number") == issue_ref or value.get("relative_path") == issue_ref
        ):
            return key, value
    raise ValueError(f"issue {issue_ref} is not present in runner manifest")


def follow_issue(run_dir: Path, issue_ref: str) -> int:
    manifest = read_manifest(run_dir)
    key, record = issue_record(manifest, issue_ref)
    log_value = record.get("log")
    if not isinstance(log_value, str):
        raise ValueError(f"issue {key} has no log path in runner manifest")
    log_path = Path(log_value)
    if not log_path.is_absolute():
        log_path = run_dir / log_path
    print(f"following issue {key}: {log_path}")
    position = 0
    while True:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as log:
                log.seek(position)
                while line := log.readline():
                    print(line, end="")
                position = log.tell()
        except FileNotFoundError:
            pass
        manifest = read_manifest(run_dir)
        _, record = issue_record(manifest, key)
        if record.get("status") in {"finished", "failed", "aborted"}:
            return 1 if record.get("status") != "finished" else 0
        time.sleep(0.25)


def attach_issue(opencode: str, run_dir: Path, issue_ref: str) -> int:
    manifest = read_manifest(run_dir)
    key, record = issue_record(manifest, issue_ref)
    if record.get("status") != "running":
        raise ValueError(f"issue {key} is not running; use --follow to inspect its log")
    session_id = record.get("session_id")
    server_url = manifest.get("server_url")
    if not isinstance(session_id, str):
        raise ValueError(f"issue {key} does not have a discovered OpenCode session yet")
    if not isinstance(server_url, str):
        raise ValueError("runner manifest has no OpenCode server URL")
    print(f"attaching to issue {key} session {session_id}")
    return subprocess.run(
        [opencode, "attach", server_url, "--session", session_id],
        check=False,
    ).returncode


def start_server(opencode: str) -> tuple[subprocess.Popen[bytes], str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            opencode,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    server_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if process.poll() is not None:
            raise ValueError("OpenCode server exited before becoming ready")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return process, server_url
        except OSError:
            time.sleep(0.05)
    process.terminate()
    process.wait()
    raise ValueError(f"OpenCode server did not become ready: {server_url}")


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def extract_session_id(line: str) -> str | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    for key in ("sessionID", "sessionId", "session_id"):
        session_id = value.get(key)
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def consume_output(
    state: ProcessState,
    chunk: bytes,
    manifest: dict[str, object],
    run_dir: Path,
) -> None:
    state.output.write(chunk)
    state.output.flush()
    state.display_buffer += chunk.decode("utf-8", errors="replace")
    lines = state.display_buffer.splitlines(keepends=True)
    state.display_buffer = ""
    if lines and not lines[-1].endswith(("\n", "\r")):
        state.display_buffer = lines.pop()
    for line in lines:
        process_output_line(state, line, manifest, run_dir)


def process_output_line(
    state: ProcessState,
    line: str,
    manifest: dict[str, object],
    run_dir: Path,
) -> None:
    session_id = extract_session_id(line)
    if not session_id or session_id == state.session_id:
        return
    state.session_id = session_id
    record = manifest["issues"][str(state.issue.number)]
    record["session_id"] = session_id
    write_manifest(run_dir, manifest)
    print(
        f"issue {state.issue.number} session: {session_id}\n"
        f"  attach: opencode attach {manifest['server_url']} "
        f"--session {session_id}",
        flush=True,
    )


def flush_output(
    state: ProcessState,
    manifest: dict[str, object],
    run_dir: Path,
) -> None:
    if state.display_buffer:
        process_output_line(state, state.display_buffer, manifest, run_dir)
        state.display_buffer = ""


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
        current = read_issue(issue.path, issue.path.parent, issue.number)
        if current.status not in COMPLETED_STATUSES:
            raise ValueError(f"issue {issue.number} has no status field")
        issue.status = "completed"
        return
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
    requested_run_dir: Path | None = None,
) -> int:
    run_dir = create_run_dir(requested_run_dir)
    server_process, server_url = start_server(opencode)
    manifest: dict[str, object] = {
        "run_id": run_dir.name,
        "started_at": datetime.now().astimezone().isoformat(),
        "server_url": server_url,
        "issues": {
            str(issue.number): {
                "number": str(issue.number),
                "relative_path": issue.relative_path,
                "name": display_name(issue),
                "status": "pending",
                "session_id": None,
            }
            for issue in issues
        },
    }
    write_manifest(run_dir, manifest)
    print(f"run directory: {run_dir}")
    print(f"OpenCode server: {server_url}")
    running: dict[subprocess.Popen[bytes], ProcessState] = {}
    failed: set[int] = set()
    output_selector = selectors.DefaultSelector()

    try:
        while True:
            for issue in issues:
                if issue.number in failed or issue.status in COMPLETED_STATUSES:
                    continue
                if all(
                    state.issue is not issue for state in running.values()
                ) and can_start(issue):
                    started_at = datetime.now().astimezone()
                    print_issue_event("started", issue, started_at)
                    log_path = run_dir / f"issue-{issue.number}.log"
                    output = log_path.open("w+b")
                    command = [
                        opencode,
                        "run",
                        "--attach",
                        server_url,
                        "--format",
                        "json",
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
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                        )
                    except OSError as error:
                        output.close()
                        failed.add(issue.number)
                        record = manifest["issues"][str(issue.number)]
                        record.update(
                            {
                                "status": "failed",
                                "log": str(log_path),
                                "error": str(error),
                            }
                        )
                        write_manifest(run_dir, manifest)
                        print_issue_failure(
                            issue, datetime.now().astimezone(), 0, 0, str(error)
                        )
                    else:
                        state = ProcessState(
                            issue=issue,
                            started_at=started_at,
                            started_clock=time.monotonic(),
                            output=output,
                            log_path=log_path,
                        )
                        running[process] = state
                        output_selector.register(process.stdout, selectors.EVENT_READ, process)
                        record = manifest["issues"][str(issue.number)]
                        record.update(
                            {
                                "status": "running",
                                "pid": process.pid,
                                "log": str(log_path),
                                "started_at": started_at.isoformat(),
                            }
                        )
                        write_manifest(run_dir, manifest)
                        print(f"  log: {log_path}", flush=True)

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

            for selected, _ in output_selector.select(timeout=0.2):
                process = selected.data
                state = running.get(process)
                if state is None:
                    continue
                chunk = os.read(selected.fileobj.fileno(), 65536)
                if chunk:
                    consume_output(state, chunk, manifest, run_dir)
                else:
                    output_selector.unregister(selected.fileobj)
                    state.stream_closed = True

            for process, state in list(running.items()):
                return_code = process.poll()
                if return_code is None or not state.stream_closed:
                    continue
                del running[process]
                finished_at = datetime.now().astimezone()
                flush_output(state, manifest, run_dir)
                elapsed = time.monotonic() - state.started_clock
                record = manifest["issues"][str(state.issue.number)]
                record.update(
                    {
                        "finished_at": finished_at.isoformat(),
                        "return_code": return_code,
                    }
                )
                if return_code != 0:
                    failed.add(state.issue.number)
                    record["status"] = "failed"
                    write_manifest(run_dir, manifest)
                    state.output.flush()
                    print_issue_failure(
                        state.issue,
                        finished_at,
                        elapsed,
                        return_code,
                        f"OpenCode exited with status {return_code}; log: {state.log_path}",
                    )
                    state.output.close()
                    continue
                try:
                    mark_completed(state.issue)
                except (OSError, ValueError) as error:
                    failed.add(state.issue.number)
                    record["status"] = "failed"
                    record["error"] = str(error)
                    write_manifest(run_dir, manifest)
                    print_issue_failure(
                        state.issue,
                        datetime.now().astimezone(),
                        elapsed,
                        0,
                        str(error),
                    )
                    state.output.close()
                    continue
                record["status"] = "finished"
                write_manifest(run_dir, manifest)
                state.output.close()
                print_issue_event("finished", state.issue, finished_at, elapsed)
    except KeyboardInterrupt:
        for process, state in running.items():
            process.terminate()
            state.output.close()
            record = manifest["issues"][str(state.issue.number)]
            record["status"] = "aborted"
        write_manifest(run_dir, manifest)
        raise
    finally:
        output_selector.close()
        for process, state in running.items():
            if process.poll() is None:
                process.terminate()
            state.output.close()
        stop_server(server_process)


def main() -> int:
    arguments = parse_args()
    if arguments.follow or arguments.attach:
        try:
            run_dir = resolve_run_dir(arguments.run_dir)
            if arguments.follow:
                return follow_issue(run_dir, arguments.follow)
            return attach_issue(arguments.opencode, run_dir, arguments.attach)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
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
        return run_issues(
            issues,
            arguments.opencode,
            model_arg,
            selected_effort,
            arguments.run_dir,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
