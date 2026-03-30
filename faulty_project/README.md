# Faulty Project (Intentional Incident Generator)

This app is intentionally unstable so your main agent platform can detect and process incidents from local APM.

## Faults Included

- High latency (`latency_ms=1800`)
- High error probability (`error_probability=0.25`)

Config file:

- `app_config.json`

## Endpoints

- `GET /api/checkout` (slow + sometimes returns 500)
- `GET /api/health`
- `GET /metrics` (Prometheus format)

## Run Through Local APM Stack

This app is already included in:

- `local_apm_demo/docker-compose.yml`

So you normally start via:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects\local_apm_demo
docker compose up --build
```

## Generate Incidents

Run load generator:

```powershell
cd C:\Personal_projects\Agentic_AI_Projects\faulty_project
python load_faulty.py
```

This triggers Prometheus alerts, Alertmanager sends webhook to bridge, and your main scheduler can pick it up.

## Expected Demo Flow

1. Start local APM stack
2. Start main agent API with scheduler polling (`APM_ALERTS_MODE=http`)
3. Run `load_faulty.py`
4. Verify alerts at bridge (`http://127.0.0.1:8085/v1/alerts`)
5. Verify changes in main platform (`/v1/changes`, dashboard metrics)
