"""Keeps connections to the manifest's MCP servers and makes synchronous, bounded calls to them.

The MCP SDK is async; agents run on worker threads. One background event loop holds a persistent session per
server (a stdio server takes about a second to start, too slow to start per call), and `call()` hands it a
coroutine and waits for the answer.

What every call gets, whatever the server does:
- our own timeout. The SDK's read timeout was found not to apply to every transport (it did not stop a slow
  tool on an in-process server), so the wait here is enforced by `asyncio.wait_for`;
- `is_error` checked: a failing MCP tool returns an error result rather than raising, and it must not be read
  as data;
- a size cap on the result;
- a circuit breaker per server, and a fresh connection after a transport failure.
Every failure is a `ToolError`, which the agents already treat as "this evidence could not be collected".

A stdio server is started with only PATH (and SYSTEMROOT on Windows) plus the variables its manifest entry names,
and in an empty working directory of its own. The empty directory matters: the community Prometheus MCP server
loads a `.env` from its working directory on start, which, started from this repository, would hand it this
project's secrets.
"""
import asyncio
import json
import logging
import os
import tempfile
import threading
from contextlib import AsyncExitStack
from typing import Any

from app.adapters.llm_router import CircuitBreaker
from app.mcp.manifest import ServerSpec
from app.tools.base import ToolError

logger = logging.getLogger("app.mcp")

_BASE_ENV = ("PATH", "SYSTEMROOT")  # what a process needs to start at all (SYSTEMROOT: Windows networking)
MAX_ERROR_CHARS = 300


class McpConnectionManager:
    def __init__(self, servers: dict[str, ServerSpec], targets: dict[str, Any] | None = None) -> None:
        """`targets` replaces a server's transport with an in-process server object; for tests only."""
        self.servers = dict(servers)
        self._targets = dict(targets or {})
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-loop", daemon=True)
        self._thread.start()
        self._clients: dict[str, Any] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._breakers = {name: CircuitBreaker(threshold=3, cooldown_seconds=30.0) for name in self.servers}
        self._workdir = tempfile.mkdtemp(prefix="incidentflow-mcp-")  # empty: nothing for a server to pick up
        self.warnings: list[str] = []

    # ------------------------------------------------------------------------- public

    def call(self, server: str, tool: str, arguments: dict[str, Any], max_bytes: int = 500_000) -> Any:
        """Call one tool and return its result as parsed JSON. Raises ToolError for any failure."""
        spec = self._spec(server)
        if not self._breakers[server].allow():
            raise ToolError(f"MCP server '{server}' is skipped for now: it has been failing")
        future = asyncio.run_coroutine_threadsafe(self._call(spec, tool, arguments, max_bytes), self._loop)
        try:
            return future.result(timeout=spec.connect_timeout_seconds + spec.call_timeout_seconds + 5)
        except ToolError:
            raise
        except Exception as exc:  # a timeout of the wait itself, or anything unexpected
            future.cancel()
            self._breakers[server].failure()
            raise ToolError(f"MCP {server}.{tool} failed: {type(exc).__name__}: {str(exc)[:MAX_ERROR_CHARS]}") from exc

    def tool_names(self, server: str) -> set[str]:
        """The tool names a server offers (only the names: its descriptions are never used)."""
        spec = self._spec(server)
        future = asyncio.run_coroutine_threadsafe(self._list(spec), self._loop)
        try:
            return future.result(timeout=spec.connect_timeout_seconds + spec.call_timeout_seconds + 5)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"MCP server '{server}' could not be reached: {type(exc).__name__}: {str(exc)[:MAX_ERROR_CHARS]}") from exc

    def check_bindings(self, wanted: dict[str, set[str]]) -> None:
        """At startup: each server is reachable and offers the tools the manifest binds. Raises ToolError."""
        for server, tools in wanted.items():
            offered = self.tool_names(server)
            missing = sorted(tools - offered)
            if missing:
                raise ToolError(f"MCP server '{server}' does not offer {missing} (it offers {sorted(offered)[:20]})")

    def close(self) -> None:
        if not self._loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._close_all(), self._loop)
        try:
            future.result(timeout=15)
        except Exception:
            logger.exception("mcp-close-failed")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # -------------------------------------------------------------------- in the loop

    def _spec(self, server: str) -> ServerSpec:
        if server not in self.servers:
            raise ToolError(f"unknown MCP server '{server}'")
        return self.servers[server]

    async def _client(self, spec: ServerSpec):
        if spec.name in self._clients:
            return self._clients[spec.name]
        from mcp import Client

        stack = AsyncExitStack()
        try:
            client = await asyncio.wait_for(
                stack.enter_async_context(Client(self._target(spec))), timeout=spec.connect_timeout_seconds
            )
        except BaseException:
            await stack.aclose()
            raise
        self._clients[spec.name], self._stacks[spec.name] = client, stack
        return client

    def _target(self, spec: ServerSpec):
        if spec.name in self._targets:
            return self._targets[spec.name]
        if spec.transport == "http":
            return spec.url
        from mcp import StdioServerParameters

        env = {name: os.environ[name] for name in (*_BASE_ENV, *spec.env_passthrough) if name in os.environ}
        return StdioServerParameters(command=spec.command[0], args=list(spec.command[1:]), env=env, cwd=self._workdir)

    async def _call(self, spec: ServerSpec, tool: str, arguments: dict[str, Any], max_bytes: int) -> Any:
        breaker = self._breakers[spec.name]
        try:
            client = await self._client(spec)
            result = await asyncio.wait_for(
                client.call_tool(tool, arguments, read_timeout_seconds=spec.call_timeout_seconds),
                timeout=spec.call_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            breaker.failure()
            await self._drop(spec.name)  # a stuck session is not reused
            raise ToolError(f"MCP {spec.name}.{tool} did not answer within {spec.call_timeout_seconds:g}s") from exc
        except Exception as exc:
            breaker.failure()
            await self._drop(spec.name)
            raise ToolError(f"MCP {spec.name}.{tool} failed: {type(exc).__name__}: {str(exc)[:MAX_ERROR_CHARS]}") from exc

        text = "".join(getattr(block, "text", "") for block in (result.content or []))
        if result.is_error:
            # The tool ran and reported a failure; the server itself is fine.
            breaker.success()
            raise ToolError(f"MCP {spec.name}.{tool} returned an error: {text[:MAX_ERROR_CHARS]}")
        breaker.success()
        if result.structured_content is not None:
            payload = result.structured_content
            size = len(json.dumps(payload))
        else:
            size = len(text.encode("utf-8"))
            if size <= max_bytes:
                try:
                    payload = json.loads(text)
                except ValueError as exc:
                    raise ToolError(f"MCP {spec.name}.{tool} did not return JSON") from exc
        if size > max_bytes:
            raise ToolError(f"MCP {spec.name}.{tool} returned {size} bytes, over the {max_bytes}-byte limit")
        return payload

    async def _list(self, spec: ServerSpec) -> set[str]:
        client = await self._client(spec)
        listed = await asyncio.wait_for(client.list_tools(), timeout=spec.call_timeout_seconds)
        for tool in listed.tools:
            hint = getattr(tool.annotations, "read_only_hint", None) if tool.annotations else None
            if hint is not True:
                self.warnings.append(f"{spec.name}.{tool.name} does not declare itself read-only (a hint, not trusted either way)")
        return {tool.name for tool in listed.tools}

    async def _drop(self, name: str) -> None:
        self._clients.pop(name, None)
        stack = self._stacks.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException:  # a broken transport may fail to close cleanly; it is being discarded anyway
                logger.debug("mcp-close-error", extra={"server": name})

    async def _close_all(self) -> None:
        for name in list(self._clients):
            await self._drop(name)
