"""Built In NYC company directory discovery."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import DiscoveryClient
from .models import CompanyCandidate

# First page of each category only: Built In's robots.txt allows company
# directory pages but not deep pagination. Categories cover the candidate's
# role families (AI and data, product, strategy, solutions) and the NYC
# industries the candidate wants (media, music, sports, fashion, gaming, consumer).
BUILTIN_TYPES = (
    "artificial-intelligence", "fintech", "big-data", "analytics", "software",
    "productivity", "consumer-web", "e-commerce", "edtech", "healthtech",
    "marketing-tech", "media", "entertainment", "music", "sports", "fashion", "gaming",
)  # fmt: skip
BUILTIN_URLS = tuple(f"https://www.builtinnyc.com/companies/type/{t}" for t in BUILTIN_TYPES)
EMPLOYEES_RE = re.compile(r"\b([\d,]+)\s+Employees\b", re.I)


def parse_page(html: str, source_url: str) -> list[CompanyCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.company-card-horizontal"):
        anchor = card.select_one('a[data-track-id="title"][href]')
        if anchor is None:
            continue
        name = anchor.get_text(" ", strip=True)
        text = card.get_text(" ", strip=True)
        match = EMPLOYEES_RE.search(text)
        tags_container = card.select_one(".company-info-section")
        tags_text = tags_container.get_text(" ", strip=True) if tags_container else ""
        tags = tuple(
            token.strip()
            for token in re.split(r"\s*[•|]\s*", tags_text.replace(name, "", 1))
            if token.strip()
        )
        out.append(
            CompanyCandidate(
                name=name,
                source="builtin_nyc",
                source_url=urljoin(source_url, str(anchor.get("href"))),
                employee_count=int(match.group(1).replace(",", "")) if match else None,
                hiring="Hiring Now" in text or "Teams Hiring" in text,
                tags=tags,
            )
        )
    return out


def fetch(client: DiscoveryClient) -> list[CompanyCandidate]:
    out = []
    for url in BUILTIN_URLS:
        out.extend(parse_page(client.get(url).text, url))
    return out
