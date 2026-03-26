# Proprietary module. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    shopware_category_ids = fields.One2many(
        'shopware.category', 'odoo_category_id', string="Shopware Kategorien",
    )
    shopware_category_count = fields.Integer(
        string="Shopware", compute='_compute_shopware_category_count',
    )

    @api.depends('shopware_category_ids')
    def _compute_shopware_category_count(self):
        for rec in self:
            rec.shopware_category_count = len(rec.shopware_category_ids)

    def action_view_shopware_categories(self):
        self.ensure_one()
        if self.shopware_category_count == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'shopware.category',
                'res_id': self.shopware_category_ids.id,
                'view_mode': 'form',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Kategorien"),
            'res_model': 'shopware.category',
            'view_mode': 'list,form',
            'domain': [('odoo_category_id', '=', self.id)],
        }
