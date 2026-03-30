# Local APM Demo Stack (No SaaS Account Needed)

This stack provides a local APM-like flow using:

- Prometheus
- Alertmanager
- Grafana
- Sample app with intentional slow/error endpoints
- Faulty project service with intentionally bad checkout behavior
- Alert bridge that converts Alertmanager webhook alerts to `/v1/alerts`

Your scheduler can poll the bridge through `APM_ALERTS_MODE=http`.

## Start Local APM Stack

```powershell
cd C:\Personal_projects\Agentic_AI_Projects\local_apm_demo
docker compose up --build
```

Endpoints:

- Sample app: `http://127.0.0.1:8001`
- Faulty project: `http://127.0.0.1:8010`
- Prometheus: `http://127.0.0.1:9090`
- Alertmanager: `http://127.0.0.1:9093`
- Grafana: `http://127.0.0.1:3000` (`admin/admin`)
- Alert bridge: `http://127.0.0.1:8085`

## Generate Load and Alerts

In another terminal:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects\local_apm_demo
python loadgen.py
```

After ~1-2 minutes, alerts should start firing based on latency and error rate.

For the dedicated faulty project:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects\faulty_project
python load_faulty.py
```

## Configure Main Scheduler Project

Set these in `C:\Personal_projects\Agentic_AI_Projects\.env`:

```env
APM_POLL_MODE=poll
APM_ALERTS_MODE=http
APM_ALERTS_BASE_URL=http://127.0.0.1:8085
APM_ALERTS_TIMEOUT_SECONDS=10
AUTO_REMEDIATION_MODE=safe_auto
SAFE_AUTO_ISSUE_TYPES=PERFORMANCE_DEGRADATION,DEPENDENCY_FAILURE
```

Then restart your API server.

## Verify End-to-End

1. Check scheduler status: `GET /v1/scheduler/status`
2. Watch processed counters increase.
3. Inspect changes: `GET /v1/changes`
4. View generated patch artifacts in `generated_patches/`.
