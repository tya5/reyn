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
    if "error" in body:
        err = body["error"]
        return None, f"jsonrpc_error({err.get('code','?')}): {err.get('message','unknown')}"
    result = body.get("result")
    if result is None:
        return None, "missing result in response"
    parts = result.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text":
            t = part.get("text", "")
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks), None
