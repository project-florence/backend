import trafilatura


def get_text_from_url(url: str) -> str:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise Exception("Could not download from url")

    text = trafilatura.extract(downloaded)
    if not text:
        raise Exception("Could not extract text from url")

    return text
