# import asyncio
# import os
#
# import httpx
# import pykap
# from dotenv import load_dotenv
#
# load_dotenv()
#
# API_KEY = os.getenv("LOGODEV_API_KEY")
# SAVE_PATH = os.getenv("LOGO_SAVE_PATH")
#
#
#
# if not API_KEY:
#     raise ValueError("LOGODEV_API_KEY not set")
#
# if not SAVE_PATH:
#     raise ValueError("LOGO_SAVE_PATH not set")
#
# os.makedirs(SAVE_PATH, exist_ok=True)
#
#
# async def download_and_save_logo(client, ticker, _format="png"):
#     print(f"Downloading {ticker} logo")
#     response = await client.get(
#         f"https://img.logo.dev/ticker/{ticker}.IS",
#         params={"token": API_KEY, "format": _format, "size": 256},
#     )
#     response.raise_for_status()
#     path = os.path.join(SAVE_PATH, f"{ticker}.{_format}")
#
#     with open(path, "wb") as f:
#         f.write(response.content)
#
#
# async def main():
#     tickers = pykap.bist_company_list()
#     downloaded = 0
#
#     async with httpx.AsyncClient() as client:
#         for ticker in tickers:
#             try:
#                 await download_and_save_logo(client, ticker)
#                 downloaded += 1
#             except Exception as e:
#                 print(f"Couldn't download {ticker}. Error: {e}")
#
#     print(f"Downloaded {downloaded} logos")
#
#
# asyncio.run(main())

import pykap

companies = pykap.get_bist_companies()
print(companies)