# Proprietary module. See LICENSE file for full copyright and licensing details.

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        res = super()._action_done()
        self._shopware_push_delivery_shipped()
        return res

    def _shopware_push_delivery_shipped(self):
        """Nach erledigtem Lieferschein: Shopware-Versandstatus „versendet“ (order_delivery)."""
        for picking in self:
            if picking.picking_type_id.code not in ('outgoing', 'dropship'):
                continue
            so = picking.sale_id
            if not so or not so.shopware_order_id:
                continue
            backend = so.shopware_order_id.backend_id
            sw = so.shopware_order_id.sudo()
            if backend.push_delivery_shipped_on_picking_done:
                try:
                    sw.push_delivery_shipped_to_shopware()
                except Exception:
                    _logger.exception(
                        "Shopware Versand „versendet“ für Auftrag %s / Transfer %s fehlgeschlagen",
                        so.name, picking.name,
                    )
            if backend.push_order_completed_when_shipped_and_invoiced:
                try:
                    sw.try_push_order_completed_if_ready_to_shopware()
                except Exception:
                    _logger.exception(
                        "Shopware Bestellung „abgeschlossen“ für Auftrag %s / Transfer %s fehlgeschlagen",
                        so.name, picking.name,
                    )
