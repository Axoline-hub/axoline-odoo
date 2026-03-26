# Proprietary module. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopware_order_id = fields.Many2one(
        'shopware.order',
        string="Shopware Bestellung",
        copy=False,
        index=True,
        readonly=True,
        help="Verknüpfter Datensatz im Axoline Shopware Connector (wird beim Import gesetzt).",
    )
    shopware_customer_comment = fields.Text(
        string="Kundenkommentar (Shopware)",
        copy=False,
        help="Kommentar des Kunden aus der Shopware-Bestellung (customerComment).",
    )
    shopware_payment_method = fields.Char(
        string="Zahlungsart (Shopware)",
        copy=False,
        help="Bezeichnung der Zahlungsart laut Shopware.",
    )
    shopware_shipping_method = fields.Char(
        string="Versandart (Shopware)",
        copy=False,
        help="Bezeichnung der Versandart laut Shopware.",
    )
    shopware_payment_status = fields.Char(
        string="Zahlungsstatus (Shopware)",
        copy=False,
        help="Status der letzten Zahlungstransaktion in Shopware (z. B. Offen, Bezahlt).",
    )
    shopware_order_state = fields.Char(
        string="Bestellstatus (Shopware)",
        copy=False,
        help="Anzeigename des Bestellstatus in Shopware (stateMachineState).",
    )
    shopware_has_customer_comment = fields.Boolean(
        string="SW-Kommentar",
        copy=False,
        help="Gesetzt, wenn in Shopware ein Kundenkommentar zur Bestellung existiert.",
    )
    shopware_customer_comment_preview = fields.Char(
        string="Kundenkommentar (Auszug)",
        compute="_compute_shopware_comment_preview",
        store=False,
    )

    @api.depends("shopware_customer_comment")
    def _compute_shopware_comment_preview(self):
        for so in self:
            c = (so.shopware_customer_comment or "").strip()
            if not c:
                so.shopware_customer_comment_preview = ""
            elif len(c) > 80:
                so.shopware_customer_comment_preview = c[:80] + "…"
            else:
                so.shopware_customer_comment_preview = c

    def _shopware_odoo_ready_for_completed_push(self):
        """True, wenn ausgehende Lieferungen erledigt und eine gebuchte Kundenrechnung existiert."""
        self.ensure_one()
        pickings = self.picking_ids.filtered(
            lambda p: p.picking_type_id.code in ('outgoing', 'dropship') and p.state != 'cancel',
        )
        if not pickings or not all(p.state == 'done' for p in pickings):
            return False
        invoices = self.invoice_ids.filtered(
            lambda m: m.state == 'posted' and m.move_type == 'out_invoice',
        )
        return bool(invoices)

    def _sync_shopware_meta_from_connector(self):
        """Übernimmt Kommentar/Zahlung/Versand aus der verknüpften Shopware-Bestellung."""
        for so in self:
            sw = so.shopware_order_id or self.env['shopware.order'].search(
                [('odoo_sale_order_id', '=', so.id)],
                limit=1,
            )
            if not sw:
                continue
            comment = (sw.customer_comment or "").strip()
            pay_st = (sw.payment_status or '').strip() or (sw.payment_status_technical or '').strip()
            so.write({
                'shopware_order_id': sw.id,
                'shopware_customer_comment': sw.customer_comment or False,
                'shopware_payment_method': sw.payment_method or False,
                'shopware_shipping_method': sw.shipping_method or False,
                'shopware_payment_status': pay_st or False,
                'shopware_order_state': (sw.state_name or '').strip() or False,
                'shopware_has_customer_comment': bool(comment),
            })

    @api.model
    def _backfill_shopware_links_from_shopware_orders(self):
        """Bestehende Verknüpfungen: odoo_sale_order_id → shopware_order_id + Metadaten."""
        for sw in self.env['shopware.order'].search([('odoo_sale_order_id', '!=', False)]):
            so = sw.odoo_sale_order_id
            if so:
                so._sync_shopware_meta_from_connector()

    def action_view_shopware_order(self):
        """Springt zur Shopware-Bestellung im Connector (legt Verknüpfung bei Bedarf an)."""
        self.ensure_one()
        self._sync_shopware_meta_from_connector()
        if not self.shopware_order_id:
            raise UserError(
                _("Es ist keine Shopware-Bestellung mit diesem Odoo-Auftrag verknüpft."),
            )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Shopware Bestellung'),
            'res_model': 'shopware.order',
            'res_id': self.shopware_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
