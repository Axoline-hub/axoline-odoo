# Proprietary module. See LICENSE file for full copyright and licensing details.

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def action_post(self):
        res = super().action_post()
        self._shopware_try_push_order_completed_after_post()
        return res

    def _shopware_try_push_order_completed_after_post(self):
        for move in self:
            if move.move_type != 'out_invoice' or move.state != 'posted':
                continue
            for so in move.line_ids.sale_line_ids.order_id:
                sw = so.shopware_order_id
                if not sw:
                    continue
                try:
                    sw.sudo().try_push_order_completed_if_ready_to_shopware()
                except Exception:
                    _logger.exception(
                        "Shopware Bestellung „abgeschlossen“ nach Rechnung %s / Auftrag %s fehlgeschlagen",
                        move.name, so.name,
                    )
