# Proprietary module. See LICENSE file for full copyright and licensing details.

{
    'name': 'Axoline Shopware Connector',
    'version': '19.0.1.1.47',
    'category': 'Sales/Sales',
    'summary': 'Bidirektionale Synchronisation zwischen Odoo und Shopware 6',
    'description': """
Axoline Shopware Connector für Odoo 19
=======================================

Dieses Modul ermöglicht die bidirektionale Synchronisation zwischen
Odoo und einem Shopware 6 Shop:

* **Bestellungen** aus Shopware importieren
* **Kundendaten** synchronisieren
* **Produkte** bidirektional abgleichen
* **Kategorien** bidirektional abgleichen
* **Automatische Cron-Synchronisation**
* **Manuelle Synchronisation** über das Backend
    """,
    'author': 'Axoline',
    'website': 'https://www.axoline.de',
    'support': 'info@axoline.de',
    'price': 299.0,
    'currency': 'EUR',
    'depends': [
        'base',
        'sale_management',
        'sale_stock',
        'stock',
        'product',
        'contacts',
        'account',
    ],
    'data': [
        'security/shopware_security.xml',
        'security/ir.model.access.csv',
        'data/shopware_cron.xml',
        'views/shopware_backend_views.xml',
        'views/shopware_category_views.xml',
        'views/shopware_product_views.xml',
        'views/shopware_price_rule_views.xml',
        'views/shopware_customer_views.xml',
        'views/shopware_order_views.xml',
        'views/sale_order_views.xml',
        'views/shopware_menu.xml',
        'views/product_template_views.xml',
        'views/product_category_views.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'OPL-1',
}
