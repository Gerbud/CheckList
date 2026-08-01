import pytest
from django.urls import reverse

from checklists.models import EmployeeProfile, Store, StorePriceTagTemplate
from checklists.price_tags import ImportedProduct, import_product
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


def test_director_can_generate_but_cannot_edit_template(
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
    denied = client.post(
        reverse('checklists:director_price_tags'),
        {'action': 'save_template', 'heading': 'Акция'},
    )
    assert denied.status_code == 403


def test_admin_can_save_separate_store_template(client, price_tag_setup):
    client.force_login(price_tag_setup['admin'])
    client.post(
        reverse('checklists:system_select_managed_store'),
        {'store': price_tag_setup['store'].pk},
    )

    response = client.post(
        reverse('checklists:director_price_tags'),
        {
            'action': 'save_template',
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


def test_terminal_account_cannot_open_price_tags(client, price_tag_setup):
    client.force_login(price_tag_setup['terminal'])
    response = client.get(reverse('checklists:director_price_tags'))
    assert response.status_code == 403
