# Proprietary module. See LICENSE file for full copyright and licensing details.

{
    'name': 'Axoline Shopware Connector',
    'version': '19.0.1.1.47',
    'category': 'Sales/Sales',
    'summary': 'Bidirectional sync between Odoo and Shopware 6',
    'description': """
Axoline Shopware Connector for Odoo 19
======================================

Bidirectional integration between Odoo and Shopware 6 via the Admin API (OAuth2):

* Import **orders** from Shopware into Odoo sales orders
* Synchronize **customers** and link them to contacts
* Align **products** (including variants) and **categories** with optional export to Shopware
* Sync **price rules** to Odoo pricelists when product sync is enabled
* **Scheduled (cron)** and **manual** synchronization from Shopware backend records
* Optional **state transitions** in Shopware after fulfillment and invoicing in Odoo
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
    'images': [
        'static/description/banner.png',
    ],
    'installable': True,
    'application': True,
    'license': 'OPL-1',
}
