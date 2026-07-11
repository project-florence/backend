from datetime import datetime, timezone

from google.cloud import bigquery
import pandas as pd

from article import Article
from generate_bist_mapping import load_bist_mapping
from src.config import get_config

_client: bigquery.Client | None = None
_mapping: dict[str, dict] | None = None


def _get_client() -> bigquery.Client:
    global _client
    if _client is None:
        cfg = get_config()["article_collector"]
        _client = bigquery.Client(project=cfg["bigquery_project"])
    return _client


def _get_mapping() -> dict[str, dict]:
    global _mapping
    if _mapping is None:
        _mapping = load_bist_mapping()
    return _mapping


def _resolve_search_terms(query: str) -> dict:
    mapping = _get_mapping()
    upper = query.upper()

    if upper in mapping:
        return mapping[upper]

    title_terms = [upper]
    gkg_terms = [upper]
    name_tr = query
    return {
        "name_tr": name_tr,
        "search_title": title_terms,
        "search_gkg": gkg_terms,
    }


def _build_title_clause(terms: list[str], filter_lang: dict | None, lang: list[str] | None) -> str:
    clauses = []
    for term in terms:
        if filter_lang and term in filter_lang:
            term_langs = filter_lang[term]
            if lang:
                term_langs = [l for l in term_langs if l in [x.upper() for x in lang]]
            if term_langs:
                langs_str = ", ".join(f"'{li}'" for li in term_langs)
                clauses.append(
                    f"(UPPER(title) LIKE '%{term}%' AND UPPER(lang) IN ({langs_str}))"
                )
            continue
        if lang:
            langs_str = ", ".join(f"'{li.upper()}'" for li in lang)
            clauses.append(
                f"(UPPER(title) LIKE '%{term}%' AND UPPER(lang) IN ({langs_str}))"
            )
        else:
            clauses.append(f"UPPER(title) LIKE '%{term}%'")
    if not clauses:
        return "FALSE"
    return " OR ".join(clauses)


def _build_gkg_clause(terms: list[str]) -> str:
    return " OR ".join(f"UPPER(V2Organizations) LIKE '%{t}%'" for t in terms)


def collect_articles(
    query: str,
    from_date: datetime | None = None,
    limit: int | None = None,
    lang: list[str] | None = None,
    diverse: bool = False,
) -> list[Article]:
    cfg = get_config()["article_collector"]
    if limit is None:
        limit = cfg["default_limit"]

    entry = _resolve_search_terms(query)
    filter_lang = entry.get("filter_lang")

    title_clause = _build_title_clause(entry["search_title"], filter_lang, lang)
    gkg_clause = _build_gkg_clause(entry["search_gkg"])

    if from_date is None:
        from_date = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    from_date_str = from_date.strftime("%Y-%m-%d")

    if diverse:
        tail_sql = """
    deduped AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY url ORDER BY date DESC NULLS LAST) AS rn
      FROM combined
    ),
    unique_articles AS (
      SELECT url, title, lang, date
      FROM deduped
      WHERE rn = 1 AND title IS NOT NULL
    ),
    bucketed AS (
      SELECT *, NTILE(@limit) OVER (ORDER BY date DESC NULLS LAST) AS bucket
      FROM unique_articles
    ),
    random_pick AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY RAND()) AS pick_rn
      FROM bucketed
    )
    SELECT url, title, lang, date
    FROM random_pick
    WHERE pick_rn = 1
    ORDER BY date DESC NULLS LAST
    """
    else:
        tail_sql = """
    deduped AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY url ORDER BY date DESC NULLS LAST) AS rn
      FROM combined
    )
    SELECT url, title, lang, date
    FROM deduped
    WHERE rn = 1 AND title IS NOT NULL
    ORDER BY date DESC NULLS LAST
    LIMIT @limit
    """

    sql = f"""
    WITH
    gqg_rows AS (
      SELECT url, title, lang, date
      FROM `{cfg["gdelt_gqg_table"]}`
      WHERE date >= TIMESTAMP(@from_date)
        AND ({title_clause})
    ),
    gkg_rows AS (
      SELECT DocumentIdentifier AS url, CAST(NULL AS STRING) AS title, CAST(NULL AS STRING) AS lang, CAST(NULL AS TIMESTAMP) AS date
      FROM `{cfg["gdelt_gkg_table"]}`
      WHERE _PARTITIONTIME >= TIMESTAMP(@from_date)
        AND ({gkg_clause})
    ),
    combined AS (
      SELECT * FROM gqg_rows
      UNION DISTINCT
      SELECT * FROM gkg_rows
    ),{tail_sql}
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("from_date", "STRING", from_date_str),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )

    results = _get_client().query(sql, job_config=job_config).result().to_dataframe()

    articles: list[Article] = []
    for _, row in results.iterrows():
        date_val = row["date"]
        if isinstance(date_val, pd.Timestamp):
            date_val = date_val.to_pydatetime()
        elif pd.isna(date_val):
            date_val = None

        title_val = row["title"]
        if pd.isna(title_val):
            title_val = ""

        lang_val = row["lang"]
        if pd.isna(lang_val):
            lang_val = None

        articles.append(Article(
            url=row["url"],
            title=title_val,
            lang=lang_val,
            date=date_val,
        ))

    return articles
