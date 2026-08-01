import ipaddress
import json
import re
import socket
from io import BytesIO
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
    brand: str = ''
    model: str = ''
    product_type: str = ''
    category_name: str = ''
    displayed_properties: list = field(default_factory=list)
    property_rows: list = field(default_factory=list)
    category_rule: object = None
    price_tag_profile: object = None
    tracking_url: str = ''

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

    @property
    def prominent_name(self):
        value = ' '.join(item for item in (self.brand, self.model) if item).strip()
        value = value or self.name
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
        for preferred_names in (
            {'производитель', 'производител', 'manufacturer'},
            {'бренд', 'brand'},
        ):
            for name, value in self.properties:
                if name.casefold().strip().rstrip(':') in preferred_names:
                    return value
        return self.brand

    @property
    def qr_url(self):
        return self.tracking_url or self.url


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


def render_qr_svg(value):
    import qrcode
    from qrcode.image.svg import SvgPathImage

    image = qrcode.make(
        value,
        image_factory=SvgPathImage,
        box_size=10,
        border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    output = BytesIO()
    image.save(output)
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
        r'\s+\d{2,4}(?:[.,]\d+)?\s*[xх×*]\s*\d{2,4}(?:[.,]\d+)?'
        r'(?:\s*[xх×*]\s*\d{2,4}(?:[.,]\d+)?)?\s*$',
        '',
        value,
        flags=re.I,
    )
    value = value.strip(' ,—-')
    return value[:1].upper() + value[1:] if value else value


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
    properties = _properties(product, parser)
    brand, model, product_type, category = _product_identity(
        product, name, properties, meta,
    )
    return ImportedProduct(
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
    )
