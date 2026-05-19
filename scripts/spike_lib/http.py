"""HTTP helpers — stdlib only, no extra deps."""
from __future__ import annotations

import json


def post_json(
    url: str, payload: dict, timeout: float
) -> tuple[int, dict | None, str | None]:
    """POST a JSON payload. Returns (http_status, parsed_body, error_str)."""
    import urllib.error
    import urllib.request

    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "dogfood_g4_spike/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = ""
    except TimeoutError:
        return 0, None, "timeout"
    except OSError as exc:
        return 0, None, f"network_error:{exc}"

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return status, None, f"invalid_json:{body_text[:120]}"

    return status, body, None


def get_url(url: str, timeout: float = 5.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "dogfood_g4_spike/1.0"}),
            timeout=timeout,
        ) as resp:
            return resp.getcode() < 500
    except Exception:
        return False


def build_message_send(text: str, message_id: str, rpc_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": message_id,
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def extract_reply(body: dict) -> tuple[str | None, str | None]:
    """Extract narration text from a JSON-RPC ``message/send`` response.

    Per A2A spec v0.2.0, ``result`` may be a Message envelope
    (``kind="message"`` with ``parts``) OR a Task envelope
    (``kind="task"`` with ``id`` for follow-up polling). This helper
    handles Message; callers that need Task-mode follow-up should
    use ``extract_task_id`` and poll ``/a2a/tasks/{id}`` separately.

    Returns ``(text, error)``. For a Task envelope, returns
    ``(None, "task_envelope:<task_id>")`` so the caller can branch on
    the error prefix and switch to polling.
    """
    if "error" in body:
        err = body["error"]
        return None, f"jsonrpc_error({err.get('code','?')}): {err.get('message','unknown')}"
    result = body.get("result")
    if result is None:
        return None, "missing result in response"
    # A2A spec v0.2.0 discriminator
    if result.get("kind") == "task":
        task_id = result.get("id") or ""
        return None, f"task_envelope:{task_id}"
    parts = result.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            t = part.get("text", "")
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks), None


def extract_task_id(body: dict) -> str | None:
    """Return the Task envelope's ``id`` field, or ``None`` if the
    response is a Message envelope (= no follow-up polling needed).

    Per A2A spec v0.2.0, the caller MUST inspect ``result.kind`` to
    decide whether to consume ``parts`` directly or follow up with
    ``GET /a2a/tasks/{id}``. This helper centralises the
    Message-vs-Task discrimination.
    """
    result = body.get("result") or {}
    if not isinstance(result, dict):
        return None
    if result.get("kind") != "task":
        return None
    task_id = result.get("id")
    return task_id if isinstance(task_id, str) and task_id else None


def poll_task(
    base_url: str,
    task_id: str,
    *,
    deadline_s: float = 600.0,
    poll_interval_s: float = 1.0,
    timeout_s: float = 5.0,
) -> tuple[str | None, str | None]:
    """Poll ``GET /a2a/tasks/{task_id}`` until the task reaches a
    terminal state (``completed`` / ``failed``), or the deadline fires.

    ``base_url`` is the Reyn server root (e.g. ``http://localhost:8243``).
    Returns ``(result_text, error)``. ``result_text`` is the harvested
    narration on ``completed``; ``error`` is non-None on ``failed`` or
    deadline expiry.
    """
    import time as _time
    import urllib.error
    import urllib.request

    deadline = _time.monotonic() + deadline_s
    url = f"{base_url.rstrip('/')}/a2a/tasks/{task_id}"
    last_status = "unknown"
    while True:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "dogfood_a2a_poll/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, f"poll_network_error:{exc}"
        except json.JSONDecodeError as exc:
            return None, f"poll_invalid_json:{exc}"
        last_status = body.get("status") or last_status
        if last_status == "completed":
            return body.get("result") or "", None
        if last_status == "failed":
            return None, f"task_failed:{body.get('error', 'unknown')}"
        if _time.monotonic() >= deadline:
            return None, f"poll_deadline_expired:status={last_status}"
        _time.sleep(poll_interval_s)
