# Proprietary module. See LICENSE file for full copyright and licensing details.

import base64
import logging
import re

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopwareProduct(models.Model):
    _name = 'shopware.product'
    _description = 'Shopware Produkt'
    _rec_name = 'display_name'
    _order = 'name'
    _sql_constraints = [
        ('unique_shopware_backend', 'UNIQUE(shopware_id, backend_id)',
         'Dieses Shopware-Produkt existiert bereits für dieses Backend.'),
    ]

    name = fields.Char(string="Name", required=True, index=True)
    display_name = fields.Char(
        string="Anzeigename", compute='_compute_display_name', store=True,
    )
    shopware_id = fields.Char(string="Shopware ID", index=True, readonly=True)
    backend_id = fields.Many2one(
        'shopware.backend', string="Backend", required=True, ondelete='cascade', index=True,
    )
    odoo_product_id = fields.Many2one('product.product', string="Odoo Produkt")
    odoo_template_id = fields.Many2one(
        'product.template', string="Odoo Produktvorlage",
        related='odoo_product_id.product_tmpl_id', store=True,
    )
    active = fields.Boolean(default=True)

    # Variant fields
    shopware_parent_id = fields.Char(string="Shopware Parent ID", index=True, readonly=True)
    parent_bind_id = fields.Many2one(
        'shopware.product', string="Hauptprodukt",
        domain="[('backend_id', '=', backend_id)]",
        readonly=True,
    )
    variant_bind_ids = fields.One2many(
        'shopware.product', 'parent_bind_id', string="Varianten",
    )
    is_variant = fields.Boolean(string="Ist Variante", readonly=True)
    variant_option_names = fields.Char(
        string="Varianten-Optionen", readonly=True,
        help="Z.B. 'Farbe: Rot, Größe: XL'",
    )
    variant_count = fields.Integer(
        string="Anzahl Varianten", compute='_compute_variant_count',
    )

    shopware_product_number = fields.Char(string="Artikelnummer", index=True)
    ean = fields.Char(string="EAN / Barcode")
    description = fields.Html(string="Beschreibung")
    price = fields.Float(string="Preis (Brutto)", digits='Product Price')
    net_price = fields.Float(string="Preis (Netto)", digits='Product Price')
    stock = fields.Integer(string="Lagerbestand")
    shopware_category_ids = fields.Many2many(
        'shopware.category',
        'shopware_product_category_rel',
        'product_id',
        'category_id',
        string="Shopware Kategorien",
    )
    manufacturer = fields.Char(string="Hersteller")
    weight = fields.Float(string="Gewicht")
    width = fields.Float(string="Breite")
    height = fields.Float(string="Höhe")
    length = fields.Float(string="Länge")
    tax_rate = fields.Float(string="Steuersatz (%)")

    sync_date = fields.Datetime(string="Letzte Synchronisation", readonly=True)

    _sql_constraints = [
        (
            'shopware_uniq',
            'unique(shopware_id, backend_id)',
            'Die Shopware-ID muss pro Backend eindeutig sein.',
        ),
    ]

    @api.depends('name', 'variant_option_names', 'is_variant')
    def _compute_display_name(self):
        for rec in self:
            if rec.is_variant and rec.variant_option_names:
                rec.display_name = f"{rec.name} [{rec.variant_option_names}]"
            else:
                rec.display_name = rec.name or ''

    @api.depends('variant_bind_ids')
    def _compute_variant_count(self):
        for rec in self:
            rec.variant_count = len(rec.variant_bind_ids)

    # -------------------------------------------------------------------------
    # Import from Shopware
    # -------------------------------------------------------------------------

    @api.model
    def sync_from_shopware(self, backend):
        """Import products from Shopware 6 with full variant support.

        Two-phase approach:
        1. Delta sync: fetch only products updated since last sync
        2. Missing sync: fetch all IDs from Shopware, compare with local,
           and import any that are missing locally
        Products are fetched in small batches to avoid 502 gateway timeouts.
        """
        _logger.info("Starte Produkt-Import von Shopware Backend %s", backend.name)
        batch_size = backend.api_batch_size or 10
        base_filter = [{'type': 'equals', 'field': 'parentId', 'value': None}]
        count = 0

        # Phase 1: Delta sync (updated products)
        if backend.last_product_sync:
            _logger.info("Phase 1 - Delta-Sync: Änderungen seit %s", backend.last_product_sync)
            delta_ids_payload = {
                'limit': 500,
                'includes': {'product': ['id']},
                'filter': base_filter + [{
                    'type': 'range',
                    'field': 'updatedAt',
                    'parameters': {'gte': backend.last_product_sync.isoformat()},
                }],
            }
            delta_results = backend._api_search('product', delta_ids_payload)
            delta_ids = [p.get('id') for p in delta_results if p.get('id')]
            _logger.info("Delta-Sync: %d geänderte Hauptprodukte", len(delta_ids))
            count += self._fetch_and_process_by_ids(backend, delta_ids, batch_size)

        # Phase 2: Find and import missing products
        _logger.info("Phase 2 - Fehlende Produkte ermitteln")
        all_ids_payload = {
            'limit': 500,
            'includes': {'product': ['id']},
            'filter': base_filter,
        }
        all_sw_products = backend._api_search('product', all_ids_payload)
        all_sw_ids = {p.get('id') for p in all_sw_products if p.get('id')}

        existing_sw_ids = set(
            self.search([
                ('backend_id', '=', backend.id),
                ('is_variant', 'in', [False, None]),
                ('shopware_parent_id', '=', False),
                ('odoo_product_id', '!=', False),
            ]).mapped('shopware_id')
        )
        missing_ids = list(all_sw_ids - existing_sw_ids)

        if missing_ids:
            _logger.info("Fehlend: %d Produkte werden nachgeladen", len(missing_ids))
            count += self._fetch_and_process_by_ids(backend, missing_ids, batch_size)
        else:
            _logger.info("Keine fehlenden Produkte")

        self._backfill_default_codes(backend)
        _logger.info("Produkt-Import abgeschlossen: %d Produkte verarbeitet", count)
        return count

    def _backfill_default_codes(self, backend):
        """Sync shopware_product_number to Odoo product when default_code is empty."""
        to_fix = self.search([
            ('backend_id', '=', backend.id),
            ('odoo_product_id', '!=', False),
            ('shopware_product_number', '!=', False),
        ])
        for sp in to_fix:
            if not sp.odoo_product_id.default_code and sp.shopware_product_number:
                sp.odoo_product_id.write({'default_code': sp.shopware_product_number})

    def _get_full_associations(self):
        """Return the full associations payload for detailed product fetching."""
        return {
            'categories': {},
            'manufacturer': {},
            'cover': {
                'associations': {'media': {}},
            },
            'options': {
                'associations': {'group': {}},
            },
            'prices': {
                'associations': {'rule': {}},
            },
            'configuratorSettings': {
                'associations': {
                    'option': {
                        'associations': {'group': {}},
                    },
                },
            },
            'children': {
                'limit': 100,
                'associations': {
                    'categories': {},
                    'cover': {
                        'associations': {'media': {}},
                    },
                    'options': {
                        'associations': {'group': {}},
                    },
                    'prices': {
                        'associations': {'rule': {}},
                    },
                },
            },
        }

    @staticmethod
    def _uuid_variants_for_shopware_search(uid):
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

    @api.model
    def import_product_by_shopware_id(self, backend, shopware_product_uuid):
        """Produkt per API laden und wie beim Produkt-Sync importieren.

        Nutzt den gleichen Pfad wie der Massenimport (inkl. Varianten über
        :meth:`_import_parent_with_variants`). Wird von Bestellpositionen
        aufgerufen, wenn lokal noch kein ``shopware.product`` existiert.
        """
        self = self.env['shopware.product']
        raw = (shopware_product_uuid or '').strip()
        if not raw:
            return self.browse()
        for vid in self._uuid_variants_for_shopware_search(raw):
            found = self.search([
                ('shopware_id', '=', vid),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if found:
                return found

        sw_data = None
        for vid in self._uuid_variants_for_shopware_search(raw):
            payload = {
                'limit': 1,
                'associations': self._get_full_associations(),
                'filter': [{'type': 'equals', 'field': 'id', 'value': vid}],
            }
            rows = backend._api_search('product', payload, max_records=1)
            if rows:
                sw_data = rows[0]
                break
        if not sw_data:
            _logger.info(
                "Shopware-Produkt %s: API liefert keinen Datensatz — keine Zuordnung",
                raw,
            )
            return self.browse()

        attrs = sw_data.get('attributes', sw_data)
        parent_id = attrs.get('parentId') if isinstance(attrs, dict) else None
        if parent_id:
            self.import_product_by_shopware_id(backend, parent_id)
        else:
            self._import_parent_with_variants(backend, sw_data)

        for vid in self._uuid_variants_for_shopware_search(raw):
            found = self.search([
                ('shopware_id', '=', vid),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if found:
                return found
        _logger.warning(
            "Shopware-Produkt %s: nach Import weiterhin keine lokale shopware.product-Zeile",
            raw,
        )
        return self.browse()

    @api.model
    def import_product_by_product_number(self, backend, product_number):
        """Produkt per Artikelnummer suchen, ggf. API, dann Import per ID."""
        self = self.env['shopware.product']
        pn = (product_number or '').strip()
        if not pn:
            return self.browse()
        found = self.search([
            ('shopware_product_number', '=', pn),
            ('backend_id', '=', backend.id),
        ], limit=1)
        if found:
            return found
        payload = {
            'limit': 1,
            'associations': self._get_full_associations(),
            'filter': [{'type': 'equals', 'field': 'productNumber', 'value': pn}],
        }
        rows = backend._api_search('product', payload, max_records=1)
        if not rows:
            return self.browse()
        sw_id = rows[0].get('id')
        if not sw_id:
            return self.browse()
        return self.import_product_by_shopware_id(backend, sw_id)

    def _fetch_and_process_by_ids(self, backend, product_ids, batch_size):
        """Fetch full product data in small batches and process them."""
        count = 0
        total = len(product_ids)
        for batch_start in range(0, total, batch_size):
            batch = product_ids[batch_start:batch_start + batch_size]
            batch_num = (batch_start // batch_size) + 1
            total_batches = (total + batch_size - 1) // batch_size
            _logger.info(
                "Batch %d/%d: Lade %d Produkte...",
                batch_num, total_batches, len(batch),
            )
            payload = {
                'limit': batch_size,
                'associations': self._get_full_associations(),
                'filter': [{
                    'type': 'equalsAny',
                    'field': 'id',
                    'value': batch,
                }],
            }
            try:
                products = backend._api_search('product', payload)
                count += self._process_products(backend, products)
                backend.write({'last_product_sync': fields.Datetime.now()})
                self.env.cr.commit()
            except Exception:
                _logger.exception("Fehler bei Batch %d/%d", batch_num, total_batches)
                self.env.cr.rollback()
        return count

    def _process_products(self, backend, sw_products):
        """Process a list of Shopware parent products."""
        count = 0
        for sw_prod in sw_products:
            try:
                self._import_parent_with_variants(backend, sw_prod)
                count += 1
                attrs = sw_prod.get('attributes', sw_prod)
                count += len(attrs.get('children') or [])
            except Exception:
                _logger.exception(
                    "Fehler beim Import von Shopware-Produkt %s",
                    sw_prod.get('id', '?'),
                )
        return count

    def _import_parent_with_variants(self, backend, sw_data):
        """Import a parent product and all its variants."""
        parent_record = self._import_single_product(backend, sw_data, is_variant=False)

        attrs = sw_data.get('attributes', sw_data)
        children = attrs.get('children') or []
        configurator = attrs.get('configuratorSettings') or []

        if children:
            attribute_map = self._build_attribute_map(configurator)
            odoo_template = parent_record.odoo_product_id.product_tmpl_id if parent_record.odoo_product_id else None

            if odoo_template and attribute_map:
                self._ensure_odoo_attributes(odoo_template, attribute_map, children)
                # Odoo's variant system may delete the original product.product
                # when attribute lines are added, so re-link the parent
                if not parent_record.odoo_product_id:
                    first_variant = odoo_template.product_variant_ids[:1]
                    if first_variant:
                        parent_record.write({'odoo_product_id': first_variant.id})

            for child in children:
                child_attrs = child.get('attributes', child)
                variant_record = self._import_single_product(
                    backend, child, is_variant=True, parent_sw_id=sw_data.get('id'),
                )
                variant_record.write({'parent_bind_id': parent_record.id})

                if odoo_template:
                    self._link_variant_to_odoo(variant_record, odoo_template, child_attrs)

                if variant_record.odoo_product_id:
                    self._sync_advanced_prices(backend, child_attrs, variant_record.odoo_product_id)

        if parent_record.odoo_product_id:
            self._sync_advanced_prices(backend, attrs, parent_record.odoo_product_id)

        return parent_record

    def _import_single_product(self, backend, sw_data, is_variant=False, parent_sw_id=False):
        """Import a single product record (parent or variant)."""
        sw_id = sw_data.get('id')
        attrs = sw_data.get('attributes', sw_data)
        translated = attrs.get('translated', {})

        name = translated.get('name') or attrs.get('name') or 'Unbenannt'
        product_number = attrs.get('productNumber', '')
        ean = attrs.get('ean') or ''
        description = translated.get('description') or attrs.get('description') or ''
        stock = attrs.get('stock', 0)
        weight = attrs.get('weight') or 0.0
        width = attrs.get('width') or 0.0
        height = attrs.get('height') or 0.0
        length = attrs.get('length') or 0.0

        price = 0.0
        net_price = 0.0
        tax_rate = 0.0
        prices = attrs.get('price') or []
        if prices and isinstance(prices, list):
            price_entry = prices[0]
            price = price_entry.get('gross', 0.0)
            net_price = price_entry.get('net', 0.0)
        tax = attrs.get('tax')
        if tax:
            tax_rate = tax.get('taxRate', 0.0)

        manufacturer_name = ''
        manufacturer_data = attrs.get('manufacturer')
        if manufacturer_data:
            m_translated = manufacturer_data.get('translated', {})
            manufacturer_name = m_translated.get('name') or manufacturer_data.get('name') or ''

        option_names = self._extract_option_names(attrs)

        category_sw_ids = []
        for cat in (attrs.get('categories') or []):
            if cat.get('id'):
                category_sw_ids.append(cat['id'])

        existing = self.search([
            ('shopware_id', '=', sw_id),
            ('backend_id', '=', backend.id),
        ], limit=1)

        sw_cat_records = self.env['shopware.category'].search([
            ('shopware_id', 'in', category_sw_ids),
            ('backend_id', '=', backend.id),
        ])

        vals = {
            'name': name,
            'shopware_id': sw_id,
            'backend_id': backend.id,
            'shopware_product_number': product_number,
            'ean': ean,
            'description': description,
            'price': price,
            'net_price': net_price,
            'stock': stock,
            'weight': weight,
            'width': width,
            'height': height,
            'length': length,
            'tax_rate': tax_rate,
            'manufacturer': manufacturer_name,
            'shopware_category_ids': [(6, 0, sw_cat_records.ids)],
            'active': attrs.get('active', True),
            'sync_date': fields.Datetime.now(),
            'is_variant': is_variant,
            'shopware_parent_id': parent_sw_id or attrs.get('parentId') or False,
            'variant_option_names': option_names,
        }

        if existing:
            existing.write(vals)
            if not is_variant and not existing.odoo_product_id:
                odoo_product = self._find_or_create_odoo_product(vals, backend)
                existing.write({'odoo_product_id': odoo_product.id})
            self._update_odoo_product(existing, attrs)
            return existing

        try:
            with self.env.cr.savepoint():
                record = self.create(vals)
        except Exception:
            existing = self.search([
                ('shopware_id', '=', sw_id),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if existing:
                existing.write(vals)
                if not is_variant and not existing.odoo_product_id:
                    odoo_product = self._find_or_create_odoo_product(vals, backend)
                    existing.write({'odoo_product_id': odoo_product.id})
                return existing
            raise

        if not is_variant:
            odoo_product = self._find_or_create_odoo_product(vals, backend)
            record.write({'odoo_product_id': odoo_product.id})
            self._sync_image(record, backend, attrs)
        return record

    def _extract_option_names(self, attrs):
        """Build a readable string from variant options, e.g. 'Farbe: Rot, Größe: XL'."""
        options = attrs.get('options') or []
        parts = []
        for opt in options:
            opt_attrs = opt.get('attributes', opt)
            opt_translated = opt_attrs.get('translated', {})
            opt_name = opt_translated.get('name') or opt_attrs.get('name') or ''
            group = opt_attrs.get('group') or {}
            group_translated = group.get('translated', {})
            group_name = group_translated.get('name') or group.get('name') or ''
            if group_name and opt_name:
                parts.append(f"{group_name}: {opt_name}")
            elif opt_name:
                parts.append(opt_name)
        return ', '.join(parts)

    def _build_attribute_map(self, configurator_settings):
        """Build a map of {group_id: {group_name, options: [{id, name}]}} from configuratorSettings."""
        groups = {}
        for setting in configurator_settings:
            s_attrs = setting.get('attributes', setting)
            option = s_attrs.get('option') or {}
            o_attrs = option.get('attributes', option)
            o_translated = o_attrs.get('translated', {})
            option_name = o_translated.get('name') or o_attrs.get('name') or ''
            option_id = option.get('id') or o_attrs.get('id')

            group = o_attrs.get('group') or {}
            g_attrs = group.get('attributes', group)
            g_translated = g_attrs.get('translated', {})
            group_name = g_translated.get('name') or g_attrs.get('name') or ''
            group_id = group.get('id') or g_attrs.get('id')

            if not group_id or not option_id:
                continue
            if group_id not in groups:
                groups[group_id] = {'name': group_name, 'options': []}
            groups[group_id]['options'].append({
                'id': option_id,
                'name': option_name,
            })
        return groups

    def _ensure_odoo_attributes(self, template, attribute_map, children):
        """Create Odoo product attributes and attribute lines on the template."""
        ProductAttribute = self.env['product.attribute']
        ProductAttributeValue = self.env['product.attribute.value']

        for group_id, group_data in attribute_map.items():
            attribute = ProductAttribute.search([
                ('name', '=', group_data['name']),
            ], limit=1)
            if not attribute:
                attribute = ProductAttribute.create({
                    'name': group_data['name'],
                    'create_variant': 'always',
                })

            value_ids = []
            for opt in group_data['options']:
                value = ProductAttributeValue.search([
                    ('name', '=', opt['name']),
                    ('attribute_id', '=', attribute.id),
                ], limit=1)
                if not value:
                    value = ProductAttributeValue.create({
                        'name': opt['name'],
                        'attribute_id': attribute.id,
                    })
                value_ids.append(value.id)

            existing_line = template.attribute_line_ids.filtered(
                lambda l: l.attribute_id.id == attribute.id
            )
            if existing_line:
                current_value_ids = set(existing_line.value_ids.ids)
                new_value_ids = set(value_ids)
                if not new_value_ids.issubset(current_value_ids):
                    existing_line.write({
                        'value_ids': [(4, vid) for vid in new_value_ids - current_value_ids],
                    })
            else:
                self.env['product.template.attribute.line'].create({
                    'product_tmpl_id': template.id,
                    'attribute_id': attribute.id,
                    'value_ids': [(6, 0, value_ids)],
                })

    def _link_variant_to_odoo(self, variant_record, template, child_attrs):
        """Try to match a Shopware variant to an Odoo product.product variant."""
        if variant_record.odoo_product_id and variant_record.odoo_product_id.product_tmpl_id == template:
            return

        already_linked = set(
            self.search([
                ('backend_id', '=', variant_record.backend_id.id),
                ('odoo_product_id', '!=', False),
                ('parent_bind_id', '=', variant_record.parent_bind_id.id),
            ]).mapped('odoo_product_id.id')
        )

        options = child_attrs.get('options') or []
        option_names = set()
        for opt in options:
            o_attrs = opt.get('attributes', opt)
            o_translated = o_attrs.get('translated', {})
            option_names.add(o_translated.get('name') or o_attrs.get('name') or '')

        # Match by attribute value names
        for variant in template.product_variant_ids:
            if variant.id in already_linked:
                continue
            variant_value_names = set(
                variant.product_template_attribute_value_ids.mapped(
                    'product_attribute_value_id.name'
                )
            )
            if option_names and option_names == variant_value_names:
                variant_record.write({'odoo_product_id': variant.id})
                update = {}
                if variant_record.shopware_product_number:
                    update['default_code'] = variant_record.shopware_product_number
                if variant_record.ean:
                    update['barcode'] = variant_record.ean
                if update:
                    variant.write(update)
                return

        # Fallback: if template has only one variant (no attributes), link to it
        unlinked = template.product_variant_ids.filtered(lambda v: v.id not in already_linked)
        if len(unlinked) == 1 and not option_names:
            variant_record.write({'odoo_product_id': unlinked.id})
            return

        _logger.info(
            "Keine passende Odoo-Variante für %s (%s) - Template hat %d Varianten, %d bereits verknüpft",
            variant_record.shopware_product_number, variant_record.variant_option_names,
            len(template.product_variant_ids), len(already_linked),
        )

    def _find_or_create_odoo_product(self, vals, backend):
        """Find or create the corresponding Odoo product.product."""
        product = None
        if vals.get('shopware_product_number'):
            product = self.env['product.product'].search([
                ('default_code', '=', vals['shopware_product_number']),
            ], limit=1)
        if not product and vals.get('ean'):
            product = self.env['product.product'].search([
                ('barcode', '=', vals['ean']),
            ], limit=1)

        if product:
            return product

        categ_id = False
        sw_cats = self.env['shopware.category'].browse(
            vals.get('shopware_category_ids', [(6, 0, [])])[0][2]
            if vals.get('shopware_category_ids') else []
        )
        if sw_cats:
            categ_id = sw_cats[0].odoo_category_id.id

        create_vals = {
            'name': vals['name'],
            'default_code': vals.get('shopware_product_number') or '',
            'barcode': vals.get('ean') or False,
            'list_price': vals.get('net_price', 0.0),
            'weight': vals.get('weight', 0.0),
            'type': 'consu',
        }
        if categ_id:
            create_vals['categ_id'] = categ_id
        return self.env['product.product'].create(create_vals)

    def _update_odoo_product(self, sw_product, attrs):
        """Update the linked Odoo product with fresh data."""
        if not sw_product.odoo_product_id:
            return
        odoo_prod = sw_product.odoo_product_id
        update = {}
        if sw_product.net_price and odoo_prod.list_price != sw_product.net_price:
            update['list_price'] = sw_product.net_price
        if sw_product.weight and odoo_prod.weight != sw_product.weight:
            update['weight'] = sw_product.weight
        if sw_product.ean and odoo_prod.barcode != sw_product.ean:
            update['barcode'] = sw_product.ean
        if sw_product.shopware_product_number and (
            not odoo_prod.default_code or odoo_prod.default_code != sw_product.shopware_product_number
        ):
            update['default_code'] = sw_product.shopware_product_number
        if update:
            odoo_prod.write(update)

    def _sync_image(self, sw_product, backend, attrs):
        """Download product cover image from Shopware and assign to Odoo product."""
        cover = attrs.get('cover')
        if not cover or not sw_product.odoo_product_id:
            return
        media = cover.get('media')
        if not media:
            return
        image_url = media.get('url')
        if not image_url:
            return
        try:
            resp = requests.get(image_url, timeout=15)
            resp.raise_for_status()
            image_data = base64.b64encode(resp.content)
            sw_product.odoo_product_id.write({'image_1920': image_data})
        except Exception:
            _logger.warning("Bild-Download fehlgeschlagen für Produkt %s", sw_product.name)

    # -------------------------------------------------------------------------
    # Export to Shopware
    # -------------------------------------------------------------------------

    @api.model
    def export_to_shopware(self, backend):
        """Export products linked to this backend to Shopware 6."""
        _logger.info("Starte Produkt-Export zu Shopware Backend %s", backend.name)
        parents = self.search([
            ('backend_id', '=', backend.id),
            ('is_variant', '=', False),
        ])
        for product in parents:
            try:
                product._export_single(backend)
                for variant in product.variant_bind_ids:
                    variant._export_single(backend)
            except Exception:
                _logger.exception("Fehler beim Export von Produkt %s", product.name)
        _logger.info("Produkt-Export abgeschlossen")

    def _export_single(self, backend):
        self.ensure_one()
        payload = {
            'name': self.name,
            'productNumber': self.shopware_product_number or self.odoo_product_id.default_code or '',
            'stock': self.stock,
            'price': [
                {
                    'currencyId': self._get_default_currency_id(backend),
                    'gross': self.price,
                    'net': self.net_price,
                    'linked': True,
                },
            ],
        }
        if self.weight:
            payload['weight'] = self.weight
        if self.ean:
            payload['ean'] = self.ean
        if self.is_variant and self.shopware_parent_id:
            payload['parentId'] = self.shopware_parent_id

        category_ids = [
            {'id': cat.shopware_id}
            for cat in self.shopware_category_ids if cat.shopware_id
        ]
        if category_ids:
            payload['categories'] = category_ids

        if self.shopware_id:
            backend._api_patch(f'product/{self.shopware_id}', payload)
            self.write({'sync_date': fields.Datetime.now()})
            _logger.info("Produkt %s in Shopware aktualisiert", self.name)
        else:
            tax_id = self._get_or_create_tax_id(backend)
            if tax_id:
                payload['taxId'] = tax_id
            result = backend._api_post('product', payload)
            if result:
                sw_id = ''
                if isinstance(result, dict):
                    sw_id = result.get('data', {}).get('id', '')
                self.write({
                    'shopware_id': sw_id,
                    'sync_date': fields.Datetime.now(),
                })
            _logger.info("Produkt %s in Shopware erstellt", self.name)

    def _get_default_currency_id(self, backend):
        try:
            result = backend._api_post('search/currency', {
                'filter': [{'type': 'equals', 'field': 'isoCode', 'value': 'EUR'}],
                'limit': 1,
            })
            data = result.get('data', [])
            if data:
                return data[0].get('id')
        except Exception:
            _logger.warning("Konnte Standard-Währung nicht ermitteln")
        return None

    def _get_or_create_tax_id(self, backend):
        if not self.tax_rate:
            return None
        try:
            result = backend._api_post('search/tax', {
                'filter': [
                    {'type': 'equals', 'field': 'taxRate', 'value': self.tax_rate},
                ],
                'limit': 1,
            })
            data = result.get('data', [])
            if data:
                return data[0].get('id')
        except Exception:
            _logger.warning("Konnte Shopware Steuer-ID nicht ermitteln")
        return None

    def action_export_to_shopware(self):
        for record in self:
            record._export_single(record.backend_id)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Export"),
                'message': _("Produkte wurden zu Shopware exportiert."),
                'type': 'success',
                'sticky': False,
            },
        }

    # -------------------------------------------------------------------------
    # Advanced Pricing (Staffelpreise / Kundengruppen-Preise)
    # -------------------------------------------------------------------------

    def _sync_advanced_prices(self, backend, sw_attrs, odoo_product):
        """Sync Shopware product.prices → Odoo pricelist items."""
        sw_prices = sw_attrs.get('prices') or []
        if not sw_prices:
            return

        PriceRule = self.env['shopware.price.rule']
        PricelistItem = self.env['product.pricelist.item']

        is_variant = bool(odoo_product.product_tmpl_id.product_variant_count > 1)

        for sw_price in sw_prices:
            p_attrs = sw_price.get('attributes', sw_price)
            rule_id = p_attrs.get('ruleId')
            if not rule_id:
                continue

            rule_record = PriceRule.search([
                ('shopware_id', '=', rule_id),
                ('backend_id', '=', backend.id),
            ], limit=1)
            if not rule_record or not rule_record.pricelist_id:
                continue

            pricelist = rule_record.pricelist_id
            quantity_start = p_attrs.get('quantityStart', 1) or 1
            price_data = p_attrs.get('price') or []
            if not price_data or not isinstance(price_data, list):
                continue
            net_price = price_data[0].get('net', 0.0)
            gross_price = price_data[0].get('gross', 0.0)

            domain = [
                ('pricelist_id', '=', pricelist.id),
                ('min_quantity', '=', quantity_start),
                ('applied_on', '=', '0_product_variant' if is_variant else '1_product'),
            ]
            if is_variant:
                domain.append(('product_id', '=', odoo_product.id))
            else:
                domain.append(('product_tmpl_id', '=', odoo_product.product_tmpl_id.id))

            existing_item = PricelistItem.search(domain, limit=1)

            item_vals = {
                'pricelist_id': pricelist.id,
                'applied_on': '0_product_variant' if is_variant else '1_product',
                'product_id': odoo_product.id if is_variant else False,
                'product_tmpl_id': odoo_product.product_tmpl_id.id,
                'min_quantity': quantity_start,
                'compute_price': 'fixed',
                'fixed_price': net_price,
            }

            if existing_item:
                existing_item.write(item_vals)
            else:
                PricelistItem.create(item_vals)
