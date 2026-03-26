# Proprietary module. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    shopware_product_ids = fields.One2many(
        'shopware.product', 'odoo_template_id', string="Shopware Produkte",
    )
    shopware_product_count = fields.Integer(
        string="Shopware", compute='_compute_shopware_product_count',
    )

    @api.depends('shopware_product_ids')
    def _compute_shopware_product_count(self):
        for rec in self:
            rec.shopware_product_count = len(rec.shopware_product_ids)

    def _prepare_variant_values(self, combination):
        vals = super()._prepare_variant_values(combination)
        if not self._should_preserve_variant_data():
            return vals
        combo_ids = set(combination.ids)
        for old in self.with_context(active_test=False).product_variant_ids:
            old_ids = set(old.product_template_attribute_value_ids.ids)
            if not old_ids:
                continue
            if old_ids <= combo_ids or combo_ids <= old_ids:
                vals['default_code'] = old.default_code or ''
                vals['barcode'] = old.barcode or False
                break
        return vals

    def _should_preserve_variant_data(self):
        """True if any Shopware backend has preserve_variant_data_on_attribute_change enabled."""
        return bool(
            self.env['shopware.backend'].sudo().search([
                ('preserve_variant_data_on_attribute_change', '=', True),
            ], limit=1)
        )

    def action_view_shopware_products(self):
        self.ensure_one()
        if self.shopware_product_count == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'shopware.product',
                'res_id': self.shopware_product_ids.id,
                'view_mode': 'form',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Produkte"),
            'res_model': 'shopware.product',
            'view_mode': 'list,form',
            'domain': [('odoo_template_id', '=', self.id)],
        }


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _unlink_or_archive(self, check_access=True):
        """Override to preserve default_code/barcode when variants are merged on attribute removal."""
        if self._should_preserve_variant_data_for_merge():
            self._preserve_variant_data_before_archive()
        super()._unlink_or_archive(check_access=check_access)

    def _should_preserve_variant_data_for_merge(self):
        """True if we should preserve variant data when archiving (Shopware backend setting)."""
        return bool(
            self.env['shopware.backend'].sudo().search([
                ('preserve_variant_data_on_attribute_change', '=', True),
            ], limit=1)
        )

    def _preserve_variant_data_before_archive(self):
        """
        When attributes are removed, variants with the same combination are merged.
        The kept variant might not have default_code/barcode.
        Copy from variants we're about to archive to their sibling (same combination).
        """
        for variant in self:
            if not variant.default_code and not variant.barcode:
                continue
            combo_str = variant.product_template_attribute_value_ids._ids2str()
            sibling = self.env['product.product'].search([
                ('product_tmpl_id', '=', variant.product_tmpl_id.id),
                ('combination_indices', '=', combo_str),
                ('id', '!=', variant.id),
                ('active', '=', True),
            ], limit=1)
            if not sibling:
                continue
            updates = {}
            if variant.default_code and not sibling.default_code:
                updates['default_code'] = variant.default_code
            if variant.barcode and not sibling.barcode:
                updates['barcode'] = variant.barcode
            if updates:
                sibling.write(updates)

    shopware_bind_ids = fields.One2many(
        'shopware.product', 'odoo_product_id', string="Shopware Verknüpfungen",
    )
    shopware_bind_count = fields.Integer(
        string="Shopware", compute='_compute_shopware_bind_count',
    )

    @api.depends('shopware_bind_ids')
    def _compute_shopware_bind_count(self):
        for rec in self:
            rec.shopware_bind_count = len(rec.shopware_bind_ids)

    def action_view_shopware_products(self):
        """Required because product.product inherits the template form view."""
        self.ensure_one()
        bindings = self.env['shopware.product'].search([
            ('odoo_product_id', '=', self.id),
        ])
        if len(bindings) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'shopware.product',
                'res_id': bindings.id,
                'view_mode': 'form',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Produkte"),
            'res_model': 'shopware.product',
            'view_mode': 'list,form',
            'domain': [('odoo_product_id', '=', self.id)],
        }
