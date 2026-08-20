import html
import re
from urllib import request
from urllib.parse import urljoin

from django.db import transaction

from warranty.models import GreenworksDrawing


CATALOG_URL = 'https://greenworks-service.ru/service/drawings-and-spare-parts-catalogs/'
ARTICLE_RE = re.compile(r'\(\s*арт(?:икул)?\.?\s*[:№]?\s*([^)]+?)\s*\)', re.IGNORECASE)
CARD_RE = re.compile(
    r'<div\s+class="[^"]*\bmanual-card\b[^"]*">(?P<body>.*?)(?=<div\s+class="[^"]*\bmanual-card\b|\Z)',
    re.DOTALL,
)
CARD_ARTICLE_RE = re.compile(r'manual-card__code[^>]*>\s*Артикул\s+([^<]+)', re.IGNORECASE)
DRAWING_LINK_RE = re.compile(
    r'<a\s+class="green-link"\s+href="([^"]+)"[^>]*title="([^"]*)"',
    re.IGNORECASE,
)
PAGE_RE = re.compile(r'[?&]PAGEN_1=(\d+)')
# Новая редакция пока открывается напрямую, но ещё не привязана к карточке
# 2516107 в общем каталоге Greenworks.
CATALOG_SUPPLEMENTS = {
    '2516107': [{
        'url': f'{CATALOG_URL}131524/',
        'title': '2516107RU Exploded view — дата производства с 01.01.2026',
    }],
}


def normalize_article(value):
    return re.sub(r'\s+', '', html.unescape(str(value or ''))).upper()


def base_article(value):
    article = normalize_article(value)
    match = re.fullmatch(r'(\d{5,})(?:[A-Z]{1,3})', article)
    return match.group(1) if match else article


def catalog_article_keys(value):
    source = html.unescape(str(value or '')).upper()
    keys = []
    for token in re.findall(r'[A-Z0-9][A-Z0-9-]*', source):
        for article in (normalize_article(token), base_article(token)):
            if article and article not in keys:
                keys.append(article)
    return keys


def product_article(claim):
    match = ARTICLE_RE.search(claim.product_name or '')
    if match:
        return normalize_article(match.group(1))
    raw = claim.raw_source_data or {}
    for key in ('UF_ARTICLE', 'ARTICLE', 'SKU', 'XML_ID'):
        value = normalize_article(raw.get(key))
        if value:
            return value
    return ''


def drawing_links_for_claim(claim):
    article = product_article(claim)
    if not article:
        return []
    candidates = list(dict.fromkeys((article, base_article(article))))
    links = []
    for drawing in GreenworksDrawing.objects.filter(article__in=candidates):
        links.extend(item for item in drawing.links if item not in links)
    return links


def parse_catalog_page(source):
    drawings = {}
    for card_match in CARD_RE.finditer(source):
        card = card_match.group('body')
        article_match = CARD_ARTICLE_RE.search(card)
        if not article_match:
            continue
        articles = catalog_article_keys(article_match.group(1))
        links = []
        for path, title in DRAWING_LINK_RE.findall(card):
            item = {
                'url': urljoin(CATALOG_URL, html.unescape(path)),
                'title': html.unescape(title).strip() or 'Чертёж',
            }
            if item not in links:
                links.append(item)
        if links:
            for article in articles:
                article_links = drawings.setdefault(article, [])
                article_links.extend(item for item in links if item not in article_links)
    pages = [int(value) for value in PAGE_RE.findall(source)]
    return drawings, max(pages, default=1)


def fetch_catalog_page(page=1, timeout=30):
    url = CATALOG_URL if page == 1 else f'{CATALOG_URL}?PAGEN_1={page}'
    req = request.Request(url, headers={
        'User-Agent': 'StoreChecklist/1.0 (+https://checklist.es-helper.ru/)',
    })
    with request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or 'utf-8'
        return response.read().decode(charset, errors='replace')


def refresh_catalog(timeout=30):
    first = fetch_catalog_page(timeout=timeout)
    drawings, _ = parse_catalog_page(first)
    page_count = 1
    for page in range(2, 101):
        page_drawings, _ = parse_catalog_page(fetch_catalog_page(page, timeout=timeout))
        if not page_drawings:
            break
        changed = False
        for article, links in page_drawings.items():
            article_links = drawings.setdefault(article, [])
            new_links = [item for item in links if item not in article_links]
            if new_links:
                article_links.extend(new_links)
                changed = True
        if not changed:
            break
        page_count = page
    for article, links in CATALOG_SUPPLEMENTS.items():
        article_links = drawings.setdefault(article, [])
        article_links.extend(item for item in links if item not in article_links)
    with transaction.atomic():
        GreenworksDrawing.objects.exclude(article__in=drawings).delete()
        for article, links in drawings.items():
            GreenworksDrawing.objects.update_or_create(article=article, defaults={'links': links})
    return {'articles': len(drawings), 'pages': page_count}
