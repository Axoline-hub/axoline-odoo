# Proprietary module. See LICENSE file for full copyright and licensing details.

import json
import logging
import re
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_round

_logger = logging.getLogger(__name__)

# Bestellungs-stateMachineState.technicalName: abgeschlossen/storniert → kein Abgleich mehr
SHOPWARE_ORDER_REFRESH_SKIP_STATES = frozenset({
    'completed',
    'cancelled',
})


class ShopwareOrder(models.Model):
    _name = 'shopware.order'
    _description = 'Shopware Bestellung'
    _rec_name = 'shopware_order_number'
    _order = 'order_date desc, id desc'
    _sql_constraints = [
        ('unique_shopware_backend', 'UNIQUE(shopware_id, backend_id)',
         'Diese Shopware-Bestellung existiert bereits für dieses Backend.'),
    ]

    shopware_id = fields.Char(string="Shopware ID", index=True, readonly=True)
    shopware_order_number = fields.Char(string="Bestellnummer", index=True, readonly=True)
    backend_id = fields.Many2one(
        'shopware.backend', string="Backend", required=True, ondelete='cascade', index=True,
    )
    odoo_sale_order_id = fields.Many2one('sale.order', string="Odoo Auftrag")

    order_date = fields.Datetime(string="Bestelldatum", readonly=True)
    amount_total = fields.Float(string="Gesamtbetrag (Brutto)", digits='Product Price', readonly=True)
    amount_net = fields.Float(string="Nettobetrag", digits='Product Price', readonly=True)
    currency_code = fields.Char(string="Währung", readonly=True)

    shopware_customer_id = fields.Many2one(
        'shopware.customer', string="Shopware Kunde", readonly=True,
    )
    customer_email = fields.Char(string="Kunden-E-Mail", readonly=True)
    customer_name = fields.Char(string="Kundenname", readonly=True)

    shipping_street = fields.Char(string="Lieferstraße", readonly=True)
    shipping_zipcode = fields.Char(string="Liefer-PLZ", readonly=True)
    shipping_city = fields.Char(string="Lieferstadt", readonly=True)
    shipping_country_code = fields.Char(string="Lieferland (ISO)", readonly=True)

    billing_street = fields.Char(string="Rechnungsstraße", readonly=True)
    billing_zipcode = fields.Char(string="Rechnungs-PLZ", readonly=True)
    billing_city = fields.Char(string="Rechnungsstadt", readonly=True)
    billing_country_code = fields.Char(string="Rechnungsland (ISO)", readonly=True)

    state_name = fields.Char(string="Shopware-Status", readonly=True)
    order_state_technical = fields.Char(
        string="Shopware-Status (technisch)",
        readonly=True,
        index=True,
        help="stateMachineState.technicalName der Bestellung (z. B. open, completed).",
    )
    payment_method = fields.Char(string="Zahlungsmethode", readonly=True)
    payment_status = fields.Char(
        string="Zahlungsstatus",
        readonly=True,
        help="Status der letzten Zahlungstransaktion in Shopware (z. B. Offen, Bezahlt).",
    )
    payment_status_technical = fields.Char(
        string="Zahlungsstatus (technisch)",
        readonly=True,
        index=True,
        help="Shopware stateMachineState.technicalName der Transaktion (z. B. open, paid).",
    )
    shipping_method = fields.Char(string="Versandmethode", readonly=True)
    shopware_delivery_id = fields.Char(
        string="Shopware Lieferung (UUID)",
        readonly=True,
        index=True,
        help="Erste order_delivery in Shopware — für den Versandstatus „versendet“ (Transition ship).",
    )

    customer_comment = fields.Text(
        string="Kundenkommentar",
        readonly=True,
        help="Kommentar des Kunden aus Shopware (Feld customerComment).",
    )

    line_ids = fields.One2many('shopware.order.line', 'order_id', string="Positionen")
    sync_date = fields.Datetime(string="Letzte Synchronisation", readonly=True)
    active = fields.Boolean(default=True)

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
    def _enrich_jsonapi_order_from_included(self, order_record, included):
        """JSON:API: relationships + included[] in attributes-Struktur für den Import mappen.

        Die Suche liefert u. a. ``transactions``/``deliveries`` nur in ``included``,
        nicht in ``attributes`` — ohne Merge bleiben Zahlungsstatus und Versand leer.
        """
        if not order_record or not included:
            return order_record
        incl = {
            (i.get('type'), i.get('id')): i
            for i in included
            if i.get('type') and i.get('id')
        }
        attrs = dict(order_record.get('attributes') or {})
        rel = order_record.get('relationships') or {}

        def _hydrate_tx(tx):
            if not tx:
                return tx
            tx_attrs = dict(tx.get('attributes') or {})
            tx_rel = tx.get('relationships') or {}
            pm_ref = (tx_rel.get('paymentMethod') or {}).get('data')
            if pm_ref and not tx_attrs.get('paymentMethod'):
                pm = incl.get((pm_ref.get('type'), pm_ref.get('id')))
                if pm:
                    tx_attrs['paymentMethod'] = pm.get('attributes') or {}
            sm_ref = (tx_rel.get('stateMachineState') or {}).get('data')
            if sm_ref and not tx_attrs.get('stateMachineState'):
                sm = incl.get((sm_ref.get('type'), sm_ref.get('id')))
                if sm:
                    tx_attrs['stateMachineState'] = sm.get('attributes') or {}
            return {**tx, 'attributes': tx_attrs}

        tx_refs = (rel.get('transactions') or {}).get('data') or []
        if tx_refs and not attrs.get('transactions'):
            txs = []
            for ref in tx_refs:
                tx = incl.get((ref.get('type'), ref.get('id')))
                if tx:
                    txs.append(_hydrate_tx(tx))
            if txs:
                attrs['transactions'] = txs

        del_refs = (rel.get('deliveries') or {}).get('data') or []
        if del_refs and not attrs.get('deliveries'):
            dels = []
            for ref in del_refs:
                d = incl.get((ref.get('type'), ref.get('id')))
                if not d:
                    continue
                d_attrs = dict(d.get('attributes') or {})
                d_rel = d.get('relationships') or {}
                sm_ref = (d_rel.get('shippingMethod') or {}).get('data')
                if sm_ref and not d_attrs.get('shippingMethod'):
                    sm = incl.get((sm_ref.get('type'), sm_ref.get('id')))
                    if sm:
                        d_attrs['shippingMethod'] = sm.get('attributes') or {}
                addr_ref = (d_rel.get('shippingOrderAddress') or {}).get('data')
                if addr_ref and not d_attrs.get('shippingOrderAddress'):
                    addr = incl.get((addr_ref.get('type'), addr_ref.get('id')))
                    if addr:
                        a_attrs = addr.get('attributes') or {}
                        cnt = (addr.get('relationships') or {}).get('country', {}).get('data')
                        if cnt:
                            c = incl.get((cnt.get('type'), cnt.get('id')))
                            if c:
                                a_attrs = dict(a_attrs)
                                a_attrs['country'] = c.get('attributes') or {}
                        d_attrs['shippingOrderAddress'] = a_attrs
                dels.append({**d, 'attributes': d_attrs})
            if dels:
                attrs['deliveries'] = dels

        st_ref = (rel.get('stateMachineState') or {}).get('data')
        if st_ref and not attrs.get('stateMachineState'):
            st = incl.get((st_ref.get('type'), st_ref.get('id')))
            if st:
                attrs['stateMachineState'] = st.get('attributes') or {}

        out = dict(order_record)
        out['attributes'] = attrs
        return out

    @api.model
    def _order_import_associations(self):
        """Gemeinsame Shopware-Assoziationen für Bestell-API-Suche."""
        return {
            'lineItems': {
                'associations': {
                    'product': {},
                },
            },
            'orderCustomer': {},
            'deliveries': {
                'associations': {
                    'shippingOrderAddress': {
                        'associations': {'country': {}},
                    },
                    'shippingMethod': {},
                },
            },
            'billingAddress': {
                'associations': {'country': {}},
            },
            'transactions': {
                'associations': {
                    'paymentMethod': {},
                    'stateMachineState': {},
                },
            },
            'stateMachineState': {},
            'currency': {},
        }

    @api.model
    def _order_import_status_technical_name(self, backend):
        """stateMachineState.technicalName laut „Bestellstatus-Filter“ oder None bei „Alle“."""
        sel = backend.import_order_status
        if sel == 'open':
            return 'open'
        if sel == 'completed':
            return 'completed'
        return None

    @api.model
    def _build_order_search_criteria_filters(self, backend, date_filter_iso=None, order_id=None):
        """Shopware-Suchfilter: optionale Bestell-ID, Status-Filter, Datum — Bedingungen per UND."""
        queries = []
        if order_id:
            queries.append({'type': 'equals', 'field': 'id', 'value': order_id})
        st = self._order_import_status_technical_name(backend)
        if st:
            queries.append({'type': 'equals', 'field': 'stateMachineState.technicalName', 'value': st})
        if date_filter_iso:
            queries.append({
                'type': 'range',
                'field': 'orderDateTime',
                'parameters': {'gte': date_filter_iso},
            })
        if not queries:
            return []
        if len(queries) == 1:
            return queries
        return [{'type': 'multi', 'operator': 'AND', 'queries': queries}]

    @api.model
    def _api_fetch_orders_by_shopware_ids(self, backend, shopware_ids, apply_import_status_filter=True):
        """Lädt Bestellungen per Shopware-UUID-Liste (gebündelte Suche).

        Mit ``apply_import_status_filter`` (Standard True) wird der Backend-„Bestellstatus-Filter“
        mit UND verknüpft — außer „Alle“. Für technische Nachladung (z. B. nur Status-Felder)
        auf False setzen.
        """
        if not shopware_ids:
            return []
        ass = self._order_import_associations()
        st = (
            self._order_import_status_technical_name(backend)
            if apply_import_status_filter
            else None
        )
        out = []
        chunk_size = 20
        for i in range(0, len(shopware_ids), chunk_size):
            chunk = shopware_ids[i:i + chunk_size]
            or_queries = [
                {'type': 'equals', 'field': 'id', 'value': oid}
                for oid in chunk
            ]
            if st:
                payload = {
                    'filter': [{
                        'type': 'multi',
                        'operator': 'AND',
                        'queries': [
                            {'type': 'multi', 'operator': 'OR', 'queries': or_queries},
                            {'type': 'equals', 'field': 'stateMachineState.technicalName', 'value': st},
                        ],
                    }],
                    'associations': ass,
                }
            else:
                payload = {
                    'filter': [{'type': 'multi', 'operator': 'OR', 'queries': or_queries}],
                    'associations': ass,
                }
            try:
                part = backend._api_search('order', payload, max_records=len(chunk))
                out.extend(part or [])
            except Exception:
                _logger.exception(
                    "Shopware Order-Batch (OR) fehlgeschlagen, Einzelabruf für %d IDs",
                    len(chunk),
                )
                for oid in chunk:
                    try:
                        id_only = [{'type': 'equals', 'field': 'id', 'value': oid}]
                        if st:
                            filt = [{
                                'type': 'multi',
                                'operator': 'AND',
                                'queries': id_only + [
                                    {'type': 'equals', 'field': 'stateMachineState.technicalName', 'value': st},
                                ],
                            }]
                        else:
                            filt = id_only
                        one = backend._api_search(
                            'order',
                            {'filter': filt, 'associations': ass},
                            max_records=1,
                        )
                        out.extend(one or [])
                    except Exception:
                        _logger.exception(
                            "Shopware Bestellung %s konnte nicht geladen werden",
                            oid,
                        )
        return out

    @api.model
    def sync_linked_orders_from_shopware(self, backend):
        """Aktualisiert verknüpfte Bestellungen, solange der Shopware-Bestellstatus nicht abgeschlossen ist."""
        limit = int(backend.sync_order_linked_refresh_limit or 0)
        if not limit:
            return 0
        skip_states = list(SHOPWARE_ORDER_REFRESH_SKIP_STATES)
        to_refresh = self.search([
            ('backend_id', '=', backend.id),
            ('odoo_sale_order_id', '!=', False),
            ('shopware_id', '!=', False),
            '|', '|',
            ('order_state_technical', '=', False),
            ('order_state_technical', '=', ''),
            ('order_state_technical', 'not in', skip_states),
        ], order='sync_date asc, id asc', limit=limit)
        if not to_refresh:
            return 0
        ids = [r.shopware_id for r in to_refresh]
        # Ohne Bestellstatus-Filter: Abruf nur per ID, damit Zahlungsstatus u. a. auch nach
        # „in_progress“ / außerhalb des Import-Filters weiter nachziehen.
        rows = self._api_fetch_orders_by_shopware_ids(
            backend, ids, apply_import_status_filter=False,
        )
        done = 0
        for sw_data in rows:
            try:
                self._import_order(backend, sw_data)
                done += 1
            except Exception:
                _logger.exception(
                    "Fehler beim Aktualisieren der verknüpften Bestellung %s",
                    sw_data.get('id', '?'),
                )
        return done

    @api.model
    def sync_from_shopware(self, backend):
        _logger.info("Starte Bestell-Import von Shopware Backend %s", backend.name)

        test_id = (backend.import_order_test_id or '').strip()
        if test_id:
            # Test-UUID: nur nach ID suchen — unabhängig vom „Bestellstatus-Filter“ (Hauptimport).
            payload = {
                'limit': 5,
                'associations': self._order_import_associations(),
                'filter': [
                    {'type': 'equals', 'field': 'id', 'value': test_id},
                ],
            }
            sw_orders = backend._api_search('order', payload, max_records=1)
            if not sw_orders:
                raise UserError(_(
                    "Keine Bestellung mit dieser Shopware-ID gefunden: %s"
                ) % test_id)
            _logger.info(
                "Bestell-Testimport: nur UUID %s (%d Datensatz)",
                test_id, len(sw_orders),
            )
        else:
            date_filter = None
            if backend.last_order_sync:
                date_filter = backend.last_order_sync.isoformat()
            elif backend.import_orders_from_date:
                date_filter = f"{backend.import_orders_from_date}T00:00:00"

            criteria = self._build_order_search_criteria_filters(backend, date_filter_iso=date_filter)
            payload = {
                'associations': self._order_import_associations(),
                'sort': [{'field': 'orderDateTime', 'order': 'DESC'}],
            }
            if criteria:
                payload['filter'] = criteria

            max_records = backend.import_order_limit or 0
            sw_orders = backend._api_search('order', payload, max_records=max_records)
            _logger.info("Gefunden: %d Bestellungen in Shopware", len(sw_orders))

        count = 0
        for sw_order in sw_orders:
            try:
                self._import_order(backend, sw_order)
                count += 1
            except Exception:
                _logger.exception(
                    "Fehler beim Import von Shopware-Bestellung %s",
                    sw_order.get('id', '?'),
                )
        _logger.info("Bestell-Import abgeschlossen: %d Bestellungen verarbeitet", count)

        refresh = 0
        if not test_id and backend.sync_order_linked_refresh_limit:
            refresh = self.sync_linked_orders_from_shopware(backend)
            if refresh:
                _logger.info(
                    "Verknüpfte Bestellungen neu geladen (u. a. Zahlungsstatus): %d",
                    refresh,
                )

        if count or refresh:
            self.env['sale.order']._backfill_shopware_links_from_shopware_orders()
        return count + refresh

    def _import_order(self, backend, sw_data):
        sw_id = sw_data.get('id')
        attrs = sw_data.get('attributes', sw_data)

        existing = self.search([
            ('shopware_id', '=', sw_id),
            ('backend_id', '=', backend.id),
        ], limit=1)

        order_number = attrs.get('orderNumber', '')
        order_datetime = attrs.get('orderDateTime', '')
        if order_datetime:
            try:
                order_datetime = datetime.fromisoformat(
                    order_datetime.replace('Z', '+00:00')
                ).replace(tzinfo=None)
            except (ValueError, TypeError):
                order_datetime = fields.Datetime.now()

        amount_total = attrs.get('amountTotal', 0.0)
        amount_net = attrs.get('amountNet', 0.0)

        currency_data = attrs.get('currency') or {}
        currency_code = currency_data.get('isoCode', 'EUR')

        customer_data = attrs.get('orderCustomer') or {}
        customer_email = customer_data.get('email', '')
        customer_name = ' '.join(
            p for p in [customer_data.get('firstName', ''), customer_data.get('lastName', '')] if p
        )
        customer_sw_id = customer_data.get('customerId', '')

        sw_customer = self.env['shopware.customer']
        if customer_sw_id:
            # Gleiche Kontakt-Logik wie beim Kunden-Sync (API + _import_customer)
            sw_customer = self.env['shopware.customer'].import_customer_by_shopware_id(
                backend, customer_sw_id,
            )

        billing = attrs.get('billingAddress') or {}
        billing_country = billing.get('country') or {}

        shipping_address = {}
        shipping_method_name = ''
        shopware_delivery_id = False
        deliveries = attrs.get('deliveries') or []
        if deliveries:
            delivery = deliveries[0] if isinstance(deliveries, list) else deliveries
            if isinstance(delivery, dict):
                shopware_delivery_id = delivery.get('id') or False
            d_attrs = delivery.get('attributes', delivery)
            shipping_address = d_attrs.get('shippingOrderAddress') or {}
            sm = d_attrs.get('shippingMethod') or {}
            sm_translated = sm.get('translated', {})
            shipping_method_name = sm_translated.get('name') or sm.get('name') or ''
        shipping_country = shipping_address.get('country') or {}

        payment_name = ''
        payment_status_name = ''
        payment_status_technical = ''
        transactions = attrs.get('transactions') or []
        if transactions:
            tx = transactions[-1] if isinstance(transactions, list) else transactions
            tx_attrs = tx.get('attributes', tx)
            pm = tx_attrs.get('paymentMethod') or {}
            pm_translated = pm.get('translated', {})
            payment_name = pm_translated.get('name') or pm.get('name') or ''
            tx_state = tx.get('stateMachineState') or tx_attrs.get('stateMachineState') or {}
            if isinstance(tx_state, dict):
                tx_state_tr = tx_state.get('translated') or {}
                payment_status_name = tx_state_tr.get('name') or tx_state.get('name') or ''
                payment_status_technical = (tx_state.get('technicalName') or '').strip()

        state_machine = attrs.get('stateMachineState') or {}
        state_translated = state_machine.get('translated', {})
        state_name = state_translated.get('name') or state_machine.get('name') or ''
        order_state_technical = (state_machine.get('technicalName') or '').strip()

        raw_comment = attrs.get('customerComment')
        if raw_comment is None:
            raw_comment = attrs.get('customer_comment')
        if raw_comment is None:
            customer_comment = ''
        elif isinstance(raw_comment, str):
            customer_comment = raw_comment.strip()
        else:
            customer_comment = str(raw_comment).strip()

        vals = {
            'shopware_id': sw_id,
            'shopware_order_number': order_number,
            'backend_id': backend.id,
            'order_date': order_datetime,
            'amount_total': amount_total,
            'amount_net': amount_net,
            'currency_code': currency_code,
            'customer_email': customer_email,
            'customer_name': customer_name,
            'shopware_customer_id': sw_customer.id if sw_customer else False,
            'billing_street': billing.get('street', ''),
            'billing_zipcode': billing.get('zipcode', ''),
            'billing_city': billing.get('city', ''),
            'billing_country_code': billing_country.get('iso', ''),
            'shipping_street': shipping_address.get('street', ''),
            'shipping_zipcode': shipping_address.get('zipcode', ''),
            'shipping_city': shipping_address.get('city', ''),
            'shipping_country_code': shipping_country.get('iso', ''),
            'state_name': state_name,
            'order_state_technical': order_state_technical or False,
            'payment_method': payment_name,
            'payment_status': payment_status_name,
            'payment_status_technical': payment_status_technical,
            'shipping_method': shipping_method_name,
            'shopware_delivery_id': shopware_delivery_id or False,
            'customer_comment': customer_comment or False,
            'sync_date': fields.Datetime.now(),
        }

        if existing:
            existing.write(vals)
            existing.line_ids.unlink()
            self._import_order_lines(existing, attrs)
            if existing.odoo_sale_order_id:
                existing.odoo_sale_order_id._sync_shopware_meta_from_connector()
            order_rec = existing
        else:
            order_rec = self.create(vals)
            self._import_order_lines(order_rec, attrs)
            self._create_sale_order(order_rec, backend)

        self._push_order_in_progress_after_import(
            backend, order_rec, sw_id, order_state_technical,
        )
        return order_rec

    @api.model
    def _push_order_in_progress_after_import(self, backend, order_record, sw_id, order_state_technical):
        """Nach Import: in Shopware von „open“ auf „in_progress“ (Standard-Transition process)."""
        if not backend.order_push_in_progress_on_import:
            return
        if order_state_technical != 'open':
            return
        trans = (backend.order_import_open_transition or 'process').strip() or 'process'
        if not backend._api_order_state_transition(sw_id, trans):
            return
        # Sofort Odoo an den neuen Shopware-Status anbinden (Import hatte noch „open“ aus der API).
        order_record.write({
            'order_state_technical': 'in_progress',
            'state_name': _('In Bearbeitung'),
            'sync_date': fields.Datetime.now(),
        })
        self._refresh_order_state_fields_from_shopware(backend, order_record, sw_id)

    @api.model
    def _refresh_order_state_fields_from_shopware(self, backend, order_record, sw_id):
        """Lädt eine Bestellung erneut und aktualisiert nur Status-Felder (nach Transition).

        Überschreibt einen bereits gesetzten „in_progress“-Stand nicht durch ein veraltetes
        „open“ aus der API (Timing nach State-Transition).
        """
        rows = self._api_fetch_orders_by_shopware_ids(
            backend, [sw_id], apply_import_status_filter=False,
        )
        if not rows:
            return
        item = rows[0]
        attrs = item.get('attributes') or {}
        sm = attrs.get('stateMachineState') or {}
        st_tr = sm.get('translated') or {}
        state_name = (st_tr.get('name') or sm.get('name') or '').strip()
        tech = (sm.get('technicalName') or '').strip()
        vals = {'sync_date': fields.Datetime.now()}
        if state_name:
            vals['state_name'] = state_name
        if tech:
            if tech == 'open' and order_record.order_state_technical == 'in_progress':
                _logger.debug(
                    "Shopware-Bestellung %s: API liefert noch „open“ nach Transition — "
                    "technischen Status in Odoo nicht zurücksetzen",
                    sw_id,
                )
            else:
                vals['order_state_technical'] = tech
        order_record.write(vals)

    def push_delivery_shipped_to_shopware(self):
        """Setzt die Shopware-Lieferung (order_delivery) per Transition ``ship`` auf „versendet“."""
        self.ensure_one()
        backend = self.backend_id
        delivery_id = self.shopware_delivery_id or self._fetch_shopware_delivery_id_from_api()
        if not delivery_id:
            _logger.warning(
                "Shopware Bestellung %s: keine order_delivery-ID für Versand-Transition",
                self.shopware_order_number or self.id,
            )
            return False
        return backend._api_order_delivery_state_transition(delivery_id, 'ship')

    def try_push_order_completed_if_ready_to_shopware(self):
        """Setzt die Shopware-Bestellung auf „completed“, wenn Versand (Odoo) und Rechnung erfüllt sind."""
        self.ensure_one()
        backend = self.backend_id
        if not backend.push_order_completed_when_shipped_and_invoiced:
            return False
        if self.order_state_technical in SHOPWARE_ORDER_REFRESH_SKIP_STATES:
            return False
        if not self.shopware_id:
            return False
        so = self.odoo_sale_order_id
        if not so or not so._shopware_odoo_ready_for_completed_push():
            return False
        trans = (backend.order_complete_transition or 'complete').strip() or 'complete'
        if not backend._api_order_state_transition(self.shopware_id, trans):
            return False
        self._refresh_order_state_fields_from_shopware(backend, self, self.shopware_id)
        return True

    def _fetch_shopware_delivery_id_from_api(self):
        """Lädt die Bestellung einmal nach und speichert die erste order_delivery-ID."""
        self.ensure_one()
        rows = self.env['shopware.order']._api_fetch_orders_by_shopware_ids(
            self.backend_id, [self.shopware_id], apply_import_status_filter=False,
        )
        if not rows:
            return False
        attrs = rows[0].get('attributes') or {}
        dels = attrs.get('deliveries') or []
        if not dels:
            return False
        d0 = dels[0] if isinstance(dels, list) else dels
        if not isinstance(d0, dict):
            return False
        did = d0.get('id')
        if not did:
            return False
        self.write({'shopware_delivery_id': did})
        return did

    @staticmethod
    def _order_line_net_unit_and_total(item_attrs):
        """Netto-Einzel- und Gesamtpreis — keine Brutto-Skalare als Netto verwenden.

        Bei ``taxStatus: gross`` sind ``unitPrice``/``totalPrice`` oft **Brutto** (z. B. 132,99).
        Netto: ``price.*.net``, oder Brutto minus Summe ``calculatedTaxes[].tax``, oder
        Brutto / (1 + ``taxRules[].taxRate``/100).
        """
        qty = float(item_attrs.get('quantity') or 1) or 1.0

        def _net_from_currency_price(val):
            if isinstance(val, dict) and val.get('net') is not None:
                return float(val['net'])
            return None

        def _gross_scalar(val):
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, dict) and val.get('gross') is not None:
                return float(val['gross'])
            return None

        def _first_tax_rate(rules):
            if not rules:
                return None
            tr0 = rules[0] if isinstance(rules, list) else rules
            if not isinstance(tr0, dict):
                return None
            r = tr0.get('taxRate')
            if r is None and isinstance(tr0.get('tax'), dict):
                r = tr0['tax'].get('taxRate')
            if r is None:
                return None
            return float(r)

        price_block = item_attrs.get('price')
        if isinstance(price_block, dict):
            tax_status = (price_block.get('taxStatus') or 'gross').lower()
            u_raw = price_block.get('unitPrice')
            t_raw = price_block.get('totalPrice')
            taxes = price_block.get('calculatedTaxes') or []
            tax_rules = price_block.get('taxRules') or []

            total_gross = _gross_scalar(t_raw)
            unit_gross = _gross_scalar(u_raw)

            # 1) CurrencyPrice mit explizitem net — Zeilensumme (total) ist führend,
            #    Einzelpreis daraus, damit Summe(qty * unit) nicht von Shopware abweicht.
            u_net = _net_from_currency_price(u_raw)
            t_net = _net_from_currency_price(t_raw)
            if t_net is not None:
                return (t_net / qty if qty else t_net), t_net
            if u_net is not None:
                return u_net, u_net * qty

            # 2) Brutto-Gesamt + calculatedTaxes (Summe „tax“ = Steuerbetrag)
            if total_gross is not None and taxes:
                tax_sum = sum(
                    float(t.get('tax', 0) or 0)
                    for t in taxes
                    if isinstance(t, dict)
                )
                total_n = total_gross - tax_sum
                unit_n = total_n / qty if qty else total_n
                return unit_n, total_n

            # 3) taxStatus net: Skalare sind Netto (nicht Brutto) — Gesamt zuerst
            if tax_status == 'net':
                tg = float(t_raw) if isinstance(t_raw, (int, float)) else _net_from_currency_price(t_raw)
                if tg is not None:
                    return (tg / qty if qty else tg), tg
                ug = float(u_raw) if isinstance(u_raw, (int, float)) else _net_from_currency_price(u_raw)
                if ug is not None:
                    return ug, ug * qty

            # 4) Brutto ohne Steuerzeilen: Steuersatz aus taxRules
            rate = _first_tax_rate(tax_rules)
            if tax_status == 'gross' and rate and rate > 0:
                if total_gross is not None:
                    total_n = total_gross / (1.0 + rate / 100.0)
                    unit_n = total_n / qty if qty else total_n
                    return unit_n, total_n
                if unit_gross is not None:
                    unit_n = unit_gross / (1.0 + rate / 100.0)
                    return unit_n, unit_n * qty

        _logger.debug(
            "Shopware-Bestellposition: kein Nettopreis ermittelt (0 €). Keys: %s",
            list(item_attrs.keys()) if isinstance(item_attrs, dict) else '?',
        )
        return 0.0, 0.0

    @staticmethod
    def _order_line_shopware_gross_total(item_attrs):
        """Zeilenbrutto aus Shopware (CurrencyPrice.gross bzw. Skalare bei taxStatus gross)."""
        price_block = item_attrs.get('price')
        if not isinstance(price_block, dict):
            return 0.0
        qty = float(item_attrs.get('quantity') or 1) or 1.0
        t_raw = price_block.get('totalPrice')
        if isinstance(t_raw, dict) and t_raw.get('gross') is not None:
            return float(t_raw['gross'])
        u_raw = price_block.get('unitPrice')
        if isinstance(u_raw, dict) and u_raw.get('gross') is not None:
            return float(u_raw['gross']) * qty
        tax_status = (price_block.get('taxStatus') or 'gross').lower()
        if tax_status == 'gross':
            if isinstance(t_raw, (int, float)):
                return float(t_raw)
            if isinstance(u_raw, (int, float)):
                return float(u_raw) * qty
        return 0.0

    def _parse_order_line_payload(self, item_attrs):
        """Payload kann dict oder JSON-String sein (Shopware-API)."""
        p = item_attrs.get('payload')
        if p is None:
            return {}
        if isinstance(p, dict):
            return p
        if isinstance(p, str):
            try:
                return json.loads(p)
            except (ValueError, TypeError):
                return {}
        return {}

    def _configurator_inner_dict(self, item):
        """Eintrag aus options/user_configuration: liefert das configurator-Dict oder None."""
        if not isinstance(item, dict):
            return None
        inner = item.get('configurator')
        if isinstance(inner, dict):
            return inner
        if any(
            k in item
            for k in ('field_label', 'user_value_formatted', 'field_key', 'user_value')
        ):
            return item
        return None

    def _format_one_configurator_line(self, inner):
        """Eine Zeile „Bezeichnung : Anzeigewert“ (Neonlines-Konfigurator)."""
        if not isinstance(inner, dict):
            return None
        label = (inner.get('field_label') or inner.get('group') or '').strip()
        val = inner.get('user_value_formatted')
        if val is None or val == '':
            val = inner.get('option')
        if val is None or val == '':
            val = inner.get('user_value')
        if val is not None:
            val = str(val).strip()
        if not label and not val:
            return None
        if not label:
            return val
        if not val:
            return label
        return f"{label} : {val}"

    def _payload_configurator_display_lines(self, payload):
        """
        Kurzliste wie „Design : Plus“ aus options (Reihenfolge) und fehlenden
        Feldern aus neon_configurator.user_configuration (dedupliziert per field_key).
        """
        if not isinstance(payload, dict):
            return []
        lines = []
        seen = set()

        def append_from_item(item):
            inner = self._configurator_inner_dict(item)
            if not inner:
                return
            line = self._format_one_configurator_line(inner)
            if not line:
                return
            fk = inner.get('field_key') or inner.get('key')
            dedup = fk if fk else line
            if dedup in seen:
                return
            seen.add(dedup)
            lines.append(line)

        opts = payload.get('options')
        if isinstance(opts, list):
            for item in opts:
                append_from_item(item)

        neon = payload.get('neon_configurator')
        if isinstance(neon, dict):
            uc = neon.get('user_configuration')
            if isinstance(uc, list):
                for item in uc:
                    append_from_item(item)

        return lines

    def _order_line_description_with_config(self, item_attrs, base_label):
        """Bezeichnung inkl. kompakter Konfiguration (Label : Wert) aus Payload."""
        label = (base_label or '').strip()
        payload = self._parse_order_line_payload(item_attrs)
        lines = self._payload_configurator_display_lines(payload)
        if not lines:
            return label
        extra = '\n'.join(lines)
        if len(extra) > 12000:
            extra = extra[:11900] + '\n…'
        return (label + '\n\n' + extra) if label else extra

    @staticmethod
    def _shopware_uuid_search_variants(uid):
        """Gleiche UUID in verschiedenen Schreibweisen (mit/ohne Bindestriche)."""
        if not uid:
            return []
        s = str(uid).strip()
        if not s:
            return []
        out = []
        for cand in (s, s.lower()):
            if cand not in out:
                out.append(cand)
        hexonly = re.sub(r'[^0-9a-fA-F]', '', s)
        if len(hexonly) == 32:
            canon = (
                f"{hexonly[0:8]}-{hexonly[8:12]}-"
                f"{hexonly[12:16]}-{hexonly[16:20]}-{hexonly[20:32]}"
            )
            for cand in (canon, canon.lower(), hexonly.lower()):
                if cand not in out:
                    out.append(cand)
        return out

    def _order_line_candidate_product_sw_ids(self, item, item_attrs):
        """Mögliche Shopware-Produkt-UUIDs aus Position (Attribute, Payload, Assoziationen)."""
        ids = []
        pid = item_attrs.get('productId')
        if pid:
            ids.append(str(pid).strip())
        line_type = (
            item_attrs.get('type') or item_attrs.get('orderLineItemType') or ''
        ).lower()
        if not pid and line_type in ('product', 'custom'):
            rid = item_attrs.get('referencedId')
            if rid:
                ids.append(str(rid).strip())

        payload = self._parse_order_line_payload(item_attrs)
        for key in ('productId', 'id'):
            v = payload.get(key) if isinstance(payload, dict) else None
            if v:
                ids.append(str(v).strip())

        prod = item_attrs.get('product')
        if isinstance(prod, dict):
            if prod.get('id'):
                ids.append(str(prod['id']).strip())
            inner = prod.get('attributes')
            if isinstance(inner, dict) and inner.get('id'):
                ids.append(str(inner['id']).strip())

        rel = item.get('relationships') or {}
        rp = rel.get('product') or {}
        data = rp.get('data')
        if isinstance(data, dict) and data.get('id'):
            ids.append(str(data['id']).strip())
        elif isinstance(data, list):
            for d in data:
                if isinstance(d, dict) and d.get('id'):
                    ids.append(str(d['id']).strip())

        seen = set()
        out = []
        for i in ids:
            if i and i not in seen:
                seen.add(i)
                out.append(i)
        return out

    def _find_shopware_product_for_order_line(self, order, item, item_attrs, product_number):
        """shopware.product per UUID (inkl. Varianten) oder Artikelnummer finden bzw. nachladen."""
        Product = self.env['shopware.product']
        backend = order.backend_id
        backend_id = backend.id

        all_variants = []
        for uid in self._order_line_candidate_product_sw_ids(item, item_attrs):
            all_variants.extend(self._shopware_uuid_search_variants(uid))
        all_variants = list(dict.fromkeys(all_variants))
        if all_variants:
            sw_product = Product.search([
                ('shopware_id', 'in', all_variants),
                ('backend_id', '=', backend_id),
            ], limit=1)
            if sw_product:
                return sw_product

        if product_number:
            sw_product = Product.search([
                ('shopware_product_number', '=', product_number),
                ('backend_id', '=', backend_id),
            ], limit=1)
            if sw_product:
                return sw_product

        for uid in self._order_line_candidate_product_sw_ids(item, item_attrs):
            sw_product = Product.import_product_by_shopware_id(backend, uid)
            if sw_product:
                return sw_product

        if product_number:
            sw_product = Product.import_product_by_product_number(backend, product_number)
            if sw_product:
                return sw_product

        return Product.browse()

    def _normalize_order_line_item_attrs(self, item):
        """Attribute der Bestellposition vereinheitlichen (JSON:API + productId aus Relationships)."""
        if not isinstance(item, dict):
            return {}
        raw = item.get('attributes')
        if isinstance(raw, dict):
            item_attrs = dict(raw)
        else:
            skip = {'relationships', 'links', 'meta', 'included'}
            item_attrs = {
                k: v for k, v in item.items()
                if k not in skip
            }
        rel = item.get('relationships') or {}
        pdata = (rel.get('product') or {}).get('data')
        if isinstance(pdata, dict) and pdata.get('id') and not item_attrs.get('productId'):
            item_attrs['productId'] = pdata['id']
        if not item_attrs.get('productId') and item_attrs.get('product_id'):
            item_attrs['productId'] = item_attrs['product_id']
        return item_attrs

    def _import_order_lines(self, order, attrs):
        line_items = attrs.get('lineItems') or []
        deliveries = attrs.get('deliveries') or []
        d0 = deliveries[0] if deliveries else {}
        d_attrs = d0.get('attributes', d0) if isinstance(d0, dict) else {}
        delivery_shipping_costs = (
            d_attrs.get('shippingCosts')
            if isinstance(d_attrs.get('shippingCosts'), dict)
            else None
        )

        for item in line_items:
            item_attrs = self._normalize_order_line_item_attrs(item)
            label = self._order_line_description_with_config(
                item_attrs, item_attrs.get('label', ''),
            )
            quantity = item_attrs.get('quantity', 1)
            unit_price, total_price = self._order_line_net_unit_and_total(item_attrs)
            if (
                delivery_shipping_costs
                and self._line_item_type(item_attrs) == 'shipping'
                and total_price <= 0
                and unit_price <= 0
            ):
                unit_price, total_price = self._order_line_net_unit_and_total({
                    'price': delivery_shipping_costs,
                    'quantity': 1,
                })
            payload = self._parse_order_line_payload(item_attrs)
            product_number = (
                (payload.get('productNumber') if isinstance(payload, dict) else None)
                or item_attrs.get('productNumber')
                or ''
            )

            sw_product = self._find_shopware_product_for_order_line(
                order, item, item_attrs, product_number,
            )

            line_gross = self._order_line_shopware_gross_total(item_attrs)

            self.env['shopware.order.line'].create({
                'order_id': order.id,
                'shopware_product_id': sw_product.id if sw_product else False,
                'name': label,
                'product_number': product_number,
                'quantity': quantity,
                'unit_price': unit_price,
                'total_price': total_price,
                'shopware_line_gross': line_gross,
            })

        self._append_shipping_from_delivery_if_needed(order, attrs)

    def _line_item_type(self, item_attrs):
        return (item_attrs.get('type') or item_attrs.get('orderLineItemType') or '').lower()

    def _append_shipping_from_delivery_if_needed(self, order, attrs):
        """Versandkosten aus OrderDelivery.shippingCosts, wenn Shopware keine Versand-Position liefert."""
        line_items = attrs.get('lineItems') or []
        if any(
            self._line_item_type(item.get('attributes', item)) == 'shipping'
            for item in line_items
        ):
            return
        deliveries = attrs.get('deliveries') or []
        if not deliveries:
            return
        d0 = deliveries[0] if isinstance(deliveries, list) else deliveries
        d_attrs = d0.get('attributes', d0)
        sc = d_attrs.get('shippingCosts')
        if not isinstance(sc, dict):
            return
        ship_attrs = {'price': sc, 'quantity': 1}
        unit_n, total_n = self._order_line_net_unit_and_total(ship_attrs)
        if total_n <= 0 and unit_n <= 0:
            return
        line_gross = self._order_line_shopware_gross_total(ship_attrs)
        sm = d_attrs.get('shippingMethod') or {}
        tr = sm.get('translated') or {}
        label = tr.get('name') or sm.get('name') or _('Versand')
        self.env['shopware.order.line'].create({
            'order_id': order.id,
            'shopware_product_id': False,
            'name': label,
            'product_number': '',
            'quantity': 1.0,
            'unit_price': unit_n,
            'total_price': total_n,
            'shopware_line_gross': line_gross,
        })
        _logger.info(
            "Versandposition aus Lieferung übernommen: %s (netto %.2f)",
            label, total_n,
        )

    def _shopware_sale_line_price_unit(self, line):
        """Einzelpreis für Odoo aus Shopware-Zeilensumme (netto), konsistent gerundet."""
        prec = self.env['decimal.precision'].precision_get('Product Price')
        qty = float(line.quantity or 0.0) or 1.0
        if line.total_price and qty:
            raw = float(line.total_price) / qty
        else:
            raw = float(line.unit_price or 0.0)
        return float_round(raw, precision_digits=prec)

    def _align_sale_order_line_to_shopware_gross(self, sol, sw_line):
        """Netto-Einzelpreis iterativ anpassen, damit Odoo-Zeilenbrutto = Shopware ``shopware_line_gross``.

        Ohne das liefert z. B. 3,24 × 1,19 → 3,8556 → gerundet 3,86 statt Shopware 3,85.
        """
        target = float(sw_line.shopware_line_gross or 0.0)
        if not target:
            return
        taxes = sol.tax_ids
        if not taxes:
            return
        currency = sol.order_id.currency_id
        target = currency.round(target)
        partner = sol.order_id.partner_id
        qty = sol.product_uom_qty or 1.0
        product = sol.product_id
        pu = float(sol.price_unit)
        for _ in range(28):
            res = taxes.compute_all(
                pu,
                currency=currency,
                quantity=qty,
                product=product,
                partner=partner,
            )
            got = currency.round(res['total_included'])
            if currency.is_zero(target - got):
                break
            if abs(got) < 1e-12:
                break
            pu *= target / got
        sol.write({
            'price_unit': pu,
            'technical_price_unit': 0.0,
        })

    def _shopware_adjustment_service_product(self, backend):
        """Ein verkaufsfähiges Dienstleistungsprodukt für Rundungszeilen."""
        return self.env['product.product'].search([
            ('type', '=', 'service'),
            ('sale_ok', '=', True),
            '|', ('company_id', '=', False),
            ('company_id', '=', backend.company_id.id),
        ], limit=1)

    def _shopware_apply_sale_order_net_rounding(self, sale_order, sw_order, backend):
        """Rest-Differenz der Nettosumme zu Shopware per Zusatzzeile (max. 5 ct).

        Fehler hier dürfen den Auftrag nicht verwerfen (kein HTTP 500 beim Import).
        """
        try:
            if not sale_order.order_line:
                return
            if any(l.shopware_line_gross for l in sw_order.line_ids):
                return
            currency = sale_order.currency_id
            target_r = currency.round(float(sw_order.amount_net or 0.0))
            actual_r = currency.round(float(sale_order.amount_untaxed))
            diff = currency.round(target_r - actual_r)
            if currency.is_zero(diff) or abs(diff) > 0.05:
                return
            ref = sale_order.order_line.filtered(lambda l: not l.display_type)[:1]
            if not ref:
                return
            adj = self._shopware_adjustment_service_product(backend)
            if not adj:
                _logger.debug(
                    "Shopware Netto-Rundung %.4f €: kein verkaufsfähiges Dienstleistungsprodukt",
                    diff,
                )
                return
            line_vals = {
                'order_id': sale_order.id,
                'product_id': adj.id,
                'sequence': 9999,
                'name': _('Shopware Netto-Rundungsausgleich'),
                'product_uom_qty': 1.0,
                'price_unit': diff,
                'technical_price_unit': 0.0,
            }
            if ref.tax_ids:
                line_vals['tax_ids'] = [(6, 0, ref.tax_ids.ids)]
            self.env['sale.order.line'].create(line_vals)
        except Exception:
            _logger.exception(
                "Shopware Netto-Rundungszeile übersprungen (Auftrag bleibt erhalten)",
            )

    def _shopware_apply_sale_order_gross_rounding(self, sale_order, sw_order, backend):
        """Bruttosumme an Shopware ``amountTotal`` anbinden (typisch 0,01 € Steuerrundung).

        Netto-Rundung allein reicht nicht, wenn Odoo die USt. pro Zeile anders rundet.
        """
        try:
            if not sale_order.order_line:
                return
            target = float(sw_order.amount_total or 0.0)
            if not target:
                return
            currency = sale_order.currency_id
            sale_order.invalidate_recordset()
            actual = float(sale_order.amount_total)
            diff_gross = currency.round(target - actual)
            if currency.is_zero(diff_gross) or abs(diff_gross) > 0.02:
                return
            ref = sale_order.order_line.filtered(lambda l: not l.display_type)[:1]
            if not ref:
                return
            adj = self._shopware_adjustment_service_product(backend)
            if not adj:
                _logger.debug(
                    "Shopware Brutto-Rundung %.4f €: kein Dienstleistungsprodukt",
                    diff_gross,
                )
                return
            partner = sale_order.partner_id
            taxes = ref.tax_ids
            prec = self.env['decimal.precision'].precision_get('Product Price') or 2
            if taxes:
                delta_net = diff_gross
                for _ in range(16):
                    res = taxes.compute_all(
                        delta_net,
                        currency=currency,
                        quantity=1.0,
                        product=adj,
                        partner=partner,
                    )
                    got = res['total_included']
                    if currency.is_zero(diff_gross - got):
                        break
                    if currency.is_zero(got):
                        delta_net = diff_gross
                        break
                    delta_net *= diff_gross / got
                delta_net = float_round(delta_net, precision_digits=prec)
            else:
                delta_net = float_round(diff_gross, precision_digits=prec)
            line_vals = {
                'order_id': sale_order.id,
                'product_id': adj.id,
                'sequence': 10000,
                'name': _('Shopware Brutto-Rundungsausgleich'),
                'product_uom_qty': 1.0,
                'price_unit': delta_net,
                'technical_price_unit': 0.0,
            }
            if taxes:
                line_vals['tax_ids'] = [(6, 0, taxes.ids)]
            self.env['sale.order.line'].create(line_vals)
        except Exception:
            _logger.exception(
                "Shopware Brutto-Rundungszeile übersprungen (Auftrag bleibt erhalten)",
            )

    def _create_sale_order(self, sw_order, backend):
        """Create a sale.order in Odoo from the imported Shopware order."""
        partner = False
        if sw_order.shopware_customer_id and sw_order.shopware_customer_id.odoo_partner_id:
            partner = sw_order.shopware_customer_id.odoo_partner_id
        elif sw_order.customer_email:
            partner = self.env['res.partner'].search([
                ('email', '=', sw_order.customer_email),
            ], limit=1)

        if not partner:
            lang_code = self.env['shopware.customer']._partner_lang_de_code()
            create_vals = {
                'name': sw_order.customer_name or sw_order.customer_email or 'Shopware Kunde',
                'email': sw_order.customer_email or '',
                'street': sw_order.billing_street or '',
                'zip': sw_order.billing_zipcode or '',
                'city': sw_order.billing_city or '',
            }
            if lang_code:
                create_vals['lang'] = lang_code
            partner = self.env['res.partner'].create(create_vals)

        so_vals = {
            'partner_id': partner.id,
            'date_order': sw_order.order_date or fields.Datetime.now(),
            'client_order_ref': sw_order.shopware_order_number,
            'company_id': backend.company_id.id,
        }
        if backend.default_payment_term_id:
            so_vals['payment_term_id'] = backend.default_payment_term_id.id

        cmt = (sw_order.customer_comment or '').strip()
        pay_st = (sw_order.payment_status or '').strip() or (sw_order.payment_status_technical or '').strip()
        so_vals.update({
            'shopware_order_id': sw_order.id,
            'shopware_customer_comment': sw_order.customer_comment or False,
            'shopware_payment_method': sw_order.payment_method or False,
            'shopware_shipping_method': sw_order.shipping_method or False,
            'shopware_payment_status': pay_st or False,
            'shopware_order_state': (sw_order.state_name or '').strip() or False,
            'shopware_has_customer_comment': bool(cmt),
        })

        order_lines = []
        for line in sw_order.line_ids:
            product = False
            if line.shopware_product_id and line.shopware_product_id.odoo_product_id:
                product = line.shopware_product_id.odoo_product_id
            else:
                product = self.env['product.product'].search([
                    ('default_code', '=', line.product_number),
                ], limit=1) if line.product_number else False

            if not product:
                product = self.env['product.product'].search([
                    ('name', '=', line.name),
                ], limit=1)

            if not product:
                product = self.env['product.product'].create({
                    'name': line.name or 'Shopware Artikel',
                    'default_code': line.product_number or '',
                    'type': 'consu',
                    'list_price': line.unit_price,
                })

            pu = self._shopware_sale_line_price_unit(line)
            order_lines.append((0, 0, {
                'product_id': product.id,
                'name': line.name or product.name,
                'product_uom_qty': line.quantity,
                'price_unit': pu,
            }))

        so_vals['order_line'] = order_lines

        try:
            sale_order = self.env['sale.order'].create(so_vals)
            # Nach dem Anlegen: manueller Nettopreis (sonst überschreibt Odoo 19 die Preisliste).
            od_lines = sale_order.order_line
            sw_lines = sw_order.line_ids
            if len(od_lines) != len(sw_lines):
                _logger.warning(
                    "Odoo %d vs. Shopware %d Positionen — Preise nur für gemeinsame Zeilen gesetzt",
                    len(od_lines), len(sw_lines),
                )
            for sol, swl in zip(od_lines, sw_lines):
                pu = self._shopware_sale_line_price_unit(swl)
                sol.write({
                    'price_unit': pu,
                    'technical_price_unit': 0.0,
                })
            for sol, swl in zip(od_lines, sw_lines):
                self._align_sale_order_line_to_shopware_gross(sol, swl)
            self._shopware_apply_sale_order_net_rounding(
                sale_order, sw_order, backend,
            )
            self._shopware_apply_sale_order_gross_rounding(
                sale_order, sw_order, backend,
            )
            sw_order.write({'odoo_sale_order_id': sale_order.id})
            _logger.info(
                "Odoo Auftrag %s erstellt für Shopware Bestellung %s",
                sale_order.name, sw_order.shopware_order_number,
            )
        except Exception:
            _logger.exception(
                "Fehler beim Erstellen des Odoo Auftrags für %s",
                sw_order.shopware_order_number,
            )

    def write(self, vals):
        res = super().write(vals)
        sync_keys = (
            'customer_comment', 'payment_method', 'shipping_method',
            'odoo_sale_order_id', 'payment_status', 'payment_status_technical',
            'state_name', 'order_state_technical',
        )
        if any(k in vals for k in sync_keys):
            for order in self:
                if order.odoo_sale_order_id:
                    order.odoo_sale_order_id._sync_shopware_meta_from_connector()
        return res

    def action_view_sale_order(self):
        self.ensure_one()
        if not self.odoo_sale_order_id:
            raise UserError(_("Kein Odoo-Auftrag verknüpft."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.odoo_sale_order_id.id,
            'view_mode': 'form',
        }

    def action_create_sale_order(self):
        """Manually create a sale order for this Shopware order."""
        self.ensure_one()
        if self.odoo_sale_order_id:
            raise UserError(_("Es existiert bereits ein verknüpfter Odoo-Auftrag."))
        self._create_sale_order(self, self.backend_id)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Auftrag erstellt"),
                'message': _("Der Odoo-Auftrag wurde erfolgreich erstellt."),
                'type': 'success',
                'sticky': False,
            },
        }


class ShopwareOrderLine(models.Model):
    _name = 'shopware.order.line'
    _description = 'Shopware Bestellposition'

    order_id = fields.Many2one(
        'shopware.order', string="Bestellung", required=True, ondelete='cascade',
    )
    shopware_product_id = fields.Many2one('shopware.product', string="Shopware Produkt")
    name = fields.Char(string="Bezeichnung")
    product_number = fields.Char(string="Artikelnummer")
    quantity = fields.Float(string="Menge", default=1.0)
    unit_price = fields.Float(string="Einzelpreis (Netto)", digits='Product Price')
    total_price = fields.Float(string="Gesamtpreis (Netto)", digits='Product Price')
    shopware_line_gross = fields.Float(
        string="Zeilenbrutto (Shopware)",
        digits='Product Price',
        help="Bruttosumme der Position laut Shopware-API; wird genutzt, um den Odoo-Nettopreis "
             "so anzupassen, dass Brutto nach USt.-Berechnung übereinstimmt.",
    )
