from pathlib import Path

_TOOLS_DIR = Path(__file__).parent

TOOL_REGISTRY = {
    "news_search": "src.clients.search.news_search",
    "content_fetch": "src.clients.scraping.get_text_from_url",
    "economic_data": "src.services.report.tools.execute_tool",
    "generate_report": "src.services.report.tools.execute_tool",
}


import json


def load_tool_definitions() -> list[dict]:
    tools = []
    for f in sorted(_TOOLS_DIR.glob("*.json")):
        with open(f, encoding="utf-8") as fp:
            func_def = json.load(fp)
            tools.append({
                "type": "function",
                "function": func_def,
            })
    return tools


_tool_context: dict = {}


def execute_tool(name: str, arguments: dict) -> str:
    global _tool_context

    if name == "news_search":
        from src.clients.search import news_search
        try:
            items = news_search(arguments["query"], limit=20)
        except Exception as e:
            return json.dumps({"error": f"News search failed: {e}"})
        _tool_context["_last_news"] = items
        serialized = json.dumps(
            [{"index": i + 1, "url": it.url, "title": it.title,
              "content": it.content, "source": getattr(it, "source_engine", "")}
             for i, it in enumerate(items)],
            ensure_ascii=False,
        )
        return serialized

    if name == "content_fetch":
        from src.clients.scraping import get_text_from_url
        previous = _tool_context.get("_last_news", [])
        indices = arguments.get("indices", [])
        results = []
        for idx in indices:
            if 1 <= idx <= len(previous):
                item = previous[idx - 1]
                try:
                    full_text = get_text_from_url(item.url)
                except Exception as e:
                    full_text = f"[Icerik cekilemedi: {e}]"
                results.append({"index": idx, "url": item.url, "content": full_text})
            else:
                results.append({"index": idx, "error": f"Index {idx} disarida"})
        return json.dumps(results, ensure_ascii=False)

    if name == "economic_data":
        from src.services.company import get_company_info
        from src.services.economy import get_currency, get_gold_prices
        from src.analysis.metrics import compute_all
        from src.analysis.stock_vector import company_vector
        try:
            ticker = arguments["ticker"]
            profile = get_company_info(ticker)
            if not profile:
                return json.dumps({"error": f"No data found for {ticker}"})
            metrics = compute_all(profile)
            vector = company_vector(profile)
            economy = {}
            try:
                economy = {**get_currency(), **get_gold_prices()}
            except Exception:
                pass
            result = {
                "ticker": ticker,
                "company_name": profile.get("name"),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "market": profile.get("market", {}),
                "trading": profile.get("trading", {}),
                "valuation": profile.get("valuation", {}),
                "financials": profile.get("financials", {}),
                "metrics": metrics,
                "vector": vector,
                "economy": economy,
            }
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"economic_data failed: {e}"})

    if name == "generate_report":
        return json.dumps(arguments)

    return json.dumps({"error": f"Unknown tool: {name}"})


def run_tool_loop(
    prompt: str,
    tools: list[dict] | None = None,
    messages: list[dict] | None = None,
    max_turns: int = 10,
    endpoint: str = "tool_loop",
    user_id: int | None = None,
) -> dict:
    from src.clients.llm import get_response
    from src.services.token import log_token_usage

    if messages is None:
        messages = [{"role": "user", "content": prompt}]

    accumulated = {"prompt": 0, "completion": 0, "total": 0}

    for _ in range(max_turns):
        result = get_response("", tools=tools, messages=messages)

        usage = result.get("usage")
        if usage:
            accumulated["prompt"] += usage["prompt"]
            accumulated["completion"] += usage["completion"]
            accumulated["total"] += usage["total"]
            log_token_usage(
                model=usage["model"],
                prompt_tokens=usage["prompt"],
                completion_tokens=usage["completion"],
                total_tokens=usage["total"],
                endpoint=endpoint,
                user_id=user_id,
            )

        if result["type"] == "text":
            result["usage"] = accumulated
            return result

        if result["type"] == "error":
            result["usage"] = accumulated
            return result

        if result["type"] == "tool_calls":
            assistant_msg = result["assistant_message"]
            messages.append(assistant_msg)

            for call in result["calls"]:
                if call["name"] == "generate_report":
                    return {"type": "report", "content": call["arguments"], "usage": accumulated}
                output = execute_tool(call["name"], call["arguments"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": output,
                })

    return {"type": "error", "detail": "Max turns reached without final response", "usage": accumulated}
