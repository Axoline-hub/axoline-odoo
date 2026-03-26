# Axoline Shopware Connector — Funktions- und Ablaufbeschreibung

Dieses Dokument beschreibt den **Zweck**, die **Architektur**, die **Konfiguration** und die **typischen Abläufe** des Odoo-Addons **Axoline Shopware Connector** (technischer Name: `axoline_shopware_connector`) für die Anbindung an **Shopware 6** über die Admin-API.

---

## 1. Zweck und Überblick

Das Modul verbindet einen oder mehrere Shopware-6-Shops mit Odoo und ermöglicht:

- **Import** von Kategorien, Produkten (inkl. Varianten), Kunden und Bestellungen **Shopware → Odoo**
- optionale **Exporte** von Odoo-Produktkategorien und -produkten **Odoo → Shopware** (wenn im Backend aktiviert)
- **Zwischenablage** der Shopware-Daten in eigenen Odoo-Modellen (`shopware.*`), die mit Standarddatensätzen (z. B. `product.product`, `res.partner`, `sale.order`) verknüpft sind
- **Nachbearbeitung von Bestellungen in Odoo** mit Rückmeldung von Liefer- und Bestellstatus an Shopware (State-Machine-Transitions)

Die Kommunikation erfolgt per **OAuth2 Client Credentials** und den Shopware-**REST/JSON-API**-Endpunkten (`/api/…`).

---

## 2. Technische Abhängigkeiten

Laut `__manifest__.py` baut das Modul auf folgenden Odoo-Basismodulen auf:

| Modul | Rolle |
|--------|--------|
| `base`, `contacts` | Grundsystem, Partner |
| `sale_management`, `sale_stock` | Verkauf, Lieferscheine, Lieferstatus |
| `stock` | Lager, Transfers |
| `product` | Produkte und Kategorien |
| `account` | Rechnungen (Buchung, Kundenrechnungen) |

---

## 3. Benutzeroberfläche und Menüs

Unter dem Hauptmenü **„Axoline Shopware Connector“** (mit Modul-Icon) finden sich:

| Bereich | Inhalt |
|---------|--------|
| **Konfiguration → Backends** | `shopware.backend`-Datensätze: Shop-URL, Zugangsdaten, Sync-Optionen, Importfilter, Export-Flags |
| **Daten → Bestellungen / Kunden / Produkte / Kategorien / Preisregeln** | Listen- und Formularansichten der Connector-Modelle |

Auf **Backend-Formularen** gibt es u. a. **Smart Buttons** (Zähler) zu Produkten, Kategorien, Kunden und Bestellungen dieses Backends.

---

## 4. Das Shopware-Backend (`shopware.backend`)

Ein Backend repräsentiert **eine** Shopware-Instanz. Zentrale Felder und Bedeutung:

### 4.1 Verbindung und Status

- **Shop-URL**, **Client ID**, **Client Secret** — Shopware-API-Zugang (Integration).
- **Status** (`draft` / `confirmed` / `error`): „Verbunden“ wird u. a. nach erfolgreichem **Verbindungstest** gesetzt.
- **Unternehmen** (`company_id`) — Mandantenbezug in Odoo.

### 4.2 Sync-Steuerung (Checkboxen)

- **Produkte / Kategorien / Kunden / Bestellungen synchronisieren** — steuern, welche Entitäten beim **Voll-Sync** (Button und Cron) mitlaufen.
- **Produkte / Kategorien exportieren** — schaltet die **Export-Buttons** im Backend frei (Odoo → Shopware).

### 4.3 Standardwerte für Odoo/Shopware

- **Standard Sales Channel ID** — Shopware-seitig (z. B. für Exporte).
- **Standard-Steuer**, **Standard-Zahlungsbedingung** — für erzeugte Odoo-Verkaufsaufträge aus Shopware-Bestellungen.

### 4.4 Bestellimport-Filter und -limits

- **Bestellstatus-Filter** (`all` / `open` / `completed`) — welche **Suchfilter** beim Hauptimport auf **Bestellungen** angewendet werden (nicht zwingend bei ID-basierten Abrufen).
- **Bestellungen ab Datum** — unterbindet „alles seit Anbeginn“, wenn kein „letzter Bestell-Sync“ gesetzt ist.
- **Max. Bestellungen pro Sync** — Obergrenze pro Lauf (0 = unbegrenzt).
- **Verknüpfte Bestellungen pro Lauf aktualisieren** — nach dem Hauptimport: erneuter Abruf bereits mit Odoo verknüpfter Shopware-Bestellungen **ohne** den Statusfilter des Hauptimports, damit z. B. **Zahlungsstatus** aktuell bleibt (bis Status `completed`/`cancelled`).

### 4.5 Shopware ↔ Odoo Bestellprozesse (State Machine)

- **Nach Import: Status „In Bearbeitung“ in Shopware** — wenn die Bestellung in Shopware noch `open` ist, wird die Transition **`process`** (konfigurierbar) ausgeführt → typisch **`in_progress`**.
- **Transition offen → in Bearbeitung** — Feldname der Shopware-Transition (Standard: `process`).
- **Lieferschein: Versand in Shopware auf „versendet“** — bei **erledigtem** ausgehenden oder Dropship-**Lieferschein** wird die Transition **`ship`** auf die **order_delivery** angewendet.
- **Bestellung: in Shopware „abgeschlossen“ bei Versand + Rechnung** — wenn alle ausgehenden/dropship-Lieferscheine des Auftrags erledigt sind **und** mindestens eine **gebuchte Kundenrechnung** existiert, wird die Bestell-Transition **`complete`** (konfigurierbar) ausgeführt → **`completed`**.
- **Transition → Bestellung abgeschlossen** — technischer Name der Transition (Standard: `complete`).

### 4.6 Sonstiges

- **Test: eine Shopware-Bestellungs-ID** / **Test: eine Shopware-Kunden-ID** — Import nur **dieses** Datensatzes; **Sync-Zeitstempel** werden nicht angepasst, damit anschließende Voll-Syncs unverändert bleiben.
- **Max. Kunden pro Lauf** — Begrenzung für Stichproben (0 = unbegrenzt).
- **Variantendaten bei Attributänderung erhalten** — überträgt u. a. **Artikelnummer** und **Barcode** bei Variantenänderungen auf neue Varianten (verknüpft mit `product.template` / `product.product`).
- **API Batch-Größe** — Datensätze pro API-Anfrage (z. B. Produktimport in Batches).

### 4.7 Synchronisations-Log

Zeitstempel: **Letzte Synchronisation gesamt**, **letzter Produkt-/Kategorie-/Kunden-/Bestell-Sync**.  
Button **„Alle Sync-Daten zurücksetzen“** — erzwingt beim nächsten Lauf einen **vollen Neuimport** (Delta-Logik greift dann erst wieder nach neuen Zeitstempeln).

### 4.8 Aktionen (Buttons)

- **Verbindung testen** — Authentifizierung, Status `confirmed`.
- **Alles synchronisieren** — interner Ablauf wie Cron (siehe Abschnitt 5).
- **API Diagnose** — minimale Produktsuche, zeigt strukturierte Antwort (Fehleranalyse).
- **Einzel-Syncs** — Kategorien, Produkte, Kunden, Bestellungen (jeweils nur wenn sinnvoll aktiviert).
- **Export** — Kategorien, Produkte, sofern Export-Flags gesetzt sind.

---

## 5. Vollständiger Sync (UI und Cron)

### 5.1 Ablauf `_do_sync_all`

1. **Preisregeln** (`shopware.price.rule.sync_rules_from_shopware`) — wenn Produktsync aktiv ist, wird **zuerst** die Regel-Synchronisation ausgeführt (Shopware-`rule`-Entitäten → Odoo-Preislisten-Zuordnung).
2. Danach nacheinander (wenn jeweils aktiviert):
   - **Kategorien** (`shopware.category.sync_from_shopware`)
   - **Produkte** (`shopware.product.sync_from_shopware`)
   - **Kunden** (`shopware.customer.sync_from_shopware`)
   - **Bestellungen** (`shopware.order.sync_from_shopware`)

**Fehler** in einer Phase blockieren die nächste **nicht** — es wird geloggt, Rollback auf Phasenebene, dann Fortsetzung.

### 5.2 Geplanter Auftrag (Cron)

- **Name:** „Axoline Shopware Connector: Vollständige Synchronisation“
- **Intervall:** alle **15 Minuten** (Standard)
- **Code:** `model._cron_sync_all()` auf `shopware.backend`
- Es werden nur Backends mit **Status „Verbunden“** und **aktiv** berücksichtigt.

---

## 6. Datenmodelle (Connector-Schicht)

| Modell | Kurzbeschreibung |
|--------|--------------------|
| `shopware.backend` | Verbindung, Sync-Parameter, API-Hilfsmethoden |
| `shopware.category` | Shopware-Kategorie; optional `product.category` in Odoo |
| `shopware.product` | Shopware-Produkt/Variante; Verknüpfung zu `product.product` |
| `shopware.price.rule` | Shopware-Preisregel; Zuordnung zu Odoo-**Preisliste** |
| `shopware.customer` | Shopware-Kunde; Verknüpfung zu `res.partner` |
| `shopware.order` | Shopware-Bestellung; Positionen `shopware.order.line`; Verknüpfung zu `sale.order` |
| `shopware.order.line` | Bestellpositionen aus Shopware |

---

## 7. Import-Details nach Entität

### 7.1 Kategorien (`shopware.category`)

- **Delta:** seit `last_category_sync` über `updatedAt`
- **Nachziehen fehlender IDs:** Vergleich aller Remote-IDs mit lokalen Binds
- **Hierarchie:** Nach dem Import werden **Eltern-Beziehungen** aufgelöst
- Odoo: **find or create** `product.category` nach Namen beim ersten Anlegen

### 7.2 Produkte (`shopware.product`)

- **Zwei Phasen:**  
  - Delta über `updatedAt` seit `last_product_sync` (nur Hauptprodukte ohne `parentId`)  
  - **Fehlende Produkte:** alle Shopware-Hauptprodukt-IDs vs. lokal gebundene IDs
- **Varianten:** Kinderprodukte werden mit Optionen, Preisen, Medien/Referenzen verarbeitet (Assoziationen in der API)
- **Preisregeln** werden für Preislisten-Mapping genutzt
- **Backfill:** fehlende `default_code` in Odoo aus `shopware_product_number` nachziehen

### 7.3 Preisregeln (`shopware.price.rule`)

- Import aller Shopware-**Regeln** (`rule`-Suche)
- Pro neuer Regel: **Odoo-Preisliste** „Shopware: {Name}“ anlegen, falls nicht vorhanden

### 7.4 Kunden (`shopware.customer`)

- Suche mit **Assoziationen** (Standard-/Rechnungs-/Lieferadresse, Gruppe, …)
- Adressen werden **pro Kunde** nachgeladen (kein Aufblasen der Massensuche)
- Import einzelner Kunden per UUID wird auch vom **Bestellimport** genutzt, damit **dieselbe Logik** wie beim Kunden-Sync gilt

### 7.5 Bestellungen (`shopware.order`)

- **Suchfilter** abhängig von Backend-Einstellungen (Status, Datum, Limit)
- **Test-UUID:** exakt eine Bestellung
- **JSON:API:** `included[]` wird in **Bestellattribute** gemappt (Transaktionen, Lieferungen, …), damit Zahlungs- und Lieferinfos vollständig sind
- Anlage/Aktualisierung von **Kunden**, **Positionen**, **Zahlungsstatus**, **Versand**, **Liefer-/Shopware-Delivery-ID**
- Optional: **Odoo-Verkaufsauftrag** wird angelegt (`_create_sale_order`), inkl. Preis-/Steuerlogik aus Shopware
- Nach Import: optional **„open“ → „in_progress“** in Shopware
- **Verknüpfte Bestellungen:** separater Refresh-Stapel für bereits mit `sale.order` verknüpfte Datensätze

---

## 8. Export Odoo → Shopware

### 8.1 Kategorien

- Alle `shopware.category` des Backends: **PATCH** bei bekannter `shopware_id`, sonst **POST** `category`
- Elternbeziehung über `parentId`, wenn die Parent-Kategorie bereits eine Shopware-ID hat

### 8.2 Produkte

- Für jedes **Hauptprodukt** und dessen **Varianten** (`variant_bind_ids`): **PATCH**/`POST` `product`
- Payload enthält u. a. Name, Nummer, **Bestand**, **Brutto-/Nettopreis**, Gewicht, EAN, **Kategorien**, bei Varianten `parentId`
- Währung **EUR** wird per Suche ermittelt; für neue Produkte ggf. **Steuer-ID** über Shopware-`tax`-Suche zum `tax_rate`

Exporte sind **nicht** Teil des automatischen Cron-Syncs, sondern **manuell** über Backend-Buttons (sofern Export-Checkboxen aktiv).

---

## 9. Erweiterungen Standard-Modelle

| Modell | Ergänzung |
|--------|-----------|
| `sale.order` | Verknüpfung `shopware_order_id`, Shopware-Metadaten (Kommentar, Zahlungsart, Versand, Status, …), Hilfsmethoden für Shopware-Abgleich und **Abschlussbedingungen** |
| `stock.picking` | Nach `_action_done` für **outgoing** / **dropship**: optional Versand-Push und **Abschluss-Push** (siehe unten) |
| `account.move` | Nach **Buchung** einer **Kundenrechnung**: Versuch des **Bestell-Abschlusses** in Shopware, wenn Lieferung + Rechnung in Odoo erfüllt sind |
| `product.template` / `product.product` | Shopware-Produkt-Zähler, **Variantendaten** beim Attributwechsel (optional), Aktionen zur Shopware-Produktansicht |
| `product.category` | Verknüpfung zu Shopware-Kategorien (wo vorgesehen) |
| `res.partner` | Shopware-Kunden-Zähler und Sprung zu `shopware.customer` |

---

## 10. Odoo → Shopware: Lieferung und Bestellabschluss

### 10.1 Voraussetzungen

- Der **Verkaufsauftrag** ist mit der **Shopware-Bestellung** (`shopware_order_id` / `odoo_sale_order_id`) verknüpft.
- Backend-Optionen sind entsprechend gesetzt.

### 10.2 Versand „Versendet“ (order_delivery)

- Auslöser: **Lieferschein erledigt** (`_action_done`), Typ **Lieferung** oder **Dropshipping**
- API: State-Machine-Transition **`ship`** auf die **order_delivery** (UUID in `shopware_delivery_id` oder Nachladen aus der Bestellung)

### 10.3 Bestellung „Abgeschlossen“ (order)

- Bedingungen in Odoo:
  - alle **nicht stornierten** ausgehenden/dropship-**Lieferscheine** des Auftrags sind **erledigt**
  - mindestens eine **gebuchte Kundenrechnung** (`out_invoice`, `posted`)
- Auslöser:
  - nach **Lieferschein erledigt** (falls Option aktiv)
  - nach **Rechnungsbuchung** (`action_post`), damit die Reihenfolge **Versand zuerst** oder **Rechnung zuerst** egal ist
- API: Transition **`complete`** (Standard) auf die **Bestellung**

Fehler werden **geloggt** (kein harter Abbruch der Odoo-Transaktion durch die Shopware-API).

---

## 11. Sicherheit und Berechtigungen

- **Privileg:** „Axoline Shopware Connector“
- **Gruppen:** **Benutzer** (Lesen Connector-Daten), **Administrator** (volle CRUD auf Connector-Modelle)
- `shopware.backend` ist **Chatter**-fähig (`mail.thread` / Aktivitäten)

---

## 12. API-Hilfen im Backend

- **Authentifizierung** mit Token-Cache und Ablaufzeit
- **GET/POST/PATCH** zentral gekapselt
- **Suche** `_api_search` mit Pagination, Schutz vor Endlosschleifen, JSON:API-**Enrichment** für Bestellungen
- **Transitions** `_api_order_state_transition` und `_api_order_delivery_state_transition` mit **fallback**-Pfaden (verschiedene Shopware-API-Versionen/Route-Varianten), Fehler **loggen** statt `UserError` (für Import-Folgeprozesse)

---

## 13. Logging und Betrieb

- Umfangreiche **Logger-**Ausgaben (`INFO`/`WARNING`/`ERROR`) zu Suchläufen, Seiten, Importzählern und API-Fehlern
- Bei **Cron-Fehlern** Rollback pro Backend, nächster Lauf versucht erneut
- **Diagnose-Button** zeigt Rohstruktur einer kleinen Produktsuche

---

## 14. Versionshinweis

Die **Manifest-Version** steht in `__manifest__.py` (z. B. `19.0.1.1.47`). Diese Beschreibung bezieht sich auf den **Funktionsumfang** der beschriebenen Modulversion; bei Updates können einzelne Felder oder Abläufe erweitert worden sein.

---

*Hinweis: Dieses Addon ist proprietär (Lizenz siehe `LICENSE` im Modulverzeichnis).*
