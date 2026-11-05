# Anleitung: Onshape per Prompt bedienen

## Vor dem Start

- Der Server ist in Claude Code für **alle Projekte** eingetragen (`claude mcp get onshape`).
- Nach Änderungen am Server-Code oder an der `.env`: **Claude Code neu starten**.
- Funktioniert etwas gar nicht: in Claude Code `/mcp` eingeben – `onshape` muss „connected“ sein.

## Die wichtigsten Gewohnheiten

1. **Links mitgeben.** Am einfachsten ist ein Onshape-Link aus der Browser-Adresszeile
   (`https://cad.onshape.com/documents/…/w/…/e/…`). Daraus liest Claude Dokument, Arbeitsbereich
   und Element ab.
2. **Maße in mm** angeben (Standard). Winkel in Grad.
3. **Erst prüfen lassen, dann ändern:** „Zeig mir, was in dem Dokument ist“ → `inspect_onshape_link`.
4. **Material zuweisen**, bevor du Masse, Simulation oder Druckgewicht brauchst.
5. **Nach größeren Schritten kontrollieren lassen:** „Prüfe die Maße“, „Zeig die Positionen“,
   „Exportiere eine Vorschau der Zeichnung“.
6. **Kleine Schritte:** lieber „mach eine Platte 80×40×5 mm“ und danach „4 Bohrungen M3 in die Ecken,
   5 mm vom Rand“ als alles in einem Satz.

## Beispiel-Prompts

**Modellieren**
- „Leg ein neues Dokument ‚Halterung‘ an und mach eine Platte 80 × 40 × 5 mm.“
- „Mach auf der Oberseite vier Durchgangsbohrungen Ø3,4 mm, je 6 mm vom Rand.“
- „Höhle das Teil mit 2 mm Wandstärke aus, Oberseite offen.“
- „Verrunde alle senkrechten Kanten mit 3 mm.“

**Fremde Modelle**
- „Prüf diesen Link: …“ – zeigt, ob Kopieren oder nur Exportieren erlaubt ist.
- „Kopier das Dokument in mein Konto“ (mit Verlauf) oder „Importier die Baugruppe ‚Robot‘“
  (nur Geometrie, wenn Kopieren verboten ist).
- „Importier die Datei ~/Downloads/servo.step in das Dokument …“

**Baugruppen & Fertigteile**
- „Setz 4 Zylinderkopfschrauben ISO 4762 M3 × 10 in die Baugruppe.“
- „Welche Längen gibt es für M3 bei ISO 4762?“
- „Verbinde Lagerbock und Grundplatte starr.“ → `create_rigid_group`
- „Mach ein Drehgelenk zwischen dem Arm (Bohrung) und dem Servohorn.“ → `create_axis_joint`
- „Markier alle Teile des Servos als Kaufteil, Hersteller Feetech, Nummer STS3215.“

**Zeichnungen**
- „Mach eine Zeichnung vom Teil ‚Halterung‘ auf A4, bemaßt, mit Werkstoff-Notiz.“
- „Exportier die Zeichnung als PDF (und zeig mir eine Vorschau).“

**3D-Druck**
- „Exportier alle Druckteile der Baugruppe als STL, mein Drucker hat 256 × 256 × 256 mm.“
- „Mach eine 3MF-Datei mit allen Druckteilen.“

**Simulation (MuJoCo)**
- „Weise allen gedruckten Teilen PLA mit 20 % Infill zu.“
- „Exportier die Baugruppe für MuJoCo, Basis ist ‚Lamp Base‘.“
- „Setz für alle Gelenke Dämpfung 0,6 und Positionsservos mit kp 17,8.“
- „Begrenz die Schulter auf −60° bis 120°.“
- „Simulier 3 Sekunden und fahr die Schulter auf 30°.“
- Ansehen: `python -m mujoco.viewer --mjcf=<datei>.xml` (aus `.venv/bin/`)

## Wo landet was?

| Was | Wo |
|---|---|
| CAD, Zeichnungen | in Onshape |
| PDF, STL, 3MF, STEP, MuJoCo-Pakete | `~/onshape-exports/` (änderbar: `ONSHAPE_EXPORT_DIR`) |
| Server-Code | `~/UVU/marathon/onshape-mcp` |

## Gut zu wissen

- **Löschen kann der Server nicht** (API-Schlüssel ohne Lösch-Recht). Testobjekte in Onshape von Hand
  löschen. Einzelne Features löschen geht.
- **Onshape bremst bei vielen Anfragen** (Rate-Limit); der Server wartet dann automatisch. Große
  Importe dauern Minuten.
- **Teile fixieren** geht per API nicht – bei der Simulation die Basis mit `base` angeben.
- **Importierte Modelle** (STEP) haben keine Mates und keine Gelenkgrenzen – Gelenke mit
  `create_axis_joint` setzen, Grenzen per `update_mujoco_model` nachtragen.
- **Nach dem Studium** (kostenloser Onshape-Tarif): in der `.env` `ONSHAPE_DOCUMENTS_PUBLIC=true`.
- **API-Schlüssel abgelaufen?** Neuen auf dev-portal.onshape.com erstellen und in `.env` eintragen.

## Server aktualisieren

```bash
cd ~/UVU/marathon/onshape-mcp
git fetch upstream            # Neuerungen vom Original-Projekt holen
git merge upstream/main       # übernehmen (Konflikte ggf. mit Claude lösen)
env -u PYTHONPATH uv sync --extra sim
env -u PYTHONPATH uv run pytest   # alles grün? dann Claude Code neu starten
```
