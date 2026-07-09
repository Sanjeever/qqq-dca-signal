from __future__ import annotations

import json
from importlib import resources

import httpx

from ndx_dca_signal.models import SignalResult


ZHENGXI_METHOD_RESOURCE = "zhengxi_views_method.md"


SYSTEM_PROMPT = """你是 NDX/QDII ETF 定投信号的 LLM 分析器，不是最终决策者。
买入/不买由规则引擎已经确定，你只能融合解释规则结果、新闻上下文和郑希公开方法框架。
不得覆盖 signal。
不得建议用户忽略规则。
不得使用确定性收益表达。
不得编造缺失数据。
如果数据不足，明确说明数据不足导致不发买入信号。
新闻上下文只能用于说明当前市场背景和补充风险解释，不参与规则决策。
不得因为新闻上下文而改变 signal。
新闻上下文可能有噪音、重复或过期，必须谨慎表述，不得夸大。
不得补充 news_context 未提供的新闻事实、宏观事件、数据类型、机构观点或因果判断。
如果 news_context 为空，简短说明未提供新闻上下文，并继续基于规则结果和郑希公开方法框架分析。
如果 news_errors 非空，简短说明新闻上下文获取失败，并继续基于规则结果和郑希公开方法框架分析。
不要声称新闻与规则信号相互印证。
可以使用郑希公开方法框架的概念，但不得引用郑希原话。
不得声称郑希本人对今日市场、NDX、新闻或本次信号发表观点。
必须自然说明这段分析不改变规则信号。
输出一段 120-220 字中文分析，控制在 3-4 句内。
谈到供给端创造需求时，表述为“供给端创造的需求是否持续兑现”或类似自然说法，不要写“供给端需求”。
不要使用小标题、列表、表格、emoji、横线分隔符、引用块或夸张语气。
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
        "news_context": result.news_context,
        "news_errors": result.news_errors,
    }


def build_rule_summary(result: SignalResult) -> str:
    lines = [
        "LLM 分析暂不可用，以下为规则摘要。",
        f"- 信号：{result.status}",
        f"- 主因：{'；'.join(result.reasons)}",
    ]
    if result.selected_fund:
        selected = result.selected_fund
        snapshot = selected.snapshot
        lines.extend(
            [
                f"- 推荐/观察基金：{snapshot.code} {snapshot.name}",
                f"- 当前溢价：{snapshot.premium:.2%}",
                f"- 近60日溢价分位：{selected.premium_percentile:.0%}",
            ]
        )
    if result.market_score:
        lines.append(
            f"- 市场评分：{result.market_score.total:.1f} / 100"
            f"（阈值 {result.market_score.threshold:.1f}）"
        )
    if result.news_errors:
        lines.append(f"- 新闻上下文：{'；'.join(result.news_errors)}")
    elif result.news_context:
        lines.append("- 新闻上下文：已获取，但本次未由 LLM 展开解读。")
    return "\n".join(lines)


def load_zhengxi_method() -> str:
    return (
        resources.files("ndx_dca_signal.resources")
        .joinpath(ZHENGXI_METHOD_RESOURCE)
        .read_text(encoding="utf-8")
        .strip()
    )


def generate_analysis(result: SignalResult, config: dict) -> str:
    llm_config = config["llm"]
    if not llm_config.get("enabled"):
        return ""
    api_url = llm_config.get("api_url")
    api_key = llm_config.get("api_key")
    if not api_url or not api_key:
        raise ValueError("LLM enabled but api_url or api_key is empty")

    zhengxi_method = load_zhengxi_method()
    payload = {
        "model": llm_config["model"],
        "temperature": float(llm_config.get("temperature", 0.2)),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请按郑希公开方法框架，融合以下规则结果和新闻上下文，给出一段结论。"
                "必须同时覆盖：规则为什么给出当前 signal；新闻上下文补充了哪些市场背景或风险；"
                "按郑希公开方法框架当前更应关注哪些变量；并说明该分析不改变规则信号。"
                "不要拆成“规则解释”和“新闻概览”，不要输出小标题、列表、表格或引用块。"
                "输出为单段 3-4 句，避免长句堆叠。"
                "不得引用郑希原话，不得声称郑希本人对今日市场或本信号发表观点。"
                "谈到供给端创造需求时，表述为“供给端创造的需求是否持续兑现”或类似自然说法，"
                "不要写“供给端需求”。"
                "不得补充 news_context 之外的新闻事实。\n\n"
                "郑希公开方法框架（只用于方法推演，不可当作今日原话引用）：\n"
                f"{zhengxi_method}\n\n"
                "规则结果与新闻上下文：\n"
                + json.dumps(build_payload(result), ensure_ascii=False, indent=2),
            },
        ],
    }
    max_tokens = llm_config.get("max_tokens")
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=float(llm_config.get("timeout_seconds", 20))) as client:
        response = client.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
    return str(data["choices"][0]["message"]["content"]).strip()
