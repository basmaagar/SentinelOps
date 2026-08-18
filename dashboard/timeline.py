"""
Frise temporelle — la vie du système sur la dernière heure.

Pourquoi ce module existe
-------------------------
La première version de la console affichait des cartes de décision isolées.
Chacune était complète, mais aucune ne racontait ce qui s'était passé : on
voyait la conclusion sans jamais voir la panne monter, le système réagir,
puis la métrique redescendre. Un opérateur d'astreinte ne raisonne pas sur
des décisions isolées, il raisonne sur une séquence.

Ce module reconstitue cette séquence en interrogeant directement Prometheus
— qui tourne déjà et conserve l'historique — puis en superposant les
décisions du système sur les courbes réelles qu'il a observées.

Le résultat se lit de gauche à droite comme un récit : la latence grimpe,
un marqueur de détection apparaît, une action s'exécute, la courbe
redescend. Ou bien : la courbe grimpe et rien ne se déclenche — et cette
absence est une information aussi importante que sa présence.

Choix de rendu : SVG écrit à la main plutôt qu'une bibliothèque de
graphiques. Les valeurs par défaut d'une bibliothèque (grilles, légendes,
axes, palettes) produiraient exactement le graphique générique qu'on ne
veut pas, et il serait plus long de les désactiver que de dessiner.
"""

import time
import html

import requests

# Séries suivies dans la frise, dans l'ordre d'affichage.
# Chaque entrée : (clé, libellé lisible, requête PromQL, unité).
SERIES = [
    ("disque", "disque", "app_disk_injection_active_mb", "Mo"),
    ("memoire", "mémoire", "app_memory_injection_active_mb", "Mo"),
    ("latence", "latence", "app_latency_injection_active_ms", "ms"),
    ("cpu", "cpu", "app_simulated_cpu_load_percent", "%"),
]

PALETTE = {
    "ground": "#0B0E1A", "panel": "#141A2E", "rule": "#2A3454",
    "ink": "#E4E8F7", "dim": "#7C87AD", "faint": "#4E5878",
    "signal": "#7C9CF5", "decide": "#F0A93B", "degraded": "#E0637F",
    "ok": "#5FD3A6",
}


def fetch_range(prometheus_url: str, promql: str, minutes: int = 60,
                step_seconds: int = 15, timeout: float = 4.0) -> list[tuple[float, float]]:
    """
    Interroge /api/v1/query_range et retourne une liste (timestamp, valeur).

    Retourne une liste vide en cas d'échec plutôt que de lever : une série
    absente est un cas normal (métrique jamais produite, Prometheus
    redémarré), et faire échouer tout l'affichage pour une courbe manquante
    serait disproportionné.
    """
    end = time.time()
    start = end - minutes * 60
    try:
        r = requests.get(
            f"{prometheus_url.rstrip('/')}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step_seconds},
            timeout=timeout,
        )
        r.raise_for_status()
        payload = r.json()
    except (requests.RequestException, ValueError):
        return []

    result = payload.get("data", {}).get("result", [])
    if not result:
        return []

    points = []
    for ts, raw in result[0].get("values", []):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value:  # écarte NaN
            points.append((float(ts), value))
    return points


def _path(points: list[tuple[float, float]], t0: float, t1: float,
          x0: float, width: float, baseline: float, height: float,
          vmax: float) -> tuple[str, str]:
    """
    Construit le tracé de la courbe et celui de l'aire sous la courbe.

    L'échelle verticale est propre à chaque série : on compare des formes,
    pas des grandeurs. Mettre des Mo et des ms sur une échelle commune
    n'aurait aucun sens, et la seule question qui compte ici est « quand
    est-ce que ça bouge ».
    """
    if not points or t1 <= t0:
        return "", ""
    span = t1 - t0
    vmax = max(vmax, 1e-9)

    coords = []
    for ts, value in points:
        x = x0 + (ts - t0) / span * width
        y = baseline - min(1.0, value / vmax) * height
        coords.append((x, y))

    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    area = (f"M {coords[0][0]:.1f} {baseline:.1f} L "
            + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
            + f" L {coords[-1][0]:.1f} {baseline:.1f} Z")
    return line, area


def _marker(x: float, top: float, bottom: float, kind: str, label: str) -> str:
    """
    Un événement du système, posé sur l'axe du temps.

    Trois natures, trois traitements visuels distincts : ce qui s'est fait
    tout seul, ce qui attend une décision humaine, ce qui a été refusé.
    L'ambre reste réservé à l'attente — sa présence signifie toujours
    « on attend quelque chose de toi ».
    """
    colour = {"auto": PALETTE["ok"], "attente": PALETTE["decide"],
              "refus": PALETTE["degraded"], "detect": PALETTE["signal"]}.get(kind, PALETTE["dim"])
    opacity = "0.9" if kind == "attente" else "0.45"
    glyph = {"auto": "●", "attente": "◆", "refus": "✕", "detect": "│"}.get(kind, "·")

    return f"""
    <g class="mk mk-{kind}">
      <line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}"
            stroke="{colour}" stroke-width="1" stroke-opacity="{opacity}"
            stroke-dasharray="{'0' if kind == 'attente' else '2 3'}"/>
      <text x="{x:.1f}" y="{bottom + 13}" fill="{colour}" font-size="9"
            text-anchor="middle" font-family="IBM Plex Mono">{glyph}</text>
      <title>{html.escape(label)}</title>
    </g>"""


def render_timeline(prometheus_url: str, decisions: list[dict],
                    incidents: list[dict], minutes: int = 60,
                    width: int = 1180) -> str:
    """
    Assemble la frise complète : courbes réelles + événements superposés.

    Les incidents injectés apparaissent comme des bandes de fond : on voit
    ainsi si le système a réagi PENDANT la panne, après, ou pas du tout.
    C'est la lecture la plus directe de sa réactivité, et elle ne demande
    aucun chiffre.
    """
    t1 = time.time()
    t0 = t1 - minutes * 60

    row_h, gap, pad_l, pad_t = 32, 15, 78, 16
    height = pad_t + len(SERIES) * (row_h + gap) + 34
    plot_w = width - pad_l - 24

    series_data = []
    for key, libelle, promql, unite in SERIES:
        pts = [p for p in fetch_range(prometheus_url, promql, minutes) if p[0] >= t0]
        series_data.append((key, libelle, unite, pts))

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'style="display:block" xmlns="http://www.w3.org/2000/svg">']

    # Bandes de fond : durée réelle de chaque panne injectée.
    for inc in incidents:
        s, e = float(inc.get("start_ts", 0)), float(inc.get("end_ts", 0) or 0)
        if e <= t0 or s >= t1:
            continue
        xs = pad_l + max(0.0, (s - t0) / (t1 - t0)) * plot_w
        xe = pad_l + min(1.0, (max(e, s + 20) - t0) / (t1 - t0)) * plot_w
        parts.append(f'<rect x="{xs:.1f}" y="{pad_t}" width="{max(2, xe - xs):.1f}" '
                     f'height="{len(SERIES) * (row_h + gap)}" fill="#1B2340" '
                     f'fill-opacity="0.55"><title>{html.escape(inc.get("type", ""))}</title></rect>')

    # Courbes, une par métrique.
    for index, (key, libelle, unite, pts) in enumerate(series_data):
        top = pad_t + index * (row_h + gap)
        baseline = top + row_h
        parts.append(f'<line x1="{pad_l}" y1="{baseline:.1f}" x2="{width - 24}" '
                     f'y2="{baseline:.1f}" stroke="{PALETTE["rule"]}" stroke-width="1" '
                     f'stroke-opacity="0.35"/>')
        parts.append(f'<text x="{pad_l - 12}" y="{baseline - 2:.1f}" fill="{PALETTE["faint"]}" '
                     f'font-size="10" text-anchor="end" font-family="IBM Plex Mono">{libelle}</text>')

        if not pts:
            parts.append(f'<text x="{pad_l + 8}" y="{baseline - 11:.1f}" '
                         f'fill="{PALETTE["faint"]}" font-size="9.5" '
                         f'font-family="IBM Plex Mono" opacity=".6">aucune donnée</text>')
            continue

        vmax = max(v for _, v in pts)
        if vmax <= 0:
            parts.append(f'<line x1="{pad_l}" y1="{baseline - 1:.1f}" x2="{width - 24}" '
                         f'y2="{baseline - 1:.1f}" stroke="{PALETTE["signal"]}" '
                         f'stroke-width="1.5" stroke-opacity="0.28"/>')
            continue

        line, area = _path(pts, t0, t1, pad_l, plot_w, baseline, row_h - 9, vmax)
        parts.append(f'<path d="{area}" fill="url(#grad)" fill-opacity="0.5"/>')
        parts.append(f'<path d="{line}" fill="none" stroke="{PALETTE["signal"]}" '
                     f'stroke-width="1.6" stroke-linejoin="round"/>')
        parts.append(f'<text x="{width - 26}" y="{baseline - row_h + 9:.1f}" '
                     f'fill="{PALETTE["faint"]}" font-size="9" text-anchor="end" '
                     f'font-family="IBM Plex Mono">{vmax:.0f} {unite}</text>')

    # Couloir des événements : les marqueurs traversent les courbes pour
    # qu'on lise la coïncidence temporelle, mais leurs symboles vivent dans
    # une bande à part, sous un filet — sinon ils se confondent avec les
    # données qu'ils commentent.
    mtop = pad_t
    mbot = pad_t + len(SERIES) * (row_h + gap) - gap + 4
    parts.append(f'<line x1="{pad_l}" y1="{mbot:.1f}" x2="{width - 24}" y2="{mbot:.1f}" '
                 f'stroke="{PALETTE["rule"]}" stroke-width="1"/>')
    for d in decisions:
        ts = float(d.get("ts", 0) or 0)
        if not (t0 <= ts <= t1):
            continue
        g = d.get("guardrail_decision") or {}
        v = d.get("arbiter_verdict") or {}
        kind = {"autoriser_auto": "auto", "validation_humaine": "attente",
                "refuser": "refus"}.get(g.get("decision", ""), "detect")
        x = pad_l + (ts - t0) / (t1 - t0) * plot_w
        conf = v.get("final_confidence")
        label = (f"{v.get('composant_suspecte', '?')} · confiance "
                 f"{conf:.2f} · {g.get('decision', '?')}" if conf is not None
                 else str(g.get("decision", "?")))
        parts.append(_marker(x, mtop, mbot, kind, label))

    # Repères horaires.
    for frac in (0, .25, .5, .75, 1):
        x = pad_l + frac * plot_w
        mins = int(minutes * (1 - frac))
        lab = "maintenant" if mins == 0 else f"-{mins} min"
        parts.append(f'<text x="{x:.1f}" y="{height - 6}" fill="{PALETTE["faint"]}" '
                     f'font-size="9" text-anchor="{"end" if frac == 1 else "middle"}" '
                     f'font-family="IBM Plex Mono">{lab}</text>')

    parts.append(f"""<defs><linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{PALETTE['signal']}" stop-opacity="0.34"/>
      <stop offset="100%" stop-color="{PALETTE['signal']}" stop-opacity="0.02"/>
    </linearGradient></defs>""")
    parts.append("</svg>")
    return "".join(parts)


def narrate(decisions: list[dict], incidents: list[dict], minutes: int = 60) -> str:
    """
    Une phrase décrivant l'heure écoulée.

    Un chiffre seul ne dit pas si la situation est normale. « 4 pannes,
    3 traitées seul, 1 vous attend » se lit d'un coup et situe l'opérateur
    avant même qu'il ait regardé une courbe.
    """
    t0 = time.time() - minutes * 60
    recents = [d for d in decisions if float(d.get("ts", 0) or 0) >= t0]
    inc = [i for i in incidents if float(i.get("start_ts", 0) or 0) >= t0]

    if not recents and not inc:
        return "Rien ne s'est produit sur la dernière heure. Le système observe."

    auto = sum(1 for d in recents
               if (d.get("guardrail_decision") or {}).get("decision") == "autoriser_auto")
    attente = sum(1 for d in recents
                  if (d.get("guardrail_decision") or {}).get("decision") == "validation_humaine")

    morceaux = []
    if inc:
        morceaux.append(f"{len(inc)} panne{'s' if len(inc) > 1 else ''} injectée"
                        f"{'s' if len(inc) > 1 else ''}")
    if recents:
        morceaux.append(f"{len(recents)} diagnostic{'s' if len(recents) > 1 else ''}")
    if auto:
        morceaux.append(f"{auto} action{'s' if auto > 1 else ''} exécutée"
                        f"{'s' if auto > 1 else ''} sans intervention")
    if attente:
        morceaux.append(f"{attente} en attente de vous")
    return " · ".join(morceaux) + "."