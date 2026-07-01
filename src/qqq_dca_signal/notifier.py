from __future__ import annotations

from urllib.parse import quote

import httpx


def configured_tokens(push_config: dict) -> list[str]:
    tokens = push_config.get("tokens")
    if not isinstance(tokens, list):
        raise ValueError("PushPlus tokens must be a non-empty list")
    return [str(token) for token in tokens if str(token).strip()]


def send_pushplus(title: str, content: str, config: dict) -> None:
    push_config = config.get("pushplus", {})
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


def configured_bark_keys(bark_config: dict) -> list[str]:
    keys = bark_config.get("keys")
    if not isinstance(keys, list):
        raise ValueError("Bark keys must be a non-empty list")
    return [str(key) for key in keys if str(key).strip()]


def send_bark(title: str, content: str, config: dict) -> None:
    bark_config = config.get("bark", {})
    if not bark_config.get("enabled"):
        return
    keys = configured_bark_keys(bark_config)
    if not keys:
        raise ValueError("Bark enabled but keys is empty")

    server_url = str(bark_config.get("server_url", "https://api.day.app")).rstrip("/")
    payload = {
        "body": content,
        "group": bark_config.get("group", "qqq-dca-signal"),
        "isArchive": "1" if bark_config.get("is_archive", True) else "0",
    }
    timeout = float(bark_config.get("timeout_seconds", 10))
    with httpx.Client(timeout=timeout) as client:
        for key in keys:
            url = f"{server_url}/{quote(key.strip())}/{quote(title)}"
            response = client.post(url, data=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError(f"Bark failed: {data}")


def send_notification(title: str, content: str, config: dict) -> None:
    send_pushplus(title, content, config)
    send_bark(title, content, config)
