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

The runner expects issue files with a `Status` field. Runnable issues use the
`ready-for-agent` status, and completed dependencies use `complete`,
`completed`, or `done`.

## License

This script is derived from YapCap and is distributed under the same
[MPL-2.0 license](https://github.com/TopiCsarno/yapcap/blob/dev/LICENSE).
