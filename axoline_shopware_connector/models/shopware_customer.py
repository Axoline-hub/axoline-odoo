# Proprietary module. See LICENSE file for full copyright and licensing details.

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Sprache für aus Shopware importierte Kontakte (Anzeige, E-Mails, …)
ODOO_PARTNER_LANG_DE = 'de_DE'
# Adress-Unterkontakte: UUID in ref (kein extra DB-Feld nötig)
SWADDR_REF_PREFIX = 'swaddr:'


class ShopwareCustomer(models.Model):
    _name = 'shopware.customer'
    _description = 'Shopware Kunde'
    _rec_name = 'name'
    _order = 'name'
    _sql_constraints = [
        ('unique_shopware_backend', 'UNIQUE(shopware_id, backend_id)',
         'Dieser Shopware-Kunde existiert bereits für dieses Backend.'),
    ]

    name = fields.Char(string="Name", compute='_compute_name', store=True)
    shopware_id = fields.Char(string="Shopware ID", index=True, readonly=True)
    backend_id = fields.Many2one(
        'shopware.backend', string="Backend", required=True, ondelete='cascade', index=True,
    )
    odoo_partner_id = fields.Many2one('res.partner', string="Odoo Kontakt")
    active = fields.Boolean(default=True)

    customer_number = fields.Char(string="Kundennummer")
    email = fields.Char(string="E-Mail", index=True)
    first_name = fields.Char(string="Vorname")
    last_name = fields.Char(string="Nachname")
    company = fields.Char(string="Firma")
    title = fields.Char(string="Anrede")
    birthday = fields.Date(string="Geburtstag")

    street = fields.Char(string="Straße")
    zipcode = fields.Char(string="PLZ")
    city = fields.Char(string="Stadt")
    country_code = fields.Char(string="Land (ISO)")
    phone = fields.Char(string="Telefon")

    shopware_group_id = fields.Char(string="Shopware Kundengruppen-ID")
    shopware_group_name = fields.Char(string="Shopware Kundengruppe")
    guest = fields.Boolean(string="Gastkunde")

    sync_date = fields.Datetime(string="Letzte Synchronisation", readonly=True)

    _sql_constraints = [
        (
            'shopware_uniq',
            'unique(shopware_id, backend_id)',
            'Die Shopware-ID muss pro Backend eindeutig sein.',
        ),
    ]

    @api.depends('first_name', 'last_name', 'company')
    def _compute_name(self):
        for rec in self:
            parts = [p for p in [rec.first_name, rec.last_name] if p]
            name = ' '.join(parts) or rec.company or 'Unbekannt'
            if rec.company and parts:
                name = f"{' '.join(parts)} ({rec.company})"
            rec.name = name

    # -------------------------------------------------------------------------
    # Import from Shopware
    # -------------------------------------------------------------------------

    @api.model
    def _customer_search_associations(self):
        """Nested associations so country/salutation are hydrated (not only UUIDs)."""
        addr_nested = {
            'associations': {
                'country': {},
                'salutation': {},
            },
        }
        # Kein „addresses“ hier: würde jeden Kunden mit allen Adressen aufblähen und
        # Massenimporte/timeouts verursachen. Adressen werden pro Kunde per
        # customer-address-Suche nachgeladen (_sw_list_address_entities).
        return {
            'defaultShippingAddress': addr_nested,
            'defaultBillingAddress': addr_nested,
            'group': {},
            'salutation': {},
        }

    @api.model
    def import_customer_by_shopware_id(self, backend, shopware_customer_uuid):
        """Kunden per API laden und mit :meth:`_import_customer` verarbeiten — gleiche Logik wie beim Sync.

        Wird u. a. vom Bestellimport aufgerufen, damit Kontakte immer wie beim
        normalen Kundenimport (Adressen, Sprache, Preisliste, …) angelegt/aktualisiert werden.
        """
        cid = (shopware_customer_uuid or '').strip()
        if not cid:
            return self.env['shopware.customer']

        payload = {
            'limit': 5,
            'associations': self._customer_search_associations(),
            'filter': [
                {'type': 'equals', 'field': 'id', 'value': cid},
            ],
        }
        rows = backend._api_search('customer', payload, max_records=1)
        if not rows:
            _logger.warning(
                "Shopware-Kunde %s nicht per API gefunden — Bestellung ohne shopware.customer",
                cid,
            )
            return self.env['shopware.customer']
        return self._import_customer(backend, rows[0])

    @staticmethod
    def _sw_entity_attributes(entity):
        if not entity or not isinstance(entity, dict):
            return {}
        attrs = entity.get('attributes')
        if isinstance(attrs, dict):
            return attrs
        return entity

    @staticmethod
    def _sw_entity_id(entity):
        if not entity or not isinstance(entity, dict):
            return None
        return entity.get('id')

    @staticmethod
    def _sw_address_to_flat_dict(address_entity):
        """Normalize Shopware address entity (JSON:API or flat) to a field dict."""
        if not address_entity or not isinstance(address_entity, dict):
            return {}
        raw = address_entity.get('attributes', address_entity)
        if not isinstance(raw, dict):
            return {}
        out = dict(raw)
        country = out.get('country')
        if isinstance(country, dict):
            c = country.get('attributes', country)
            out['country'] = c if isinstance(c, dict) else {}
        sal = out.get('salutation')
        if isinstance(sal, dict):
            s = sal.get('attributes', sal)
            out['salutation'] = s if isinstance(s, dict) else {}
        return out

    def _sw_normalize_address_entity(self, address_entity):
        """Like _sw_address_to_flat_dict but always carries Shopware UUID when present."""
        flat = self._sw_address_to_flat_dict(address_entity)
        if not isinstance(flat, dict):
            return {}
        eid = self._sw_entity_id(address_entity)
        if eid and not flat.get('id'):
            flat = dict(flat)
            flat['id'] = eid
        return flat

    @staticmethod
    def _sw_salutation_display_name(salutation_obj):
        if not isinstance(salutation_obj, dict):
            return ''
        s = salutation_obj.get('attributes', salutation_obj)
        if not isinstance(s, dict):
            return ''
        tr = s.get('translated') or {}
        return (
            tr.get('displayName')
            or tr.get('letterName')
            or s.get('displayName')
            or s.get('salutationKey')
            or ''
        )

    def _sw_country_iso_from_address(self, address):
        if not address:
            return ''
        country = address.get('country')
        if isinstance(country, str):
            return ''
        if isinstance(country, dict):
            iso = country.get('iso')
            if iso:
                return iso.upper()[:2]
        return ''

    def _sw_pick_primary_address(self, attrs):
        """Prefer default billing, else shipping (same fields Shopware shows first)."""
        billing = self._sw_address_to_flat_dict(attrs.get('defaultBillingAddress'))
        shipping = self._sw_address_to_flat_dict(attrs.get('defaultShippingAddress'))

        def _has_content(a):
            return bool(
                a and (
                    a.get('street')
                    or a.get('city')
                    or a.get('zipcode')
                )
            )

        if _has_content(billing):
            return billing
        if _has_content(shipping):
            return shipping
        return billing or shipping

    def _sw_default_billing_shipping_ids(self, attrs):
        bid = self._sw_entity_id(attrs.get('defaultBillingAddress'))
        sid = self._sw_entity_id(attrs.get('defaultShippingAddress'))
        if not bid:
            bid = attrs.get('defaultBillingAddressId')
        if not sid:
            sid = attrs.get('defaultShippingAddressId')
        return bid, sid

    def _sw_list_address_entities(self, backend, customer_sw_id, attrs):
        """Alle Shopware-Adressen: Assoziation am Kunden oder separater Search."""
        raw = attrs.get('addresses')
        entities = []
        if isinstance(raw, list):
            entities = raw
        elif isinstance(raw, dict) and isinstance(raw.get('data'), list):
            entities = raw['data']
        if entities:
            return entities
        payload = {
            'limit': 500,
            'filter': [
                {'type': 'equals', 'field': 'customerId', 'value': customer_sw_id},
            ],
            'associations': {
                'country': {},
                'salutation': {},
            },
        }
        return backend._api_search('customer-address', payload)

    def _odoo_partner_type_for_sw_address(self, addr_id, billing_id, shipping_id):
        if not addr_id:
            return 'other'
        if billing_id and shipping_id and addr_id == billing_id == shipping_id:
            return 'invoice'
        if billing_id and addr_id == billing_id:
            return 'invoice'
        if shipping_id and addr_id == shipping_id:
            return 'delivery'
        return 'other'

    @api.model
    def _partner_lang_de_code(self):
        """Aktives Odoo-Deutsch (de_DE oder Fallback de_*)."""
        Lang = self.env['res.lang'].sudo()
        lang = Lang.search([('code', '=', ODOO_PARTNER_LANG_DE)], limit=1)
        if not lang:
            lang = Lang.search([('code', '=like', 'de_%')], limit=1)
        return lang.code if lang else False

    @api.model
    def _ref_shopware_address(self, shopware_address_uuid):
        """Eindeutige ref für importierte Adress-Kinder (vermeidet neue DB-Spalte)."""
        if not shopware_address_uuid:
            return ''
        return f'{SWADDR_REF_PREFIX}{shopware_address_uuid}'

    @api.model
    def _shopware_address_id_from_ref(self, ref):
        if not ref or not isinstance(ref, str):
            return None
        if not ref.startswith(SWADDR_REF_PREFIX):
            return None
        return ref[len(SWADDR_REF_PREFIX):] or None

    def _sync_shopware_address_contacts(self, backend, main_partner, customer_sw_id, attrs):
        """Unterkontakte für zusätzliche Shopware-Adressen (Rechnung/Lieferung/weitere).

        Gibt es nur **eine** Adresse (typisch: gleiche Standard-Rechnungs- und
        -Lieferadresse), reicht der Hauptkontakt — keine Unterkontakte, sonst Redundanz.

        Ab **zwei** unterschiedlichen Adress-IDs: wie bisher je Zeile ein Kind
        (ref ``swaddr:…``), Hauptadresse weiter über _sw_pick_primary_address.
        """
        self.ensure_one()
        if not main_partner:
            return
        Partner = self.env['res.partner'].sudo()
        lang_code = self._partner_lang_de_code()

        entities = self._sw_list_address_entities(backend, customer_sw_id, attrs)
        if not entities:
            return

        billing_id, shipping_id = self._sw_default_billing_shipping_ids(attrs)

        all_sw_ids = set()
        for entity in entities:
            flat = self._sw_normalize_address_entity(entity)
            aid = flat.get('id') or self._sw_entity_id(entity)
            if aid:
                all_sw_ids.add(aid)

        if len(all_sw_ids) <= 1:
            stale = Partner.search([
                ('parent_id', '=', main_partner.id),
                ('ref', '=like', f'{SWADDR_REF_PREFIX}%'),
            ])
            if stale:
                stale.unlink()
            return

        other_nr = 0
        for entity in entities:
            flat = self._sw_normalize_address_entity(entity)
            addr_id = flat.get('id') or self._sw_entity_id(entity)
            if not addr_id:
                continue

            ptype = self._odoo_partner_type_for_sw_address(addr_id, billing_id, shipping_id)
            if ptype == 'other':
                other_nr += 1
                label = _('Weitere Adresse') + (f' {other_nr}' if other_nr > 1 else '')
            elif ptype == 'invoice':
                label = _('Rechnung')
            else:
                label = _('Lieferung')

            country_code = self._sw_country_iso_from_address(flat)
            country = False
            if country_code:
                country = self.env['res.country'].search([
                    ('code', '=', country_code.upper()),
                ], limit=1)

            street2_parts = [
                p for p in [
                    flat.get('additionalAddressLine1'),
                    flat.get('additionalAddressLine2'),
                ] if p
            ]
            street2 = '\n'.join(street2_parts) if street2_parts else ''

            first = flat.get('firstName', '') or ''
            last = flat.get('lastName', '') or ''
            company = (flat.get('company') or '').strip()
            name_parts = [p for p in [first, last] if p]
            if company:
                cname = company
            elif name_parts:
                cname = ' '.join(name_parts)
            else:
                cname = main_partner.name

            addr_ref = self._ref_shopware_address(addr_id)
            child_vals = {
                'parent_id': main_partner.id,
                'type': ptype,
                'name': f'{cname} – {label}',
                'street': flat.get('street') or '',
                'street2': street2,
                'zip': flat.get('zipcode') or '',
                'city': flat.get('city') or '',
                'phone': flat.get('phoneNumber') or '',
                'country_id': country.id if country else False,
                'ref': addr_ref,
                'email': False,
                'is_company': bool(company),
            }
            if lang_code:
                child_vals['lang'] = lang_code

            existing = Partner.search([
                ('parent_id', '=', main_partner.id),
                ('ref', '=', addr_ref),
            ], limit=1)
            if existing:
                existing.write(child_vals)
            else:
                Partner.create(child_vals)

        orphans = Partner.search([
            ('parent_id', '=', main_partner.id),
            ('ref', '=like', f'{SWADDR_REF_PREFIX}%'),
        ])
        for child in orphans:
            cid = self._shopware_address_id_from_ref(child.ref)
            if not cid:
                continue
            if cid not in all_sw_ids:
                child.unlink()

    @api.model
    def _append_missing_customers_paginated_ids(
        self, backend, customer_associations, to_process, existing_ids, import_limit,
    ):
        """Fehlende Kunden: nur ID-Seiten (kleine Payloads), volle Datensätze per equalsAny.

        Vermeidet den früheren Ablauf „alle ~186k Kunden vollständig laden“, der Minuten
        dauerte und vor dem ersten Import nichts sichtbar machte.
        """
        already_fetched = {c.get('id') for c in to_process if c.get('id')}
        id_batch = max(1, min(50, int(backend.api_batch_size or 10)))
        page = 1
        page_size = 500
        prev_page_ids = None
        max_pages = 100000

        while True:
            if page > max_pages:
                _logger.error(
                    "Shopware Kunden-ID-Scan: Schutzgrenze %d Seiten erreicht",
                    max_pages,
                )
                break
            if import_limit and len(to_process) >= import_limit:
                break

            payload = {
                'limit': page_size,
                'page': page,
                'includes': {'customer': ['id']},
                'total-count-mode': 1,
            }
            result = backend._api_post('search/customer', payload)
            data = result.get('data', [])
            ids_page = [d.get('id') for d in data if d.get('id')]
            if not ids_page:
                break

            page_ids_tuple = tuple(ids_page)
            if prev_page_ids is not None and page_ids_tuple == prev_page_ids:
                _logger.warning(
                    "Shopware Kunden-ID-Scan: Seite %d identisch mit vorheriger — Abbruch",
                    page,
                )
                break
            prev_page_ids = page_ids_tuple

            total = result.get('total') or 0
            missing_on_page = [
                i for i in ids_page
                if i and i not in existing_ids and i not in already_fetched
            ]
            if missing_on_page:
                _logger.info(
                    "Shopware Kunden-ID-Scan Seite %d: %d Kunden, %d fehlen in Odoo "
                    "(gesamt laut API: %s, bisher Queue: %d)",
                    page, len(ids_page), len(missing_on_page), total, len(to_process),
                )
                for batch_start in range(0, len(missing_on_page), id_batch):
                    if import_limit and len(to_process) >= import_limit:
                        break
                    batch = missing_on_page[batch_start:batch_start + id_batch]
                    full = backend._api_search(
                        'customer',
                        {
                            'limit': 500,
                            'associations': customer_associations,
                            'filter': [
                                {'type': 'equalsAny', 'field': 'id', 'value': batch},
                            ],
                        },
                        max_records=len(batch),
                    )
                    to_process.extend(full)
                    already_fetched.update(batch)
            elif page == 1 or page % 50 == 0:
                _logger.info(
                    "Shopware Kunden-ID-Scan Seite %d: %d Kunden, keine fehlenden "
                    "(gesamt laut API: %s)",
                    page, len(ids_page), total,
                )

            if import_limit and len(to_process) >= import_limit:
                _logger.info(
                    "Kunden-Import: Import-Limit (%d) erreicht — ID-Scan beendet",
                    import_limit,
                )
                break
            if len(ids_page) < page_size:
                break
            page += 1

        return to_process

    @api.model
    def sync_from_shopware(self, backend):
        """Import customers from Shopware 6 with delta + missing sync."""
        _logger.info("Starte Kunden-Import von Shopware Backend %s", backend.name)

        customer_associations = self._customer_search_associations()
        to_process = []
        import_limit = int(backend.import_customer_limit or 0)

        test_id = (backend.import_customer_test_id or '').strip()
        if test_id:
            payload = {
                'limit': 5,
                'associations': customer_associations,
                'filter': [
                    {'type': 'equals', 'field': 'id', 'value': test_id},
                ],
            }
            to_process = backend._api_search('customer', payload, max_records=1)
            if not to_process:
                raise UserError(_(
                    "Kein Kunde mit dieser Shopware-ID gefunden: %s"
                ) % test_id)
            _logger.info(
                "Kunden-Testimport: nur UUID %s (%d Datensatz)",
                test_id, len(to_process),
            )
        else:
            if backend.last_customer_sync:
                _logger.info(
                    "Delta-Sync: Änderungen seit %s", backend.last_customer_sync)
                delta_payload = {
                    'limit': 500,
                    'associations': customer_associations,
                    'filter': [{
                        'type': 'range',
                        'field': 'updatedAt',
                        'parameters': {'gte': backend.last_customer_sync.isoformat()},
                    }],
                }
                to_process = backend._api_search('customer', delta_payload)
                _logger.info("Delta-Sync: %d geänderte Kunden", len(to_process))

            if import_limit and len(to_process) > import_limit:
                before = len(to_process)
                to_process = to_process[:import_limit]
                _logger.info(
                    "Kunden-Import: Delta auf %d von %d begrenzt (Import-Limit)",
                    import_limit, before,
                )

            existing_ids = {
                x for x in self.search([
                    ('backend_id', '=', backend.id),
                ]).mapped('shopware_id') if x
            }

            if not import_limit or len(to_process) < import_limit:
                self._append_missing_customers_paginated_ids(
                    backend,
                    customer_associations,
                    to_process,
                    existing_ids,
                    import_limit,
                )

            if import_limit and len(to_process) > import_limit:
                before = len(to_process)
                to_process = to_process[:import_limit]
                _logger.info(
                    "Kunden-Import: Warteschlange von %d auf %d begrenzt",
                    before, import_limit,
                )

        _logger.info("Gesamt: %d Kunden zum Verarbeiten", len(to_process))
        count = 0
        for idx, sw_cust in enumerate(to_process, start=1):
            try:
                self._import_customer(backend, sw_cust)
                count += 1
                # Kurze Transaktionen: verhindert lange Locks und „Hänger“ bei vielen Kunden
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    "Fehler beim Import von Shopware-Kunde %s (%d/%d)",
                    sw_cust.get('id', '?'), idx, len(to_process),
                )
        _logger.info("Kunden-Import abgeschlossen: %d verarbeitet", count)
        return count

    def _import_customer(self, backend, sw_data):
        sw_id = sw_data.get('id')
        attrs = self._sw_entity_attributes(sw_data)

        first_name = attrs.get('firstName', '')
        last_name = attrs.get('lastName', '')
        email = attrs.get('email', '')
        customer_number = attrs.get('customerNumber', '')
        company = attrs.get('company') or ''
        title = (attrs.get('title') or '').strip()
        guest = attrs.get('guest', False)
        birthday = attrs.get('birthday')
        if birthday and isinstance(birthday, str):
            birthday = birthday[:10]

        address = self._sw_pick_primary_address(attrs)
        street = address.get('street', '') or ''
        zipcode = address.get('zipcode', '') or ''
        city = address.get('city', '') or ''
        phone = address.get('phoneNumber') or ''
        if not title:
            title = self._sw_salutation_display_name(attrs.get('salutation'))
        if not title:
            title = self._sw_salutation_display_name(address.get('salutation'))
        country_code = self._sw_country_iso_from_address(address)

        group = attrs.get('group') or {}
        g_attrs = group.get('attributes', group)
        group_id = group.get('id', '')
        group_name = ''
        if g_attrs:
            g_translated = g_attrs.get('translated', {})
            group_name = g_translated.get('name') or g_attrs.get('name') or ''

        existing = self.search([
            ('shopware_id', '=', sw_id),
            ('backend_id', '=', backend.id),
        ], limit=1)

        vals = {
            'shopware_id': sw_id,
            'backend_id': backend.id,
            'first_name': first_name,
            'last_name': last_name,
            'email': email,
            'customer_number': customer_number,
            'company': company,
            'title': title,
            'guest': guest,
            'birthday': birthday if birthday else False,
            'street': street,
            'zipcode': zipcode,
            'city': city,
            'country_code': country_code,
            'phone': phone,
            'shopware_group_id': group_id,
            'shopware_group_name': group_name,
            'sync_date': fields.Datetime.now(),
        }

        if existing:
            existing.write(vals)
            self._update_odoo_partner(existing)
            existing._sync_shopware_address_contacts(
                backend, existing.odoo_partner_id, sw_id, attrs,
            )
            self._assign_pricelist(existing, backend)
            return existing

        partner = self._find_or_create_partner(vals)
        vals['odoo_partner_id'] = partner.id
        try:
            with self.env.cr.savepoint():
                record = self.create(vals)
            self._update_odoo_partner(record)
            record._sync_shopware_address_contacts(
                backend, record.odoo_partner_id, sw_id, attrs,
            )
            self._assign_pricelist(record, backend)
            return record
        except Exception:
            existing = self.search([
                ('shopware_id', '=', sw_id),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if existing:
                existing.write(vals)
                self._update_odoo_partner(existing)
                existing._sync_shopware_address_contacts(
                    backend, existing.odoo_partner_id, sw_id, attrs,
                )
                return existing
            raise

    def _find_or_create_partner(self, vals):
        partner = None
        if vals.get('email'):
            partner = self.env['res.partner'].search([
                ('email', '=', vals['email']),
            ], limit=1)
        if not partner and vals.get('customer_number'):
            partner = self.env['res.partner'].search([
                ('ref', '=', vals['customer_number']),
            ], limit=1)

        if partner:
            return partner

        name_parts = [p for p in [vals.get('first_name'), vals.get('last_name')] if p]
        name = ' '.join(name_parts) or vals.get('company') or vals.get('email') or 'Shopware Kunde'

        country = False
        if vals.get('country_code'):
            country = self.env['res.country'].search([
                ('code', '=', vals['country_code'].upper()),
            ], limit=1)

        create_vals = {
            'name': name,
            'email': vals.get('email') or '',
            'ref': vals.get('customer_number') or '',
            'phone': vals.get('phone') or '',
            'street': vals.get('street') or '',
            'zip': vals.get('zipcode') or '',
            'city': vals.get('city') or '',
            'is_company': bool(vals.get('company')),
        }
        if country:
            create_vals['country_id'] = country.id
        lang_code = self._partner_lang_de_code()
        if lang_code:
            create_vals['lang'] = lang_code
        return self.env['res.partner'].create(create_vals)

    def _update_odoo_partner(self, sw_customer):
        if not sw_customer.odoo_partner_id:
            return
        partner = sw_customer.odoo_partner_id
        update = {}
        if sw_customer.email and partner.email != sw_customer.email:
            update['email'] = sw_customer.email
        if sw_customer.phone and partner.phone != sw_customer.phone:
            update['phone'] = sw_customer.phone
        if sw_customer.street and partner.street != sw_customer.street:
            update['street'] = sw_customer.street
        if sw_customer.zipcode and partner.zip != sw_customer.zipcode:
            update['zip'] = sw_customer.zipcode
        if sw_customer.city and partner.city != sw_customer.city:
            update['city'] = sw_customer.city
        if sw_customer.country_code:
            country = self.env['res.country'].search([
                ('code', '=', sw_customer.country_code.upper()),
            ], limit=1)
            if country and partner.country_id != country:
                update['country_id'] = country.id
        lang_code = self._partner_lang_de_code()
        if lang_code and partner.lang != lang_code:
            update['lang'] = lang_code
        if update:
            partner.write(update)

    def _assign_pricelist(self, sw_customer, backend):
        """Assign Odoo pricelist based on Shopware customer group rules."""
        if not sw_customer.odoo_partner_id or not sw_customer.shopware_group_name:
            return
        group_name = sw_customer.shopware_group_name
        pl_name = f"Shopware: {group_name}"
        pricelist = self.env['product.pricelist'].search([
            ('name', '=', pl_name),
        ], limit=1)
        if not pricelist:
            pricelist = self.env['product.pricelist'].create({
                'name': pl_name,
            })
            _logger.info("Preisliste '%s' für Kundengruppe erstellt", pl_name)
        if sw_customer.odoo_partner_id.property_product_pricelist != pricelist:
            sw_customer.odoo_partner_id.write({
                'property_product_pricelist': pricelist.id,
            })
