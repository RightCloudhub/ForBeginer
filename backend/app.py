from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

if __package__:
    from .execution import ProcessResult, run_process
else:
    from execution import ProcessResult, run_process

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional in local development
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv()


LOGGER = logging.getLogger(__name__)
TRACE_PREFIX = "__TRACE__:"
SUPPORTED_LANGUAGES = {"python", "c"}
FUNCTION_PATTERN = re.compile(
    r"^\s*([A-Za-z_][\w\s\*]*?)\s+([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*\{"
)
STRUCT_PATTERN = re.compile(r"^\s*struct\s+([A-Za-z_]\w*)\s*\{")
VARIABLE_PATTERN = re.compile(
    r"^\s*([A-Za-z_][\w\s\*]*?)\s+([A-Za-z_]\w*)\s*=\s*.+;\s*$"
)

PYTHON_TRACE_BOOTSTRAP = textwrap.dedent(
    """
    import json
    import pathlib
    import sys
    import traceback

    TRACE_PREFIX = "__TRACE__:"
    MAX_TRACE_STEPS = int(sys.argv[2])
    trace_count = 0
    trace_limit_reached = False
    call_stack = []
    target_path = pathlib.Path(sys.argv[1]).resolve()

    def safe_repr(value):
        try:
            rendered = repr(value)
        except Exception:
            rendered = f"<unrepresentable {type(value).__name__}>"

        if len(rendered) > 160:
            rendered = rendered[:157] + "..."
        return rendered

    def normalize_locals(frame_locals):
        variables = {}
        for name, value in frame_locals.items():
            if name.startswith("__"):
                continue
            if callable(value):
                continue
            variables[name] = safe_repr(value)
            if len(variables) >= 64:
                break
        return variables

    def tracer(frame, event, arg):
        global trace_count, trace_limit_reached

        try:
            frame_path = pathlib.Path(frame.f_code.co_filename).resolve()
        except Exception:
            return tracer

        if frame_path != target_path:
            return tracer

        if event == "call":
            call_stack.append({"name": frame.f_code.co_name, "line": frame.f_lineno})
            return tracer

        if event == "return":
            if call_stack and call_stack[-1]["name"] == frame.f_code.co_name:
                call_stack.pop()
            return tracer

        if event != "line":
            return tracer

        if trace_count >= MAX_TRACE_STEPS:
            if not trace_limit_reached:
                trace_limit_reached = True
                print(
                    TRACE_PREFIX
                    + json.dumps({"event": "trace_limit_reached", "limit": MAX_TRACE_STEPS}),
                    file=sys.stderr,
                    flush=True,
                )
            return None

        trace_count += 1
        if call_stack:
            call_stack[-1]["line"] = frame.f_lineno
        entry = {
            "line": frame.f_lineno,
            "function": frame.f_code.co_name,
            "variables": normalize_locals(frame.f_locals),
            "call_stack": list(call_stack),
        }
        print(TRACE_PREFIX + json.dumps(entry, ensure_ascii=False), file=sys.stderr, flush=True)
        return tracer

    def main():
        code_path = target_path
        with code_path.open("r", encoding="utf-8") as handle:
            source = handle.read()

        globals_dict = {
            "__name__": "__main__",
            "__file__": str(code_path),
            "__package__": None,
        }

        sys.settrace(tracer)
        try:
            exec(compile(source, str(code_path), "exec"), globals_dict, globals_dict)
        finally:
            sys.settrace(None)
            print(
                TRACE_PREFIX + json.dumps(
                    {"event": "final_state", "variables": normalize_locals(globals_dict)},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )

    if __name__ == "__main__":
        try:
            main()
        except SystemExit as error:
            if error.code not in (None, 0):
                traceback.print_exc()
            raise
        except BaseException:
            traceback.print_exc()
            sys.exit(1)
    """
)


@dataclass(slots=True)
class ApiError(Exception):
    message: str
    status_code: int = 400
    code: str = "bad_request"
    details: dict[str, Any] | None = None


def create_app() -> Flask:
    app = Flask(__name__)
    environment = os.getenv("APP_ENV", "development").strip().lower()
    app.config.update(
        APP_ENV=environment,
        HOST=os.getenv("HOST", "127.0.0.1"),
        PORT=read_number_env("PORT", 5000, 1, 65535),
        MAX_CODE_LENGTH=read_number_env("MAX_CODE_LENGTH", 20000, 1, 1_000_000),
        EXECUTION_TIMEOUT_SECONDS=read_number_env(
            "EXECUTION_TIMEOUT_SECONDS", 5.0, 0.05, 60.0
        ),
        MAX_TRACE_STEPS=read_number_env("MAX_TRACE_STEPS", 800, 1, 5000),
        MAX_MEMORY_MB=read_number_env("MAX_MEMORY_MB", 256, 32, 4096),
        MAX_OUTPUT_BYTES=read_number_env("MAX_OUTPUT_BYTES", 65536, 1024, 1_048_576),
        ALLOW_CODE_EXECUTION=read_bool_env(
            "ALLOW_CODE_EXECUTION", environment in {"development", "testing"}
        ),
        C_COMPILER=os.getenv("C_COMPILER", "gcc"),
        CORS_ORIGINS=parse_origins(os.getenv("CORS_ORIGINS", "http://localhost:3000")),
    )
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_CODE_LENGTH"] * 12 + 65536

    configure_logging(app)
    CORS(app, resources={r"/*": {"origins": app.config["CORS_ORIGINS"]}})
    register_error_handlers(app)
    register_routes(app)
    return app


def read_number_env(name: str, default: int | float, minimum, maximum) -> int | float:
    try:
        value = type(default)(os.getenv(name, str(default)))
        if math.isfinite(value) and minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} must be between {minimum} and {maximum}.")


def read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value in {"true", "1", "yes", "on"}:
        return True
    if raw_value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def parse_origins(raw_value: str) -> list[str] | str:
    value = raw_value.strip()
    if not value or value == "*":
        return "*"
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def configure_logging(app: Flask) -> None:
    level = logging.DEBUG if app.config["APP_ENV"] == "development" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        payload = {
            "status": "error",
            "error": {
                "code": error.code,
                "message": error.message,
            },
        }
        if error.details:
            payload["error"]["details"] = error.details
        return jsonify(payload), error.status_code

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        response = error.get_response()
        response.data = json.dumps(
            {
                "status": "error",
                "error": {
                    "code": error.name.lower().replace(" ", "_"),
                    "message": error.description,
                },
            }
        )
        response.content_type = "application/json"
        return response

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        LOGGER.exception("Unhandled application error", exc_info=error)
        return (
            jsonify(
                {
                    "status": "error",
                    "error": {
                        "code": "internal_error",
                        "message": "Internal server error",
                    },
                }
            ),
            500,
        )


def register_routes(app: Flask) -> None:
    @app.get("/")
    @app.get("/api")
    def index():
        return jsonify(
            {
                "status": "ok",
                "service": "code-visualizer-api",
                "version": 2,
                "supported_languages": sorted(SUPPORTED_LANGUAGES),
            }
        )

    @app.get("/health")
    @app.get("/api/health")
    def health():
        compiler_name = app.config["C_COMPILER"]
        compiler_path = shutil.which(compiler_name)
        if compiler_path:
            compiler_path = str(Path(compiler_path).resolve())
        return jsonify(
            {
                "status": "ok",
                "environment": app.config["APP_ENV"],
                "python_version": sys.version.split()[0],
                "execution_enabled": app.config["ALLOW_CODE_EXECUTION"],
                "limits": {
                    "timeout_seconds": app.config["EXECUTION_TIMEOUT_SECONDS"],
                    "trace_steps": app.config["MAX_TRACE_STEPS"],
                    "memory_mb": app.config["MAX_MEMORY_MB"],
                    "output_bytes": app.config["MAX_OUTPUT_BYTES"],
                    "resource_limits_supported": os.name == "posix",
                },
                "compiler": {
                    "configured": compiler_name,
                    "available": bool(compiler_path),
                    "path": compiler_path,
                },
            }
        )

    @app.post("/analyze")
    @app.post("/api/analyze")
    def analyze():
        payload = request.get_json(silent=True)
        if payload is None:
            raise ApiError(
                "Request body must be valid JSON.",
                code="invalid_json",
            )
        if not isinstance(payload, dict):
            raise ApiError("Request body must be a JSON object.", code="invalid_body")

        code = payload.get("code", "")
        language = payload.get("language", "python")
        debug = payload.get("debug", False)

        if not isinstance(code, str) or not code.strip():
            raise ApiError("Code is required.", code="code_required")
        if not isinstance(language, str):
            raise ApiError("Language must be a string.", code="invalid_language")
        language = language.strip().lower()
        if type(debug) is not bool:
            raise ApiError("Debug must be a boolean.", code="invalid_debug")

        if language not in SUPPORTED_LANGUAGES:
            raise ApiError(
                "Unsupported language.",
                code="unsupported_language",
                details={"supported_languages": sorted(SUPPORTED_LANGUAGES)},
            )

        if len(code) > app.config["MAX_CODE_LENGTH"]:
            raise ApiError(
                "Code exceeds the maximum allowed size.",
                code="code_too_large",
                details={"max_code_length": app.config["MAX_CODE_LENGTH"]},
            )
        breakpoints = normalize_breakpoints(
            payload.get("breakpoints", []), code.count("\n") + 1
        )

        analysis = analyze_code(
            code=code,
            language=language,
            debug=debug,
            breakpoints=breakpoints,
            config=app.config,
        )
        return jsonify(analysis)


def normalize_breakpoints(raw_breakpoints: Any, line_count: int) -> list[int]:
    if raw_breakpoints in (None, ""):
        return []
    if not isinstance(raw_breakpoints, list):
        raise ApiError(
            "Breakpoints must be an array of line numbers.",
            code="invalid_breakpoints",
        )

    cleaned: set[int] = set()
    for item in raw_breakpoints:
        if type(item) is not int or not 1 <= item <= line_count:
            raise ApiError(
                "Breakpoints must be integer line numbers within the snippet.",
                code="invalid_breakpoints",
            )
        cleaned.add(item)

    return sorted(cleaned)


def analyze_code(
    *,
    code: str,
    language: str,
    debug: bool,
    breakpoints: list[int],
    config: Any,
) -> dict[str, Any]:
    if language == "python":
        return analyze_python_code(
            code=code,
            debug=debug,
            breakpoints=breakpoints,
            timeout_seconds=config["EXECUTION_TIMEOUT_SECONDS"],
            max_trace_steps=config["MAX_TRACE_STEPS"],
            max_memory_mb=config["MAX_MEMORY_MB"],
            max_output_bytes=config["MAX_OUTPUT_BYTES"],
            execution_enabled=config["ALLOW_CODE_EXECUTION"],
        )
    return analyze_c_code(
        code=code,
        breakpoints=breakpoints,
        compiler_name=config["C_COMPILER"],
        timeout_seconds=config["EXECUTION_TIMEOUT_SECONDS"],
        max_memory_mb=config["MAX_MEMORY_MB"],
        max_output_bytes=config["MAX_OUTPUT_BYTES"],
        execution_enabled=config["ALLOW_CODE_EXECUTION"],
    )


def analyze_python_code(
    *,
    code: str,
    debug: bool,
    breakpoints: list[int],
    timeout_seconds: float,
    max_trace_steps: int,
    max_memory_mb: int,
    max_output_bytes: int,
    execution_enabled: bool,
) -> dict[str, Any]:
    try:
        code_structure = analyze_python_structure(code)
    except SyntaxError as error:
        return build_validation_error_response(
            language="python",
            error=error,
            code=code,
            execution_enabled=execution_enabled,
        )

    if not execution_enabled:
        response = build_base_response(
            language="python",
            code=code,
            code_structure=code_structure,
            execution_trace=[],
            stdout="",
            stderr="",
            status="success",
            error_message=None,
            note="Code execution is disabled. Returned static analysis only.",
            supports_execution_trace=False,
            breakpoints=breakpoints,
        )
        response["metadata"]["execution_enabled"] = False
        return response

    with tempfile.TemporaryDirectory(prefix="code-visualizer-python-") as temp_dir:
        code_path = Path(temp_dir) / "snippet.py"
        bootstrap_path = Path(temp_dir) / "bootstrap.py"
        code_path.write_text(code, encoding="utf-8")
        bootstrap_path.write_text(PYTHON_TRACE_BOOTSTRAP, encoding="utf-8")

        process = run_process(
            [
                sys.executable,
                "-I",
                "-B",
                "-u",
                str(bootstrap_path),
                str(code_path),
                str(max_trace_steps),
            ],
            cwd=temp_dir,
            timeout_seconds=timeout_seconds,
            max_memory_mb=max_memory_mb,
            max_output_bytes=max_output_bytes,
            max_stderr_bytes=4 * 1024 * 1024,
        )

    trace_entries, stderr, trace_limit_reached, final_variables = extract_trace_entries(
        process.stderr
    )
    execution_trace = enrich_execution_trace(trace_entries)
    summary = build_execution_summary(execution_trace, code, final_variables)
    breakpoint_hits = find_breakpoint_hits(execution_trace, breakpoints)
    output_limit_reached = process.output_limit_reached or len(stderr.encode("utf-8")) > max_output_bytes
    error_message = process_error(process, "Python execution", stderr)
    if output_limit_reached:
        error_message = "Python execution exceeded the output limit."
    status = (
        "error" if process.timed_out or output_limit_reached
        else "runtime_error" if process.returncode
        else "success"
    )
    if process.timed_out:
        note = "Execution stopped at the time limit. Partial output and trace are shown."
    elif output_limit_reached:
        note = "Output reached the configured limit. Captured output is truncated."
    elif trace_limit_reached:
        note = f"Trace limited to {max_trace_steps} steps. Final values reflect the completed program."
    else:
        note = "Trace snapshots record values before each line. Breakpoints navigate this recorded run."

    response = build_base_response(
        language="python",
        code=code,
        code_structure=code_structure,
        execution_trace=execution_trace,
        stdout=process.stdout,
        stderr=bounded_text(stderr, max_output_bytes),
        status=status,
        error_message=error_message,
        note=note,
        supports_execution_trace=True,
        breakpoints=breakpoints,
        summary=summary,
    )
    response["breakpoint_hits"] = breakpoint_hits
    response["paused_at"] = resolve_paused_at(debug, breakpoint_hits, execution_trace)
    response["metadata"]["trace_limit_reached"] = trace_limit_reached
    response["metadata"]["output_limit_reached"] = output_limit_reached
    response["metadata"]["timed_out"] = process.timed_out
    response["metadata"]["execution_enabled"] = True
    response["metadata"]["final_variables_available"] = final_variables is not None
    return response


def analyze_c_code(
    *,
    code: str,
    breakpoints: list[int],
    compiler_name: str,
    timeout_seconds: float,
    max_memory_mb: int,
    max_output_bytes: int,
    execution_enabled: bool,
) -> dict[str, Any]:
    code_structure = analyze_c_structure(code)
    compiler_path = shutil.which(compiler_name)
    if compiler_path:
        compiler_path = str(Path(compiler_path).resolve())
    compiler = {
        "configured": compiler_name,
        "available": bool(compiler_path),
        "path": compiler_path,
    }

    def respond(
        process: ProcessResult | None = None,
        *,
        status: str = "success",
        error: str | None = None,
        note: str = "C analysis includes structure, validation, and program output; no execution trace.",
        diagnostics: str = "",
    ) -> dict[str, Any]:
        response = build_base_response(
            language="c",
            code=code,
            code_structure=code_structure,
            execution_trace=[],
            stdout=process.stdout if process else "",
            stderr=bounded_text(
                "\n".join(part for part in (diagnostics, process.stderr if process else "") if part),
                max_output_bytes,
            ),
            status=status,
            error_message=error,
            note=note,
            supports_execution_trace=False,
            breakpoints=breakpoints,
            compiler=compiler,
        )
        response["metadata"].update(
            execution_enabled=execution_enabled,
            output_limit_reached=bool(process and process.output_limit_reached),
            timed_out=bool(process and process.timed_out),
        )
        return response

    if not execution_enabled:
        return respond(note="Code execution is disabled. Returned static analysis only.")
    if compiler_path is None:
        return respond(note="C compiler is not available. Returned static analysis only.")

    with tempfile.TemporaryDirectory(prefix="code-visualizer-c-") as temp_dir:
        source_path = Path(temp_dir) / "snippet.c"
        binary_path = Path(temp_dir) / "snippet.out"
        source_path.write_text(code, encoding="utf-8")

        def execute(command: list[str]) -> ProcessResult:
            return run_process(
                command,
                cwd=temp_dir,
                timeout_seconds=timeout_seconds,
                max_memory_mb=max_memory_mb,
                max_output_bytes=max_output_bytes,
            )

        syntax_result = execute([compiler_path, "-fsyntax-only", str(source_path)])
        error = process_error(syntax_result, "C syntax validation")
        if error:
            return respond(
                syntax_result,
                status="error" if syntax_result.timed_out or syntax_result.output_limit_reached else "validation_error",
                error=error,
                note="Returned static analysis because C syntax validation did not complete successfully.",
            )

        compile_result = execute(
            [compiler_path, str(source_path), "-O0", "-g", "-o", str(binary_path)]
        )
        error = process_error(compile_result, "C compilation")
        if error:
            return respond(
                compile_result,
                status="error",
                error=error,
                note="The source passed syntax validation but did not build successfully.",
            )

        run_result = execute([str(binary_path)])

    error = process_error(run_result, "C execution")
    return respond(
        run_result,
        status=(
            "error" if run_result.timed_out or run_result.output_limit_reached
            else "runtime_error" if run_result.returncode
            else "success"
        ),
        error=error,
        diagnostics=compile_result.stderr,
    )


def bounded_text(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def process_error(process: ProcessResult, label: str, stderr: str | None = None) -> str | None:
    if process.timed_out:
        return f"{label} timed out."
    if process.output_limit_reached:
        return f"{label} exceeded the output limit."
    if process.returncode:
        return extract_runtime_error(process.stderr if stderr is None else stderr) or (
            f"{label} exited with status {process.returncode}."
        )
    return None


def build_validation_error_response(
    language: str, error: SyntaxError, code: str, execution_enabled: bool
) -> dict[str, Any]:
    location = {
        "line": error.lineno,
        "column": error.offset,
    }
    return {
        "status": "validation_error",
        "language": language,
        "execution_trace": [],
        "code_structure": empty_structure_for_language(language),
        "stdout": "",
        "stderr": error.text.strip() if error.text else "",
        "error": error.msg,
        "note": "Source parsing failed before execution.",
        "paused_at": None,
        "breakpoint_hits": [],
        "summary": build_execution_summary([], code),
        "metadata": {
            "location": location,
            "supports_execution_trace": language == "python" and execution_enabled,
            "execution_enabled": execution_enabled,
            "breakpoints": [],
            "code_metrics": {"lines": len(code.splitlines()), "characters": len(code)},
        },
    }


def build_base_response(
    *,
    language: str,
    code: str,
    code_structure: dict[str, Any],
    execution_trace: list[dict[str, Any]],
    stdout: str,
    stderr: str,
    status: str,
    error_message: str | None,
    note: str | None,
    supports_execution_trace: bool,
    breakpoints: list[int],
    summary: dict[str, Any] | None = None,
    compiler: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = {
        "status": status,
        "language": language,
        "execution_trace": execution_trace,
        "code_structure": code_structure,
        "stdout": stdout,
        "stderr": stderr,
        "error": error_message,
        "note": note,
        "paused_at": None,
        "breakpoint_hits": [],
        "summary": summary or build_execution_summary(execution_trace, code),
        "metadata": {
            "supports_execution_trace": supports_execution_trace,
            "breakpoints": breakpoints,
            "code_metrics": {
                "lines": len(code.splitlines()),
                "characters": len(code),
            },
        },
    }
    if compiler is not None:
        response["metadata"]["compiler"] = compiler
    return response


def empty_structure_for_language(language: str) -> dict[str, Any]:
    if language == "c":
        return {
            "functions": [],
            "structs": [],
            "variables": [],
            "loops": [],
            "conditionals": [],
        }
    return {
        "functions": [],
        "classes": [],
        "loops": [],
        "conditionals": [],
    }


def extract_trace_entries(
    stderr: str,
) -> tuple[list[dict[str, Any]], str, bool, dict[str, str] | None]:
    trace_entries: list[dict[str, Any]] = []
    clean_stderr: list[str] = []
    trace_limit_reached = False
    final_variables = None

    for raw_line in stderr.splitlines():
        if not raw_line.startswith(TRACE_PREFIX):
            clean_stderr.append(raw_line)
            continue

        payload = raw_line[len(TRACE_PREFIX) :]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            clean_stderr.append(raw_line)
            continue
        if not isinstance(parsed, dict):
            clean_stderr.append(raw_line)
            continue

        if parsed.get("event") == "trace_limit_reached":
            trace_limit_reached = True
            continue
        variables = parsed.get("variables")
        if not isinstance(variables, dict) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in variables.items()
        ):
            clean_stderr.append(raw_line)
            continue
        if parsed.get("event") == "final_state":
            final_variables = variables
            continue
        if type(parsed.get("line")) is not int or parsed["line"] < 1:
            clean_stderr.append(raw_line)
            continue

        trace_entries.append(parsed)

    return trace_entries, "\n".join(clean_stderr).strip(), trace_limit_reached, final_variables


def enrich_execution_trace(trace_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for entry in trace_entries:
        variables = entry.get("variables", {})
        memory = build_memory_view(variables)
        enriched.append(
            {
                "line": entry.get("line"),
                "function": entry.get("function"),
                "variables": variables,
                "call_stack": entry.get("call_stack", []),
                "memory": memory,
            }
        )
    return enriched


def build_memory_view(variables: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    stack: list[dict[str, str]] = []
    heap: list[dict[str, str]] = []

    for name, value in variables.items():
        if is_primitive_value(value):
            stack.append({"name": name, "value": value, "type": "primitive"})
            continue

        heap_id = f"heap_{len(heap) + 1}"
        stack.append({"name": name, "value": heap_id, "type": "reference"})
        heap.append({"id": heap_id, "value": value})

    return {"stack": stack, "heap": heap}


def is_primitive_value(value: str | None) -> bool:
    if value is None:
        return True

    stripped = value.strip()
    numeric = re.fullmatch(r"-?\d+(\.\d+)?", stripped)
    quoted = re.fullmatch(r"(['\"]).*\1", stripped)
    boolean = stripped in {"True", "False", "None", "true", "false", "null"}
    return bool(numeric or quoted or boolean)


def find_breakpoint_hits(
    execution_trace: list[dict[str, Any]],
    breakpoints: list[int],
) -> list[int]:
    if not breakpoints:
        return []

    breakpoint_set = set(breakpoints)
    return [
        index
        for index, step in enumerate(execution_trace)
        if step.get("line") in breakpoint_set
    ]


def resolve_paused_at(
    debug: bool,
    breakpoint_hits: list[int],
    execution_trace: list[dict[str, Any]],
) -> int | None:
    if not execution_trace:
        return None
    if not debug:
        return 0
    if breakpoint_hits:
        return breakpoint_hits[0]
    return 0


def extract_runtime_error(stderr: str) -> str | None:
    if not stderr:
        return None

    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else None


def build_execution_summary(
    execution_trace: list[dict[str, Any]],
    code: str,
    final_variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    unique_lines = sorted(
        {step["line"] for step in execution_trace if isinstance(step.get("line"), int)}
    )
    return {
        "step_count": len(execution_trace),
        "executed_lines": unique_lines,
        "executed_line_count": len(unique_lines),
        "final_variables": (
            final_variables if final_variables is not None
            else execution_trace[-1]["variables"] if execution_trace
            else {}
        ),
        "code_line_count": len(code.splitlines()) if code else 0,
    }


def analyze_python_structure(code: str) -> dict[str, Any]:
    tree = ast.parse(code)
    structure = {
        "functions": [],
        "classes": [],
        "loops": [],
        "conditionals": [],
    }

    class StructureVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            arguments = [*node.args.posonlyargs, *node.args.args]
            if node.args.vararg:
                arguments.append(node.args.vararg)
            arguments.extend(node.args.kwonlyargs)
            if node.args.kwarg:
                arguments.append(node.args.kwarg)
            structure["functions"].append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                    "args": [argument.arg for argument in arguments],
                    "async": isinstance(node, ast.AsyncFunctionDef),
                }
            )
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            structure["classes"].append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": node.end_lineno,
                    "bases": [ast.unparse(base) for base in node.bases],
                }
            )
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            structure["loops"].append(
                {"type": "for", "line": node.lineno, "end_line": node.end_lineno}
            )
            self.generic_visit(node)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            structure["loops"].append(
                {"type": "async_for", "line": node.lineno, "end_line": node.end_lineno}
            )
            self.generic_visit(node)

        def visit_While(self, node: ast.While) -> None:
            structure["loops"].append(
                {"type": "while", "line": node.lineno, "end_line": node.end_lineno}
            )
            self.generic_visit(node)

        def visit_If(self, node: ast.If) -> None:
            structure["conditionals"].append(
                {"type": "if", "line": node.lineno, "end_line": node.end_lineno}
            )
            self.generic_visit(node)

        def visit_Match(self, node: ast.Match) -> None:
            structure["conditionals"].append(
                {"type": "match", "line": node.lineno, "end_line": node.end_lineno}
            )
            self.generic_visit(node)

    StructureVisitor().visit(tree)
    return structure


def analyze_c_structure(code: str) -> dict[str, Any]:
    structure = {
        "functions": [],
        "structs": [],
        "variables": [],
        "loops": [],
        "conditionals": [],
    }

    brace_depth = 0
    active_function: str | None = None

    for index, raw_line in enumerate(code.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            brace_depth += raw_line.count("{") - raw_line.count("}")
            if brace_depth <= 0:
                active_function = None
                brace_depth = max(brace_depth, 0)
            continue

        struct_match = STRUCT_PATTERN.match(raw_line)
        if struct_match:
            structure["structs"].append(
                {"name": struct_match.group(1), "line": index}
            )

        function_match = FUNCTION_PATTERN.match(raw_line)
        if function_match:
            params = [
                param.strip()
                for param in function_match.group(3).split(",")
                if param.strip() and param.strip() != "void"
            ]
            active_function = function_match.group(2)
            structure["functions"].append(
                {
                    "name": function_match.group(2),
                    "line": index,
                    "return_type": function_match.group(1).strip(),
                    "params": params,
                }
            )

        variable_match = VARIABLE_PATTERN.match(raw_line)
        if variable_match:
            structure["variables"].append(
                {
                    "name": variable_match.group(2),
                    "type": variable_match.group(1).strip(),
                    "line": index,
                    "scope": "local" if active_function else "global",
                }
            )

        if stripped.startswith("for ") or stripped.startswith("for("):
            structure["loops"].append({"type": "for", "line": index})
        if stripped.startswith("while ") or stripped.startswith("while("):
            structure["loops"].append({"type": "while", "line": index})
        if stripped.startswith("if ") or stripped.startswith("if("):
            structure["conditionals"].append({"type": "if", "line": index})
        if stripped.startswith("else if ") or stripped.startswith("else if("):
            structure["conditionals"].append({"type": "else_if", "line": index})
        if stripped == "else" or stripped.startswith("else {"):
            structure["conditionals"].append({"type": "else", "line": index})

        brace_depth += raw_line.count("{") - raw_line.count("}")
        if brace_depth <= 0:
            active_function = None
            brace_depth = 0

    return structure


app = create_app()


if __name__ == "__main__":
    app.run(
        debug=app.config["APP_ENV"] == "development",
        host=app.config["HOST"],
        port=app.config["PORT"],
    )
