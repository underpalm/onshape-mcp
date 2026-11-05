# Onshape MCP – CAD per Prompt

Ein MCP-Server, mit dem man [Onshape](https://www.onshape.com) in natürlicher Sprache bedient –
z. B. aus [Claude Code](https://claude.com/claude-code). Statt zu klicken sagt man
*„Mach eine Platte 80 × 40 × 5 mm mit vier M3-Bohrungen“* oder
*„Exportier die Baugruppe für MuJoCo“* – der Server setzt das über die Onshape-API um.

Ausgelegt auf deutsche Gewohnheiten: **Millimeter**, ISO/DIN-Normteile, Zeichnungen in
**europäischer Projektion** mit Komma als Dezimaltrennzeichen.

> Fork von [hedless/onshape-mcp](https://github.com/hedless/onshape-mcp) (MIT), stark erweitert.

## Was es kann

| Bereich | Beispiele |
|---|---|
| **Modellieren** | Skizzen mit mehreren Formen (Rechteck, Kreis, Langloch, Polygon …) auf Ebenen oder beliebigen Flächen; Extrudieren, Drehen, Verrunden, Fasen, Muster, Boolesche Operationen; Zugriff auf **alle 97 Onshape-Features** (Schale, Spiegeln, Ebene, Helix, Sweep, Loft, Blech …) |
| **Fremde Designs übernehmen** | Link prüfen (darf ich kopieren/exportieren?), Vollkopie mit Verlauf, oder Geometrie-Import, wenn nur Export erlaubt ist; lokale STEP/STL-Dateien importieren |
| **Fertigteile** | 156 ISO/DIN-Normteile (Schrauben, Muttern, Scheiben, Stifte, Sicherungsringe …) in passender Größe einsetzen; Teile aus anderen Dokumenten (Servos, Motoren, Platinen) einfügen; Kaufteile markieren |
| **Baugruppen** | Mates, starre Gruppen, Gelenke auf einer Achse ohne Verschieben (auch für importierte Modelle), Positionen, Kollisionsprüfung |
| **Physik** | 189 Materialien (auch „Stahl“, „Aluminium“ …), eigene Dichte bzw. 3D-Druck-Infill; Masse, Schwerpunkt, Trägheit |
| **Zeichnungen** | ISO-Zeichnung A4–A0 mit Normalansichten, automatische Bemaßung (Gesamtmaße, Bohrungen), Notizen; Export als PDF, DXF, DWG, SVG, PNG |
| **3D-Druck** | STL in mm pro Teil, 3MF; ganze Baugruppen: nur Druckteile, mit Stückzahl, plus **Einkaufsliste** für Kaufteile; Prüfung gegen die Druckbettgröße |
| **Simulation** | Baugruppe → **URDF** (ROS) und **MJCF** (MuJoCo) mit Massen, Trägheiten, Gelenken, Grenzen, Dämpfung, Reibung, Federn, Servos, Sensoren; kurzer Testlauf direkt aus dem Chat |

Insgesamt 79 Tools – vollständige Liste in [docs/ERWEITERUNGEN.md](docs/ERWEITERUNGEN.md).

## Einrichtung

**Voraussetzungen:** Linux/macOS/Windows, Python ≥ 3.10, [uv](https://docs.astral.sh/uv/),
ein Onshape-Konto (kostenlos, Education oder bezahlt) und Claude Code.

```bash
git clone https://github.com/underpalm/onshape-mcp.git
cd onshape-mcp
uv sync --extra sim          # ohne "--extra sim" nur CAD, ohne MuJoCo
cp .env.example .env         # danach API-Schlüssel eintragen
```

**API-Schlüssel** auf [dev-portal.onshape.com](https://dev-portal.onshape.com) → *API-Schlüssel*
erstellen, Berechtigungen:

| Berechtigung | |
|---|---|
| Profilinformationen lesen | ✅ |
| Dokumente lesen | ✅ |
| Dokumente ändern | ✅ |
| Dokumente löschen | ❌ (bewusst – Claude kann so nichts Wichtiges löschen) |
| Käufe / Teilen | ❌ |

Den Secret Key zeigt Onshape nur einmal – sofort in die `.env` kopieren.

**In Claude Code eintragen** (einmalig, gilt für alle Projekte):

```bash
claude mcp add onshape --scope user -- env -u PYTHONPATH "$PWD/.venv/bin/python" -m onshape_mcp.server
```

Claude Code neu starten, `/mcp` zeigt „onshape: connected“. Test: *„Liste meine Onshape-Dokumente.“*

> `env -u PYTHONPATH` verhindert Konflikte, wenn ROS installiert ist; ohne ROS schadet es nicht.

### Einstellungen (`.env`)

| Variable | Bedeutung | Standard |
|---|---|---|
| `ONSHAPE_ACCESS_KEY`, `ONSHAPE_SECRET_KEY` | API-Schlüssel | – |
| `ONSHAPE_LENGTH_UNIT` | Längeneinheit aller Tools: `mm`, `cm`, `m`, `in` | `mm` |
| `ONSHAPE_DOCUMENTS_PUBLIC` | neue Dokumente öffentlich (kostenloser Tarif: `true`) | `false` |
| `ONSHAPE_EXPORT_DIR` | Ordner für Exporte | `~/onshape-exports` |
| `ONSHAPE_MCP_CACHE` | Cache für Normteile/Materialien | `~/.cache/onshape-mcp` |

## Benutzung

Einfach in Claude Code schreiben, was in Onshape passieren soll. Am besten den Link des
Dokuments aus der Browser-Adresszeile mitgeben und in kleinen Schritten arbeiten.

```text
Leg ein Dokument "Halterung" an und mach eine Platte 80 × 40 × 5 mm.
Mach vier Durchgangsbohrungen Ø3,4 mm, je 6 mm vom Rand, und verrunde die Ecken mit 4 mm.
Weise PLA mit 20 % Infill zu und sag mir das Gewicht.
Mach eine bemaßte Zeichnung auf A4 und exportier sie als PDF.
Exportier das Teil als STL für meinen Drucker (256 × 256 × 256 mm).
```

```text
Prüf diesen Link: https://cad.onshape.com/documents/…  – darf ich das kopieren?
Setz vier Zylinderkopfschrauben ISO 4762 M3 × 10 in die Baugruppe.
Exportier die Baugruppe für MuJoCo, Basis ist "Grundplatte", und simulier 3 Sekunden.
```

Ausführliche Anleitung mit vielen Beispielen und Tipps: **[docs/ANLEITUNG.md](docs/ANLEITUNG.md)**.

**Ergebnisse:** CAD und Zeichnungen bleiben in Onshape; Dateien (PDF, STL, 3MF, STEP,
MuJoCo-Pakete) landen in `~/onshape-exports/`. Ein MuJoCo-Modell öffnet man mit
`.venv/bin/python -m mujoco.viewer --mjcf=<datei>.xml`.

## Bekannte Grenzen

- Teile lassen sich per API nicht fixieren – bei der Simulation die Basis angeben.
- Importierte Geometrie (STEP) hat keinen Feature-Verlauf und keine Mates; Gelenke und
  Gelenkgrenzen müssen neu gesetzt werden.
- Zeichnungsansichten können nicht verschoben werden; Bohrungspositionen und Gewinde werden
  nicht automatisch bemaßt.
- Bestehende Features können noch nicht geändert werden (nur neu anlegen/löschen).
- Kugel-, Planar- und Zylindergelenke exportiert Onshape nicht als bewegliche URDF-Gelenke.

Details und alle abgefangenen Onshape-Eigenheiten: [docs/ERWEITERUNGEN.md](docs/ERWEITERUNGEN.md).

## Entwicklung

```bash
env -u PYTHONPATH uv run pytest              # 622 Tests
env -u PYTHONPATH uv run ruff check onshape_mcp
env -u PYTHONPATH uv run ruff format onshape_mcp
```

Neue Tools gehören nach `onshape_mcp/tools/` (registriert in `tools/__init__.py`), damit
`server.py` nah am Original bleibt und Updates leicht zu übernehmen sind:

```bash
git remote add upstream https://github.com/hedless/onshape-mcp.git   # einmalig
git fetch upstream && git merge upstream/main
```

### Aufbau

```
onshape_mcp/
├── server.py              # MCP-Server, Original-Tools
├── units.py               # Einheiten (Standard mm)
├── exports.py             # lokale Export-Dateien
├── standard_content.py    # ISO/DIN-Normteile (sichere Auswertung der Onshape-Regeln)
├── api/                   # Onshape-REST-Zugriff (Client, Dokumente, Transfer, Zeichnungen …)
├── builders/              # Feature-JSON (Skizze, Extrude, Mate …, generic.py = alle Features)
├── analysis/              # Positionen, Kollisionen, Flächen-Koordinatensysteme
├── sim/                   # URDF → MJCF-Konverter, MuJoCo-Prüfung
└── tools/                 # Erweiterungs-Tools (Kopieren, Teile, Features, Physik,
                           #   Zeichnungen, Druck, Simulation, Baugruppen)
tests/                     # pytest
docs/                      # ANLEITUNG.md, ERWEITERUNGEN.md, Original-Dokumentation
```

Weitere Dokumentation aus dem Original-Projekt: [docs/QUICK_START.md](docs/QUICK_START.md),
[docs/DEV_SETUP.md](docs/DEV_SETUP.md), [docs/TESTING.md](docs/TESTING.md),
[knowledge_base/assembly_workflow_guide.md](knowledge_base/assembly_workflow_guide.md).

## Lizenz und Dank

MIT-Lizenz, wie das Original-Projekt.

- Basis: [hedless/onshape-mcp](https://github.com/hedless/onshape-mcp)
- [Model Context Protocol](https://modelcontextprotocol.io/), [Onshape-API-Dokumentation](https://onshape-public.github.io/docs/), [MuJoCo](https://mujoco.org/)
- Getestet u. a. mit LeLamp (Human Computer Lab)

Beim Kopieren und Ändern fremder Designs gilt deren eigene Lizenz.
