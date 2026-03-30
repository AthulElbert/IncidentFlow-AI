# Simple Local Demo Project

This folder gives you a fast way to verify that the main platform is running and processing alerts end-to-end.

## What It Checks

The script:

1. Calls `/health`
2. Queues a mock scheduler alert (`/v1/apm/mock-alerts`)
3. Runs one scheduler cycle (`/v1/scheduler/run-once`)
4. Reads latest change (`/v1/changes`)
5. Reads metrics summary (`/v1/metrics/summary`)

If all steps work, it prints `DEMO CHECK PASSED`.

## Prerequisites

- Main API running on `http://127.0.0.1:8000`
- `.env` should include scheduler endpoints enabled (default app already supports this)

Recommended `.env` for demo:

```env
APM_POLL_MODE=off
APM_ALERTS_MODE=mock
AUTO_REMEDIATION_MODE=assistive
```

`APM_POLL_MODE=off` is fine because script triggers scheduler manually with `run-once`.

## Run

From project root:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects
$env:PYTHONPATH="C:\Personal_projects\Agentic_AI_Projects\src"
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" demo_project\run_demo.py
```

Optional full change JSON output:

```powershell
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" demo_project\run_demo.py --print-change-json
```

## Custom Keys or URL

```powershell
& "C:\Personal_projects\Agentic_AI_Projects\.venv\Scripts\python.exe" demo_project\run_demo.py `
  --base-url http://127.0.0.1:8000 `
  --viewer-key viewer-local-key `
  --approver-key approver-local-key
```
