from __future__ import annotations

import httpx


def configured_tokens(push_config: dict) -> list[str]:
    tokens = push_config.get("tokens")
    if not isinstance(tokens, list):
        raise ValueError("PushPlus tokens must be a non-empty list")
    return [str(token) for token in tokens if str(token).strip()]


def send_pushplus(title: str, content: str, config: dict) -> None:
    push_config = config["pushplus"]
    if not push_config.get("enabled"):
        return
    tokens = configured_tokens(push_config)
    if not tokens:
        raise ValueError("PushPlus enabled but tokens is empty")

    topic = push_config.get("topic")
    with httpx.Client(timeout=float(push_config.get("timeout_seconds", 10))) as client:
        for token in tokens:
            payload = {
                "token": token,
                "title": title,
                "content": content,
                "template": push_config.get("template", "markdown"),
            }
            if topic:
                payload["topic"] = topic
            response = client.post("https://www.pushplus.plus/send", json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError(f"PushPlus failed: {data}")
