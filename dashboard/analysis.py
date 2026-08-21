"""
Le système jugé par ses propres critères — calibration et contrefactuel.

Deux analyses que l'on ne trouve pas dans un tableau de bord d'exploitation,
parce qu'elles ne portent pas sur l'infrastructure mais sur le système
lui-même.

CALIBRATION — « quand il annonce 0.7, a-t-il raison 70 % du temps ? »
Un score de confiance peut être parfaitement calculable et parfaitement
inutile s'il ne correspond à rien. La calibration est la seule mesure qui
répond à la question, et elle n'est possible que parce qu'on dispose d'une
vérité terrain.

CONTREFACTUEL — « et si le seuil avait été différent ? »
Les décisions passées contiennent tout ce qu'il faut pour rejouer
l'histoire sous d'autres réglages : le score, le risque de l'action, et la
bonne réponse. Déplacer le seuil ne demande donc aucune nouvelle campagne,
seulement une reclassification. Le choix d'un seuil cesse d'être une
opinion pour devenir un arbitrage chiffré entre autonomie et erreurs.
"""

import html

PALETTE = {
    "ink": "#E4E8F7", "dim": "#7C87AD", "faint": "#4E5878", "rule": "#2A3454",
    "signal": "#7C9CF5", "decide": "#F0A93B", "degraded": "#E0637F", "ok": "#5FD3A6",
}

# Correspondance type de panne -> composant attendu, alignée sur la campagne.
EXPECTED = {
    "disk_saturation": "target-app", "saturation_disque": "target-app",
    "memory_leak": "target-app", "fuite_memoire": "target-app",
    "latency_injection": "dependency-service", "latence_dependance": "dependency-service",
}

ATTRIBUTION_WINDOW = 240


def build_dataset(decisions: list[dict], incidents: list[dict]) -> list[dict]:
    """
    Apparie chaque décision à l'incident qu'elle concerne, et détermine si
    le diagnostic était correct.

    L'appariement est temporel : la boucle de supervision ignore
    l'existence des incidents injectés, et c'est précisément ce qui rend la
    mesure honnête — le système n'a aucun moyen de connaître la réponse.
    """
    par_ts = sorted(((float(i.get("start_ts", 0) or 0), i) for i in incidents),
                    key=lambda p: p[0])
    dataset = []

    for d in decisions:
        ts = float(d.get("ts", 0) or 0)
        verdict = d.get("arbiter_verdict") or {}
        guardrail = d.get("guardrail_decision") or {}
        conf = verdict.get("final_confidence")
        if conf is None or not ts:
            continue

        # Incident le plus récent ayant commencé avant cette décision.
        cible = None
        for start, inc in par_ts:
            if start <= ts <= start + ATTRIBUTION_WINDOW:
                cible = inc
        if cible is None:
            continue

        attendu = EXPECTED.get(cible.get("type", ""), cible.get("composant_cible", ""))
        dataset.append({
            "ts": ts,
            "confiance": float(conf),
            "correct": verdict.get("composant_suspecte") == attendu,
            "risque": guardrail.get("intrinsic_risk", "modere"),
            "action": guardrail.get("action"),
            "decision": guardrail.get("decision"),
            "type": cible.get("type", "?"),
            "attendu": attendu,
            "obtenu": verdict.get("composant_suspecte"),
        })
    return dataset


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
BANDES = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]


def calibration(dataset: list[dict]) -> list[dict]:
    out = []
    for bas, haut in BANDES:
        dedans = [d for d in dataset if bas <= d["confiance"] < haut]
        out.append({
            "bande": f"{bas:.1f}–{min(haut, 1.0):.1f}",
            "centre": (bas + min(haut, 1.0)) / 2,
            "n": len(dedans),
            "exactitude": (sum(d["correct"] for d in dedans) / len(dedans)) if dedans else None,
        })
    return out


def brier_score(dataset: list[dict]) -> float | None:
    """
    Erreur quadratique moyenne entre confiance annoncée et issue réelle.
    Plus bas vaut mieux ; 0.25 correspond à une confiance constante de 0.5,
    c'est-à-dire à une absence totale d'information.
    """
    if not dataset:
        return None
    return sum((d["confiance"] - (1.0 if d["correct"] else 0.0)) ** 2
               for d in dataset) / len(dataset)


def render_calibration(dataset: list[dict], width: int = 470, height: int = 300) -> str:
    """
    Diagramme de fiabilité. La diagonale représente la calibration
    parfaite ; un point au-dessus signale un système trop modeste, en
    dessous un système trop sûr de lui. C'est cette seconde situation qui
    est dangereuse, puisque c'est elle qui déclenche des actions.
    """
    pad_l, pad_b, pad_t, pad_r = 44, 34, 14, 14
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">']

    for frac in (0, .25, .5, .75, 1):
        y = pad_t + (1 - frac) * ph
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + pw}" y2="{y:.1f}" '
                     f'stroke="{PALETTE["rule"]}" stroke-width="1" stroke-opacity="0.35"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 3:.1f}" fill="{PALETTE["faint"]}" '
                     f'font-size="9" text-anchor="end" font-family="IBM Plex Mono">{frac:.0%}</text>')

    parts.append(f'<line x1="{pad_l}" y1="{pad_t + ph}" x2="{pad_l + pw}" y2="{pad_t}" '
                 f'stroke="{PALETTE["dim"]}" stroke-width="1" stroke-dasharray="4 4" '
                 f'stroke-opacity="0.5"/>')
    parts.append(f'<text x="{pad_l + pw - 4}" y="{pad_t + 12}" fill="{PALETTE["faint"]}" '
                 f'font-size="9" text-anchor="end" font-family="IBM Plex Mono">'
                 f'calibration parfaite</text>')

    points = [b for b in calibration(dataset) if b["exactitude"] is not None]
    if points:
        coords = [(pad_l + b["centre"] * pw, pad_t + (1 - b["exactitude"]) * ph, b)
                  for b in points]
        parts.append('<path d="M ' + " L ".join(f"{x:.1f} {y:.1f}" for x, y, _ in coords)
                     + f'" fill="none" stroke="{PALETTE["signal"]}" stroke-width="1.8"/>')
        for x, y, b in coords:
            r = 3.5 + min(7.0, b["n"] * 0.55)
            trop_sur = b["exactitude"] < b["centre"] - 0.1
            couleur = PALETTE["degraded"] if trop_sur else PALETTE["signal"]
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{couleur}" '
                         f'fill-opacity="0.28" stroke="{couleur}" stroke-width="1.4">'
                         f'<title>confiance {b["bande"]} — exactitude '
                         f'{b["exactitude"]:.0%} sur {b["n"]} cas</title></circle>')
    else:
        parts.append(f'<text x="{pad_l + pw / 2}" y="{pad_t + ph / 2}" '
                     f'fill="{PALETTE["faint"]}" font-size="11" text-anchor="middle" '
                     f'font-family="IBM Plex Mono">données insuffisantes</text>')

    parts.append(f'<text x="{pad_l + pw / 2}" y="{height - 6}" fill="{PALETTE["faint"]}" '
                 f'font-size="9.5" text-anchor="middle" font-family="IBM Plex Mono">'
                 f'confiance annoncée →</text>')
    parts.append(f'<text x="12" y="{pad_t + ph / 2}" fill="{PALETTE["faint"]}" font-size="9.5" '
                 f'text-anchor="middle" font-family="IBM Plex Mono" '
                 f'transform="rotate(-90 12 {pad_t + ph / 2:.0f})">exactitude réelle →</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Contrefactuel
# ---------------------------------------------------------------------------
def replay(dataset: list[dict], seuil_faible: float, seuil_modere: float) -> dict:
    """
    Rejoue toutes les décisions passées sous d'autres seuils.

    Aucune inférence n'est refaite : les scores, les risques et les bonnes
    réponses sont déjà connus. Seule la frontière de décision se déplace.
    C'est ce qui rend l'exercice instantané et exact — ce n'est pas une
    estimation, c'est ce qui se serait produit.
    """
    seuils = {"faible": seuil_faible, "modere": seuil_modere, "eleve": 1.01}
    auto_correct = auto_faux = humain_correct = humain_faux = 0

    for d in dataset:
        seuil = seuils.get(d["risque"], seuil_modere)
        if d["confiance"] >= seuil:
            if d["correct"]:
                auto_correct += 1
            else:
                auto_faux += 1
        else:
            if d["correct"]:
                humain_correct += 1
            else:
                humain_faux += 1

    total = len(dataset) or 1
    return {
        "total": len(dataset),
        "auto": auto_correct + auto_faux,
        "auto_correct": auto_correct,
        "auto_faux": auto_faux,
        "humain": humain_correct + humain_faux,
        # Une décision correcte envoyée à un humain est une occasion
        # manquée d'automatisation : c'est le coût de la prudence.
        "humain_correct": humain_correct,
        "humain_faux": humain_faux,
        "taux_automatisation": (auto_correct + auto_faux) / total,
        "precision_auto": (auto_correct / (auto_correct + auto_faux))
                          if (auto_correct + auto_faux) else None,
    }


def render_tradeoff(dataset: list[dict], seuil_courant: float,
                    width: int = 470, height: int = 300) -> str:
    """
    Courbe du compromis : pour chaque seuil possible, combien d'actions
    automatiques et combien d'erreurs. Le seuil courant y est repéré.

    C'est la réponse définitive à « pourquoi ce seuil et pas un autre » :
    on ne discute plus d'un principe, on lit un arbitrage.
    """
    pad_l, pad_b, pad_t, pad_r = 40, 34, 14, 40
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">']

    if not dataset:
        parts.append(f'<text x="{width / 2}" y="{height / 2}" fill="{PALETTE["faint"]}" '
                     f'font-size="11" text-anchor="middle" font-family="IBM Plex Mono">'
                     f'aucune décision appariée</text></svg>')
        return "".join(parts)

    seuils = [i / 40 for i in range(16, 41)]      # 0.40 -> 1.00
    courbes = [(s, replay(dataset, s - 0.15, s)) for s in seuils]
    max_actions = max(max(r["auto"] for _, r in courbes), 1)

    def pt(s, valeur, maxi):
        x = pad_l + (s - seuils[0]) / (seuils[-1] - seuils[0]) * pw
        y = pad_t + (1 - valeur / max(maxi, 1)) * ph
        return x, y

    for frac in (0, .5, 1):
        y = pad_t + (1 - frac) * ph
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + pw}" y2="{y:.1f}" '
                     f'stroke="{PALETTE["rule"]}" stroke-width="1" stroke-opacity="0.3"/>')
        parts.append(f'<text x="{pad_l - 7}" y="{y + 3:.1f}" fill="{PALETTE["faint"]}" '
                     f'font-size="9" text-anchor="end" font-family="IBM Plex Mono">'
                     f'{int(frac * max_actions)}</text>')

    for cle, couleur, libelle in (("auto", PALETTE["signal"], "actions automatiques"),
                                  ("auto_faux", PALETTE["degraded"], "dont erronées")):
        coords = [pt(s, r[cle], max_actions) for s, r in courbes]
        parts.append('<path d="M ' + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
                     + f'" fill="none" stroke="{couleur}" stroke-width="1.9"/>')
        xe, ye = coords[-1]
        parts.append(f'<text x="{xe + 5:.1f}" y="{ye + 3:.1f}" fill="{couleur}" font-size="9" '
                     f'font-family="IBM Plex Mono">{libelle.split()[0]}</text>')

    x = pad_l + (min(max(seuil_courant, seuils[0]), seuils[-1]) - seuils[0]) \
        / (seuils[-1] - seuils[0]) * pw
    parts.append(f'<line x1="{x:.1f}" y1="{pad_t - 4}" x2="{x:.1f}" y2="{pad_t + ph}" '
                 f'stroke="{PALETTE["decide"]}" stroke-width="1.6"/>')
    parts.append(f'<text x="{x:.1f}" y="{pad_t - 7}" fill="{PALETTE["decide"]}" font-size="9" '
                 f'text-anchor="middle" font-family="IBM Plex Mono">seuil actuel</text>')

    for frac, s in ((0, seuils[0]), (0.5, (seuils[0] + seuils[-1]) / 2), (1, seuils[-1])):
        xx = pad_l + frac * pw
        parts.append(f'<text x="{xx:.1f}" y="{height - 6}" fill="{PALETTE["faint"]}" '
                     f'font-size="9" text-anchor="middle" font-family="IBM Plex Mono">'
                     f'{s:.2f}</text>')
    parts.append("</svg>")
    return "".join(parts)


def agent_scorecard(decisions: list[dict], incidents: list[dict]) -> dict:
    """
    Chaque enquêteur, séparément : combien de fois a-t-il désigné le bon
    composant, et à quel prix ?

    Cette mesure répond à la question posée depuis le début — l'architecture
    multi-agents est-elle justifiée ? Si un agent n'apporte rien, il faut
    pouvoir le dire, et le chiffrer.
    """
    par_ts = sorted(((float(i.get("start_ts", 0) or 0), i) for i in incidents),
                    key=lambda p: p[0])
    stats = {"metriques": {"interroge": 0, "correct": 0},
             "logs": {"interroge": 0, "correct": 0},
             "accord": {"total": 0, "correct": 0},
             "desaccord": {"total": 0, "correct": 0}}

    for d in decisions:
        ts = float(d.get("ts", 0) or 0)
        cible = None
        for start, inc in par_ts:
            if start <= ts <= start + ATTRIBUTION_WINDOW:
                cible = inc
        if cible is None:
            continue
        attendu = EXPECTED.get(cible.get("type", ""), cible.get("composant_cible", ""))

        for cle, champ in (("metriques", "metrics_hypothesis"), ("logs", "logs_hypothesis")):
            hyp = d.get(champ) or {}
            if hyp.get("evidence") and float(hyp.get("confidence") or 0) > 0:
                stats[cle]["interroge"] += 1

        verdict = d.get("arbiter_verdict") or {}
        statut = verdict.get("agreement_status")
        juste = verdict.get("composant_suspecte") == attendu
        if statut in ("accord", "desaccord"):
            stats[statut]["total"] += 1
            stats[statut]["correct"] += int(juste)

    return stats