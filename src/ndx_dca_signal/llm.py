from __future__ import annotations

import json

import httpx

from ndx_dca_signal.models import SignalResult


SYSTEM_PROMPT = """你是投资规则解释器，不是最终决策者。
买入/不买由规则引擎已经确定，你只能解释规则结果。
不得覆盖 signal。
不得建议用户忽略规则。
不得使用确定性收益表达。
不得编造缺失数据。
如果数据不足，明确说明数据不足导致不发买入信号。
输出应简洁、克制、可审计。"""


def build_payload(result: SignalResult) -> dict:
    selected = result.selected_fund
    return {
        "signal": result.status,
        "as_of": result.as_of.isoformat(),
        "selected_fund": selected.snapshot.code if selected else None,
        "selected_fund_name": selected.snapshot.name if selected else None,
        "premium": selected.snapshot.premium if selected else None,
        "premium_percentile": selected.premium_percentile if selected else None,
        "market_score": result.market_score.total if result.market_score else None,
        "market_score_threshold": result.market_score.threshold if result.market_score else None,
        "market_score_breakdown": result.market_score.components if result.market_score else None,
        "hard_filters": result.market_score.hard_filters if result.market_score else [],
        "reasons": result.reasons,
    }


def generate_analysis(result: SignalResult, config: dict) -> str:
    llm_config = config["llm"]
    if not llm_config.get("enabled"):
        return ""
    api_url = llm_config.get("api_url")
    api_key = llm_config.get("api_key")
    if not api_url or not api_key:
        raise ValueError("LLM enabled but api_url or api_key is empty")

    payload = {
        "model": llm_config["model"],
        "temperature": float(llm_config.get("temperature", 0.2)),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请解释以下规则结果，不要改变信号：\n"
                + json.dumps(build_payload(result), ensure_ascii=False, indent=2),
            },
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=float(llm_config.get("timeout_seconds", 20))) as client:
        response = client.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    return str(data["choices"][0]["message"]["content"]).strip()
