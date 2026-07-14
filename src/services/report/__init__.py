from src.models.article import Article
from src.models.content import Content
from src.clients.scraping import get_text_from_url
from src.clients.llm import get_response
from src.services.news import collect_articles
from src.utils.dates import get_last_quarter_start
from src.core.config import get_config
from src.services.report.prompts import article_selection_prompt, article_analyze_prompt, report_generation_prompt


def _generate_report(query: str, article_limit: int):
    articles = collect_articles(query, get_last_quarter_start(), article_limit, lang=["TURKISH"])
    if not articles:
        raise Exception("No articles found")

    selection_list = ""
    for i, article in enumerate(articles):
        selection_list += "{} -- {}\n".format(i + 1, article.title)
    selection_prompt = article_selection_prompt.format(query, article_limit) + "\n" + selection_list

    res = get_response(selection_prompt)
    if not res:
        raise Exception("No articles selected")

    selected_articles_idxes = res.split(",")
    selected_articles = []
    for idx in selected_articles_idxes:
        idx = int(idx.strip()) - 1
        if 0 <= idx < len(articles):
            selected_articles.append(articles[idx])

    contents = []
    for article in selected_articles:
        txt = ""
        try:
            txt = get_text_from_url(article.url)
        except Exception as e:
            print(e)
        contents.append(Content(article.title, article.date, txt))

    summaries = []
    for content in contents:
        res = get_response(article_analyze_prompt.format(query, content.to_string()))
        if not res:
            print("No output generated")
        summaries.append(res)

    all_contents = "".join(summaries)
    report = get_response(report_generation_prompt.format(query, "TURKISH", all_contents))
    return report or "No output generated"


def generate_quick_report(query: str) -> str:
    cfg = get_config()["article_analyzer"]
    return _generate_report(query, cfg["quick_report_article_limit"])


def generate_deep_report(query: str) -> str:
    cfg = get_config()["article_analyzer"]
    return _generate_report(query, cfg["deep_report_article_limit"])
