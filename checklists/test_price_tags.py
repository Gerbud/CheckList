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
    return {'store': store, 'director': director, 'admin': admin, 'terminal': terminal}


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
        {'action': 'generate', 'urls': 'https://example.test/product/1/'},
    )

    content = response.content.decode()
    assert response.status_code == 200
    assert 'Тестовый автобокс' in content
    assert '50 000 ₽' in content
    assert 'На листе A4 будет 4 ценника' in content
    assert 'data-qr-url="https://example.test/product/1/"' in content
    saved = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'heading': 'Акция',
            'primary_color': '#112233',
            'accent_color': '#ff6600',
            'max_properties': 4,
        },
    )
    assert saved.status_code == 302
    assert StorePriceTagTemplate.objects.get(
        store=price_tag_setup['store']
    ).heading == 'Акция'


def test_admin_can_save_separate_store_template(client, price_tag_setup):
    client.force_login(price_tag_setup['admin'])
    client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': price_tag_setup['store'].pk},
    )

    response = client.post(
        reverse('checklists:price_tag_profile'),
        {
            'heading': 'Лучшая цена',
            'primary_color': '#112233',
            'accent_color': '#ff6600',
            'show_image': 'on',
            'show_sku': 'on',
            'show_properties': 'on',
            'max_properties': 4,
            'footer': 'Цена действительна на дату печати',
        },
    )

    assert response.status_code == 302
    template = StorePriceTagTemplate.objects.get(store=price_tag_setup['store'])
    assert template.heading == 'Лучшая цена'
    assert template.max_properties == 4


def test_terminal_account_can_open_tool_and_manage_category(client, price_tag_setup):
    StorePriceTagTemplate.objects.create(
        store=price_tag_setup['store'],
        available_property_names=['Мощность', 'Ширина скашивания'],
    )
    client.force_login(price_tag_setup['terminal'])
    response = client.get(reverse('checklists:director_price_tags'))
    assert response.status_code == 200
    assert 'Свойства по категориям' in response.content.decode()

    saved = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'save_category',
            'name': 'Газонокосилки',
            'keywords': 'газонокосилка, lawn mower',
            'property_names': ['Мощность', 'Ширина скашивания'],
            'sort_order': 1,
            'is_active': 'on',
        },
    )
    assert saved.status_code == 302
    assert StorePriceTagCategory.objects.filter(
        store=price_tag_setup['store'],
        name='Газонокосилки',
    ).exists()


def test_category_rule_selects_and_orders_properties(price_tag_setup):
    category = StorePriceTagCategory.objects.create(
        store=price_tag_setup['store'],
        name='Снегоуборщики',
        keywords='снегоуборщик',
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


def test_seo_product_name_is_cleaned_without_losing_variant():
    value = (
        'Купить бокс на крышу Element 590 белый карбон '
        '(скоба), Белый матовый 216x85x46 —купить в Москве'
    )

    assert clean_product_name(value) == (
        'Бокс на крышу Element 590 белый карбон '
        '(скоба), Белый матовый'
    )


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
            'urls': '\n'.join(
                f'https://example.test/product/{index}/'
                for index in range(1, 5)
            ),
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count('class="price-tag-sheet"') == 1
    assert content.count('class="price-tag"') == 4
    assert 'grid-template-columns: repeat(2, 105mm)' in content
    assert 'grid-template-rows: repeat(2, 148.5mm)' in content
