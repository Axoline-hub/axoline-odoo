# Proprietary module. See LICENSE file for full copyright and licensing details.

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ShopwarePriceRule(models.Model):
    _name = 'shopware.price.rule'
    _description = 'Shopware Preisregel'
    _rec_name = 'name'
    _order = 'name'
    _sql_constraints = [
        ('unique_shopware_backend', 'UNIQUE(shopware_id, backend_id)',
         'Diese Shopware-Preisregel existiert bereits für dieses Backend.'),
    ]

    name = fields.Char(string="Name", required=True, index=True)
    shopware_id = fields.Char(string="Shopware Rule ID", index=True, readonly=True)
    backend_id = fields.Many2one(
        'shopware.backend', string="Backend", required=True,
        ondelete='cascade', index=True,
    )
    pricelist_id = fields.Many2one(
        'product.pricelist', string="Odoo Preisliste",
        help="Die Odoo-Preisliste, die dieser Shopware-Regel zugeordnet ist.",
    )
    priority = fields.Integer(string="Priorität", default=5)
    description = fields.Char(string="Beschreibung")
    sync_date = fields.Datetime(string="Letzte Synchronisation", readonly=True)

    @api.model
    def sync_rules_from_shopware(self, backend):
        """Fetch all rules used in product prices from Shopware."""
        payload = {
            'limit': 500,
            'includes': {'rule': ['id', 'name', 'description', 'priority']},
        }
        rules = backend._api_search('rule', payload)
        _logger.info("Shopware: %d Regeln gefunden", len(rules))
        count = 0
        for rule in rules:
            attrs = rule.get('attributes', rule)
            sw_id = rule.get('id')
            if not sw_id:
                continue
            name = attrs.get('name') or 'Unbenannt'

            existing = self.search([
                ('shopware_id', '=', sw_id),
                ('backend_id', '=', backend.id),
            ], limit=1)

            vals = {
                'name': name,
                'shopware_id': sw_id,
                'backend_id': backend.id,
                'priority': attrs.get('priority', 5),
                'description': attrs.get('description') or '',
                'sync_date': fields.Datetime.now(),
            }

            if existing:
                existing.write(vals)
            else:
                pricelist = self._find_or_create_pricelist(name, backend)
                vals['pricelist_id'] = pricelist.id
                try:
                    with self.env.cr.savepoint():
                        self.create(vals)
                except Exception:
                    existing = self.search([
                        ('shopware_id', '=', sw_id),
                        ('backend_id', '=', backend.id),
                    ], limit=1)
                    if existing:
                        existing.write(vals)
                    else:
                        raise
            count += 1
        return count

    def _find_or_create_pricelist(self, name, backend):
        """Find or create an Odoo pricelist for a Shopware rule."""
        pl_name = f"Shopware: {name}"
        pricelist = self.env['product.pricelist'].search([
            ('name', '=', pl_name),
        ], limit=1)
        if not pricelist:
            pricelist = self.env['product.pricelist'].create({
                'name': pl_name,
            })
            _logger.info("Preisliste '%s' erstellt", pl_name)
        return pricelist
