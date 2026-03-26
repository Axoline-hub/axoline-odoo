# Proprietary module. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopware_customer_ids = fields.One2many(
        'shopware.customer', 'odoo_partner_id', string="Shopware Kunden",
    )
    shopware_customer_count = fields.Integer(
        string="Shopware", compute='_compute_shopware_customer_count',
    )

    @api.depends('shopware_customer_ids')
    def _compute_shopware_customer_count(self):
        for rec in self:
            rec.shopware_customer_count = len(rec.shopware_customer_ids)

    def action_view_shopware_customers(self):
        self.ensure_one()
        if self.shopware_customer_count == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'shopware.customer',
                'res_id': self.shopware_customer_ids.id,
                'view_mode': 'form',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Kunden"),
            'res_model': 'shopware.customer',
            'view_mode': 'list,form',
            'domain': [('odoo_partner_id', '=', self.id)],
        }
