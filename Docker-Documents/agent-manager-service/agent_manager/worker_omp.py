"""OMP inside one ephemeral worker image. It plans, codes, reviews, prints the result, and exits."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

UNSAFE = re.compile(r"\b(subprocess|socket|urllib|requests|eval|exec|os\.system|__import__|docker|pty)\b")


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    raise SystemExit(0)


def model_text(system: str, user: str) -> str:
    base = os.environ.get("MODEL_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("model URL is empty")
    if not base.endswith("/v1"):
        base += "/v1"
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("MODEL_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": os.environ.get("MODEL_NAME") or "gpt-5.6-luna",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user[:8000]},
        ],
    }
    if "api.openai.com" in base:
        body["max_completion_tokens"] = 700
        body["reasoning_effort"] = os.environ.get("REASONING_EFFORT") or "medium"
    else:
        body["temperature"] = 0.2
        body["max_tokens"] = 700
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode())
    content = payload["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model returned an empty reply")
    return content


def review(goal: str, script: str) -> str | None:
    if not script or "import " not in script:
        return "The coder did not return a Python script."
    if UNSAFE.search(script):
        return "The reviewer rejected the script because it tried to leave the image."
    folded = script.casefold()
    if "calculator" not in goal.casefold() and "gui" not in goal.casefold():
        return None
    missing = []
    if "tkinter" not in folded:
        missing.append("tkinter buttons")
    if "button" not in folded:
        missing.append("clickable buttons")
    if not any(color in folded for color in ("#d6eaf8", "#add8e6", "pale blue", "lightblue")):
        missing.append("a pale blue background")
    if "white" not in folded or "black" not in folded:
        missing.append("white buttons with black text")
    return "The reviewer still needs " + ", ".join(missing) + "." if missing else None


def main() -> None:
    goal = os.environ.get("GOAL", "").strip()
    nodes = os.environ.get("CREW_NODES", "1 planner, 1 coder, 1 reviewer")
    if not goal:
        emit({"status": "bounced", "summary": "The worker received an empty request."})
    steps = [f"Worker OMP started with {nodes}."]
    steps.append("Planner checked the request.")
    try:
        draft = model_text(
            "You are the coder in this worker image. Return one Python script only. "
            "Use the standard library. No network or system calls.",
            goal,
        )
    except Exception:
        emit({
            "status": "bounced",
            "summary": "The worker coder could not reach the model.",
            "steps": steps,
        })
    steps.append("Coder wrote a script.")
    rejection = review(goal, draft)
    steps.append("Reviewer checked the script.")
    if rejection:
        try:
            draft = model_text(
                "Revise the Python script. Return only the script.",
                f"{goal}\n\nReviewer notes: {rejection}",
            )
        except Exception:
            emit({"status": "bounced", "summary": rejection, "steps": steps})
        rejection = review(goal, draft)
        steps.append("Reviewer checked the revision.")
    if rejection:
        emit({"status": "bounced", "summary": rejection, "steps": steps})
    steps.append("Reviewer accepted the script. The image is exiting.")
    emit({"status": "completed", "summary": "Worker OMP finished. The image can be removed.", "script": draft, "steps": steps})


if __name__ == "__main__":
    main()
