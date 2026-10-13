"""Open WebUI Pipe that submits work to Agent Manager and waits for its result."""

import asyncio
import inspect
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        AGENT_MANAGER_URL: str = Field(
            default="http://agent-manager:8000",
            description="Agent Manager base URL reachable from the Open WebUI container.",
        )
        AGENT_MANAGER_API_TOKEN: str = Field(
            default="",
            description="Bearer token configured in the Agent Manager .env file.",
        )
        FORGEJO_REPOSITORY: str = Field(
            default="",
            description="Required repository identifier, for example owner/project.",
        )
        BASE_REF: str = Field(default="main", description="Repository base ref.")
        WORKER_ROLE: str = Field(default="coder", description="Worker-pool role.")
        POLL_INTERVAL_SECONDS: float = Field(default=2.0, ge=0.5, le=30.0)
        RUN_TIMEOUT_SECONDS: int = Field(default=900, ge=30, le=7200)
        MAX_GOAL_CHARACTERS: int = Field(default=18000, ge=100, le=20000)

    def __init__(self) -> None:
        self.valves = self.Valves()
        self.type = "manifold"
        self.id = "agent-manager"
        self.name = "Agent Manager"

    def pipes(self) -> list[dict[str, str]]:
        return [{"id": "worker", "name": "Worker (queued task)"}]

    @staticmethod
    def _latest_user_message(body: dict[str, Any]) -> str:
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Open WebUI request did not include a messages list.")

        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = "\n".join(
                    block.get("text", "").strip()
                    for block in content
                    if isinstance(block, dict)
                    and isinstance(block.get("text"), str)
                    and block.get("text", "").strip()
                )
            else:
                text = ""
            if text:
                return text

        raise ValueError("Could not find a text prompt in the latest user message.")

    @staticmethod
    async def _emit_status(
        emitter: Any, description: str, *, done: bool = False
    ) -> None:
        if not callable(emitter):
            return
        try:
            result = emitter(
                {
                    "type": "status",
                    "data": {"description": description, "done": done},
                }
            )
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Status rendering is optional; it must not prevent the run/result flow.
            pass

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            if isinstance(detail, dict):
                return str(detail.get("message") or detail.get("code") or detail)
            return str(detail)
        except (ValueError, TypeError):
            return response.text[:1000] or f"HTTP {response.status_code}"

    @staticmethod
    def _render_result(run: dict[str, Any]) -> str:
        run_id = run.get("id", "unknown")
        metadata = run.get("metadata") or {}
        result = metadata.get("result") or {}
        worker = metadata.get("worker") or {}
        worker_id = result.get("worker_id") or worker.get("worker_id")
        worker_text = f" on worker `{worker_id}`" if worker_id else ""

        if run.get("status") == "completed":
            lines = [
                f"**Worker run completed{worker_text}.**",
                f"Run ID: `{run_id}`",
            ]
            stdout = result.get("stdout")
            stderr = result.get("stderr")
            if stdout:
                lines.extend(["", "### Worker output", str(stdout).rstrip()])
            else:
                lines.extend(
                    ["", "The worker completed successfully with no stdout output."]
                )
            if stderr:
                lines.extend(["", "### Worker stderr", str(stderr).rstrip()])
            if result.get("script"):
                lines.extend(["", f"Generated script on worker: `{result['script']}`"])
            return "\n".join(lines)

        if run.get("status") == "cancelled":
            return f"**Worker run cancelled.**\nRun ID: `{run_id}`"

        detail = (
            result.get("error")
            or run.get("error")
            or result.get("stderr")
            or "Worker failed without details."
        )
        lines = [
            f"**Worker run failed{worker_text}.**",
            f"Run ID: `{run_id}`",
            "",
            str(detail).rstrip(),
        ]
        if result.get("stdout"):
            lines.extend(
                ["", "### Partial worker output", str(result["stdout"]).rstrip()]
            )
        return "\n".join(lines)

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Any = None,
    ) -> str:
        del __user__  # Authentication to Agent Manager uses the server-side valve.
        try:
            goal = self._latest_user_message(body)
        except ValueError as exc:
            return f"Could not submit the worker request: {exc}"

        if len(goal) > self.valves.MAX_GOAL_CHARACTERS:
            return (
                "Could not submit the worker request: the latest user message is "
                f"longer than {self.valves.MAX_GOAL_CHARACTERS} characters."
            )
        if not self.valves.FORGEJO_REPOSITORY.strip():
            return (
                "Could not submit the worker request: configure FORGEJO_REPOSITORY "
                "in this Pipe's valves."
            )

        token = self.valves.AGENT_MANAGER_API_TOKEN.strip()
        if not token:
            return (
                "Could not submit the worker request: configure the Agent Manager "
                "API token in this Pipe's valves."
            )

        base_url = self.valves.AGENT_MANAGER_URL.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"}
        request_body = {
            "goal": goal,
            "forgejo_repository": self.valves.FORGEJO_REPOSITORY.strip(),
            "base_ref": self.valves.BASE_REF,
            "model": body.get("model"),
            "metadata": {
                "worker_role": self.valves.WORKER_ROLE,
                "operation": "create-python-script",
            },
        }
        timeout = httpx.Timeout(30.0, connect=10.0)
        await self._emit_status(__event_emitter__, "Submitting request to Agent Manager…")

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    f"{base_url}/v1/runs", headers=headers, json=request_body
                )
                response.raise_for_status()
                run = response.json()
            except httpx.HTTPStatusError as exc:
                return (
                    "Agent Manager rejected the request "
                    f"(HTTP {exc.response.status_code}): {self._error_detail(exc.response)}"
                )
            except (httpx.RequestError, ValueError) as exc:
                return f"Could not reach Agent Manager to submit the request: {exc}"

            run_id = run.get("id")
            if not isinstance(run_id, str) or not run_id:
                return "Agent Manager accepted an unexpected response without a run ID."

            start_time = time.monotonic()
            deadline = start_time + self.valves.RUN_TIMEOUT_SECONDS
            last_status: str | None = None
            last_status_emit = 0.0
            await self._emit_status(
                __event_emitter__, f"Run `{run_id}` queued; waiting for a worker…"
            )

            while time.monotonic() < deadline:
                try:
                    response = await client.get(
                        f"{base_url}/v1/runs/{run_id}", headers=headers
                    )
                    response.raise_for_status()
                    run = response.json()
                except httpx.RequestError:
                    now = time.monotonic()
                    if now - last_status_emit >= 15:
                        await self._emit_status(
                            __event_emitter__,
                            f"Run `{run_id}` is still active; retrying the status check…",
                        )
                        last_status_emit = now
                    await asyncio.sleep(self.valves.POLL_INTERVAL_SECONDS)
                    continue
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code >= 500:
                        await asyncio.sleep(self.valves.POLL_INTERVAL_SECONDS)
                        continue
                    return (
                        f"Run `{run_id}` was submitted, but its status could not be read "
                        f"(HTTP {exc.response.status_code}): {self._error_detail(exc.response)}"
                    )
                except ValueError:
                    await asyncio.sleep(self.valves.POLL_INTERVAL_SECONDS)
                    continue

                state = run.get("status", "unknown")
                now = time.monotonic()
                if state != last_status:
                    if state == "queued":
                        description = f"Run `{run_id}` is queued; waiting for a worker…"
                    elif state == "running":
                        worker = (run.get("metadata") or {}).get("worker") or {}
                        worker_id = worker.get("worker_id")
                        who = f" `{worker_id}`" if worker_id else ""
                        description = f"Worker{who} is processing run `{run_id}`…"
                    else:
                        description = f"Run `{run_id}` status: {state}."
                    await self._emit_status(__event_emitter__, description)
                    last_status = state
                    last_status_emit = now

                if state in {"completed", "failed", "cancelled"}:
                    elapsed = int(now - start_time)
                    await self._emit_status(
                        __event_emitter__,
                        f"Worker run {state} after {elapsed}s.",
                        done=True,
                    )
                    return self._render_result(run)

                if now - last_status_emit >= 20:
                    await self._emit_status(
                        __event_emitter__,
                        f"Run `{run_id}` is still {state} ({int(now - start_time)}s elapsed)…",
                    )
                    last_status_emit = now
                await asyncio.sleep(self.valves.POLL_INTERVAL_SECONDS)

        await self._emit_status(
            __event_emitter__,
            f"Run `{run_id}` is still processing. The worker continues in the background.",
            done=True,
        )
        return (
            f"Run `{run_id}` is still processing after "
            f"{self.valves.RUN_TIMEOUT_SECONDS}s. The worker continues in the background; "
            "submit the run ID to your administrator to retrieve its status."
        )
