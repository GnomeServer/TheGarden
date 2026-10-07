"""
title: Agent Manager Coder
version: 1.0.0
description: Submit pooled create-python-script runs and report worker results.
"""

import asyncio
import re
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        AGENT_MANAGER_URL: str = Field(
            default="http://agent-manager:8000",
            description="Direct internal Manager API, not the dashboard or a browser URL.",
        )
        AGENT_MANAGER_API_KEY: str = Field(
            default="",
            description="Dedicated AGENT_MANAGER_OPENWEBUI_TOKEN; never a bootstrap admin token.",
            json_schema_extra={"input": {"type": "password"}},
        )
        FORGEJO_REPOSITORY: str = Field(
            default="", description="Required administrator-approved owner/repository."
        )
        MODEL_NAME: str = Field(default="luna", description="Requested worker model name.")
        POLL_INTERVAL_SECONDS: float = Field(default=2, ge=0.5, le=30)
        MAX_WAIT_SECONDS: float = Field(
            default=300, ge=5, le=1800,
            description="Polling deadline after submission; expiry does not cancel the run.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
        __task__: Optional[str] = None,
    ) -> str:
        # Snapshot per call: concurrent chats must not share mutable run state.
        valves = self.valves
        token = valves.AGENT_MANAGER_API_KEY.strip()
        run_id = None
        submission_attempted = False
        final_status = "Failed: run was not submitted."

        def safe(value) -> str:
            text = str(value)
            return text.replace(token, "[redacted]") if token else text

        async def status(description: str, done: bool = False):
            if __event_emitter__ is not None:
                try:
                    await __event_emitter__({
                        "type": "status",
                        "data": {"description": safe(description), "done": done},
                    })
                except Exception:
                    # A disconnected UI must not change job execution or cause a resubmit.
                    pass

        def recover(reason: str) -> str:
            if run_id:
                return safe(
                    f"{reason}\n\nRun ID: {run_id}\n"
                    "The run was not cancelled by this Pipe; its current outcome is unconfirmed. "
                    "Do not resubmit just to retrieve the result. Ask an operator to find this run "
                    "in the Agent Manager dashboard, or read "
                    f"GET /v1/runs/{quote(run_id, safe='')} through the authenticated Manager API."
                )
            return (
                f"{reason}\n\nSubmission outcome is unknown; a run may already exist. "
                "No automatic retry was made. Do not resend this goal until an operator checks "
                "the Agent Manager dashboard for the matching goal, requester, and submission time "
                "and confirms whether a run was created."
            )

        try:
            # Open WebUI can route title/tag/follow-up tasks to the selected Pipe.
            if __task__:
                final_status = "No run submitted: background chat task."
                return "Agent Manager Coder submits jobs only for direct chat messages."

            base_url = valves.AGENT_MANAGER_URL.strip().rstrip("/")
            parsed_url = urlsplit(base_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
                or parsed_url.path not in {"", "/"}
            ):
                return "Not submitted: ask an administrator to configure the direct Agent Manager API origin."
            if not token or not valves.FORGEJO_REPOSITORY.strip() or not valves.MODEL_NAME.strip():
                return (
                    "Not submitted: an administrator must configure the Pipe's dedicated run-only "
                    "credential, FORGEJO_REPOSITORY, and MODEL_NAME in Admin Panel → Functions → Valves."
                )
            goal = self._last_user_text(body)
            if not goal:
                return "Not submitted: the latest user message must contain a non-empty textual task."
            requester = __user__ if isinstance(__user__, dict) else {}
            payload = {
                "goal": goal,
                "forgejo_repository": valves.FORGEJO_REPOSITORY.strip(),
                "base_ref": "main",
                "model": valves.MODEL_NAME.strip(),
                "metadata": {
                    "worker_role": "coder",
                    "operation": "create-python-script",
                    "source": "open-webui",
                    # Service-supplied attribution, not verified Forgejo identity.
                    "requester": {key: requester.get(key) for key in ("id", "name")},
                },
            }
            await status("Submitting a pooled coder run to Agent Manager…")
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=15, follow_redirects=False, trust_env=False,
            ) as client:
                submission_attempted = True
                try:
                    response = await asyncio.wait_for(
                        client.post(f"{base_url}/v1/runs", json=payload), timeout=15,
                    )
                except (httpx.HTTPError, asyncio.TimeoutError):
                    final_status = "Submission unconfirmed; operator check required."
                    return recover("Agent Manager did not return a confirmed submission response.")

                if response.status_code in {401, 403}:
                    final_status = "Failed: Manager authorization needs administrator attention."
                    return "Not submitted: ask an administrator to check the Pipe's run-only Manager credential."
                if response.status_code == 503 and self._error_code(response) == "NO_WORKER_AVAILABLE":
                    final_status = "Failed: no enabled coder worker is available."
                    return (
                        "Not submitted: NO_WORKER_AVAILABLE (503). No enabled coder worker answered "
                        "the Manager admission check. Ask an operator to check worker health, NATS "
                        "connectivity, and ALLOW_GENERATED_CODE=1 on an approved disposable worker. "
                        "Only try again after a worker is available."
                    )
                if response.status_code in {400, 404, 405, 422, 429}:
                    final_status = "Failed: Manager rejected the submission."
                    return (
                        f"Not submitted: Agent Manager rejected the request (HTTP {response.status_code}). "
                        "Ask an administrator to check the Pipe configuration and Manager availability."
                    )
                if not 200 <= response.status_code < 300:
                    final_status = "Submission unconfirmed; operator check required."
                    return recover(f"Agent Manager submission returned HTTP {response.status_code}.")
                record = self._record(response)
                if record is None:
                    final_status = "Submission unconfirmed; operator check required."
                    return recover("Agent Manager returned an unreadable submission response.")
                run_id = record["id"]
                await status(f"Submitted run {run_id}; waiting for worker completion.")
                deadline = time.monotonic() + valves.MAX_WAIT_SECONDS
                previous_status = None
                while True:
                    state = record.get("status")
                    if state in {"completed", "failed", "cancelled"}:
                        result_text, failed = self._result(record)
                        final_status = f"{'Failed' if failed else 'Completed'}: run {run_id} (Manager-reported)."
                        return safe(result_text)
                    if state not in {"queued", "running", "cancel_requested"}:
                        final_status = f"Run {run_id}: result unconfirmed."
                        return recover("Agent Manager returned an unrecognized run status; completion is not confirmed.")
                    if state != previous_status:
                        await status(f"Run {run_id}: {state}.")
                        previous_status = state
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        final_status = f"Stopped waiting for run {run_id}; not cancelled."
                        return recover("Polling time limit reached before a terminal result was observed.")
                    await asyncio.sleep(min(valves.POLL_INTERVAL_SECONDS, remaining))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        final_status = f"Stopped waiting for run {run_id}; not cancelled."
                        return recover("Polling time limit reached before a terminal result was observed.")
                    try:
                        response = await asyncio.wait_for(
                            client.get(f"{base_url}/v1/runs/{quote(run_id, safe='')}", timeout=min(15, remaining)),
                            timeout=remaining,
                        )
                    except (httpx.HTTPError, asyncio.TimeoutError):
                        final_status = f"Stopped waiting for run {run_id}; not cancelled."
                        return recover("Could not retrieve the run result before the request deadline or connection failure.")
                    if response.status_code in {401, 403}:
                        final_status = "Failed to read result: administrator attention required."
                        return recover("Ask an administrator to check the Pipe's run-only Manager credential.")
                    if response.status_code != 200:
                        final_status = f"Failed to read result for run {run_id}."
                        return recover(f"Could not read the run result (HTTP {response.status_code}).")
                    record = self._record(response)
                    if record is None or record["id"] != run_id:
                        final_status = f"Failed to read result for run {run_id}."
                        return recover("Agent Manager returned an unreadable or mismatched run result.")
        except asyncio.CancelledError:
            final_status = (
                f"Stopped waiting for run {run_id}; not cancelled by this Pipe."
                if run_id else "Chat interrupted; check Manager before resubmitting."
            )
            raise
        except Exception:
            # Never show exception text: HTTP errors can contain credentials or response bodies.
            final_status = "Failed to report the run; administrator attention required."
            if submission_attempted:
                return recover("The Pipe could not finish reporting the Manager response.")
            return "Not submitted: the Pipe could not prepare the request. Ask an administrator to check its configuration."
        finally:
            await status(final_status, done=True)

    @staticmethod
    def _last_user_text(body: dict) -> str:
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return "\n".join(
                    part["text"] for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ).strip()
            # Do not repeat an older goal when the newest message has only an attachment.
            return ""
        return ""

    @staticmethod
    def _error_code(response: httpx.Response) -> Optional[str]:
        try:
            detail = response.json().get("detail")
            return detail.get("code") if isinstance(detail, dict) else None
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _record(response: httpx.Response) -> Optional[dict]:
        try:
            record = response.json()
        except ValueError:
            return None
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"].strip():
            return None
        return record

    @staticmethod
    def _result(record: dict) -> tuple[str, bool]:
        metadata = record.get("metadata")
        result = metadata.get("result") if isinstance(metadata, dict) else None
        result = result if isinstance(result, dict) else {}
        state = record["status"]
        returncode = result.get("returncode")
        inconsistent = state == "completed" and (
            returncode not in (None, 0) or bool(result.get("error"))
            or bool(record.get("error")) or result.get("status") == "failed"
        )
        lines = [
            f"Agent Manager run: {state} (Manager-reported).",
            f"Run ID: {record['id']}",
            f"Worker: {result.get('worker_id') or 'not reported'}",
            f"Return code: {returncode if returncode is not None else 'not reported'}",
            "",
            "This reports the worker execution outcome, not independent validation of the generated code, "
            "its artifacts, or the requested task. No Forgejo commit or review is implied.",
        ]
        if inconsistent:
            lines.append("The completion record contains an execution error; do not treat this as successful execution.")
        for label, value in (
            ("stdout", result.get("stdout")),
            ("stderr", result.get("stderr")),
            ("Worker error", result.get("error")),
            ("Manager error", record.get("error")),
        ):
            text = str(value) if value is not None else ""
            if not text:
                text = "(empty)" if label in {"stdout", "stderr"} and label in result else "(not reported)"
            # Worker output is untrusted text, not live Markdown/HTML instructions.
            fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
            lines.extend(["", f"### {label}", f"{fence}text\n{text}\n{fence}"])
        return "\n".join(lines), state != "completed" or inconsistent
