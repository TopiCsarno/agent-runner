# YapCap Agent Runner

Standalone issue runner extracted from [YapCap](https://github.com/TopiCsarno/yapcap).
It runs ready issue Markdown files through OpenCode in dependency order.

## Usage

```sh
./run_issues.py [issues-directory]
```

Use `--dry-run` to inspect the issue graph and OpenCode configuration without
running anything. Use `--opencode PATH` when the OpenCode executable is not on
your `PATH`.

Each run creates a directory containing `manifest.json` and one live log per
issue. The runner prints the run directory, session IDs, and attach commands.
Agent output stays in the issue logs; the main terminal shows runner progress.
While a run is active, observe an issue without affecting it:

```sh
./run_issues.py --follow 1 --run-dir /path/to/run-directory
```

Open the interactive OpenCode session instead with:

```sh
./run_issues.py --attach 1 --run-dir /path/to/run-directory
```

Use the same commands with `2` to switch to a second concurrently running
issue. Following or attaching to one issue does not stop or interact with the
other issue.

The runner expects issue files with a `Status` field. Runnable issues use the
`ready-for-agent` status, and completed dependencies use `complete`,
`completed`, or `done`.

Before an issue process exits successfully, the agent must check every satisfied
acceptance criterion and set the issue status to `completed`. The runner rejects
issues with unchecked checklist items and accepts an already-completed status.

## License

This script is derived from YapCap and is distributed under the same
[MPL-2.0 license](https://github.com/TopiCsarno/yapcap/blob/dev/LICENSE).
