import pytest
from django.urls import reverse

from checklists.models import (
    EmployeeProfile,
    Store,
    StorePriceTagCategory,
    StorePriceTagTemplate,
)
from checklists.price_tags import (
    ImportedProduct,
    apply_category_rules,
    build_qr_url,
    clean_product_name,
    import_product,
)
from checklists.test_portals import create_access_user


pytestmark = pytest.mark.django_db


@pytest.fixture
def price_tag_setup():
    store = Store.objects.create(
        name='Магазин ценников',
        code='price-tags',
    )
    director, _, _ = create_access_user(
        'price-tags-director',
        EmployeeProfile.Role.STORE_DIRECTOR,
        store,
    )
    admin, _, _ = create_access_user(
        'price-tags-admin',
        EmployeeProfile.Role.SYSTEM_ADMIN,
    )
    terminal, _, _ = create_access_user(
        'price-tags-terminal',
        EmployeeProfile.Role.STORE_ACCOUNT,
        store,
    )
    es_profile = StorePriceTagTemplate.objects.create(
        store=store,
        name='ES-AUTO',
        site_domain='example.test',
    )
    pinel_profile = StorePriceTagTemplate.objects.create(
        store=store,
        name='PINEL',
        site_domain='pinel.test',
    )
    return {
        'store': store,
        'director': director,
        'admin': admin,
        'terminal': terminal,
        'es_profile': es_profile,
        'pinel_profile': pinel_profile,
    }


def test_product_is_imported_from_schema_org(monkeypatch):
    html = '''
        <html><head>
        <meta property="og:image" content="/fallback.jpg">
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product",
         "name":"Бокс Broomer Venture L","sku":"1617","brand":{"name":"Broomer"},
         "image":"/box.jpg","offers":{"price":"54990","priceCurrency":"RUB"},
         "additionalProperty":[{"name":"Объём","value":"430 л"}]}
        </script></head></html>
    '''
    monkeypatch.setattr(
        'checklists.price_tags._download',
        lambda url: (html, 'https://shop.example/catalog/1617/'),
    )

    product = import_product('https://shop.example/catalog/1617/')

    assert product.name == 'Бокс Broomer Venture L'
    assert product.formatted_price == '54 990 ₽'
    assert product.sku == '1617'
    assert product.image_url == 'https://shop.example/box.jpg'
    assert ('Бренд', 'Broomer') in product.properties
    assert ('Объём', '430 л') in product.properties
    assert product.prominent_name == 'Broomer Venture L'
    assert product.secondary_name == 'Бокс'


def test_product_falls_back_to_heading_price_and_table(monkeypatch):
    html = '''
        <html><head><meta property="og:image" content="/item.jpg"></head>
        <body><h1>Автобокс Venture L</h1><div>Артикул: 01.129.01</div>
        <strong>58&nbsp;900 p</strong><table>
        <tr><td>Объем (л.)</td><td>430</td></tr>
        <tr><td>Цвет</td><td>Черный матовый</td></tr>
        </table></body></html>
    '''
    monkeypatch.setattr(
        'checklists.price_tags._download',
        lambda url: (html, 'https://shop.example/product/'),
    )

    product = import_product('https://shop.example/product/')

    assert product.name == 'Автобокс Venture L'
    assert product.formatted_price == '58 900 ₽'
    assert product.sku == '01.129.01'
    assert ('Объем (л.)', '430') in product.properties


def test_director_can_generate_and_edit_store_profile(
    client,
    price_tag_setup,
    monkeypatch,
):
    monkeypatch.setattr(
        'checklists.portal_views.import_product',
        lambda url: ImportedProduct(
            url=url,
            name='Тестовый автобокс',
            price='50000',
            sku='BOX-1',
            properties=[('Объём', '430 л')],
            source_name='example.test',
        ),
    )
    client.force_login(price_tag_setup['director'])

    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'generate',
            'profile': price_tag_setup['es_profile'].pk,
            'urls': 'https://example.test/product/1/',
        },
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert 'Тестовый автобокс' in content
    assert '50 000 ₽' in content
    assert 'На листе A4 будет 4 ценника' in content
    assert 'price-tags/qr/?data=https%3A//example.test/product/1/' in content
    assert 'Дата печати:' in content
    saved = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'action': 'save_profile',
            'profile_id': price_tag_setup['es_profile'].pk,
            'name': 'ES-AUTO',
            'site_domain': 'example.test',
            'category_detection_mode': StorePriceTagTemplate.CategoryDetectionMode.URL,
            'heading': 'Акция',
            'primary_color': '#112233',
            'accent_color': '#ff6600',
            'max_properties': 4,
            'print_mode': StorePriceTagTemplate.PrintMode.COLOR,
        },
    )
    assert saved.status_code == 302
    price_tag_setup['es_profile'].refresh_from_db()
    assert price_tag_setup['es_profile'].heading == 'Акция'


def test_admin_can_save_separate_store_template(client, price_tag_setup):
    client.force_login(price_tag_setup['admin'])
    client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': price_tag_setup['store'].pk},
    )

    response = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'action': 'save_profile',
            'profile_id': price_tag_setup['es_profile'].pk,
            'name': 'ES-AUTO',
            'site_domain': 'example.test',
            'category_detection_mode': StorePriceTagTemplate.CategoryDetectionMode.URL,
            'heading': 'Лучшая цена',
            'primary_color': '#112233',
            'accent_color': '#ff6600',
            'show_image': 'on',
            'show_sku': 'on',
            'show_properties': 'on',
            'max_properties': 4,
            'footer': 'Цена действительна на дату печати',
            'qr_utm_parameters': 'utm_source=price_tag&utm_medium=offline',
            'print_mode': StorePriceTagTemplate.PrintMode.MONOCHROME,
        },
    )

    assert response.status_code == 302
    template = StorePriceTagTemplate.objects.get(
        pk=price_tag_setup['es_profile'].pk,
    )
    assert template.heading == 'Лучшая цена'
    assert template.max_properties == 4
    assert template.qr_utm_parameters == 'utm_source=price_tag&utm_medium=offline'
    assert template.print_mode == StorePriceTagTemplate.PrintMode.MONOCHROME


def test_director_can_create_another_site_profile(client, price_tag_setup):
    client.force_login(price_tag_setup['director'])

    response = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'action': 'save_profile',
            'create_profile': '1',
            'name': 'Запасной сайт',
            'site_domain': 'shop.example.test',
            'category_detection_mode': StorePriceTagTemplate.CategoryDetectionMode.URL,
            'primary_color': '#112233',
            'accent_color': '#ff6600',
            'max_properties': 5,
            'print_mode': StorePriceTagTemplate.PrintMode.COLOR,
            'is_active': 'on',
        },
    )

    assert response.status_code == 302
    assert StorePriceTagTemplate.objects.filter(
        store=price_tag_setup['store'],
        name='Запасной сайт',
        site_domain='shop.example.test',
    ).exists()


def test_director_defines_url_category_in_site_profile(client, price_tag_setup):
    profile = price_tag_setup['es_profile']
    client.force_login(price_tag_setup['director'])

    response = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'action': 'save_category',
            'profile_id': profile.pk,
            'name': 'Автомобильные боксы',
            'source_url': 'https://example.test/car-box/',
            'sort_order': 10,
            'is_active': 'on',
        },
    )

    assert response.status_code == 302
    category = StorePriceTagCategory.objects.get(
        profile=profile,
        name='Автомобильные боксы',
    )
    assert category.source_url == 'https://example.test/car-box/'
    assert category.property_names == ''


def test_url_category_must_belong_to_profile_domain(client, price_tag_setup):
    profile = price_tag_setup['es_profile']
    client.force_login(price_tag_setup['director'])

    response = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'action': 'save_category',
            'profile_id': profile.pk,
            'name': 'Чужой раздел',
            'source_url': 'https://pinel.test/catalog/',
            'sort_order': 1,
            'is_active': 'on',
        },
    )

    assert response.status_code == 200
    assert 'Ссылка должна вести на сайт example.test.' in response.content.decode()
    assert not StorePriceTagCategory.objects.filter(
        profile=profile,
        name='Чужой раздел',
    ).exists()


def test_profile_shows_uploaded_logo_preview(client, price_tag_setup):
    template = price_tag_setup['es_profile']
    template.logo.name = 'stores/price_tag_logo/es-auto.png'
    template.save(update_fields=('logo',))
    client.force_login(price_tag_setup['director'])

    response = client.get(
        reverse('checklists:price_tag_profile'),
        {'profile': template.pk},
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert 'src="/media/stores/price_tag_logo/es-auto.png"' in content
    assert 'alt="Логотип ES-AUTO"' in content
    assert 'Currently:' not in content


def test_qr_endpoint_returns_server_generated_svg(
    client,
    price_tag_setup,
    monkeypatch,
):
    monkeypatch.setattr(
        'checklists.portal_views.render_qr_svg',
        lambda value: f'<svg><title>{value}</title></svg>'.encode(),
    )
    client.force_login(price_tag_setup['director'])

    response = client.get(
        reverse('checklists:price_tag_qr'),
        {'data': 'https://example.test/product/?utm_source=price_tag'},
    )

    assert response.status_code == 200
    assert response['Content-Type'] == 'image/svg+xml'
    assert b'utm_source=price_tag' in response.content


def test_qr_endpoint_rejects_non_web_url(client, price_tag_setup):
    client.force_login(price_tag_setup['director'])

    response = client.get(
        reverse('checklists:price_tag_qr'),
        {'data': 'javascript:alert(1)'},
    )

    assert response.status_code == 400


def test_qr_url_keeps_product_parameters_and_applies_store_utm():
    result = build_qr_url(
        'https://example.test/product/?variant=white&utm_source=old#details',
        'utm_source=price_tag&utm_medium=offline',
    )

    assert result == (
        'https://example.test/product/?variant=white&utm_source=price_tag'
        '&utm_medium=offline#details'
    )


def test_monochrome_price_tag_uses_utm_qr_and_header_photo(
    client,
    price_tag_setup,
    monkeypatch,
):
    profile = price_tag_setup['es_profile']
    profile.print_mode = StorePriceTagTemplate.PrintMode.MONOCHROME
    profile.qr_utm_parameters = 'utm_source=price_tag&utm_medium=offline'
    profile.save(update_fields=('print_mode', 'qr_utm_parameters'))
    monkeypatch.setattr(
        'checklists.portal_views.import_product',
        lambda url: ImportedProduct(
            url='https://example.test/product/?variant=white',
            name='Бокс Element 590',
            image_url='https://example.test/product.jpg',
        ),
    )
    client.force_login(price_tag_setup['director'])

    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'generate',
            'profile': profile.pk,
            'urls': 'https://example.test/product/',
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert 'print-mode-monochrome' in content
    assert 'class="price-tag-head"' in content
    assert 'class="price-tag-image"' in content
    assert 'width: 44mm; height: 27mm' in content
    assert 'align-items: center; justify-content: center' in content
    assert (
        'price-tags/qr/?data=https%3A//example.test/product/%3Fvariant%3Dwhite%26'
        'utm_source%3Dprice_tag%26utm_medium%3Doffline'
    ) in content
    assert '>Боксы<' not in content


def test_terminal_account_can_select_loaded_category_properties(
    client,
    price_tag_setup,
    monkeypatch,
):
    profile = price_tag_setup['es_profile']
    category = StorePriceTagCategory.objects.create(
        profile=profile,
        name='Газонокосилки',
        source_url='https://example.test/lawn-mowers/',
    )
    monkeypatch.setattr(
        'checklists.portal_views.import_product',
        lambda url: ImportedProduct(
            url=url,
            name='Газонокосилка Greenworks',
            properties=[
                ('Мощность', '2 кВт'),
                ('Ширина скашивания', '46 см'),
            ],
        ),
    )
    client.force_login(price_tag_setup['terminal'])
    empty_response = client.get(
        reverse('checklists:director_price_tags'),
    )
    assert empty_response.status_code == 200
    assert 'Свойства найденных категорий' not in empty_response.content.decode()

    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'generate',
            'urls': 'https://example.test/lawn-mowers/item-1/',
        },
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert 'ES-AUTO · Газонокосилки' in content
    assert 'Мощность' in content
    assert 'Ширина скашивания' in content

    saved = client.post(
        reverse('checklists:price_tag_category_properties'),
        {
            'category_id': category.pk,
            'property_names': ['Мощность'],
        },
    )
    assert saved.status_code == 200
    category.refresh_from_db()
    assert category.property_names == 'Мощность'

    forbidden = client.post(
        reverse('checklists:price_tag_profile'),
        {'action': 'delete_category', 'profile_id': profile.pk,
         'category_id': category.pk},
    )
    assert forbidden.status_code == 403


def test_category_rule_selects_and_orders_properties(price_tag_setup):
    category = StorePriceTagCategory.objects.create(
        profile=price_tag_setup['es_profile'],
        name='Снегоуборщики',
        source_url='https://example.test/snow/',
        property_names='Ширина захвата\nМощность\nВес',
    )
    product = ImportedProduct(
        url='https://example.test/snow/',
        name='Снегоуборщик Greenworks GD82ST',
        product_type='Снегоуборщик',
        properties=[
            ('Вес', '68 кг'),
            ('Мощность', '2.2 кВт'),
            ('Цвет', 'зеленый'),
            ('Ширина захвата', '61 см'),
        ],
    )

    apply_category_rules(product, [category], max_properties=3)

    assert product.category_rule == category
    assert product.displayed_properties == [
        ('Ширина захвата', '61 см'),
        ('Мощность', '2.2 кВт'),
        ('Вес', '68 кг'),
    ]


def test_site_profiles_have_separate_url_categories(price_tag_setup):
    es_category = StorePriceTagCategory.objects.create(
        profile=price_tag_setup['es_profile'],
        name='Автомобильные боксы',
        source_url='https://example.test/car-box/',
        property_names='Объём',
    )
    StorePriceTagCategory.objects.create(
        profile=price_tag_setup['pinel_profile'],
        name='Каталог PINEL',
        source_url='https://pinel.test/catalog/',
        property_names='Мощность',
    )
    product = ImportedProduct(
        url='https://example.test/car-box/sku/element-590/',
        name='Бокс Element 590',
        properties=[('Объём', '590 л'), ('Мощность', '1 кВт')],
    )

    apply_category_rules(
        product,
        list(price_tag_setup['es_profile'].categories.all()),
    )

    assert product.category_rule == es_category
    assert product.displayed_properties == [('Объём', '590 л')]


def test_unknown_domain_is_rejected_before_import(
    client,
    price_tag_setup,
    monkeypatch,
):
    imported = []
    monkeypatch.setattr(
        'checklists.portal_views.import_product',
        lambda url: imported.append(url),
    )
    client.force_login(price_tag_setup['director'])

    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'generate',
            'urls': 'https://unknown.test/car-box/sku/element-590/',
        },
    )

    assert response.status_code == 200
    assert 'Для домена этой ссылки не настроен профиль' in response.content.decode()
    assert imported == []


def test_mixed_domains_automatically_use_their_own_profiles(
    client,
    price_tag_setup,
    monkeypatch,
):
    pinel = price_tag_setup['pinel_profile']
    pinel.category_detection_mode = StorePriceTagTemplate.CategoryDetectionMode.PROPERTY
    pinel.print_mode = StorePriceTagTemplate.PrintMode.MONOCHROME
    pinel.save(update_fields=('category_detection_mode', 'print_mode'))
    StorePriceTagCategory.objects.create(
        profile=pinel,
        name='Газонокосилки PINEL',
        match_property_name='Тип товара',
        match_property_value='Газонокосилка',
    )

    def fake_import(url):
        return ImportedProduct(
            url=url,
            name='PINEL mower' if 'pinel.test' in url else 'ES-AUTO box',
            properties=(
                [('Тип товара', 'Газонокосилка'), ('Мощность', '2 кВт')]
                if 'pinel.test' in url else []
            ),
        )

    monkeypatch.setattr('checklists.portal_views.import_product', fake_import)
    client.force_login(price_tag_setup['director'])
    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'generate',
            'urls': (
                'https://example.test/product/1/\n'
                'https://pinel.test/catalog/product/2/'
            ),
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert 'ES-AUTO box' in content
    assert 'PINEL mower' in content
    assert 'print-mode-color' in content
    assert 'print-mode-monochrome' in content
    assert 'PINEL · Газонокосилки PINEL' in content
    assert content.count('class="price-tag-name" contenteditable="true"') == 2


def test_seo_product_name_is_cleaned_without_losing_variant():
    value = (
        'Купить бокс на крышу Element 590 белый карбон '
        '(скоба), Белый матовый 216x85x46 —купить в Москве'
    )

    assert clean_product_name(value) == (
        'Бокс на крышу Element 590 белый карбон '
        '(скоба), Белый матовый'
    )

    product = ImportedProduct(
        url='https://example.test/element/',
        name=clean_product_name(value),
    )
    from checklists.price_tags import _product_identity
    product.brand, product.model, product.product_type, product.category_name = (
        _product_identity({}, product.name, [], {})
    )
    assert product.secondary_name == 'Бокс на крышу'
    assert product.prominent_name == 'Element 590 (скоба)'


def test_four_price_tags_are_grouped_on_one_a4_sheet(
    client,
    price_tag_setup,
    monkeypatch,
):
    monkeypatch.setattr(
        'checklists.portal_views.import_product',
        lambda url: ImportedProduct(url=url, name='Товар', price='1000'),
    )
    client.force_login(price_tag_setup['director'])
    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'generate',
            'profile': price_tag_setup['es_profile'].pk,
            'urls': '\n'.join(
                f'https://example.test/product/{index}/'
                for index in range(1, 5)
            ),
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count('class="price-tag-sheet"') == 1
    assert content.count('class="price-tag print-mode-') == 4
    assert 'grid-template-columns: repeat(2, 105mm)' in content
    assert 'grid-template-rows: repeat(2, 148.5mm)' in content
