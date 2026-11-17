"""Our tool interfaces (metrics, logs, deploy history) answered by MCP servers.

Each backend is an adapter: reviewed code that decides exactly what is sent to the server and turns the reply back
into our own types. A reply that does not fit is a `ToolError`, never half-parsed data. Text from a server (log
messages, deploy summaries) is untrusted like any other evidence, and is length-capped here before it goes further.

Adapters:
- `prometheus` (metrics): for a server that runs PromQL range queries and returns Prometheus' own result format,
  such as the community `prometheus-mcp-server`. Queries come from a fixed catalog (the same one the direct
  Prometheus backend uses, plus the manifest's `promql` entries); an agent picks a metric name, never PromQL.
- `incidentflow` (metrics, logs, deploys): for a server that implements this project's small contract, for example
  a bridge in front of an in-house system. The contract is the JSON shapes parsed below.
"""
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.mcp.manager import McpConnectionManager
from app.mcp.manifest import Binding
from app.tools.base import TimeWindow, ToolError
from app.tools.metrics import DEFAULT_PROMQL_CATALOG, PromQuery, series_from_prometheus
from app.tools.models import DeployRecord, LogLine, MetricPoint, MetricSeries

MAX_TEXT_CHARS = 2000
MAX_LOG_LINES = 1000


class _McpBackend:
    def __init__(self, manager: McpConnectionManager, binding: Binding) -> None:
        self.manager = manager
        self.binding = binding
        self.backend_label = f"mcp:{binding.server}"

    def _call(self, operation: str, arguments: dict[str, Any]) -> Any:
        return self.manager.call(self.binding.server, self.binding.tools[operation], arguments, self.binding.max_result_bytes)


class McpPrometheusMetrics(_McpBackend):
    """Metrics through an MCP server that runs PromQL range queries."""

    def __init__(self, manager: McpConnectionManager, binding: Binding) -> None:
        super().__init__(manager, binding)
        self.catalog = dict(DEFAULT_PROMQL_CATALOG)
        self.catalog.update({key: PromQuery(expr, unit) for key, (expr, unit) in binding.promql.items()})

    def available_metrics(self, service: str) -> list[str]:
        return sorted(metric for (svc, metric) in self.catalog if svc == service)

    def query(self, service: str, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries:
        entry = self.catalog.get((service, metric))
        if entry is None:
            raise ToolError(f"no query defined for {service}/{metric}; available for {service}: {self.available_metrics(service)}")
        reply = self._call(
            "query",
            {
                "query": entry.expr,
                # Unix seconds formatted exactly as the direct backend sends them, so both return the same points:
                # with ISO timestamps Prometheus truncated to the millisecond where the direct backend rounded, and
                # every point came back 1 ms apart (found against a real Prometheus).
                "start": f"{window.start.timestamp():.3f}",
                "end": f"{window.end.timestamp():.3f}",
                "step": f"{max(1, step_seconds)}s",
            },
        )
        if not isinstance(reply, dict) or not isinstance(reply.get("result"), list):
            raise ToolError(f"MCP {self.binding.server}: a range query reply needs a 'result' list")
        if reply.get("resultType") not in (None, "matrix"):
            raise ToolError(f"MCP {self.binding.server}: expected a matrix result, got {reply.get('resultType')!r}")
        return series_from_prometheus(service, metric, entry.unit, reply["result"])


class McpMetrics(_McpBackend):
    """Metrics through a server implementing this project's contract.

    available_metrics(service) -> {"metrics": [name, ...]}
    query(service, metric, start, end, step_seconds) -> {"unit": str, "points": [[iso_timestamp, value], ...]}
    """

    def available_metrics(self, service: str) -> list[str]:
        reply = self._call("available_metrics", {"service": service})
        names = reply.get("metrics") if isinstance(reply, dict) else None
        if not (isinstance(names, list) and all(isinstance(n, str) for n in names)):
            raise ToolError(f"MCP {self.binding.server}: available_metrics must return {{'metrics': [names]}}")
        return sorted(names)

    def query(self, service: str, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries:
        reply = self._call(
            "query",
            {"service": service, "metric": metric, "start": window.start.isoformat(), "end": window.end.isoformat(), "step_seconds": step_seconds},
        )
        try:
            points = [MetricPoint(timestamp=_utc(ts), value=float(value)) for ts, value in reply["points"]]
            return MetricSeries(service=service, metric=metric, unit=str(reply.get("unit", ""))[:20], points=points)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ToolError(f"MCP {self.binding.server}: a metrics query reply did not have the expected shape") from exc


class McpLogs(_McpBackend):
    """search(service, query, start, end, limit) -> {"lines": [{"timestamp", "service", "level", "message"}, ...]}"""

    def search(self, service: str, query: str, window: TimeWindow, limit: int = 100) -> list[LogLine]:
        reply = self._call(
            "search",
            {"service": service, "query": query, "start": window.start.isoformat(), "end": window.end.isoformat(), "limit": limit},
        )
        try:
            lines = [
                LogLine(
                    timestamp=_utc(item["timestamp"]),
                    service=str(item.get("service", service)),
                    level=str(item.get("level", "INFO"))[:10],
                    message=str(item["message"])[:MAX_TEXT_CHARS],
                )
                for item in reply["lines"][:MAX_LOG_LINES]
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ToolError(f"MCP {self.binding.server}: a log search reply did not have the expected shape") from exc
        # The server's own filtering is not trusted to be right: keep only what was asked for.
        lines = [line for line in lines if line.service == service and window.contains(line.timestamp)]
        return sorted(lines, key=lambda line: line.timestamp)[-max(1, limit):]


class McpDeployHistory(_McpBackend):
    """recent(service, start, end) -> {"deploys": [{"service", "ref", "deployed_at", "deployed_by", "kind", "summary"}, ...]}"""

    def recent(self, service: str, window: TimeWindow) -> list[DeployRecord]:
        reply = self._call("recent", {"service": service, "start": window.start.isoformat(), "end": window.end.isoformat()})
        try:
            deploys = [
                DeployRecord(
                    service=str(item.get("service", service)),
                    ref=str(item["ref"])[:200],
                    deployed_at=_utc(item["deployed_at"]),
                    deployed_by=str(item.get("deployed_by", ""))[:200],
                    kind=str(item.get("kind", "code")) if item.get("kind") in {"code", "config", "rollback"} else "code",
                    summary=str(item.get("summary", ""))[:MAX_TEXT_CHARS],
                )
                for item in reply["deploys"]
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ToolError(f"MCP {self.binding.server}: a deploy history reply did not have the expected shape") from exc
        return [d for d in deploys if d.service == service and window.contains(d.deployed_at)]


def _utc(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
