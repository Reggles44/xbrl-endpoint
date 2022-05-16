import asyncio
import json
import logging
import logging.config
import re
import os
import datetime
import math
import marshmallow
import xbrl_endpoint

import httpx
from aiolimiter import AsyncLimiter

from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta

logger = logging.getLogger()

HTTPX_CLIENT = httpx.AsyncClient(timeout=5)
SEC_RATE_LIMITER = AsyncLimiter(5, 1)

CRAWLER_IDX_URL = 'https://www.sec.gov/Archives/edgar/full-index/{}/QTR{}/crawler.idx'
CRAWLER_LINE_REGEX = re.compile('(.+)\s+([\dA-Z\-\/]+)\s+(\d+)\s+(\d{4}-\d{2}-\d{2}).*\/([\d\-]+)-index.htm\s*$')

INDEX_URL = 'https://www.sec.gov/Archives/edgar/data/{}/{}-index.htm'
SCHEMA_TICKET_REGEX = re.compile('(\w+)-\d+.xsd')

START_DATE = datetime.date(year=2019, month=1, day=1)
END_DATE = datetime.date.today()

META_JSON = 'meta.json'
META_MAPPING = json.load(open(META_JSON)) if os.path.isfile(META_JSON) else {}

INDEX_JSON = 'index.json'
INDEX_MAPPING = json.load(open(INDEX_JSON)) if os.path.isfile(INDEX_JSON) else {}

"""
Index schema

{
    # CIK is primary index
    "0000001": {
        "ticker": "ABC",
        "company_name": "ABC Inc.",
        "forms": {
            "10-K": {
                "2022-02-09": "0001127602-22-004061"
            }
        }
    }
}
"""


async def get(url):
    async with SEC_RATE_LIMITER:
        try:
            response = await HTTPX_CLIENT.get(url, headers={'User-Agent': 'Company Name myname@company.com'})
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            pass


async def scrape_quarter(date):
    year, qtr = date.year, math.ceil(date.month / 3)
    key = f'{year}-{qtr}'
    if META_MAPPING.get(key):
        return

    logger.info(f'Scraping {year} QTR{qtr}')
    crawler_data = await get(CRAWLER_IDX_URL.format(year, qtr))
    if not crawler_data:
        return

    starting_index = next(i for i, line in enumerate(crawler_data.text.split('\n')) if all(c == '-' for c in line))

    for line in crawler_data.text.split('\n')[starting_index+1:]:
        line = line.strip()
        if not line:
            continue

        company_name, form_type, cik, date_filed, index_id = CRAWLER_LINE_REGEX.search(line).groups()

        company_data = INDEX_MAPPING.setdefault(
            cik,
            {
                'company_name': company_name.strip(),
                'ticker': None,
                'forms': {}
            }
        )

        company_data['forms'].setdefault(form_type, {})[date_filed] = index_id

    META_MAPPING[key] = True


async def scrape_index(url):
    index_response = await get(url)
    if not index_response:
        return

    index_soup = BeautifulSoup(index_response.content, 'lxml')
    data_files_table = index_soup.find('table', {'summary': 'Data Files'})
    if not data_files_table:
        return

    schema_file = data_files_table.find_all(text=SCHEMA_TICKET_REGEX)[0]
    ticker = SCHEMA_TICKET_REGEX.search(schema_file.text).group(1)
    return ticker.upper()


async def find_ticker(cik, data):
    ticker = data['ticker']
    if ticker:
        return

    index_ids = list(data['forms'].get('10-Q', {}).values())

    while index_ids:
        url = INDEX_URL.format(cik, index_ids.pop())
        ticker = await scrape_index(url)
        if ticker:
            logger.debug(f'Found Ticker {ticker} for {data["company_name"]}')
            break

    data['ticker'] = ticker


async def build():
    quarters = int((END_DATE - START_DATE).days / (365/4)) + 1

    await asyncio.gather(
        *(scrape_quarter(START_DATE + relativedelta(months=3 * i)) for i in range(quarters)),
    )

    json.dump(INDEX_MAPPING, open(INDEX_JSON, 'w+'), indent=4)
    json.dump(META_MAPPING, open(META_JSON, 'w+'), indent=4)

    try:
        await asyncio.gather(
            *(find_ticker(cik, data) for cik, data in INDEX_MAPPING.items())
        )
    except:
        pass

    json.dump(INDEX_MAPPING, open(INDEX_JSON, 'w+'), indent=4)
    json.dump(META_MAPPING, open(META_JSON, 'w+'), indent=4)


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(build())
