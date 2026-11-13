# CodeScope Studio

A React and Flask workspace for learning how Python and C snippets behave.

- Record Python line events, local values, call stacks, and program output.
- Replay a recorded run with steps, a slider, and one-based breakpoints.
- Inspect Python syntax trees and lightweight C structure analysis.
- Validate and run C snippets when the configured compiler is available.
- Return structured diagnostics, including partial output on execution timeout.

## Requirements

- Python 3.11+
- Node.js 18+
- Optional: `gcc` for C syntax validation, compilation, and output

## Local development

Run these commands from the repository root.

```bash
python -m venv fvenv
source fvenv/bin/activate
python -m pip install -r requirements.txt
npm --prefix frontend install
cp .env.example .env
cp frontend/.env.example frontend/.env.local
```

Start the API:

```bash
python backend/app.py
```

In another terminal, start the frontend:

```bash
npm --prefix frontend start
```

The API listens on `127.0.0.1:5000`. The frontend uses its development proxy for
`/api` requests. The supplied local environment enables execution of trusted snippets.

## Configuration

The backend loads `.env` without replacing existing process environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Logging and default execution policy |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Development server address |
| `ALLOW_CODE_EXECUTION` | True in development/testing; false otherwise | Enable Python and C execution |
| `MAX_CODE_LENGTH` | `20000` | Maximum source characters |
| `EXECUTION_TIMEOUT_SECONDS` | `5` | Time limit per execution/compiler process |
| `MAX_TRACE_STEPS` | `800` | Maximum recorded Python line events |
| `MAX_MEMORY_MB` | `256` | Per-process address-space limit on POSIX |
| `MAX_OUTPUT_BYTES` | `65536` | Maximum captured stdout and displayed stderr |
| `C_COMPILER` | `gcc` | Compiler name or executable path |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated permitted frontend origins |

The frontend reads its settings when it starts or builds:

- `REACT_APP_API_BASE_URL`: empty by default, using the site's `/api` route. Set this
  to the API's origin when it is hosted separately.
- `REACT_APP_API_TIMEOUT_MS`: request timeout, default `30000`. Increase it if the
  backend's configured limits allow longer runs.

Invalid backend numeric or boolean settings fail at startup with the setting's name.

## Execution boundaries

The execution runner changes to a temporary directory, supplies a small environment
without server secrets, closes standard input, bounds captured output, and stops
the process group on completion or timeout. POSIX also applies address-space, CPU,
file-size, and core-dump limits. Python trace stderr has a separate 4 MiB capture
budget. These limits do **not** isolate filesystem or network access.

Use local execution only for trusted snippets. Keep execution disabled on a public
deployment unless the service runs in a separately secured container or worker
with filesystem and network isolation. No interactive input is supported.

Python snapshots contain up to 64 non-callable values per frame, represented as
strings of at most 160 characters. The value panels are a logical view, not physical
stack/heap addresses. `summary.final_variables` contains the final module snapshot
when it is available; a timed-out run can only provide a partial trace.

Debugging replays a run that has already executed. A breakpoint selects a recorded
step; the Continue button jumps to the next recorded hit. It does not pause and
resume a live server process. Editing code or changing language discards old results
and cancels pending requests. F9 toggles a breakpoint on the cursor's line.

C currently returns structure, compiler diagnostics, and output without a line or
memory trace. Its structure parser is heuristic and does not fully parse C macros
or every declaration. A missing compiler returns static analysis with a capability
note.

## Production deployment

Build the frontend:

```bash
npm --prefix frontend run build
```

Serve `frontend/build` with Nginx or Caddy and proxy `/api/` to the Flask service.
The empty frontend API base URL supports this shared-origin setup.

Run the backend with Gunicorn on POSIX:

```bash
APP_ENV=production ALLOW_CODE_EXECUTION=false gunicorn \
  --workers 2 --timeout 30 --bind 127.0.0.1:5000 backend.app:app
```

Set `ALLOW_CODE_EXECUTION=false` explicitly in the deployment environment even if
a local `.env` file exists. In this mode both languages return static structure
analysis, and `/api/health` reports `execution_enabled: false`.

Example Nginx locations inside a server whose root is `frontend/build`:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:5000;
}
location / {
    try_files $uri $uri/ /index.html;
}
```

## API

`GET /api/health` returns runtime status, compiler availability, execution policy,
and configured limits. `/health` remains available as an alias.

`POST /api/analyze` accepts a JSON object:

```json
{
  "code": "value = 1\nprint(value)\n",
  "language": "python",
  "debug": true,
  "breakpoints": [2]
}
```

`language` is `python` or `c`. `debug` must be a boolean. Breakpoints must be integer
line numbers within the snippet; duplicates are removed.

Analysis responses use HTTP 200 with `status` set to `success`,
`validation_error`, `runtime_error`, or `error`. Fields include `execution_trace`,
`code_structure`, `summary`, `stdout`, `stderr`, `note`, `error`, `metadata`,
`breakpoint_hits`, and `paused_at`. Trace values are captured **before** each line,
while the final summary snapshot is captured after execution.

Invalid requests use HTTP 400, oversized request bodies use 413, and HTTP routing
errors preserve 404/405. Their JSON shape is
`{"status":"error","error":{"code":"...","message":"..."}}`. Unexpected server errors
return a generic 500 response. `/analyze` remains an alias for existing clients.

## Verification

```bash
python -m unittest discover -s tests -v
npm --prefix frontend run test:ci -- --runInBand
npm --prefix frontend run lint
npm --prefix frontend run build
```

CI runs the backend tests, frontend regression tests, lint, and production build.
