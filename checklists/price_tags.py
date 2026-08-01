import ipaddress
import json
import re
import socket
import time
from html import unescape
from difflib import SequenceMatcher
from io import BytesIO
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib import error, parse, request


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 12


class ProductImportError(Exception):
    pass


def format_product_price(price, currency='RUB'):
    if not price:
        return 'Цена не указана'
    try:
        value = Decimal(str(price).replace(' ', '').replace(',', '.'))
        number = f'{value:,.0f}'.replace(',', ' ')
    except (InvalidOperation, ValueError):
        number = str(price)
    suffix = '₽' if str(currency).upper() in {'RUB', 'RUR', '₽'} else currency
    return f'{number} {suffix}'.strip()


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
    brand: str = ''
    model: str = ''
    product_type: str = ''
    category_name: str = ''
    displayed_properties: list = field(default_factory=list)
    property_rows: list = field(default_factory=list)
    category_rule: object = None
    price_tag_profile: object = None
    tracking_url: str = ''
    source_h1: str = ''
    original_name: str = ''
    suggested_name: str = ''
    price_variants: list = field(default_factory=list)

    @property
    def formatted_price(self):
        return format_product_price(self.price, self.currency)

    @property
    def formatted_price_variants(self):
        return [
            {
                'label': item['label'],
                'price': format_product_price(
                    item.get('price', ''), item.get('currency', self.currency),
                ),
                'is_current': item.get('url') == self.url,
                'is_base': item.get('is_base', False),
            }
            for item in self.price_variants[:5]
            if item.get('label') and item.get('price')
        ]

    @property
    def prominent_name(self):
        value = self.suggested_name or self.name
        if getattr(self.price_tag_profile, 'layout_template', '') == 'pinel':
            value = clean_pinel_display_name(
                value, self.sku, self.secondary_name, self.voltage_text,
            )
            return value
        if 'бокс' in self.secondary_name.casefold():
            return clean_box_display_name(value, self.properties)
        return value

    @property
    def secondary_name(self):
        return self.product_type or self.category_name or 'Товар'

    @property
    def price_tag_category_name(self):
        if self.category_rule:
            return self.category_rule.name
        return self.secondary_name

    @property
    def manufacturer(self):
        source_host = self.source_name.casefold().removeprefix('www.')
        if self.brand and (
            source_host == 'pinel.ru' or source_host.endswith('.pinel.ru')
        ):
            return self.brand
        for preferred_names in (
            {'производитель', 'производител', 'manufacturer'},
            {'бренд', 'brand'},
        ):
            for name, value in self.properties:
                if name.casefold().strip().rstrip(':') in preferred_names:
                    return value
        return self.brand

    @property
    def voltage_text(self):
        for name, value in self.properties:
            normalized = name.casefold().strip().rstrip(':')
            if normalized in {'вольтаж', 'напряжение'}:
                return str(value).strip()
        match = re.search(r'(?<!\w)(\d{1,3}\s*[VВ])\b', self.name, re.IGNORECASE)
        return re.sub(r'\s+', '', match.group(1)).upper() if match else ''

    @property
    def voltage_series_class(self):
        match = re.search(r'\d{1,3}', self.voltage_text)
        if not match:
            return 'series-default'
        voltage = match.group(0)
        if voltage in {'20', '24', '40', '60', '82', '220'}:
            return f'series-{voltage}'
        return 'series-default'

    @property
    def warranty_text(self):
        preferred = []
        fallback = []
        for name, value in self.properties:
            normalized = name.casefold().strip().rstrip(':')
            if 'гарант' not in normalized:
                continue
            if any(marker in normalized for marker in ('акб', 'батар')):
                fallback.append(value)
            else:
                preferred.append(value)
        value = str((preferred or fallback or [''])[0]).strip()
        if not value:
            return ''
        years = re.fullmatch(r'(\d+)(?:[.,]0+)?', value)
        if not years:
            return value
        number = int(years.group(1))
        remainder_100 = number % 100
        remainder_10 = number % 10
        if 11 <= remainder_100 <= 14:
            unit = 'лет'
        elif remainder_10 == 1:
            unit = 'год'
        elif 2 <= remainder_10 <= 4:
            unit = 'года'
        else:
            unit = 'лет'
        return f'{number} {unit}'

    @property
    def qr_url(self):
        return self.tracking_url or self.url


def clean_pinel_display_name(value, sku='', category='', voltage=''):
    value = str(value).strip()
    if sku:
        value = re.sub(re.escape(str(sku)), '', value, flags=re.IGNORECASE)
    if category:
        value = re.sub(
            rf'^\s*{re.escape(str(category).strip())}\s*',
            '', value, flags=re.IGNORECASE,
        )
    if voltage:
        value = re.sub(
            rf'(?<!\w){re.escape(str(voltage).strip())}(?!\w)',
            '', value, flags=re.IGNORECASE,
        )
    value = re.sub(r'\s*[|]​?\s*$', '', value)
    value = re.sub(r'\s+', ' ', value)
    return value.strip(' |,—-')


def build_qr_url(url, utm_parameters=''):
    """Add store tracking parameters without losing a product query string."""
    if not utm_parameters:
        return url
    parts = parse.urlsplit(url)
    tracking = parse.parse_qsl(utm_parameters.lstrip('?'), keep_blank_values=True)
    tracking_keys = {key for key, _ in tracking}
    existing = [
        pair for pair in parse.parse_qsl(parts.query, keep_blank_values=True)
        if pair[0] not in tracking_keys
    ]
    query = parse.urlencode([*existing, *tracking])
    return parse.urlunsplit(parts._replace(query=query))


def site_url_matches(url, domain):
    if not domain:
        return True
    hostname = (parse.urlsplit(url).hostname or '').casefold().removeprefix('www.')
    domain = domain.casefold().removeprefix('www.')
    return hostname == domain or hostname.endswith(f'.{domain}')


def render_qr_png(value):
    import qrcode

    image = qrcode.make(
        value,
        box_size=12,
        border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def clean_box_display_name(value, properties=()):
    value = str(value).strip()
    color_values = [
        property_value for property_name, property_value in properties
        if property_name.casefold().strip().rstrip(':') in {
            'цвет', 'цвет товара', 'цвет корпуса',
        }
    ]
    for color_value in color_values:
        value = re.sub(re.escape(str(color_value)), '', value, flags=re.I)
    color_pattern = (
        r'\b(?:белый|ч[её]рный|серый|серебристый|графитовый|красный|синий|'
        r'зел[её]ный|коричневый|бежевый|оранжевый)'
        r'(?:\s+(?:матовый|глянцевый|карбон|металлик|aeroskin|аэроскин))?\b'
    )
    value = re.sub(color_pattern, '', value, flags=re.I)
    value = re.sub(r'\s+', ' ', value)
    value = re.sub(r'\s*,\s*(?=$)', '', value)
    value = re.sub(r'\s+([,)])', r'\1', value)
    return value.strip(' ,—-')


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
        self._definition_term = None
        self._definition_value = None

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
        elif tag == 'dt':
            self._definition_term = []
        elif tag == 'dd':
            self._definition_value = []
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
        elif tag == 'dt' and self._definition_term is not None:
            value = re.sub(r'\s+', ' ', ''.join(self._definition_term)).strip()
            self._definition_term = value or None
        elif tag == 'dd' and self._definition_value is not None:
            value = re.sub(r'\s+', ' ', ''.join(self._definition_value)).strip()
            if isinstance(self._definition_term, str) and value:
                self.rows.append([self._definition_term, value])
            self._definition_term = None
            self._definition_value = None
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
        if isinstance(self._definition_term, list):
            self._definition_term.append(data)
        if self._definition_value is not None:
            self._definition_value.append(data)
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


def _cache_busted_url(url):
    parts = parse.urlsplit(url)
    query = [
        (key, value) for key, value in parse.parse_qsl(
            parts.query, keep_blank_values=True,
        )
        if key != '_price_tag_refresh'
    ]
    query.append(('_price_tag_refresh', str(time.time_ns())))
    return parse.urlunsplit(parts._replace(query=parse.urlencode(query)))


def _clean_download_url(url):
    parts = parse.urlsplit(url)
    query = [
        (key, value) for key, value in parse.parse_qsl(
            parts.query, keep_blank_values=True,
        )
        if key != '_price_tag_refresh'
    ]
    return parse.urlunsplit(parts._replace(query=parse.urlencode(query)))


def _download(url, *, force_refresh=False):
    request_url = _cache_busted_url(url) if force_refresh else url
    _validate_public_url(request_url)
    opener = request.build_opener(_SafeRedirectHandler())
    req = request.Request(
        request_url,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 Chrome/124 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'ru-RU,ru;q=0.9',
            **({
                'Cache-Control': 'no-cache, no-store, max-age=0',
                'Pragma': 'no-cache',
            } if force_refresh else {}),
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
            final_url = response.geturl()
            if force_refresh:
                final_url = _clean_download_url(final_url)
            return raw.decode(charset, errors='replace'), final_url
    except ProductImportError:
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProductImportError('Не удалось загрузить карточку товара.') from exc


def _fetch_html(url, force_refresh=False):
    if force_refresh:
        return _download(url, force_refresh=True)
    return _download(url)


def download_product_image(url):
    _validate_public_url(url)
    opener = request.build_opener(_SafeRedirectHandler())
    req = request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 PriceTagImage/1.0'},
    )
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith('image/'):
                raise ProductImportError('Ссылка ведёт не на изображение.')
            raw = response.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                raise ProductImportError('Изображение слишком большое.')
            return raw, content_type
    except ProductImportError:
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProductImportError('Не удалось загрузить фото товара.') from exc


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


def _pinel_equipment_links(html, base_url):
    """Read PINEL's visible equipment selector in the same order as the page."""
    block_match = re.search(
        r'<div[^>]+class=["\'][^"\']*\bequipments_list\b[^"\']*["\'][^>]*>'
        r'(?P<body>.*?)</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not block_match:
        return []
    links = []
    for anchor in re.finditer(
        r'<a\b(?P<attrs>[^>]*)>(?P<text>.*?)</a>',
        block_match.group('body'),
        flags=re.IGNORECASE | re.DOTALL,
    ):
        attrs = anchor.group('attrs')
        href_match = re.search(r'\bhref=["\']([^"\']+)', attrs, re.IGNORECASE)
        title_match = re.search(r'\btitle=["\']([^"\']+)', attrs, re.IGNORECASE)
        label = title_match.group(1) if title_match else re.sub(
            r'<[^>]+>', '', anchor.group('text'),
        )
        label = re.sub(r'\s+', ' ', label).strip()
        if href_match and label:
            links.append((label, parse.urljoin(base_url, href_match.group(1))))
    return links[:5]


def _offer_from_html(html):
    parser = _ProductHTMLParser()
    parser.feed(html)
    candidates = [
        item for document in parser.json_ld
        for item in _walk_json(document) if _is_product(item)
    ]
    offer = _first_offer(candidates[0]) if candidates else {}
    price = str(
        offer.get('price') or offer.get('lowPrice')
        or parser.meta.get('product:price:amount')
        or parser.meta.get('og:price:amount') or ''
    ).strip()
    if not price:
        page_text = ' '.join(parser.text_parts)
        price_match = re.search(
            r'(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{3,})\s*'
            r'(?:₽|[pр]\b)',
            page_text,
            re.IGNORECASE,
        )
        if price_match:
            price = re.sub(r'[\s\u00a0]+', '', price_match.group(1))
    currency = str(
        offer.get('priceCurrency')
        or parser.meta.get('product:price:currency')
        or parser.meta.get('og:price:currency') or 'RUB'
    ).strip()
    return price, currency


def _pinel_base_sku(sku):
    value = re.sub(r'\s+', '', str(sku)).upper()
    return re.sub(r'U[A-Z]$', '', value)


def _pinel_variant_label(name):
    if re.search(r'\bбез\s+(?:акб|аккумулятора)', name, re.IGNORECASE):
        return 'Без АКБ и ЗУ'
    capacity = re.search(
        r'(?:с\s+)?АКБ\s+((?:\d+\s*[xх×]\s*)?\d+(?:[.,]\d+)?)\s*'
        r'(?:А\s*[/.*]?\s*ч|Ач)',
        name,
        re.IGNORECASE,
    )
    if capacity:
        value = re.sub(r'\s*[xх×]\s*', '×', capacity.group(1))
        return f'+ АКБ {value.replace(".", ",")} А/ч и ЗУ'
    bare_capacity = re.fullmatch(
        r'\s*((?:\d+\s*[xх×]\s*)?\d+(?:[.,]\d+)?)\s*А\s*[/.*]?\s*ч\s*',
        name,
        re.IGNORECASE,
    )
    if bare_capacity:
        value = re.sub(r'\s*[xх×]\s*', '×', bare_capacity.group(1))
        return f'+ АКБ {value.replace(".", ",")} А/ч и ЗУ'
    return str(name).strip()


def _pinel_search_variants(
    sku, final_url, current_price='', current_currency='RUB',
    force_refresh=False,
):
    base_sku = _pinel_base_sku(sku)
    if not base_sku:
        return []
    search_url = parse.urljoin(final_url, '/search/?') + parse.urlencode(
        {'q': base_sku},
    )
    try:
        # PINEL returns an incomplete search page to some server-side clients
        # when an extra cache-busting query parameter is present. Keep its
        # canonical search URL; variant product pages are still refreshed.
        search_html, _ = _fetch_html(search_url)
    except ProductImportError:
        return []
    products_marker = re.search(
        r'search_page_text_caption[^>]*>\s*Товары\b',
        search_html,
        re.IGNORECASE,
    )
    products_html = (
        search_html[products_marker.start():] if products_marker else search_html
    )
    card_starts = list(re.finditer(
        r'<div\s+class=["\'][^"\']*\bproduct__item\b[^"\']*["\']',
        products_html,
        re.IGNORECASE,
    ))
    variants = []
    seen_urls = set()
    for index, card_start in enumerate(card_starts):
        end = (
            card_starts[index + 1].start()
            if index + 1 < len(card_starts) else len(products_html)
        )
        card = products_html[card_start.start():end]
        title_match = re.search(
            r'<a\b(?=[^>]*\bclass=["\'][^"\']*\bitem__title\b)'
            r'(?P<attrs>[^>]*)>(?P<name>.*?)</a>',
            card,
            re.IGNORECASE | re.DOTALL,
        )
        title_attrs = title_match.group('attrs') if title_match else ''
        href_match = re.search(
            r'\bhref=["\'](?P<href>/catalog/sku/[^"\']+)["\']',
            title_attrs,
            re.IGNORECASE,
        )
        sku_match = re.search(
            r'item__vendor_code[^>]*>\s*Артикул:\s*([^<]+)</p>',
            card,
            re.IGNORECASE,
        )
        price_match = re.search(
            r'<span\b[^>]*class=["\'][^"\']*\bprice\b[^"\']*["\']'
            r'[^>]*>\s*([\d\s\u00a0]+)\s*₽',
            card,
            re.IGNORECASE,
        )
        if not title_match or not href_match or not sku_match:
            continue
        candidate_sku = re.sub(r'\s+', '', sku_match.group(1))
        if _pinel_base_sku(candidate_sku) != base_sku:
            continue
        url = parse.urljoin(final_url, href_match.group('href'))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        name = re.sub(r'<[^>]+>', '', title_match.group('name'))
        name = re.sub(r'\s+', ' ', name).strip()
        price = re.sub(
            r'[\s\u00a0]+', '', price_match.group(1) if price_match else '',
        )
        currency = 'RUB'
        current_path = parse.urlsplit(final_url).path.rstrip('/')
        candidate_path = parse.urlsplit(url).path.rstrip('/')
        if not price and candidate_path == current_path:
            price, currency = current_price, current_currency
        elif not price:
            try:
                variant_html, resolved_url = _fetch_html(url, force_refresh)
                price, currency = _offer_from_html(variant_html)
                url = resolved_url
            except ProductImportError:
                continue
        if not price:
            continue
        label = _pinel_variant_label(name)
        variants.append({
            'label': label,
            'price': price,
            'currency': currency,
            'url': url,
            'is_base': label == 'Без АКБ и ЗУ',
        })
    variants.sort(key=lambda item: (
        0 if item['is_base'] else 1,
        Decimal(str(item['price']).replace(' ', '').replace(',', '.')),
    ))
    if len(variants) <= 5:
        return variants
    current_path = parse.urlsplit(final_url).path.rstrip('/')
    selected = variants[:5]
    if not any(
        parse.urlsplit(item['url']).path.rstrip('/') == current_path
        for item in selected
    ):
        current = next((
            item for item in variants
            if parse.urlsplit(item['url']).path.rstrip('/') == current_path
        ), None)
        if current:
            selected[-1] = current
    return selected


def _pinel_price_variants(
    html, final_url, current_price, current_currency, sku='',
    force_refresh=False,
):
    search_variants = _pinel_search_variants(
        sku, final_url, current_price, current_currency, force_refresh,
    )
    if len(search_variants) >= 2:
        return search_variants
    links = _pinel_equipment_links(html, final_url)
    if len(links) < 2:
        return []
    current_path = parse.urlsplit(final_url).path.rstrip('/')
    variants = []
    for label, variant_url in links:
        variant_path = parse.urlsplit(variant_url).path.rstrip('/')
        if variant_path == current_path:
            price, currency, resolved_url = (
                current_price, current_currency, final_url,
            )
        else:
            try:
                variant_html, resolved_url = _fetch_html(
                    variant_url, force_refresh,
                )
                price, currency = _offer_from_html(variant_html)
            except ProductImportError:
                continue
        if price:
            normalized_label = _pinel_variant_label(label)
            variants.append({
                'label': normalized_label,
                'price': price,
                'currency': currency,
                'url': resolved_url,
                'is_base': normalized_label == 'Без АКБ и ЗУ',
            })
    variants.sort(key=lambda item: (
        0 if item['is_base'] else 1,
        Decimal(str(item['price']).replace(' ', '').replace(',', '.')),
    ))
    return variants


def _absolute_url(value, base_url):
    if isinstance(value, list):
        value = value[0] if value else ''
    if isinstance(value, dict):
        value = value.get('url') or value.get('contentUrl') or ''
    return parse.urljoin(base_url, str(value)) if value else ''


def _looks_like_property_name(value):
    normalized = value.casefold().strip().rstrip(':')
    markers = (
        'производит', 'бренд', 'гарант', 'объем', 'объём', 'размер',
        'цвет', 'поверхност', 'грузоподъем', 'грузоподъём', 'вес',
        'креплен', 'открыт', 'замок', 'профил', 'установк', 'материал',
        'мощност', 'напряжен', 'емкост', 'ёмкост', 'ширин', 'длин',
        'высот', 'диаметр', 'скорост', 'оборот', 'тип', 'вид', 'место',
        'комплект', 'модель', 'артикул',
    )
    return any(marker in normalized for marker in markers)


def _pinel_feature_properties(html):
    block = re.search(
        r'<div\s+id=["\']features["\'][^>]*>(.*?)'
        r'<div\s+id=["\']description["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not block:
        return []
    result = []
    for row in re.finditer(
        r'product__content_features_name[^>]*>(?P<name>.*?)</div>\s*'
        r'<div[^>]*class=["\'][^"\']*product__content_features_value'
        r'[^"\']*["\'][^>]*>(?P<value>.*?)</div>',
        block.group(1),
        re.IGNORECASE | re.DOTALL,
    ):
        name = unescape(re.sub(r'<[^>]+>', '', row.group('name')))
        value = unescape(re.sub(r'<[^>]+>', '', row.group('value')))
        name = re.sub(r'\s+', ' ', name).strip().rstrip(':')
        value = re.sub(r'\s+', ' ', value).strip()
        if name and value:
            result.append((name, value))
    return result


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
        for index, text in enumerate(parser.text_parts[:-1]):
            name = text.strip().rstrip(':')
            value = parser.text_parts[index + 1].strip()
            key = name.casefold()
            if (
                text.strip().endswith(':')
                and _looks_like_property_name(name)
                and 1 < len(name) <= 80
                and 0 < len(value) <= 160
                and key not in seen
            ):
                result.append((name, value))
                seen.add(key)
    return result


def _brand_name(product, properties):
    brand = product.get('brand')
    if isinstance(brand, dict):
        brand = brand.get('name')
    if brand:
        return str(brand).strip()
    for name, value in properties:
        if name.casefold().rstrip(':') in {'бренд', 'производител'}:
            return value.split(',')[0].strip()
    return ''


def _product_identity(product, name, properties, meta):
    brand = _brand_name(product, properties)
    category = str(
        product.get('category') or meta.get('product:category') or ''
    ).strip()
    model = str(product.get('model') or '').strip()
    product_type = ''
    if brand and brand.casefold() in name.casefold():
        before, after = re.split(re.escape(brand), name, maxsplit=1, flags=re.I)
        product_type = before.strip(' ,—-')
        if not model:
            model = after.strip(' ,—-')[:100]
    if not product_type and category:
        product_type = category.split('>')[-1].strip()
    if not product_type:
        known_types = (
            'Бокс на крышу',
            'Автомобильный бокс',
            'Автобокс',
            'Газонокосилка',
            'Снегоуборщик',
            'Триммер',
            'Воздуходувка',
            'Бензопила',
            'Цепная пила',
        )
        for known_type in known_types:
            if name.casefold().startswith(known_type.casefold()):
                product_type = name[:len(known_type)]
                if not model:
                    model = name[len(known_type):].strip(' ,—-')
                break
    return brand, model, product_type, category


def clean_product_name(value):
    value = re.sub(r'\s+', ' ', str(value)).strip()
    value = re.sub(r'^Купить\s+', '', value, flags=re.I)
    value = re.split(r'\s*\|\s*(?:купить|цена|заказать)\b', value, maxsplit=1, flags=re.I)[0]
    value = re.split(r'\s+[—-]\s*купить\b', value, maxsplit=1, flags=re.I)[0]
    value = re.split(r'\s+в магазине\s+', value, maxsplit=1, flags=re.I)[0]
    value = re.sub(
        r'\s*(?:[|—-]\s*)?(?:купить\s+)?в\s+Москве'
        r'(?:\s+и|\s*,\s*)\s+Санкт[-‑ ]Петербурге.*$',
        '',
        value,
        flags=re.I,
    )
    value = re.sub(
        r'\s+\d{2,4}(?:[.,]\d+)?\s*[xх×*]\s*\d{2,4}(?:[.,]\d+)?'
        r'(?:\s*[xх×*]\s*\d{2,4}(?:[.,]\d+)?)?\s*$',
        '',
        value,
        flags=re.I,
    )
    value = value.strip(' ,—-')
    return value[:1].upper() + value[1:] if value else value


def suggest_product_name(product, corrections):
    """Conservatively reuse exact edits and repeated removed phrases."""
    base_name = product.prominent_name.strip()
    product.original_name = base_name
    normalized_url = product.url.split('#', 1)[0]
    for correction in corrections:
        if correction.source_url.split('#', 1)[0] == normalized_url:
            correction.use_count += 1
            correction.save(update_fields=('use_count', 'updated_at'))
            product.suggested_name = correction.corrected_name
            return product
    best = None
    for correction in corrections:
        ratio = SequenceMatcher(
            None,
            correction.original_name.casefold(),
            base_name.casefold(),
        ).ratio()
        if ratio >= 0.45 and (best is None or ratio > best[0]):
            best = (ratio, correction)
    if best is None:
        return product
    correction = best[1]
    matcher = SequenceMatcher(
        None,
        correction.original_name.casefold(),
        correction.corrected_name.casefold(),
    )
    learned = base_name
    removed_phrases = [
        correction.original_name[start:end].strip(' ,|—-')
        for operation, start, end, _, _ in matcher.get_opcodes()
        if operation == 'delete' and end - start >= 4
    ]
    for phrase in removed_phrases:
        learned = re.sub(re.escape(phrase), '', learned, flags=re.I)
    learned = re.sub(r'\s+', ' ', learned).strip(' ,|—-')
    if learned and learned != base_name:
        product.suggested_name = learned
    return product


def apply_category_rules(product, categories, max_properties=5):
    matched = None
    for category in categories:
        if category_matches_product(product, category):
            matched = category
            break
    product.category_rule = matched
    if matched:
        select_product_properties(
            product,
            matched.property_name_list,
            max_properties,
        )
    else:
        select_product_properties(product, (), max_properties)
    return product


def category_matches_product(product, category):
    profile = category.profile
    if (
        profile.category_detection_mode
        == profile.CategoryDetectionMode.PROPERTY
    ):
        expected_name = category.match_property_name.casefold().strip().rstrip(':')
        expected_value = category.match_property_value.casefold().strip()
        if not expected_name or not expected_value:
            return False
        return any(
            name.casefold().strip().rstrip(':') == expected_name
            and expected_value in value.casefold().strip()
            for name, value in product.properties
        )
    if not category.source_url:
        return False
    product_parts = parse.urlsplit(product.url)
    section_parts = parse.urlsplit(category.source_url)
    product_host = (product_parts.hostname or '').casefold().removeprefix('www.')
    section_host = (section_parts.hostname or '').casefold().removeprefix('www.')
    product_path = parse.unquote(product_parts.path).casefold().rstrip('/') + '/'
    section_path = parse.unquote(section_parts.path).casefold().rstrip('/') + '/'
    return product_host == section_host and product_path.startswith(section_path)


def select_product_properties(product, requested_names=(), max_properties=5):
    max_properties = min(max_properties, 5)
    normalized_names = [
        name.casefold().strip().rstrip(':') for name in requested_names
    ]
    available = {
        name.casefold().strip().rstrip(':'): (name, value)
        for name, value in product.properties
    }
    selected = []
    for key in normalized_names:
        exact = available.get(key)
        if exact:
            selected.append(exact)
            continue
        fuzzy = next(
            (
                pair for available_name, pair in available.items()
                if key in available_name or available_name in key
            ),
            None,
        )
        if fuzzy:
            selected.append(fuzzy)
    if not normalized_names:
        selected = product.properties[:max_properties]
    selected = selected[:max_properties]
    selected_keys = {
        name.casefold().strip().rstrip(':') for name, _ in selected
    }
    product.displayed_properties = selected
    remaining = [
        (name, value) for name, value in product.properties
        if name.casefold().strip().rstrip(':') not in selected_keys
    ]
    product.property_rows = [
        (
            name,
            value,
            True,
        )
        for name, value in selected
    ] + [(name, value, False) for name, value in remaining]
    return product


def import_product(url, *, _resolve_pinel_base=True, force_refresh=False):
    html, final_url = _fetch_html(url, force_refresh)
    parser = _ProductHTMLParser()
    parser.feed(html)
    candidates = [
        item for document in parser.json_ld
        for item in _walk_json(document) if _is_product(item)
    ]
    product = candidates[0] if candidates else {}
    offer = _first_offer(product)
    meta = parser.meta
    source_h1 = ''.join(parser.h1_parts).strip()
    name = (
        source_h1 or product.get('name') or meta.get('og:title')
        or meta.get('twitter:title')
        or ''.join(parser.title_parts).strip()
    )
    name = clean_product_name(name)
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
    hostname = (parse.urlsplit(final_url).hostname or '').casefold()
    is_pinel = hostname == 'pinel.ru' or hostname.endswith('.pinel.ru')
    if is_pinel:
        properties = _properties(product)
        seen_property_names = {name.casefold() for name, _ in properties}
        for property_name, property_value in _pinel_feature_properties(html):
            if property_name.casefold() not in seen_property_names:
                properties.append((property_name, property_value))
                seen_property_names.add(property_name.casefold())
    else:
        properties = _properties(product, parser)
    brand, model, product_type, category = _product_identity(
        product, name, properties, meta,
    )
    imported = ImportedProduct(
        url=final_url,
        name=name,
        price=str(price).strip(),
        currency=str(currency).strip(),
        sku=str(sku).strip(),
        image_url=image_url,
        properties=properties,
        source_name=parse.urlsplit(final_url).hostname or '',
        brand=brand,
        model=model,
        product_type=product_type,
        category_name=category,
        source_h1=source_h1,
    )
    if is_pinel:
        if not _resolve_pinel_base:
            return imported
        imported.price_variants = _pinel_price_variants(
            html, final_url, imported.price, imported.currency, imported.sku,
            force_refresh,
        )
        base_variant = next((
            item for item in imported.price_variants
            if item.get('is_base') and item.get('url')
        ), None)
        if base_variant:
            base_path = parse.urlsplit(base_variant['url']).path.rstrip('/')
            current_path = parse.urlsplit(final_url).path.rstrip('/')
            if base_path != current_path:
                try:
                    base_product = import_product(
                        base_variant['url'],
                        _resolve_pinel_base=False,
                        force_refresh=force_refresh,
                    )
                except ProductImportError:
                    pass
                else:
                    base_product.price_variants = imported.price_variants
                    return base_product
    return imported
