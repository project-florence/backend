from openai import OpenAI
from src.core.config import get_config

_client = None
_default_model = None


def init_client(url=None, default_model=None, api_key=None):
    global _client, _default_model
    cfg = get_config()
    llm_cfg = cfg.get("llm_client", {})
    article_cfg = cfg.get("article_analyzer", {})
    if url is None:
        url = llm_cfg.get("url") or article_cfg.get("llm_url")
    if api_key is None:
        api_key = llm_cfg.get("api_key")
    _client = OpenAI(api_key=api_key, base_url=url)
    _default_model = default_model


def get_response(prompt: str, role: str = "user", model: str = None):
    global _client, _default_model
    if _client is None:
        init_client()
    if model is None:
        if _default_model is not None:
            model = _default_model
        else:
            cfg = get_config().get("article_analyzer", {})
            model = cfg.get("llm_model")
            if model is None:
                raise ValueError("No model provided")
    response = _client.chat.completions.create(
        model=model,
        messages=[{"role": role, "content": prompt}],
    )
    if response:
        return response.choices[0].message.content
    return None


def health_check():
    global _client
    try:
        if _client is None:
            init_client()
        response = _client.models.list()
        return response is not None
    except Exception:
        return False
