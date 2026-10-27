"""Open WebUI Pipe that sends a chat turn to OMP and returns its reply."""

import asyncio
import inspect
import logging
import re
import secrets
import time
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

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
            description="Repository identifier, for example owner/project. OMP asks if this is empty.",
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
        return [{"id": "worker", "name": "OMP"}]

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
    def _extract_project_directive(text: str) -> tuple[str, str | None]:
        lines = text.splitlines()
        index = 0
        project_name: str | None = None
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                index += 1
                continue
            label, separator, value = line.partition(":")
            if not separator or label.strip().casefold() != "project":
                break
            if project_name is not None:
                raise ValueError("Specify Project only once.")
            project_name = value.strip()
            if not project_name:
                raise ValueError("Project needs a name.")
            index += 1

        if project_name is None:
            return text, None
        goal = "\n".join(lines[index:]).strip()
        if not goal:
            raise ValueError("Add task instructions after the Project line.")
        return goal, project_name

    @staticmethod
    def _format_artifacts(artifact: Any) -> str:
        if not isinstance(artifact, dict):
            return "The mission completed, but no downloadable artifact was reported."
        filename = artifact.get("filename")
        download_url = artifact.get("download_url")
        size_bytes = artifact.get("size_bytes")
        if not isinstance(filename, str) or not filename or not isinstance(download_url, str) or not download_url:
            return "The mission completed, but no downloadable artifact was reported."
        # Escape Markdown label metacharacters without modifying the signed URL.
        safe_filename = filename.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        size = f"{size_bytes:,} bytes" if isinstance(size_bytes, int) and size_bytes >= 0 else "size unknown"
        return f"Download the completed work:\n- [{safe_filename}]({download_url}) ({size})"

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
            pass

    @staticmethod
    def _message_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                block.get("text", "").strip()
                for block in content
                if isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and block.get("text", "").strip()
            ).strip()
        return ""

    @classmethod
    def _history(cls, body: dict[str, Any]) -> list[tuple[str, str]]:
        messages = body.get("messages")
        if not isinstance(messages, list):
            return []
        history: list[tuple[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = cls._message_text(message.get("content"))
            if text:
                history.append((role, text))
        return history

    @staticmethod
    def _project_answer(text: str) -> tuple[str | None, str | None]:
        stripped = text.strip()
        named = re.match(
            r"(?is)^(.+?)\s+is the project(?:\s+name)?\b(?:\s+and\s+)?(.*)$",
            stripped,
        )
        if named:
            extra = re.sub(r"(?i)^i want you to\s+", "", named.group(2).strip()).strip(" .")
            return named.group(1).strip(" .")[:120] or None, extra or None
        named = re.match(
            r"(?is)^(?:the\s+)?project(?:\s+name)?\s+is\s+(.+?)(?:\s+and\s+(.*))?$",
            stripped,
        )
        if named:
            extra = re.sub(r"(?i)^i want you to\s+", "", (named.group(2) or "").strip()).strip(" .")
            return named.group(1).strip(" .")[:120] or None, extra or None
        if len(stripped) <= 120 and "\n" not in stripped:
            return stripped, None
        return None, stripped

    @classmethod
    def _resolve_turn(
        cls, body: dict[str, Any], repository: str
    ) -> tuple[str, str | None, str]:
        history = cls._history(body)
        if not history or history[-1][0] != "user":
            raise ValueError("Open WebUI request did not include a user message.")
        latest = history[-1][1]
        previous_assistant = next(
            (text for role, text in reversed(history[:-1]) if role == "assistant"),
            "",
        )
        previous_user = next(
            (text for role, text in reversed(history[:-1]) if role == "user"),
            "",
        )
        if "What project name should this mission use?" in previous_assistant:
            project_name, extra_goal = cls._project_answer(latest)
            prior_goal = previous_user
            prior_project = None
            if previous_user:
                try:
                    prior_goal, prior_project = cls._extract_project_directive(previous_user)
                except ValueError:
                    prior_goal = previous_user
            return extra_goal or prior_goal or latest, project_name or prior_project, repository
        if "Which Forgejo repository should this mission use?" in previous_assistant:
            try:
                goal, project_name = cls._extract_project_directive(previous_user or latest)
            except ValueError:
                goal, project_name = previous_user or latest, None
            return goal, project_name, latest.strip()
        goal, project_name = cls._extract_project_directive(latest)
        return goal, project_name, repository

    @staticmethod
    def _is_status_query(text: str) -> bool:
        normalized = " ".join(text.casefold().split())
        return bool(
            re.fullmatch(
                r"(?:what(?:'s|s| is)\s+(?:the\s+)?(?:current\s+)?status(?:\s+(?:of|for)\s+.+)?|status(?:\s+(?:of|for)\s+.+)?|how(?:'s| is)\s+(?:the\s+)?(?:mission|job|it)\s+going|any\s+updates?(?:\s+on\s+.+)?)\??",
                normalized,
            )
        )

    @classmethod
    def _mission_id_from_history(
        cls, history: list[tuple[str, str]]
    ) -> str | None:
        pattern = re.compile(
            r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
            re.IGNORECASE,
        )
        for role, text in reversed(history):
            if role != "assistant":
                continue
            matches = pattern.findall(text)
            if matches:
                return matches[-1]
        return None


    @classmethod
    def _format_mission_status(
        cls, state: dict[str, Any], mission_id: str
    ) -> str:
        status = state.get("status")
        summary = state.get("summary") if isinstance(state.get("summary"), str) else ""
        if status in {"queued", "accepted"}:
            prefix = f"Mission `{mission_id}` is queued."
        elif status in {"planning", "running"}:
            worker_id = state.get("worker_id")
            suffix = f" on worker `{worker_id}`" if isinstance(worker_id, str) and worker_id else ""
            prefix = f"Mission `{mission_id}` is {status}{suffix}."
        elif status == "needs_input":
            prefix = f"Mission `{mission_id}` is waiting for clarification."
            question = state.get("question")
            if isinstance(question, str) and question.strip():
                summary = question.strip()
        elif status in {"completed", "failed", "bounced", "cancelled"}:
            prefix = f"Mission `{mission_id}` is {status}."
        else:
            prefix = f"Mission `{mission_id}` has status `{status or 'unknown'}`."
        reply = f"{prefix} {summary}".strip()
        if status == "completed":
            reply += cls._format_artifacts(state.get("artifact"))
        return reply

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

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Any = None,
        __event_call__: Any = None,
    ) -> str:
        del __user__
        history = self._history(body)
        if not history or history[-1][0] != "user":
            return "Could not send the chat turn to OMP: no latest user message was found."
        latest_message = history[-1][1]
        status_query = self._is_status_query(latest_message)
        mission_id = self._mission_id_from_history(history[:-1]) if status_query else None
        if status_query and not mission_id:
            return "I couldn't find a mission ID earlier in this chat. Open the original mission or include its ID."
        if status_query:
            goal, project_name, repository = latest_message, None, self.valves.FORGEJO_REPOSITORY.strip()
        else:
            try:
                goal, project_name, repository = self._resolve_turn(
                    body, self.valves.FORGEJO_REPOSITORY.strip()
                )
            except ValueError as exc:
                return f"Could not send the chat turn to OMP: {exc}"

        if len(goal) > self.valves.MAX_GOAL_CHARACTERS:
            return (
                "Could not send the chat turn to OMP: the latest user message is "
                f"longer than {self.valves.MAX_GOAL_CHARACTERS} characters."
            )

        token = self.valves.AGENT_MANAGER_API_TOKEN.strip()
        if not token:
            return "Could not reach OMP: configure the Agent Manager API token in this Pipe's valves."

        cancel_token = ""
        request_body: dict[str, Any] = {}
        if not status_query:
            mission_id = str(uuid.uuid4())
            cancel_token = secrets.token_urlsafe(32)
            request_body = {
                "goal": goal,
                "forgejo_repository": repository,
                "project_name": project_name,
                "model": body.get("model"),
                "mission_id": mission_id,
                "cancel_token": cancel_token,
            }
        await self._emit_status(
            __event_emitter__,
            "Checking mission status…" if status_query else "Asking OMP…",
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            try:
                if status_query:
                    response = await client.get(
                        f"{self.valves.AGENT_MANAGER_URL.rstrip('/')}/v1/omp/missions/{mission_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                else:
                    response = await client.post(
                        f"{self.valves.AGENT_MANAGER_URL.rstrip('/')}/v1/omp/turns",
                        headers={"Authorization": f"Bearer {token}"},
                        json=request_body,
                    )
                response.raise_for_status()
                reply = response.json()
            except asyncio.CancelledError:
                if not status_query:
                    await self._request_cancel(client, mission_id, cancel_token)
                raise
            except httpx.HTTPStatusError as exc:
                action = "status lookup" if status_query else "chat turn"
                return (
                    f"OMP {action} failed "
                    f"(HTTP {exc.response.status_code}): {self._error_detail(exc.response)}"
                )
            except (httpx.RequestError, ValueError) as exc:
                return f"Could not reach OMP: {exc}"

            if status_query:
                if not isinstance(reply, dict):
                    return "OMP returned an unexpected mission status."
                formatted = self._format_mission_status(reply, mission_id or "")
                await self._emit_status(__event_emitter__, formatted, done=True)
                return formatted
            if not isinstance(reply, dict):
                return "OMP returned an unexpected response."
            if reply.get("type") == "question":
                text = reply.get("text")
                return text if isinstance(text, str) and text.strip() else "OMP needs more information."
            if reply.get("type") == "mission":
                summary = reply.get("summary") if isinstance(reply.get("summary"), str) else "Mission recorded."
                returned_id = reply.get("mission_id")
                returned_token = reply.get("cancel_token")
                if isinstance(returned_id, str) and returned_id:
                    mission_id = returned_id
                    if isinstance(returned_token, str) and returned_token:
                        cancel_token = returned_token
                    try:
                        followed = await self._follow_mission(
                            client,
                            mission_id,
                            token,
                            summary,
                            project_name,
                            __event_emitter__,
                            cancel_token,
                            __event_call__,
                        )
                    except asyncio.CancelledError:
                        await self._request_cancel(client, mission_id, cancel_token)
                        raise
                    await self._emit_status(__event_emitter__, followed, done=True)
                    return followed
                project = f" for `{project_name}`" if project_name else ""
                await self._emit_status(__event_emitter__, summary, done=True)
                return f"{summary}{project}."
            return "OMP returned an unexpected response."

    async def _request_cancel(
        self, client: httpx.AsyncClient, mission_id: str, cancel_token: str
    ) -> None:
        url = (
            f"{self.valves.AGENT_MANAGER_URL.rstrip('/')}"
            f"/v1/omp/missions/{mission_id}/cancel"
        )
        headers = {"Authorization": f"Bearer {cancel_token}"}
        for _attempt in range(12):
            try:
                response = await client.post(url, headers=headers)
            except httpx.HTTPError:
                await asyncio.sleep(0.25)
                continue
            if response.status_code == 404 or response.status_code >= 500:
                await asyncio.sleep(0.25)
                continue
            if response.status_code >= 400:
                logger.error(
                    "mission cancellation failed for %s with HTTP %d",
                    mission_id,
                    response.status_code,
                )
            return
        logger.error("mission cancellation did not settle for %s", mission_id)


    async def _follow_mission(
        self,
        client: httpx.AsyncClient,
        mission_id: str,
        token: str,
        summary: str,
        project_name: str | None,
        emitter: Any,
        cancel_token: str,
        event_call: Any,
    ) -> str:
        deadline = time.monotonic() + self.valves.RUN_TIMEOUT_SECONDS
        project = f" for `{project_name}`" if project_name else ""
        headers = {"Authorization": f"Bearer {token}"}
        last_status = "unknown"
        url = f"{self.valves.AGENT_MANAGER_URL.rstrip('/')}/v1/omp/missions/{mission_id}"
        last_progress = None
        while time.monotonic() < deadline:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                state = response.json()
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(self.valves.POLL_INTERVAL_SECONDS)
                continue
            if not isinstance(state, dict):
                break
            status = state.get("status")
            done = state.get("summary") if isinstance(state.get("summary"), str) else ""
            handoff = done.startswith("OMP handed the job")
            progress = None
            if status == "queued":
                progress = done if handoff else "Mission queued; waiting for a worker."
            elif status == "running":
                progress = "Worker laptop picked up the mission; worker-local OMP is running the crew."
            if progress and progress != last_progress:
                await self._emit_status(emitter, progress)
                last_progress = progress
            last_status = status if isinstance(status, str) else "unknown"
            if status == "cancelled":
                return f"Mission `{mission_id}` was cancelled at your request."
            if status == "needs_input":
                question = state.get("question") or state.get("summary") or summary
                if not isinstance(question, str) or not question.strip():
                    question = "The reviewer reached its limit; clarify what should change."
                if not callable(event_call):
                    return (
                        f"Mission `{mission_id}` is waiting for clarification: "
                        f"{question}"
                    )
                answer = await event_call(
                    {
                        "type": "input",
                        "data": {
                            "title": "Clarification needed",
                            "message": question,
                            "placeholder": "Clarify the requirement for the reviewer",
                        },
                    }
                )
                if isinstance(answer, dict):
                    if answer.get("error"):
                        return (
                            f"Mission `{mission_id}` is still waiting for clarification: "
                            f"{question}"
                        )
                    answer = answer.get("value") or answer.get("text") or answer.get("content")
                if not isinstance(answer, str) or not answer.strip():
                    return (
                        f"Mission `{mission_id}` is still waiting for clarification: "
                        f"{question}"
                    )
                resume_url = (
                    f"{self.valves.AGENT_MANAGER_URL.rstrip('/')}"
                    f"/v1/omp/missions/{mission_id}/resume"
                )
                try:
                    resumed = await client.post(
                        resume_url,
                        headers={"Authorization": f"Bearer {cancel_token}"},
                        json={"clarification": answer.strip()},
                    )
                    resumed.raise_for_status()
                    resumed_state = resumed.json()
                except (httpx.HTTPError, ValueError) as exc:
                    return (
                        f"Could not resume mission `{mission_id}` after clarification: "
                        f"{exc}"
                    )
                if not isinstance(resumed_state, dict) or resumed_state.get("status") != "queued":
                    return (
                        f"Mission `{mission_id}` did not resume; its clarification remains pending."
                    )
                summary = (
                    resumed_state.get("summary")
                    if isinstance(resumed_state.get("summary"), str)
                    else "Clarification received; resuming."
                )
                last_status = "queued"
                deadline = time.monotonic() + self.valves.RUN_TIMEOUT_SECONDS
                continue
            if status == "bounced":
                question = state.get("question") or done or summary
                return question if isinstance(question, str) else summary
            if status in {"completed", "failed"} and not handoff:
                text = done or summary
                body = f"{text}{project}. Mission `{mission_id}`."
                evidence = state.get("evidence")
                evidence_parts = []
                if isinstance(state.get("attempts"), int):
                    evidence_parts.append(f"Attempts: {state['attempts']}.")
                if isinstance(evidence, dict):
                    artifacts = evidence.get("artifacts") or evidence.get("coder_artifacts")
                    reviewers = evidence.get("reviewers")
                    checks = evidence.get("supervisor_checks")
                    if isinstance(artifacts, list):
                        evidence_parts.append(f"Artifacts produced: {len(artifacts)}.")
                    if isinstance(reviewers, list):
                        approved = sum(
                            1 for item in reviewers
                            if isinstance(item, dict) and item.get("decision") == "approved"
                        )
                        evidence_parts.append(
                            f"Reviewer verdicts: {approved}/{len(reviewers)} approved."
                        )
                    if isinstance(checks, list):
                        evidence_parts.append(f"Supervisor checks: {len(checks)}.")
                if evidence_parts:
                    body += "\n\n" + " ".join(evidence_parts)
                if status == "completed":
                    body += self._format_artifacts(state.get("artifact"))
                return body
            await asyncio.sleep(self.valves.POLL_INTERVAL_SECONDS)
        return f"{summary}{project}. Mission `{mission_id}` (status: `{last_status}`). Polling timed out before completion."

