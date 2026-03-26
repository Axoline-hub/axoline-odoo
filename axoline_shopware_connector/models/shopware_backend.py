# Proprietary module. See LICENSE file for full copyright and licensing details.

import json
import logging
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SW_AUTH_ENDPOINT = '/api/oauth/token'
SW_SEARCH_SUFFIX = '/search'


class ShopwareBackend(models.Model):
    _name = 'shopware.backend'
    _description = 'Shopware 6 Backend'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Name", required=True, tracking=True)
    url = fields.Char(
        string="Shop-URL",
        required=True,
        tracking=True,
        help="Basis-URL des Shopware 6 Shops, z.B. https://meinshop.de",
    )
    client_id = fields.Char(string="Client ID / Access Key ID", required=True)
    client_secret = fields.Char(string="Client Secret / Secret Access Key", required=True)
    active = fields.Boolean(default=True, tracking=True)
    state = fields.Selection([
        ('draft', 'Entwurf'),
        ('confirmed', 'Verbunden'),
        ('error', 'Fehler'),
    ], string="Status", default='draft', tracking=True)
    company_id = fields.Many2one(
        'res.company', string="Unternehmen",
        default=lambda self: self.env.company, required=True,
    )

    last_sync_date = fields.Datetime(string="Letzte Synchronisation")
    last_product_sync = fields.Datetime(string="Letzter Produkt-Sync")
    last_category_sync = fields.Datetime(string="Letzter Kategorie-Sync")
    last_customer_sync = fields.Datetime(string="Letzter Kunden-Sync")
    last_order_sync = fields.Datetime(string="Letzter Bestell-Sync")

    sync_products = fields.Boolean(string="Produkte synchronisieren", default=True)
    sync_categories = fields.Boolean(string="Kategorien synchronisieren", default=True)
    sync_customers = fields.Boolean(string="Kunden synchronisieren", default=True)
    sync_orders = fields.Boolean(string="Bestellungen synchronisieren", default=True)

    export_products = fields.Boolean(string="Produkte exportieren", default=False)
    export_categories = fields.Boolean(string="Kategorien exportieren", default=False)

    default_sales_channel_id = fields.Char(string="Standard Sales Channel ID")
    default_tax_id = fields.Many2one('account.tax', string="Standard-Steuer")
    default_payment_term_id = fields.Many2one(
        'account.payment.term', string="Standard-Zahlungsbedingung",
    )
    import_order_status = fields.Selection([
        ('all', 'Alle'),
        ('open', 'Nur offene'),
        ('completed', 'Nur abgeschlossene'),
    ], string="Bestellstatus-Filter", default='all',
        help="Steuert, welche Shopware-Bestellungen bei Suche und Abruf berücksichtigt werden "
             "(Hauptimport, Test-UUID, verknüpfte Aktualisierung). „Alle“ = kein Statusfilter.",
    )
    import_orders_from_date = fields.Date(
        string="Bestellungen ab Datum",
        help="Nur Bestellungen ab diesem Datum importieren. "
             "Leer = alle (beim ersten Sync) bzw. seit letztem Sync.",
    )
    import_order_limit = fields.Integer(
        string="Max. Bestellungen pro Sync",
        default=500,
        help="Maximale Anzahl Bestellungen pro Synchronisation. "
             "0 = unbegrenzt. Empfohlen: 200-1000.",
    )
    sync_order_linked_refresh_limit = fields.Integer(
        string="Verknüpfte Bestellungen pro Lauf aktualisieren",
        default=200,
        help="Bestellungen mit Odoo-Verknüpfung, solange der Shopware-Bestellstatus nicht "
             "abgeschlossen/storniert ist. Der Abruf erfolgt per Bestell-ID **ohne** den "
             "„Bestellstatus-Filter“ des Hauptimports — Zahlungsstatus und andere Felder "
             "werden so unabhängig davon aktualisiert. Pro Lauf höchstens so viele "
             "Kandidaten (nach ältester Synchronisation). 0 = aus.",
    )
    order_push_in_progress_on_import = fields.Boolean(
        string="Nach Import: Status „In Bearbeitung“ in Shopware",
        default=True,
        help="Wenn aktiv und die Bestellung in Shopware noch den Status „offen“ (open) hat, "
             "wird nach erfolgreichem Import die State-Machine-Transition in Shopware ausgelöst "
             "(Standard: process → in_progress).",
    )
    push_delivery_shipped_on_picking_done = fields.Boolean(
        string="Lieferschein: Versand in Shopware auf „versendet“",
        default=True,
        help="Wenn aktiv, wird bei erfolgreicher Auslieferung (Lieferschein erledigt) die "
             "Shopware-Lieferung per State-Machine auf „versendet“ (shipped) gesetzt.",
    )
    push_order_completed_when_shipped_and_invoiced = fields.Boolean(
        string="Bestellung: in Shopware „abgeschlossen“ bei Versand + Rechnung",
        default=True,
        help="Wenn aktiv, wird die Shopware-Bestellung per State-Machine auf „abgeschlossen“ "
             "(completed) gesetzt, sobald in Odoo alle ausgehenden/dropship-Lieferscheine erledigt "
             "sind und mindestens eine gebuchte Kundenrechnung zum Auftrag existiert.",
    )
    order_complete_transition = fields.Char(
        string="Transition → Bestellung abgeschlossen",
        default='complete',
        help="Technischer Name der Shopware-Transition zur Bestellung „completed“ "
             "(Standard Shopware: complete). Nur bei abweichender State Machine anpassen.",
    )
    order_import_open_transition = fields.Char(
        string="Transition offen → in Bearbeitung",
        default='process',
        help="Technischer Name der Shopware-Transition von „open“ zu „in_progress“ "
             "(Standard Shopware: process). Nur bei abweichender State Machine anpassen.",
    )
    import_order_test_id = fields.Char(
        string="Test: eine Shopware-Bestellungs-ID",
        help="Optional: UUID einer Bestellung in Shopware (Admin → Bestellungen → Detail / URL). "
             "Wenn gesetzt, importiert der Bestell-Import nur diese eine Bestellung — "
             "unabhängig vom Feld „Bestellstatus-Filter“. "
             "„Letzter Bestell-Sync“ wird dabei nicht gesetzt, damit ein anschließender "
             "Voll-Import unverändert möglich ist. Nach dem Test Feld leeren.",
    )
    import_customer_test_id = fields.Char(
        string="Test: eine Shopware-Kunden-ID",
        help="Optional: UUID eines Kunden in Shopware (Admin → Kunden → URL / Detail). "
             "Wenn gesetzt, importiert der Kunden-Import nur diesen einen Datensatz. "
             "Der Zeitpunkt „Letzter Kunden-Sync“ wird dabei nicht gesetzt, "
             "damit ein anschließender Voll-Import unverändert möglich ist. "
             "Nach dem Test Feld leeren.",
    )
    import_customer_limit = fields.Integer(
        string="Max. Kunden pro Lauf",
        default=0,
        help="0 = unbegrenzt. Sonst höchstens so viele Kunden pro Lauf "
             "(Reihenfolge wie von der Shopware-Suche geliefert). "
             "Für Stichproben; der Sync-Zeitstempel wird normal gesetzt.",
    )
    preserve_variant_data_on_attribute_change = fields.Boolean(
        string="Variantendaten bei Attributänderung erhalten",
        default=True,
        help="Wenn aktiviert, werden Artikelnummer (default_code) und Barcode beim "
             "Hinzufügen/Ändern von Produktattributen von der alten auf die neue "
             "Variante übertragen. Verhindert Datenverlust bei Varianten-Neugenerierung.",
    )
    api_batch_size = fields.Integer(
        string="API Batch-Größe",
        default=10,
        help="Anzahl Datensätze pro API-Anfrage. "
             "Kleiner = stabiler bei langsamen Shopware-Servern. "
             "Empfohlen: 5-25.",
    )

    product_count = fields.Integer(compute='_compute_counts', string="Produkte")
    category_count = fields.Integer(compute='_compute_counts', string="Kategorien")
    customer_count = fields.Integer(compute='_compute_counts', string="Kunden")
    order_count = fields.Integer(compute='_compute_counts', string="Bestellungen")

    sw_access_token = fields.Char(string="Access Token", groups="base.group_no_one")
    sw_token_expires = fields.Datetime(string="Token Ablauf", groups="base.group_no_one")

    @api.depends('active')
    def _compute_counts(self):
        for backend in self:
            backend.product_count = self.env['shopware.product'].search_count(
                [('backend_id', '=', backend.id)])
            backend.category_count = self.env['shopware.category'].search_count(
                [('backend_id', '=', backend.id)])
            backend.customer_count = self.env['shopware.customer'].search_count(
                [('backend_id', '=', backend.id)])
            backend.order_count = self.env['shopware.order'].search_count(
                [('backend_id', '=', backend.id)])

    # -------------------------------------------------------------------------
    # API Helpers
    # -------------------------------------------------------------------------

    def _get_base_url(self):
        self.ensure_one()
        url = self.url.rstrip('/')
        return url

    def _authenticate(self):
        """Authenticate with Shopware 6 API and obtain an access token."""
        self.ensure_one()
        if self.sw_access_token and self.sw_token_expires and self.sw_token_expires > fields.Datetime.now():
            return self.sw_access_token

        url = f"{self._get_base_url()}{SW_AUTH_ENDPOINT}"
        payload = {
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        try:
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self.write({'state': 'error'})
            raise UserError(_(
                "Shopware-Authentifizierung fehlgeschlagen: %(error)s",
                error=str(e),
            )) from e

        data = response.json()
        token = data.get('access_token')
        expires_in = data.get('expires_in', 600)
        self.write({
            'sw_access_token': token,
            'sw_token_expires': fields.Datetime.now() + timedelta(seconds=expires_in - 30),
        })
        return token

    def _get_headers(self):
        token = self._authenticate()
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _api_get(self, endpoint, params=None):
        """Perform a GET request against the Shopware 6 API."""
        self.ensure_one()
        url = f"{self._get_base_url()}/api/{endpoint.lstrip('/')}"
        try:
            response = requests.get(url, headers=self._get_headers(), params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            _logger.error("Shopware API GET %s fehlgeschlagen: %s", endpoint, e)
            raise UserError(_(
                "Shopware API Anfrage fehlgeschlagen: %(error)s", error=str(e),
            )) from e

    def _api_post(self, endpoint, data=None):
        """Perform a POST request (search / create) against the Shopware 6 API."""
        self.ensure_one()
        url = f"{self._get_base_url()}/api/{endpoint.lstrip('/')}"
        try:
            response = requests.post(url, headers=self._get_headers(), json=data or {}, timeout=30)
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}
        except requests.exceptions.RequestException as e:
            _logger.error("Shopware API POST %s fehlgeschlagen: %s", endpoint, e)
            raise UserError(_(
                "Shopware API Anfrage fehlgeschlagen: %(error)s", error=str(e),
            )) from e

    def _api_patch(self, endpoint, data=None):
        """Perform a PATCH request (update) against the Shopware 6 API."""
        self.ensure_one()
        url = f"{self._get_base_url()}/api/{endpoint.lstrip('/')}"
        try:
            response = requests.patch(url, headers=self._get_headers(), json=data or {}, timeout=30)
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}
        except requests.exceptions.RequestException as e:
            _logger.error("Shopware API PATCH %s fehlgeschlagen: %s", endpoint, e)
            raise UserError(_(
                "Shopware API Anfrage fehlgeschlagen: %(error)s", error=str(e),
            )) from e

    def _api_order_state_transition(self, order_uuid, transition):
        """Bestell-Status in Shopware per State-Machine wechseln (POST _action/…).

        Standard-Transition ``process``: Shopware „open“ → „in_progress“ (siehe State-Machine-Doku).

        Kein UserError: Fehler werden geloggt, Rückgabe True/False (z. B. bei Import-Follow-up).
        """
        self.ensure_one()
        transition = (transition or '').strip() or 'process'
        paths = [
            f"_action/order-state/order/{order_uuid}/transition/{transition}",
            f"_action/order/{order_uuid}/state/{transition}",
        ]
        last_err = None
        for endpoint in paths:
            url = f"{self._get_base_url()}/api/{endpoint.lstrip('/')}"
            try:
                response = requests.post(
                    url, headers=self._get_headers(), json={}, timeout=30,
                )
                response.raise_for_status()
                return True
            except requests.exceptions.HTTPError as e:
                last_err = e
                if e.response is not None and e.response.status_code == 404:
                    continue
                body = ''
                try:
                    body = (e.response.text[:500] if e.response is not None else '')
                except Exception:
                    body = ''
                _logger.warning(
                    "Shopware Bestell-Transition %s für Order %s fehlgeschlagen (HTTP): %s %s",
                    transition, order_uuid, e, body,
                )
                return False
            except requests.exceptions.RequestException as e:
                last_err = e
                _logger.warning(
                    "Shopware Bestell-Transition %s für Order %s (%s): %s",
                    transition, order_uuid, endpoint, e,
                )
                return False
        if last_err is not None:
            _logger.warning(
                "Shopware Bestell-Transition %s für Order %s: kein Endpoint gefunden (404)",
                transition, order_uuid,
            )
        return False

    def _api_order_delivery_state_transition(self, delivery_uuid, transition):
        """order_delivery: Versandstatus in Shopware (Standard-Transition ``ship`` → shipped).

        Kein UserError: Fehler werden geloggt, Rückgabe True/False.
        """
        self.ensure_one()
        transition = (transition or '').strip() or 'ship'
        paths = [
            f"_action/order-state/order-delivery/{delivery_uuid}/transition/{transition}",
            f"_action/order-delivery/{delivery_uuid}/state/{transition}",
            f"_action/order_delivery/{delivery_uuid}/state/{transition}",
        ]
        last_err = None
        for endpoint in paths:
            url = f"{self._get_base_url()}/api/{endpoint.lstrip('/')}"
            try:
                response = requests.post(
                    url, headers=self._get_headers(), json={}, timeout=30,
                )
                response.raise_for_status()
                return True
            except requests.exceptions.HTTPError as e:
                last_err = e
                if e.response is not None and e.response.status_code == 404:
                    continue
                body = ''
                try:
                    body = (e.response.text[:500] if e.response is not None else '')
                except Exception:
                    body = ''
                _logger.warning(
                    "Shopware Liefer-Transition %s für order_delivery %s fehlgeschlagen (HTTP): %s %s",
                    transition, delivery_uuid, e, body,
                )
                return False
            except requests.exceptions.RequestException as e:
                last_err = e
                _logger.warning(
                    "Shopware Liefer-Transition %s für order_delivery %s (%s): %s",
                    transition, delivery_uuid, endpoint, e,
                )
                return False
        if last_err is not None:
            _logger.warning(
                "Shopware Liefer-Transition %s für order_delivery %s: kein Endpoint gefunden (404)",
                transition, delivery_uuid,
            )
        return False

    def _api_search(self, entity, payload=None, max_records=0):
        """Search entities via the Shopware 6 search API (POST /api/search/<entity>).

        Handles pagination and returns all matching results.
        ``max_records``: stop after this many records (0 = unlimited).
        """
        self.ensure_one()
        endpoint = f"search/{entity}"
        payload = payload or {}
        page_size = min(payload.get('limit', 500), 500)
        if max_records and max_records < page_size:
            page_size = max_records
        payload['limit'] = page_size
        payload['total-count-mode'] = 1
        page = 1
        all_data = []
        prev_page_ids = None
        # Shopware liefert oft total=0; bei wiederholter gleicher Seite / fehlerhafter Pagination Endlosschleife vermeiden
        max_pages = 100000
        while True:
            if page > max_pages:
                _logger.error(
                    "Shopware API search %s: Schutzgrenze %d Seiten erreicht (ggf. unvollständig)",
                    entity, max_pages,
                )
                break
            payload['page'] = page
            result = self._api_post(endpoint, payload)
            data = result.get('data', [])
            included = result.get('included') or []
            if entity == 'order' and included and data:
                Order = self.env['shopware.order']
                data = [
                    Order._enrich_jsonapi_order_from_included(item, included)
                    for item in data
                ]
            if data:
                page_ids = tuple(item.get('id') for item in data)
                if prev_page_ids is not None and page_ids == prev_page_ids:
                    _logger.warning(
                        "Shopware API search %s: Seite %d identisch mit vorheriger — Pagination abgebrochen",
                        entity, page,
                    )
                    break
                prev_page_ids = page_ids
            all_data.extend(data)
            total = result.get('total') or 0
            _logger.info(
                "Shopware API search %s: Seite %d, %d Ergebnisse auf Seite, %s gesamt laut API, %d bisher geladen",
                entity, page, len(data), total, len(all_data),
            )
            if max_records and len(all_data) >= max_records:
                all_data = all_data[:max_records]
                break
            if not data:
                break
            if len(data) < page_size:
                break
            if total and len(all_data) >= total:
                break
            page += 1
        _logger.info("Shopware API search %s abgeschlossen: %d Datensätze geladen", entity, len(all_data))
        return all_data

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def action_test_connection(self):
        """Test the Shopware 6 API connection."""
        self.ensure_one()
        try:
            self._authenticate()
            self.write({'state': 'confirmed'})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Erfolg"),
                    'message': _("Verbindung zu Shopware 6 erfolgreich hergestellt!"),
                    'type': 'success',
                    'sticky': False,
                },
            }
        except UserError:
            self.write({'state': 'error'})
            raise

    def _do_sync_all(self):
        """Internal sync — called by both UI buttons and cron.

        Each phase runs independently; a failure in one phase does not
        prevent subsequent phases from executing.
        """
        self.ensure_one()
        counts = {}
        if self.sync_products:
            try:
                rule_count = self.env['shopware.price.rule'].sync_rules_from_shopware(self) or 0
                counts['Preisregeln'] = rule_count
                self.env.cr.commit()
            except Exception:
                _logger.exception("Sync-Phase 'Preisregeln' fehlgeschlagen")
                self.env.cr.rollback()

        phases = [
            ('sync_categories', 'Kategorien', 'shopware.category', 'last_category_sync'),
            ('sync_products', 'Produkte', 'shopware.product', 'last_product_sync'),
            ('sync_customers', 'Kunden', 'shopware.customer', 'last_customer_sync'),
            ('sync_orders', 'Bestellungen', 'shopware.order', 'last_order_sync'),
        ]
        for flag, label, model_name, date_field in phases:
            if not getattr(self, flag, False):
                continue
            try:
                result = self.env[model_name].sync_from_shopware(self) or 0
                counts[label] = result
                skip_sync_date = (
                    (date_field == 'last_customer_sync' and self.import_customer_test_id)
                    or (date_field == 'last_order_sync' and self.import_order_test_id)
                )
                if date_field != 'last_product_sync' and not skip_sync_date:
                    self.write({date_field: fields.Datetime.now()})
                self.env.cr.commit()
            except Exception:
                _logger.exception("Sync-Phase '%s' fehlgeschlagen", label)
                self.env.cr.rollback()
        self.write({'last_sync_date': fields.Datetime.now()})
        self.env.cr.commit()
        return counts

    def action_sync_all(self):
        """Run a full synchronisation for all enabled entity types."""
        counts = self._do_sync_all()
        summary = ', '.join(f"{v} {k}" for k, v in counts.items())
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Synchronisation abgeschlossen"),
                'message': summary or _("Keine Daten synchronisiert."),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_sync_categories(self):
        self.ensure_one()
        count = self.env['shopware.category'].sync_from_shopware(self)
        self.write({'last_category_sync': fields.Datetime.now()})
        self.env.cr.commit()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Kategorie-Import"),
                'message': _("%(count)d Kategorien synchronisiert.", count=count or 0),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_sync_products(self):
        self.ensure_one()
        count = self.env['shopware.product'].sync_from_shopware(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Produkt-Import"),
                'message': _("%(count)d Produkte synchronisiert.", count=count or 0),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_sync_customers(self):
        self.ensure_one()
        count = self.env['shopware.customer'].sync_from_shopware(self)
        if not self.import_customer_test_id:
            self.write({'last_customer_sync': fields.Datetime.now()})
        self.env.cr.commit()
        msg = _("%(count)d Kunden synchronisiert.", count=count or 0)
        if self.import_customer_test_id:
            msg = _(
                "Test-Import: %(count)d Kunde(n). „Letzter Kunden-Sync“ unverändert — "
                "Test-ID danach leeren.",
                count=count or 0,
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Kunden-Import"),
                'message': msg,
                'type': 'success',
                'sticky': False,
            },
        }

    def action_sync_orders(self):
        self.ensure_one()
        count = self.env['shopware.order'].sync_from_shopware(self)
        if not self.import_order_test_id:
            self.write({'last_order_sync': fields.Datetime.now()})
        self.env.cr.commit()
        msg = _("%(count)d Bestellungen synchronisiert.", count=count or 0)
        if self.import_order_test_id:
            msg = _(
                "Test-Import: %(count)d Bestellung(en). „Letzter Bestell-Sync“ unverändert — "
                "Test-ID danach leeren.",
                count=count or 0,
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Bestell-Import"),
                'message': msg,
                'type': 'success',
                'sticky': False,
            },
        }

    def action_export_categories(self):
        self.ensure_one()
        self.env['shopware.category'].export_to_shopware(self)

    def action_export_products(self):
        self.ensure_one()
        self.env['shopware.product'].export_to_shopware(self)

    # Smart button actions
    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Produkte"),
            'res_model': 'shopware.product',
            'view_mode': 'list,form',
            'domain': [('backend_id', '=', self.id)],
            'context': {'default_backend_id': self.id},
        }

    def action_view_categories(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Kategorien"),
            'res_model': 'shopware.category',
            'view_mode': 'list,form',
            'domain': [('backend_id', '=', self.id)],
            'context': {'default_backend_id': self.id},
        }

    def action_view_customers(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Kunden"),
            'res_model': 'shopware.customer',
            'view_mode': 'list,form',
            'domain': [('backend_id', '=', self.id)],
            'context': {'default_backend_id': self.id},
        }

    def action_view_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Shopware Bestellungen"),
            'res_model': 'shopware.order',
            'view_mode': 'list,form',
            'domain': [('backend_id', '=', self.id)],
            'context': {'default_backend_id': self.id},
        }

    def action_diagnose_api(self):
        """Send a minimal product search to Shopware and display the raw response."""
        self.ensure_one()
        self._authenticate()
        url = f"{self._get_base_url()}/api/search/product"
        payload = {
            'limit': 3,
            'total-count-mode': 1,
            'filter': [
                {'type': 'equals', 'field': 'parentId', 'value': None},
            ],
            'includes': {
                'product': ['id', 'name', 'productNumber', 'parentId', 'active'],
            },
        }
        try:
            response = requests.post(url, headers=self._get_headers(), json=payload, timeout=30)
            status = response.status_code
            try:
                body = response.json()
            except Exception:
                body = response.text[:2000]

            info = {
                'status_code': status,
                'total': body.get('total') if isinstance(body, dict) else None,
                'data_count': len(body.get('data', [])) if isinstance(body, dict) else None,
                'data_keys': list(body.keys()) if isinstance(body, dict) else None,
                'first_item_keys': list(body['data'][0].keys()) if isinstance(body, dict) and body.get('data') else None,
                'first_item': body['data'][0] if isinstance(body, dict) and body.get('data') else None,
            }
            detail = json.dumps(info, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            detail = f"Fehler: {e}"

        raise UserError(_(
            "Shopware API Diagnose:\n\n%(detail)s",
            detail=detail,
        ))

    def action_reset_sync_dates(self):
        """Clear all sync dates to force a full re-import on next run."""
        self.ensure_one()
        self.write({
            'last_sync_date': False,
            'last_product_sync': False,
            'last_category_sync': False,
            'last_customer_sync': False,
            'last_order_sync': False,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Zurückgesetzt"),
                'message': _("Alle Sync-Daten wurden gelöscht. Der nächste Sync lädt alle Daten neu."),
                'type': 'warning',
                'sticky': False,
            },
        }

    # -------------------------------------------------------------------------
    # Cron
    # -------------------------------------------------------------------------

    @api.model
    def _cron_sync_all(self):
        backends = self.search([('state', '=', 'confirmed'), ('active', '=', True)])
        for backend in backends:
            try:
                backend._do_sync_all()
                self.env.cr.commit()
            except Exception:
                _logger.exception("Shopware Cron-Sync für Backend %s fehlgeschlagen", backend.name)
                self.env.cr.rollback()
