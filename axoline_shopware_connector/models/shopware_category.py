# Proprietary module. See LICENSE file for full copyright and licensing details.

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopwareCategory(models.Model):
    _name = 'shopware.category'
    _description = 'Shopware Kategorie'
    _rec_name = 'name'
    _order = 'name'
    _sql_constraints = [
        ('unique_shopware_backend', 'UNIQUE(shopware_id, backend_id)',
         'Diese Shopware-Kategorie existiert bereits für dieses Backend.'),
    ]

    name = fields.Char(string="Name", required=True, index=True)
    shopware_id = fields.Char(string="Shopware ID", index=True, readonly=True)
    backend_id = fields.Many2one(
        'shopware.backend', string="Backend", required=True, ondelete='cascade', index=True,
    )
    odoo_category_id = fields.Many2one(
        'product.category', string="Odoo Produktkategorie",
    )
    parent_id = fields.Many2one(
        'shopware.category', string="Übergeordnete Kategorie",
        domain="[('backend_id', '=', backend_id)]",
    )
    child_ids = fields.One2many('shopware.category', 'parent_id', string="Unterkategorien")
    shopware_parent_id = fields.Char(string="Shopware Parent ID", readonly=True)
    active = fields.Boolean(default=True)
    description = fields.Text(string="Beschreibung")
    sync_date = fields.Datetime(string="Letzte Synchronisation", readonly=True)
    level = fields.Integer(string="Ebene", readonly=True)

    _sql_constraints = [
        (
            'shopware_uniq',
            'unique(shopware_id, backend_id)',
            'Die Shopware-ID muss pro Backend eindeutig sein.',
        ),
    ]

    # -------------------------------------------------------------------------
    # Import from Shopware
    # -------------------------------------------------------------------------

    @api.model
    def sync_from_shopware(self, backend):
        """Import categories from Shopware 6 with delta + missing sync."""
        _logger.info("Starte Kategorie-Import von Shopware Backend %s", backend.name)

        includes = {
            'category': [
                'id', 'name', 'parentId', 'level', 'description', 'active',
                'translated', 'updatedAt',
            ],
        }
        all_sw_categories = []

        if backend.last_category_sync:
            _logger.info("Delta-Sync: Änderungen seit %s", backend.last_category_sync)
            delta_payload = {
                'limit': 500,
                'includes': includes,
                'filter': [{
                    'type': 'range',
                    'field': 'updatedAt',
                    'parameters': {'gte': backend.last_category_sync.isoformat()},
                }],
            }
            all_sw_categories = backend._api_search('category', delta_payload)
            _logger.info("Delta-Sync: %d geänderte Kategorien", len(all_sw_categories))

        all_ids_payload = {
            'limit': 500,
            'includes': {'category': ['id']},
        }
        all_remote = backend._api_search('category', all_ids_payload)
        all_remote_ids = {c.get('id') for c in all_remote if c.get('id')}
        existing_ids = set(self.search([('backend_id', '=', backend.id)]).mapped('shopware_id'))
        missing_ids = all_remote_ids - existing_ids
        already_fetched = {c.get('id') for c in all_sw_categories}
        missing_ids -= already_fetched

        if missing_ids:
            _logger.info("Fehlend: %d Kategorien werden nachgeladen", len(missing_ids))
            missing_payload = {
                'limit': 500,
                'includes': includes,
                'filter': [{'type': 'equalsAny', 'field': 'id', 'value': list(missing_ids)}],
            }
            all_sw_categories += backend._api_search('category', missing_payload)

        _logger.info("Gesamt: %d Kategorien zum Verarbeiten", len(all_sw_categories))
        for sw_cat in all_sw_categories:
            self._import_category(backend, sw_cat)
        self._resolve_parents(backend, all_sw_categories)
        _logger.info("Kategorie-Import abgeschlossen: %d verarbeitet", len(all_sw_categories))
        return len(all_sw_categories)

    def _import_category(self, backend, sw_data):
        sw_id = sw_data.get('id')
        attrs = sw_data.get('attributes', sw_data)
        translated = attrs.get('translated', {})
        name = translated.get('name') or attrs.get('name') or 'Unbenannt'

        existing = self.search([
            ('shopware_id', '=', sw_id),
            ('backend_id', '=', backend.id),
        ], limit=1)

        vals = {
            'name': name,
            'shopware_id': sw_id,
            'backend_id': backend.id,
            'shopware_parent_id': attrs.get('parentId'),
            'description': translated.get('description') or attrs.get('description') or '',
            'active': attrs.get('active', True),
            'level': attrs.get('level', 0),
            'sync_date': fields.Datetime.now(),
        }

        if existing:
            existing.write(vals)
            return existing

        vals['odoo_category_id'] = self._find_or_create_odoo_category(name).id
        try:
            with self.env.cr.savepoint():
                return self.create(vals)
        except Exception:
            existing = self.search([
                ('shopware_id', '=', sw_id),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if existing:
                existing.write(vals)
                return existing
            raise

    def _resolve_parents(self, backend, sw_categories):
        """Resolve parent/child relationships after all categories are imported."""
        for sw_cat in sw_categories:
            attrs = sw_cat.get('attributes', sw_cat)
            parent_sw_id = attrs.get('parentId')
            if not parent_sw_id:
                continue
            category = self.search([
                ('shopware_id', '=', sw_cat.get('id')),
                ('backend_id', '=', backend.id),
            ], limit=1)
            parent = self.search([
                ('shopware_id', '=', parent_sw_id),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if category and parent:
                category.write({'parent_id': parent.id})

    def _find_or_create_odoo_category(self, name):
        category = self.env['product.category'].search([('name', '=', name)], limit=1)
        if not category:
            category = self.env['product.category'].create({'name': name})
        return category

    # -------------------------------------------------------------------------
    # Export to Shopware
    # -------------------------------------------------------------------------

    @api.model
    def export_to_shopware(self, backend):
        """Export Odoo product categories to Shopware 6."""
        _logger.info("Starte Kategorie-Export zu Shopware Backend %s", backend.name)
        categories = self.search([('backend_id', '=', backend.id)])
        for category in categories:
            category._export_single(backend)
        _logger.info("Kategorie-Export abgeschlossen")

    def _export_single(self, backend):
        self.ensure_one()
        payload = {
            'name': self.name,
        }
        if self.parent_id and self.parent_id.shopware_id:
            payload['parentId'] = self.parent_id.shopware_id

        if self.shopware_id:
            backend._api_patch(f'category/{self.shopware_id}', payload)
            _logger.info("Kategorie %s in Shopware aktualisiert", self.name)
        else:
            result = backend._api_post('category', payload)
            if result and result.get('data'):
                sw_id = result['data'].get('id', '')
                self.write({'shopware_id': sw_id, 'sync_date': fields.Datetime.now()})
            _logger.info("Kategorie %s in Shopware erstellt", self.name)

    def action_export_to_shopware(self):
        """Manual export action from form view."""
        for record in self:
            record._export_single(record.backend_id)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Export"),
                'message': _("Kategorien wurden zu Shopware exportiert."),
                'type': 'success',
                'sticky': False,
            },
        }
