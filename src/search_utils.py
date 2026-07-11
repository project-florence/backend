import pykap

def search_companies_by_text(text):
    results = pykap.search_companies(text)
    if not results:
        return {}
    return results