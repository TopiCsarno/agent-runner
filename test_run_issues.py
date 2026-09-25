import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import run_issues


class ConsumeOutputTests(unittest.TestCase):
    def make_state(self, output: io.BytesIO) -> run_issues.ProcessState:
        issue = run_issues.Issue(
            number=1,
            path=Path("issue.md"),
            relative_path="issue.md",
            status="ready-for-agent",
            dependency_refs=[],
            name="Example",
        )
        return run_issues.ProcessState(
            issue=issue,
            started_at=datetime.now().astimezone(),
            started_clock=0.0,
            output=output,
            log_path=Path("issue-1.log"),
        )

    def test_model_output_is_logged_without_printing_it(self) -> None:
        output = io.BytesIO()
        state = self.make_state(output)
        manifest = {"server_url": "http://127.0.0.1:1234", "issues": {"1": {}}}
        chunk = (
            json.dumps(
                {"sessionID": "ses_test", "part": {"text": "MODEL_SECRET"}}
            ).encode()
            + b"\n"
        )
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(stdout):
                run_issues.consume_output(state, chunk, manifest, Path(directory))

        self.assertIn(b"MODEL_SECRET", output.getvalue())
        self.assertNotIn("MODEL_SECRET", stdout.getvalue())
        self.assertIn("ses_test", stdout.getvalue())

    def test_final_partial_model_output_is_not_printed(self) -> None:
        state = self.make_state(io.BytesIO())
        manifest = {"server_url": "http://127.0.0.1:1234", "issues": {"1": {}}}
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(stdout):
                run_issues.consume_output(
                    state, b"MODEL_SECRET", manifest, Path(directory)
                )
                run_issues.flush_output(state, manifest, Path(directory))

        self.assertNotIn("MODEL_SECRET", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
