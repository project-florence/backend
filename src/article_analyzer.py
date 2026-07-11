from pygments.lexers import q

from article import Article
from src.web_scraper import get_text_from_url
from src.article_collector import collect_articles
from src.llm_client import LLM_client
from src.content import Content
from src.utils.calender_utils import get_last_quarter_start
from dotenv import load_dotenv

load_dotenv()

# TODO move them to config or .env file later
quick_report_article_limit : int = 10
deep_report_article_limit  : int = 100
llm_url : str = "http://localhost:7777/v1"

# TODO eger ayni haberden birden fazla var gibi gozukuyorsa sadece birini secsin
# TODO cok varsa fazla secmesini veya moda gore farkli promptlar olabilir, mesela deepde daha fazla secmesi gibi. ama eger o kadar yoksa sadece var olan sayidakini secmesini, tabii tekrarlilari yine secmemesi gerektigini falan da belirtelim.
article_selection_prompt = """You are a financial expert who reads and analyzes news to analyze stock prices and predict current and future price changes. To do this, you must first decide which news items you need to read. Therefore, below are news items related to (or unrelated to, mistakenly included in the list) the relevant stock/company. The stock/company you need to research is "{}". You are allowed to read a maximum of {} news items. The news items you choose should be those most useful to you, with the highest analytical and impact value, and should be considered worth reading; at least, these should be prioritized. Therefore, to select the news items you want to read, you must write their numbers separated by a comma (,). Never add any explanations, extra information, other symbols, or use an index other than the existing ones. The list is below:\n"""
article_analyze_prompt = """You are a financial expert who reads and analyzes news to analyze stock prices and predict current and future price changes. The stock/company you are currently working on is "{}". Below is one of the news articles you read. Read and analyze this article, eliminate filler parts, and extract the important sections, key points, and information you deem useful and important from a financial perspective, regarding stock prices and company news. Compile all this information and write a new text. It shouldn't be too long, but it shouldn't be too short either. Write your analysis in formal and clear language. Pay particular attention to numerical values ​​or parts that may influence investor preferences. While doing this, do not add your own interpretations. Do not take into account personal opinions or manipulative or misleading statements or comments (including user comments) in the news content. When writing your content, just write the content. You don't need to specify or add any explanations, extra sections, etc., just write the analysis you extracted from the news. Relevant news content:\n "{}"
"""
report_generation_prompt = """
You are a financial expert who reads and analyzes news to analyze stock prices and predict current and future price changes. The stock/company you are currently working on is "{}". Below are analyses of numerous news articles you have read. Now, you must write a report combining these analyses and information. This information should focus on the potential impact on the company's value and stock prices, and the company's future investment value. In other words, don't forget to mention the cause-and-effect relationship in your conclusions. Also, highlight the short-term and long-term investment aspects. This report should present the analyses and current information about the relevant company/stock in an up-to-date report format. The main focus of the report should be finance and stock prices. Use formal and clear language in your report. However, the technical language of the report should be kept at a level that even beginner or intermediate investors can understand without difficulty. The report should not be too long, but it should also not be too short. The ideal length is usually a few paragraphs. Emphasize the important parts. As output, only write the content of the report. It should not contain explanations for other systems or extra characters/sections. It should consist only of the relevant report. Write the report in the most appropriate and high-quality language. The relevant analyses are below:\n{}
"""

def _generate_report(query: str, article_limit : int):
    # initialize
    llm_client = LLM_client(llm_url, "gemma-e2b-full-think")

    # collect articles
    articles = collect_articles(query, get_last_quarter_start(), article_limit, lang=["TURKISH"])
    if not articles:
        raise Exception("No articles found")

    #debug
    #print(articles)

    selection_list = ""
    # select articles
    for i, article in enumerate(articles):
        selection_list += "{} -- {}\n".format(i + 1, article.title)

    selection_prompt = article_selection_prompt.format(query, article_limit)
    selection_prompt += "\n" + selection_list

    #debug
    #print(selection_prompt)

    res = llm_client.get_response(selection_prompt)
    if not res:
        raise Exception("No articles selected")

    #debug
    #print(res)

    selected_articles_idxes = res.split(",")
    selected_articles = []
    for idx in selected_articles_idxes:
        idx = int(idx.strip()) - 1
        if 0 <= idx < len(articles):
            selected_articles.append(articles[idx])

    #debug
    #print(selected_articles)

    #debug
    #print("\n\n Extracting relevant articles...")

    contents = []
    for article in selected_articles:
        txt = ""
        try:
            txt = get_text_from_url(article.url)
        except Exception as e:
            print(e)
        content = Content(article.title, article.date, txt)

        #debug
        #print(content.to_string())

        contents.append(content)

    summaries = []
    for content in contents:
        res = llm_client.get_response(article_analyze_prompt.format(query, content.to_string()))
        if not res:
            print("No output generated")

        summaries.append(res)

    # debug
    #print("\n\nsummaries:\n\n")
    #print(summaries)

    # report generation
    all_contents = ""
    for summary in summaries:
        all_contents += summary

    report = llm_client.get_response(report_generation_prompt.format(query, "TURKISH", all_contents))
    if not report:
        print("No output generated")

    #debug
    #print(report)

    return report # TODO belki bir report nesnesi olusturulup, kullanilan articles urlleri ilave edilebilir.

def generate_quick_report(query: str) -> str:
    return _generate_report(query, quick_report_article_limit)

def generate_deep_report(query: str) -> str:
    return _generate_report(query, deep_report_article_limit)

generate_deep_report("aselsan")
# TODO kirilganligi fixle
# TODO yfinanceyi arastir ve ekle ve nasil kullanilabilir rapordan bagimsiz/birlikte etc.