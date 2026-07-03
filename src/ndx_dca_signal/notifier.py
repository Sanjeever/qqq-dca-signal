from __future__ import annotations

import sys
from urllib.parse import quote

import httpx


DEFAULT_BARK_MAX_BODY_BYTES = 2800
BARK_TRUNCATED_SUFFIX = "\n\n[Bark 摘要已截断，完整内容请看 PushPlus 或本地日志。]"


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


def truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def bark_body(content: str, bark_config: dict) -> str:
    max_bytes = int(bark_config.get("max_body_bytes", DEFAULT_BARK_MAX_BODY_BYTES))
    if len(content.encode("utf-8")) <= max_bytes:
        return content

    suffix_bytes = len(BARK_TRUNCATED_SUFFIX.encode("utf-8"))
    if max_bytes <= suffix_bytes:
        raise ValueError("Bark max_body_bytes must be larger than truncation suffix")
    allowed = max_bytes - suffix_bytes
    lines = content.splitlines()
    compact_lines: list[str] = []
    keep = True
    skipped_sections = {"## LLM 分析", "## 候选基金"}
    for line in lines:
        if line.startswith("## "):
            keep = line not in skipped_sections
        if keep:
            compact_lines.append(line)

    compact = "\n".join(compact_lines).strip()
    if not compact:
        compact = content
    return truncate_utf8(compact, allowed).rstrip() + BARK_TRUNCATED_SUFFIX


def send_bark(title: str, content: str, config: dict) -> None:
    bark_config = config.get("bark", {})
    if not bark_config.get("enabled"):
        return
    keys = configured_bark_keys(bark_config)
    if not keys:
        raise ValueError("Bark enabled but keys is empty")

    server_url = str(bark_config.get("server_url", "https://api.day.app")).rstrip("/")
    payload = {
        "body": bark_body(content, bark_config),
        "group": bark_config.get("group", "ndx-dca-signal"),
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
    sent = False
    errors: list[str] = []

    if config.get("pushplus", {}).get("enabled"):
        try:
            send_pushplus(title, content, config)
            sent = True
        except Exception as exc:
            errors.append(f"PushPlus failed: {exc}")

    if config.get("bark", {}).get("enabled"):
        try:
            send_bark(title, content, config)
            sent = True
        except Exception as exc:
            errors.append(f"Bark failed: {exc}")

    if errors and sent:
        print("；".join(errors), file=sys.stderr)
    if errors and not sent:
        raise RuntimeError("；".join(errors))
