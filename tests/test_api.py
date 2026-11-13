import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from backend.app import create_app
from backend.execution import ProcessResult


class AnalyzeApiTestCase(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ, {"APP_ENV": "testing", "ALLOW_CODE_EXECUTION": "true"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def analyze_python(self, code, **options):
        response = self.client.post(
            "/api/analyze", json={"language": "python", "code": code, **options}
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_health_endpoint_returns_runtime_status(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("python_version", payload)
        self.assertIn("available", payload["compiler"])

    def test_health_reports_execution_policy(self):
        payload = self.client.get("/api/health").get_json()
        self.assertTrue(payload["execution_enabled"])
        self.assertIn("timeout_seconds", payload["limits"])

    def test_invalid_json_request_returns_400(self):
        response = self.client.post(
            "/api/analyze",
            data="not-json",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_request_body_must_be_an_object(self):
        for body in ([], [1], "code", 1, True):
            with self.subTest(body=body):
                response = self.client.post("/api/analyze", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertIsInstance(response.get_json()["error"], dict)

    def test_request_fields_have_strict_types(self):
        invalid_fields = (
            {"code": 123},
            {"language": None},
            {"debug": "false"},
            {"debug": 1},
            {"breakpoints": True},
            {"breakpoints": [True]},
            {"breakpoints": [0]},
            {"breakpoints": [1.5]},
            {"breakpoints": ["1"]},
            {"breakpoints": [3]},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                response = self.client.post(
                    "/api/analyze", json={"code": "value = 1\n", **fields}
                )
                self.assertEqual(response.status_code, 400)

    def test_unsupported_language_returns_400(self):
        response = self.client.post(
            "/api/analyze", json={"code": "value = 1", "language": "javascript"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "unsupported_language")

    def test_code_length_limit_returns_400(self):
        self.app.config["MAX_CODE_LENGTH"] = 4
        response = self.client.post("/api/analyze", json={"code": "value = 1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"]["code"], "code_too_large")

    def test_http_errors_keep_their_status_and_json_shape(self):
        response = self.client.get("/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIsInstance(response.get_json()["error"], dict)

        response = self.client.get("/api/analyze")
        self.assertEqual(response.status_code, 405)
        self.assertIsInstance(response.get_json()["error"], dict)

    def test_request_body_size_limit_returns_json(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 128
        response = self.client.post("/api/analyze", json={"code": "x" * 1000})
        self.assertEqual(response.status_code, 413)
        self.assertIsInstance(response.get_json()["error"], dict)

    def test_python_analysis_returns_trace_and_output(self):
        response = self.client.post(
            "/api/analyze",
            json={
                "language": "python",
                "code": "value = 1\nvalue = value + 1\nprint(value)\n",
                "debug": True,
                "breakpoints": [2],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["execution_trace"])
        self.assertEqual(payload["stdout"].strip(), "2")
        self.assertEqual(payload["paused_at"], 1)
        self.assertEqual(payload["breakpoint_hits"], [1])
        self.assertTrue(payload["metadata"]["supports_execution_trace"])

    def test_final_variables_include_the_last_statement(self):
        payload = self.analyze_python("value = 1\nvalue = 2\n")
        self.assertEqual(payload["summary"]["final_variables"], {"value": "2"})
        self.assertEqual(payload["execution_trace"][-1]["variables"]["value"], "1")

    def test_final_variables_are_module_values_after_a_function_returns(self):
        payload = self.analyze_python(
            "def compute():\n    local = 42\n    return local\nanswer = compute()\n"
        )
        self.assertEqual(payload["summary"]["final_variables"], {"answer": "42"})

    def test_runtime_error_keeps_output_trace_and_final_variables(self):
        payload = self.analyze_python("value = 7\nprint(value)\nraise ValueError('broken')\n")
        self.assertEqual(payload["status"], "runtime_error")
        self.assertEqual(payload["stdout"].strip(), "7")
        self.assertIn("ValueError: broken", payload["error"])
        self.assertEqual(payload["summary"]["final_variables"], {"value": "7"})

    def test_successful_system_exit_is_not_a_runtime_error(self):
        payload = self.analyze_python("import sys\nsys.exit(0)\n")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["stderr"], "")

    def test_trace_like_program_stderr_does_not_break_analysis(self):
        payload = self.analyze_python(
            "import sys\nprint('__TRACE__:[]', file=sys.stderr)\n"
        )
        self.assertEqual(payload["status"], "success")
        self.assertIn("__TRACE__:[]", payload["stderr"])

    def test_trace_limit_keeps_final_values_and_program_output(self):
        self.app.config["MAX_TRACE_STEPS"] = 5
        payload = self.analyze_python("for value in range(10):\n    pass\nprint(value)\n")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(len(payload["execution_trace"]), 5)
        self.assertTrue(payload["metadata"]["trace_limit_reached"])
        self.assertEqual(payload["stdout"].strip(), "9")
        self.assertEqual(payload["summary"]["final_variables"], {"value": "9"})

    def test_timeout_keeps_partial_output_and_trace(self):
        self.app.config["EXECUTION_TIMEOUT_SECONDS"] = 0.5
        self.app.config["MAX_TRACE_STEPS"] = 10
        started = time.monotonic()
        payload = self.analyze_python("print('started', flush=True)\nwhile True:\n    pass\n")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(payload["status"], "error")
        self.assertIn("timed out", payload["error"].lower())
        self.assertIn("started", payload["stdout"])
        self.assertTrue(payload["execution_trace"])

    def test_program_output_is_bounded(self):
        self.app.config["MAX_OUTPUT_BYTES"] = 1024
        payload = self.analyze_python("print('x' * 200000)\n")
        self.assertEqual(payload["status"], "error")
        self.assertIn("output", payload["error"].lower())
        self.assertLessEqual(len(payload["stdout"].encode("utf-8")), 1024)
        self.assertTrue(payload["metadata"]["output_limit_reached"])

    def test_trace_capture_limit_returns_a_clear_output_error(self):
        declarations = [
            f"value_{index}_{'x' * 190} = 'x' * 160"
            for index in range(64)
        ]
        payload = self.analyze_python(
            "\n".join(declarations) + "\nfor index in range(500):\n    pass\n"
        )
        self.assertEqual(payload["status"], "error")
        self.assertIn("output limit", payload["error"])
        self.assertTrue(payload["metadata"]["output_limit_reached"])
        self.assertLessEqual(len(payload["stderr"].encode("utf-8")), 65536)

    @unittest.skipUnless(os.name == "posix", "POSIX resource limits required")
    def test_python_memory_limit_is_enforced(self):
        self.app.config["MAX_MEMORY_MB"] = 64
        payload = self.analyze_python("payload = bytearray(128 * 1024 * 1024)\n")
        self.assertEqual(payload["status"], "runtime_error")
        self.assertIn("MemoryError", payload["error"])

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_child_processes_do_not_survive_a_completed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-finished"
            child_code = (
                "import time; from pathlib import Path; time.sleep(0.5); "
                f"Path({str(marker)!r}).write_text('leftover')"
            )
            payload = self.analyze_python(
                "import subprocess, sys\n"
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}], cwd={directory!r})\n"
            )
            self.assertEqual(payload["status"], "success")
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    def test_execution_does_not_inherit_server_secrets(self):
        with patch.dict(os.environ, {"CODESCOPE_TEST_SECRET": "hidden"}):
            payload = self.analyze_python(
                "import os\nprint(os.environ.get('CODESCOPE_TEST_SECRET', 'missing'))\n"
            )
        self.assertEqual(payload["stdout"].strip(), "missing")

    def test_breakpoints_are_deduplicated_and_use_one_based_lines(self):
        payload = self.analyze_python(
            "value = 1\nprint(value)\n", debug=True, breakpoints=[2, 1, 2]
        )
        self.assertEqual(payload["metadata"]["breakpoints"], [1, 2])
        self.assertEqual(payload["breakpoint_hits"], [0, 1])
        self.assertEqual(payload["paused_at"], 0)

    def test_python_structure_includes_ranges_and_all_argument_kinds(self):
        payload = self.analyze_python(
            "def compute(first, /, second, *args, flag=True, **kwargs):\n"
            "    return first\n"
        )
        function = payload["code_structure"]["functions"][0]
        self.assertEqual(function["args"], ["first", "second", "args", "flag", "kwargs"])
        self.assertEqual(function["end_line"], 2)

    def test_python_syntax_error_returns_validation_error(self):
        response = self.client.post(
            "/api/analyze",
            json={
                "language": "python",
                "code": "def broken(:\n    return 1\n",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "validation_error")
        self.assertEqual(payload["execution_trace"], [])
        self.assertEqual(payload["metadata"]["code_metrics"]["lines"], 2)

    @patch("backend.app.shutil.which", return_value=None)
    def test_c_analysis_gracefully_degrades_without_compiler(self, _mock_which):
        response = self.client.post(
            "/api/analyze",
            json={
                "language": "c",
                "code": "#include <stdio.h>\nint main(void) { return 0; }\n",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["metadata"]["supports_execution_trace"])
        self.assertIn("static analysis only", payload["note"].lower())

    @unittest.skipUnless(shutil.which("gcc"), "gcc is not installed")
    def test_c_program_returns_output_without_a_trace(self):
        response = self.client.post(
            "/api/analyze",
            json={
                "language": "c",
                "code": '#include <stdio.h>\nint main(void) { printf("hello\\n"); return 0; }\n',
            },
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["stdout"].strip(), "hello")
        self.assertFalse(payload["metadata"]["supports_execution_trace"])

    @unittest.skipUnless(shutil.which("gcc"), "gcc is not installed")
    def test_c_nonzero_exit_always_has_an_error_message(self):
        response = self.client.post(
            "/api/analyze", json={"language": "c", "code": "int main(void) { return 7; }\n"}
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "runtime_error")
        self.assertIn("7", payload["error"])

    @unittest.skipUnless(shutil.which("gcc"), "gcc is not installed")
    def test_c_syntax_error_returns_compiler_diagnostics(self):
        response = self.client.post(
            "/api/analyze", json={"language": "c", "code": "int main(void) { return ; broken }\n"}
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "validation_error")
        self.assertTrue(payload["stderr"])

    @patch("backend.app.shutil.which", return_value="/usr/bin/gcc")
    @patch("backend.app.run_process")
    def test_c_build_timeout_returns_an_analysis_error(self, execute, _which):
        execute.side_effect = [
            ProcessResult(returncode=0),
            ProcessResult(returncode=-9, stderr="partial compiler output", timed_out=True),
        ]
        response = self.client.post(
            "/api/analyze", json={"language": "c", "code": "int main(void) { return 0; }\n"}
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "error")
        self.assertIn("C compilation timed out", payload["error"])
        self.assertIn("partial compiler output", payload["stderr"])
        self.assertTrue(payload["metadata"]["timed_out"])
        self.assertEqual(execute.call_count, 2)

    @patch("backend.app.shutil.which", return_value="/usr/bin/gcc")
    @patch("backend.app.run_process")
    def test_c_runtime_output_limit_keeps_captured_output(self, execute, _which):
        execute.side_effect = [
            ProcessResult(returncode=0),
            ProcessResult(returncode=0),
            ProcessResult(returncode=-9, stdout="partial", output_limit_reached=True),
        ]
        response = self.client.post(
            "/api/analyze", json={"language": "c", "code": "int main(void) { return 0; }\n"}
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["stdout"], "partial")
        self.assertTrue(payload["metadata"]["output_limit_reached"])

    def test_invalid_environment_configuration_fails_at_startup(self):
        for name, value in (
            ("PORT", "wrong"),
            ("EXECUTION_TIMEOUT_SECONDS", "nan"),
            ("MAX_MEMORY_MB", "0"),
            ("ALLOW_CODE_EXECUTION", "sometimes"),
        ):
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(ValueError, name):
                    create_app()

    def test_disabled_execution_is_reported_for_syntax_errors(self):
        self.app.config["ALLOW_CODE_EXECUTION"] = False
        payload = self.analyze_python("def broken(:\n    pass\n")
        self.assertEqual(payload["status"], "validation_error")
        self.assertFalse(payload["metadata"]["execution_enabled"])
        self.assertFalse(payload["metadata"]["supports_execution_trace"])

    def test_production_defaults_to_static_analysis(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            os.environ.pop("ALLOW_CODE_EXECUTION", None)
            client = create_app().test_client()
            for language, code in (
                ("python", "raise RuntimeError('must not run')\n"),
                ("c", "int main(void) { return 7; }\n"),
            ):
                with self.subTest(language=language):
                    response = client.post(
                        "/api/analyze", json={"language": language, "code": code}
                    )
                    payload = response.get_json()
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(payload["status"], "success")
                    self.assertFalse(payload["metadata"]["supports_execution_trace"])
                    self.assertFalse(payload["metadata"]["execution_enabled"])
                    self.assertEqual(payload["execution_trace"], [])
                    self.assertEqual(payload["stdout"], "")
            self.assertFalse(client.get("/api/health").get_json()["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
