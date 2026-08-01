import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib import error, parse, request


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 12


class ProductImportError(Exception):
    pass


@dataclass
class ImportedProduct:
    url: str
    name: str
    price: str = ''
    currency: str = 'RUB'
    sku: str = ''
    image_url: str = ''
    properties: list = field(default_factory=list)
    source_name: str = ''

    @property
    def formatted_price(self):
        if not self.price:
            return 'Цена не указана'
        try:
            value = Decimal(str(self.price).replace(' ', '').replace(',', '.'))
            number = f'{value:,.0f}'.replace(',', ' ')
        except (InvalidOperation, ValueError):
            number = str(self.price)
        suffix = '₽' if self.currency.upper() in {'RUB', 'RUR', '₽'} else self.currency
        return f'{number} {suffix}'.strip()


class _ProductHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.json_ld = []
        self.title_parts = []
        self.h1_parts = []
        self.text_parts = []
        self.rows = []
        self._in_title = False
        self._in_h1 = False
        self._in_json_ld = False
        self._script_parts = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = attrs.get('property') or attrs.get('name') or attrs.get('itemprop')
            value = attrs.get('content')
            if key and value and key.lower() not in self.meta:
                self.meta[key.lower()] = value.strip()
        elif tag == 'title':
            self._in_title = True
        elif tag == 'h1':
            self._in_h1 = True
        elif tag == 'tr':
            self._row = []
        elif tag in {'td', 'th'} and self._row is not None:
            self._cell = []
        elif tag == 'script' and 'ld+json' in attrs.get('type', '').lower():
            self._in_json_ld = True
            self._script_parts = []

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag == 'h1':
            self._in_h1 = False
        elif tag in {'td', 'th'} and self._cell is not None:
            value = re.sub(r'\s+', ' ', ''.join(self._cell)).strip()
            if value:
                self._row.append(value)
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            if len(self._row) >= 2:
                self.rows.append(self._row)
            self._row = None
        elif tag == 'script' and self._in_json_ld:
            raw = ''.join(self._script_parts).strip()
            if raw:
                try:
                    self.json_ld.append(json.loads(raw))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            self._in_json_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if not self._in_json_ld:
            value = re.sub(r'\s+', ' ', data).strip()
            if value:
                self.text_parts.append(value)
        if self._in_json_ld:
            self._script_parts.append(data)


def _validate_public_url(url):
    parsed = parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProductImportError('В ссылке указан неверный порт.') from exc
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        raise ProductImportError('Нужна обычная http/https-ссылка на товар.')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port or 443)
    except socket.gaierror as exc:
        raise ProductImportError('Адрес сайта не найден.') from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ProductImportError('Ссылки на внутренние адреса запрещены.')
    return url


class _SafeRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url):
    _validate_public_url(url)
    opener = request.build_opener(_SafeRedirectHandler())
    req = request.Request(
        url,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 Chrome/124 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        },
    )
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {'text/html', 'application/xhtml+xml'}:
                raise ProductImportError('Ссылка ведёт не на HTML-страницу.')
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProductImportError('Страница слишком большая.')
            charset = response.headers.get_content_charset() or 'utf-8'
            return raw.decode(charset, errors='replace'), response.geturl()
    except ProductImportError:
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProductImportError('Не удалось загрузить карточку товара.') from exc


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _is_product(value):
    types = value.get('@type', '')
    if isinstance(types, str):
        types = [types]
    return any(str(item).lower() == 'product' for item in types)


def _first_offer(value):
    offers = value.get('offers') or {}
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def _absolute_url(value, base_url):
    if isinstance(value, list):
        value = value[0] if value else ''
    if isinstance(value, dict):
        value = value.get('url') or value.get('contentUrl') or ''
    return parse.urljoin(base_url, str(value)) if value else ''


def _properties(product, parser=None):
    result = []
    values = product.get('additionalProperty') or []
    if isinstance(values, dict):
        values = [values]
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        value = str(item.get('value') or '').strip()
        if name and value:
            result.append((name, value))
    brand = product.get('brand')
    if isinstance(brand, dict):
        brand = brand.get('name')
    if brand and not any(name.lower() == 'бренд' for name, _ in result):
        result.insert(0, ('Бренд', str(brand)))
    if parser:
        seen = {name.casefold() for name, _ in result}
        for row in parser.rows:
            name = row[0].strip().rstrip(':')
            value = row[1].strip()
            if (
                name and value and len(name) <= 80 and len(value) <= 160
                and name.casefold() not in seen
            ):
                result.append((name, value))
                seen.add(name.casefold())
    return result


def import_product(url):
    html, final_url = _download(url)
    parser = _ProductHTMLParser()
    parser.feed(html)
    candidates = [
        item for document in parser.json_ld
        for item in _walk_json(document) if _is_product(item)
    ]
    product = candidates[0] if candidates else {}
    offer = _first_offer(product)
    meta = parser.meta
    name = (
        product.get('name') or meta.get('og:title')
        or meta.get('twitter:title') or ''.join(parser.h1_parts).strip()
        or ''.join(parser.title_parts).strip()
    )
    name = re.sub(r'\s+', ' ', str(name)).strip()
    if not name:
        raise ProductImportError('На странице не найдено название товара.')
    price = (
        offer.get('price') or offer.get('lowPrice')
        or meta.get('product:price:amount') or meta.get('og:price:amount') or ''
    )
    page_text = ' '.join(parser.text_parts)
    if not price:
        price_match = re.search(
            r'(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{3,})\s*(?:₽|[pр]\b)',
            page_text,
            flags=re.IGNORECASE,
        )
        if price_match:
            price = price_match.group(1).replace(' ', '').replace('\u00a0', '')
    currency = (
        offer.get('priceCurrency') or meta.get('product:price:currency')
        or meta.get('og:price:currency') or 'RUB'
    )
    sku = product.get('sku') or product.get('mpn') or meta.get('product:retailer_item_id') or ''
    if not sku:
        sku_match = re.search(
            r'Артикул\s*[:№]?\s*([A-Za-zА-Яа-я0-9._/-]+)',
            page_text,
            flags=re.IGNORECASE,
        )
        if sku_match:
            sku = sku_match.group(1)
    image_url = _absolute_url(
        product.get('image') or meta.get('og:image') or meta.get('twitter:image'),
        final_url,
    )
    return ImportedProduct(
        url=final_url,
        name=name,
        price=str(price).strip(),
        currency=str(currency).strip(),
        sku=str(sku).strip(),
        image_url=image_url,
        properties=_properties(product, parser),
        source_name=parse.urlsplit(final_url).hostname or '',
    )
