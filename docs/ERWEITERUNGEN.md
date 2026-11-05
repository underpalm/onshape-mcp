# Erweiterungen dieses Forks

Fork von [hedless/onshape-mcp](https://github.com/hedless/onshape-mcp) (MIT). Ergänzt um metrische
Einheiten, Kopieren/Importieren, Normteile, generische Features, Materialien, Zeichnungen,
3D-Druck-Export und MuJoCo-Simulation. Neue Tools liegen in `onshape_mcp/tools/`, damit
Updates vom Original leicht übernommen werden können (`git fetch upstream`).

## Einrichtung

```bash
uv sync --extra sim            # mit MuJoCo (Simulation); ohne --extra sim nur CAD
cp .env.example .env           # API-Schlüssel eintragen
claude mcp add onshape --scope user -- env -u PYTHONPATH "$PWD/.venv/bin/python" -m onshape_mcp.server
```

`env -u PYTHONPATH` ist nötig, wenn ROS installiert ist (dessen Python-Pfad stört sonst).

| `.env`-Variable | Bedeutung | Standard |
|---|---|---|
| `ONSHAPE_ACCESS_KEY`, `ONSHAPE_SECRET_KEY` | API-Schlüssel (dev-portal.onshape.com) | – |
| `ONSHAPE_LENGTH_UNIT` | Längeneinheit aller Tools: `mm`, `cm`, `m`, `in` | `mm` |
| `ONSHAPE_DOCUMENTS_PUBLIC` | neue Dokumente öffentlich (kostenloser Tarif: `true`) | `false` |
| `ONSHAPE_EXPORT_DIR` | Ordner für Exporte | `~/onshape-exports` |
| `ONSHAPE_MCP_CACHE` | Cache für Normteile/Materialien | `~/.cache/onshape-mcp` |

## Tools (zusätzlich zu den Original-Tools)

| Bereich | Tools |
|---|---|
| Kopieren / Import | `inspect_onshape_link`, `copy_document`, `copy_element`, `import_from_document`, `import_cad_file` |
| Fertigteile | `search_standard_parts`, `get_standard_part_options`, `insert_standard_part`, `insert_part_from_document` |
| Modellieren | `find_entities`, `create_sketch`, `describe_feature_type`, `create_feature` (alle 97 Onshape-Features) |
| Baugruppen-Kinematik | `create_rigid_group`, `create_axis_joint` (Gelenk ohne Verschieben, auch in Unterbaugruppen) |
| Physik | `list_materials`, `set_part_material`, `get_mass_properties` |
| Zeichnungen | `create_drawing`, `auto_dimension_drawing`, `get_drawing_views`, `add_drawing_dimension`, `add_drawing_note`, `export_drawing` |
| 3D-Druck / CAD-Export | `export_stl`, `export_assembly_for_printing` (nur Druckteile + Einkaufsliste), `mark_purchased_parts`, `export_model` (3MF, STEP, …) |
| Simulation | `export_mujoco`, `update_mujoco_model`, `simulate_mujoco` |

## MuJoCo-Workflow

1. Teile modellieren, **Material zuweisen** (`set_part_material`) – sonst keine Masse/Trägheit.
2. Baugruppe mit Mates: *Revolute* → Drehgelenk, *Slider* → Schubgelenk, *Fastened* → starr.
   Die Mate-Namen werden zu Gelenknamen.
3. `export_mujoco` mit `config`, z. B.:

```json
{
  "base": "Lagerbock",
  "joints": {
    "Pendelgelenk": {"damping": 0.002, "frictionloss": 0.0002, "range": [-60, 60],
                     "actuator": {"type": "position", "kp": 2, "forcerange": [-1, 1]}}
  },
  "springs": [{"name": "feder", "body1": "a", "point1": [0, 0, 0.1],
               "body2": "b", "point2": [0, 0, 0.2], "stiffness": 200, "rest_length": 0.08}]
}
```

4. Prüfen mit `simulate_mujoco` oder interaktiv: `python -m mujoco.viewer --mjcf=<datei>.xml`.
5. Feintuning mit `update_mujoco_model` (ohne erneuten Onshape-Export).

Einheiten in der Simulations-Config: SI (m, kg, N, s), Winkel in Grad.

Validiert mit einem physikalischen Pendel (Stahl, 164,87 g): MuJoCo-Periode 0,7333 s,
Theorie 0,7330 s (+ Amplitudenkorrektur); mit Drehfeder 0,2 Nm/rad exakt 0,4834 s.

## Praxisbeispiel: LeLamp (importiertes STEP-Modell, 5 Servos)

Importierte Geometrie hat keine Mates. So wird daraus ein simulierbarer Roboter:

1. Starre Gruppen bilden (`create_rigid_group`), z. B. Basis + Abdeckung + Basis-Servo,
   Arm + der Servo, der darin sitzt.
2. Pro Servo ein Gelenk (`create_axis_joint`): erste Instanz = angetriebenes Armteil mit der
   Bohrung auf der Servoachse, zweite = das Antriebshorn in der Servo-Unterbaugruppe
   (`"SCS215 v5 (3) <1>/金属舵盘（驱动） v2 <1>"`). Axialer Versatz und Achsrichtung werden
   ausgeglichen; das Tool prüft, dass sich kein Teil bewegt.
3. Material: gedruckte Teile `PLA` mit `infillPercent`, Servos mit effektiver Dichte, sodass die
   Masse dem Datenblatt entspricht (STS3215 ≈ 55 g).
4. `export_mujoco` mit `{"base": "Lamp Base", "joint_defaults": {"damping": 0.6,
   "frictionloss": 0.05, "armature": 0.028, "actuator": {"type": "position", "kp": 18,
   "forcerange": [-2.9, 2.9]}}}` (Servo-Richtwerte, bei Bedarf kalibrieren).

Ergebnis: 18 Körper, 5 Freiheitsgrade, 618 g; Lage aller Teile in MuJoCo innerhalb 0,01 mm
von Onshape. Onshape trennt beim URDF-Export Teile aus Unterbaugruppen heraus, an denen ein
Mate hängt (das Horn) – `export_mujoco` erkennt das und hängt sie wieder an ihren Servo
(`attach` in `sim_config.json`, manuell überschreibbar).

### Gelenkgrenzen bei importierten Modellen

Beim Geometrie-Import (STEP) gehen Mates und damit auch Gelenkgrenzen verloren. Für LeLamp
stammen die Grenzen aus der onshape-to-robot-Ausgabe des Projekts (Gelenke 1–5). Zuordnung über
die Lage der Drehachsen (identisch auf 0,00 mm), Nullstellung geprüft (≤ 0,09 mm). Gelenke, deren
Achse bei uns umgekehrt läuft, bekommen die gespiegelte Grenze. Eingetragen per
`update_mujoco_model` mit `"joints": {"<name>": {"range": [min, max]}}`.

## Bekannte Grenzen

- Teile lassen sich per API nicht „fixieren“ – deshalb `base` in der Config.
- Kugel-, Planar- und Zylinder-Mates exportiert Onshape nicht als bewegliche URDF-Gelenke
  (Warnung im Bericht).
- MuJoCo nutzt für Kollisionen die konvexe Hülle der Meshes; die starren Gruppen beiderseits
  eines Gelenks sind deshalb vom Kontakt ausgenommen.
- Mate-Connector-Koordinaten liefert Onshape im Teile-Koordinatensystem, nicht in Welt-
  koordinaten (wird in `create_axis_joint` umgerechnet).
- Teile-IDs können `/` enthalten (z. B. `JF/`) – in URLs wird kodiert.
- Zeichnungen: Ansichten können per API nicht verschoben werden; Bohrungspositionen und
  Gewinde werden nicht automatisch bemaßt.
- Der API-Schlüssel ohne Lösch-Recht kann keine Elemente löschen (gewollt).

## Onshape-API-Stolperfallen (im Code abgefangen)

- STL-Export ist standardmäßig in **Zoll**, 3MF in **Metern**; `chordTolerance` in Metern,
  `angleTolerance` im Bogenmaß.
- Rate-Limit (HTTP 429) → automatische Wiederholung mit Wartezeit.
- STL-Download leitet auf `cad-euw1.onshape.com` um; Auth-Header wird manuell weitergereicht.
- Skizzen auf Flächen: Ursprung = Projektion des Welt-Ursprungs; Normale zeigt aus dem Teil.
- Material-Dichte heißt `DENS`; keine `libraryReference` mitsenden (HTTP 500).
- Mate-Connector auf Zylinderflächen braucht `MID_AXIS_POINT` statt `CENTROID`.
- FeatureScript: `type` und `box` sind reservierte Wörter.
