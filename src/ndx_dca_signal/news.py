from __future__ import annotations

from typing import Any

import httpx


def news_enabled(config: dict) -> bool:
    return bool(config.get("news", {}).get("enabled", False))


def call_anysearch(tool_name: str, arguments: dict[str, Any], config: dict) -> str:
    news_config = config["news"]
    api_key = str(news_config.get("api_key", "")).strip()
    if not api_key:
        raise ValueError("news.enabled=true but ANYSEARCH_API_KEY is empty")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    endpoint = str(news_config.get("endpoint", "https://api.anysearch.com/mcp"))
    timeout = float(news_config.get("timeout_seconds", 15))
    with httpx.Client(timeout=timeout) as client:
        response = client.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    if "error" in data:
        message = data["error"].get("message", str(data["error"]))
        raise RuntimeError(f"AnySearch failed: {message}")

    content = data.get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "text":
            return str(item.get("text", "")).strip()
    return str(data.get("result", "")).strip()


def fetch_news_context(config: dict) -> list[dict[str, Any]]:
    if not news_enabled(config):
        return []

    news_config = config["news"]
    queries = [str(item).strip() for item in news_config.get("queries", []) if str(item).strip()]
    if not queries:
        return []

    lookback_hours = int(news_config.get("lookback_hours", 24))
    max_results = int(news_config.get("max_results", 6))
    max_chars = int(news_config.get("max_chars", 6000))
    scoped_queries = [f"{query} 最近{lookback_hours}小时 新闻" for query in queries[:5]]

    if len(scoped_queries) == 1:
        text = call_anysearch(
            "search",
            {"query": scoped_queries[0], "max_results": min(max_results, 10)},
            config,
        )
        return [{"query": scoped_queries[0], "content": text[:max_chars]}]

    per_query_results = max(1, min(3, max_results))
    text = call_anysearch(
        "batch_search",
        {
            "queries": [
                {"query": query, "max_results": per_query_results}
                for query in scoped_queries
            ]
        },
        config,
    )
    return [{"query": "；".join(scoped_queries), "content": text[:max_chars]}]
