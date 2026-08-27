"""
Calibration du score de confiance — analyse post-campagne.

La question à laquelle ce script répond
---------------------------------------
Le score de confiance est calculable et auditable : chaque terme est
journalisé, un humain peut refaire l'opération. Mais cela ne dit rien de
sa VALEUR. Un score peut être parfaitement transparent et parfaitement
inutile s'il ne correspond à rien dans la réalité.

La question est donc : **quand le système annonce 0.70, a-t-il raison
70 % du temps ?**

Un système bien calibré permet de choisir un seuil en connaissance de
cause. Un système sur-confiant — qui annonce 0.80 alors qu'il a raison
50 % du temps — déclenche des actions automatiques sur des diagnostics
faux, et c'est le mode de défaillance le plus dangereux pour un système
d'auto-remédiation.

Cette analyse n'est possible que parce qu'on dispose d'une vérité terrain
indépendante : `ground_truth.jsonl` est écrit par l'injecteur AVANT que le
système n'observe quoi que ce soit. La boucle de supervision ne lit jamais
ce fichier et ignore qu'une campagne est en cours.

Ce que le script produit
------------------------
  - un diagramme de fiabilité (PNG), directement utilisable dans le rapport
  - le score de Brier et sa décomposition
  - l'erreur de calibration attendue (ECE) et maximale (MCE)
  - un fichier JSON avec le détail par bande

Usage :  python evaluation/calibration.py
"""

import sys
import json
import math
import pathlib
import argparse

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.append(str(pathlib.Path(__file__).resolve().parent))

from dataset import charger, diagnostiquees, resume_effectifs  # noqa: E402

GROUND_TRUTH_PATH = _ROOT / "injectors" / "ground_truth.jsonl"
DECISIONS_PATH = _ROOT / "guardrails" / "decisions_log.jsonl"
SORTIE_JSON = _ROOT / "evaluation" / "calibration_results.json"
SORTIE_PNG = _ROOT / "evaluation" / "calibration.png"

ATTRIBUTION_WINDOW = 240

EXPECTED_COMPONENT = {
    "disk_saturation": "target-app", "saturation_disque": "target-app",
    "memory_leak": "target-app", "fuite_memoire": "target-app",
    "latency_injection": "dependency-service",
    "latence_dependance": "dependency-service",
}


# ---------------------------------------------------------------------------
# Mesures de calibration
# ---------------------------------------------------------------------------
BANDES = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.001)]


def intervalle_wilson(succes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """
    Intervalle de confiance de Wilson à 95 %.

    Sur de petits effectifs — quelques incidents par bande — une proportion
    brute est très instable : 2 succès sur 3 vaut 67 %, mais un seul cas de
    plus la déplace de 17 points. Afficher cette proportion sans son
    incertitude donnerait une fausse impression de précision.

    Wilson plutôt que l'approximation normale : celle-ci se comporte mal
    aux extrêmes (proportions proches de 0 ou 1) et peut produire des
    bornes hors de [0, 1], ce qui est précisément le régime attendu ici.
    """
    if total == 0:
        return (0.0, 1.0)
    p = succes / total
    denominateur = 1 + z ** 2 / total
    centre = (p + z ** 2 / (2 * total)) / denominateur
    marge = z * math.sqrt(p * (1 - p) / total + z ** 2 / (4 * total ** 2)) / denominateur
    return (max(0.0, centre - marge), min(1.0, centre + marge))


def diagramme_fiabilite(jeu: list[dict]) -> list[dict]:
    bandes = []
    for bas, haut in BANDES:
        dedans = [d for d in jeu if bas <= d["confiance"] < haut]
        n = len(dedans)
        succes = sum(1 for d in dedans if d["correct"])
        confiance_moyenne = (sum(d["confiance"] for d in dedans) / n) if n else None
        bandes.append({
            "bande": f"[{bas:.1f} – {min(haut, 1.0):.1f}[",
            "centre": (bas + min(haut, 1.0)) / 2,
            "effectif": n,
            "confiance_moyenne": confiance_moyenne,
            "exactitude": (succes / n) if n else None,
            "ic95": intervalle_wilson(succes, n) if n else None,
            # Positif = sur-confiance (le système promet plus qu'il ne tient).
            "ecart": (confiance_moyenne - succes / n) if n else None,
        })
    return bandes


def score_brier(jeu: list[dict]) -> float | None:
    """
    Erreur quadratique moyenne entre confiance annoncée et issue réelle.

    Repères : 0.0 est parfait ; 0.25 correspond à annoncer 0.5 partout,
    c'est-à-dire à n'apporter aucune information. Au-delà de 0.25, le score
    est activement trompeur — mieux vaudrait ne rien annoncer.
    """
    if not jeu:
        return None
    return sum((d["confiance"] - (1.0 if d["correct"] else 0.0)) ** 2
               for d in jeu) / len(jeu)


def decomposition_brier(jeu: list[dict], bandes: list[dict]) -> dict:
    """
    Décomposition de Murphy : Brier = fiabilité − résolution + incertitude.

    Ces trois termes répondent à trois questions distinctes, là où le score
    global les confond :

      FIABILITÉ (à minimiser) — les probabilités annoncées correspondent-
      elles aux fréquences observées ? C'est la calibration au sens strict.

      RÉSOLUTION (à maximiser) — le score discrimine-t-il, c'est-à-dire
      s'écarte-t-il du taux de base ? Un système annonçant toujours la même
      valeur serait parfaitement fiable et totalement inutile ; seule la
      résolution le révèle.

      INCERTITUDE — la difficulté intrinsèque du problème, indépendante du
      système. Elle ne dépend que du taux de succès global.
    """
    n = len(jeu)
    if n == 0:
        return {}
    taux_base = sum(1 for d in jeu if d["correct"]) / n

    fiabilite = resolution = 0.0
    for b in bandes:
        if not b["effectif"]:
            continue
        poids = b["effectif"] / n
        fiabilite += poids * (b["confiance_moyenne"] - b["exactitude"]) ** 2
        resolution += poids * (b["exactitude"] - taux_base) ** 2

    return {
        "fiabilite": round(fiabilite, 4),
        "resolution": round(resolution, 4),
        "incertitude": round(taux_base * (1 - taux_base), 4),
        "taux_base": round(taux_base, 4),
    }


def erreurs_calibration(jeu: list[dict], bandes: list[dict]) -> dict:
    """
    ECE : écart moyen entre confiance et exactitude, pondéré par l'effectif.
    MCE : le pire écart observé sur une bande.

    L'ECE résume, la MCE alerte. Un ECE acceptable peut masquer une bande
    gravement sur-confiante — et si cette bande est celle qui déclenche les
    actions automatiques, c'est elle qui compte.
    """
    n = len(jeu) or 1
    ece = sum((b["effectif"] / n) * abs(b["ecart"])
              for b in bandes if b["effectif"])
    ecarts = [abs(b["ecart"]) for b in bandes if b["effectif"]]
    return {"ece": round(ece, 4), "mce": round(max(ecarts), 4) if ecarts else None}


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------
def tracer(bandes: list[dict], mesures: dict, chemin: pathlib.Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    points = [b for b in bandes if b["effectif"]]
    if not points:
        return False

    fig, ax = plt.subplots(figsize=(6.2, 5.4), dpi=140)
    fig.patch.set_facecolor("white")

    ax.plot([0, 1], [0, 1], "--", color="#999999", linewidth=1,
            label="calibration parfaite", zorder=1)

    x = [b["confiance_moyenne"] for b in points]
    y = [b["exactitude"] for b in points]

    # Barres d'incertitude : sans elles, un point calculé sur 3 incidents
    # aurait le même poids visuel qu'un point calculé sur 15.
    bas = [b["exactitude"] - b["ic95"][0] for b in points]
    haut = [b["ic95"][1] - b["exactitude"] for b in points]
    ax.errorbar(x, y, yerr=[bas, haut], fmt="none", ecolor="#B0C4E8",
                elinewidth=1.4, capsize=4, zorder=2)

    # La taille du point traduit l'effectif de la bande.
    tailles = [40 + b["effectif"] * 22 for b in points]
    couleurs = ["#D95F7B" if b["ecart"] > 0.10 else "#3D6FD9" for b in points]
    ax.scatter(x, y, s=tailles, c=couleurs, alpha=0.75, edgecolors="white",
               linewidths=1.4, zorder=3)
    ax.plot(x, y, color="#3D6FD9", linewidth=1.6, alpha=0.65, zorder=2)

    for b in points:
        ax.annotate(f"n={b['effectif']}",
                    (b["confiance_moyenne"], b["exactitude"]),
                    textcoords="offset points", xytext=(0, -20),
                    ha="center", fontsize=8, color="#666666")

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Confiance annoncée par le système", fontsize=10)
    ax.set_ylabel("Exactitude réellement observée", fontsize=10)
    ax.set_title("Calibration du score de confiance\n"
                 f"Brier {mesures['brier']:.3f} · ECE {mesures['ece']:.3f}"
                 f" · {mesures['effectif']} incidents",
                 fontsize=11, pad=12)
    ax.grid(alpha=0.22, linewidth=0.6)
    ax.legend(fontsize=8, loc="upper left")

    # Repère de lecture : sous la diagonale, le système promet plus qu'il
    # ne tient — c'est le régime qui déclenche des actions à tort.
    ax.fill_between([0, 1], [0, 1], [0, 0], color="#D95F7B", alpha=0.045)
    ax.text(0.72, 0.12, "zone de sur-confiance", fontsize=8,
            color="#B84A64", alpha=0.8)

    fig.tight_layout()
    fig.savefig(chemin)
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
def main() -> None:
    parseur = argparse.ArgumentParser(description="Analyse de calibration SentinelOps")
    parseur.add_argument("--decisions", default=str(DECISIONS_PATH))
    parseur.add_argument("--ground-truth", default=str(GROUND_TRUTH_PATH))
    arguments = parseur.parse_args()

    appariees, non_detectes = charger(pathlib.Path(arguments.decisions),
                                      pathlib.Path(arguments.ground_truth))
    effectifs = resume_effectifs(appariees, non_detectes)

    # Les analyses en échec sont ÉCARTÉES de la calibration. Une confiance
    # de 0.0 accompagnée d'un diagnostic de repli n'est pas une prédiction
    # mal calibrée : c'est l'absence de prédiction. Les inclure gonflerait
    # artificiellement la résolution — le score paraîtrait mieux discriminer
    # qu'il ne le fait — et fausserait l'ECE.
    utiles = diagnostiquees(appariees)
    jeu = [{"confiance": d.confiance, "correct": d.correct,
            "type": d.type_panne} for d in utiles]

    print(f"\nEffectifs : {effectifs['incidents_injectes']} incidents injectés · "
          f"{effectifs['diagnostiquees']} diagnostiqués · "
          f"{effectifs['analyses_en_echec']} analyses en échec · "
          f"{effectifs['non_detectes']} non détectés")
    print("La calibration ne porte que sur les incidents diagnostiqués.")

    if len(jeu) < 5:
        print(f"\nSeulement {len(jeu)} diagnostic(s) exploitable(s) — insuffisant.")
        return

    bandes = diagramme_fiabilite(jeu)
    brier = score_brier(jeu)
    decomposition = decomposition_brier(jeu, bandes)
    erreurs = erreurs_calibration(jeu, bandes)

    mesures = {"effectif": len(jeu), "brier": round(brier, 4), **erreurs,
               **decomposition, "bandes": bandes}

    # --- Restitution lisible ---
    print(f"\nCALIBRATION — {len(jeu)} décisions appariées à leur vérité terrain\n")
    print(f"{'bande':<16}{'n':>4}{'annoncé':>10}{'observé':>10}{'écart':>9}   IC 95 %")
    print("-" * 72)
    for b in bandes:
        if not b["effectif"]:
            print(f"{b['bande']:<16}{0:>4}{'—':>10}{'—':>10}{'—':>9}")
            continue
        bas, haut = b["ic95"]
        drapeau = "  sur-confiance" if b["ecart"] > 0.10 else ""
        print(f"{b['bande']:<16}{b['effectif']:>4}"
              f"{b['confiance_moyenne']:>10.2f}{b['exactitude']:>10.2f}"
              f"{b['ecart']:>+9.2f}   [{bas:.2f} – {haut:.2f}]{drapeau}")

    print(f"\nScore de Brier      {brier:.4f}   (0 = parfait, 0.25 = sans information)")
    print(f"  fiabilité         {decomposition['fiabilite']:.4f}   à minimiser — "
          f"écart entre promesse et réalité")
    print(f"  résolution        {decomposition['resolution']:.4f}   à maximiser — "
          f"capacité à discriminer")
    print(f"  incertitude       {decomposition['incertitude']:.4f}   difficulté "
          f"intrinsèque du problème")
    print(f"\nECE                 {erreurs['ece']:.4f}   erreur de calibration moyenne")
    print(f"MCE                 {erreurs['mce']:.4f}   pire bande")

    # --- Interprétation, en toutes lettres ---
    print("\nLECTURE")
    # Sur- et sous-confiance n'ont pas la même gravité, et l'ECE seul ne
    # les distingue pas : il mesure un écart en valeur absolue.
    #
    #   SUR-confiance  : le système promet plus qu'il ne tient. C'est le
    #                    régime dangereux — il déclenche des actions
    #                    automatiques sur des diagnostics faux.
    #   SOUS-confiance : il a raison plus souvent qu'il ne l'annonce. Le
    #                    coût est une prudence excessive : des décisions
    #                    correctes renvoyées inutilement à un humain.
    #
    # Rapporter les deux sous le même verdict « mal calibré » masquerait
    # cette différence, qui est justement celle qui compte.
    sur = [b for b in bandes if b["effectif"] and b["ecart"] > 0.10]
    sous = [b for b in bandes if b["effectif"] and b["ecart"] < -0.10]

    if not sur and not sous:
        print("  Le score est bien calibré : ce qu'il annonce correspond à")
        print("  ce qui se produit.")
    elif sur:
        pires = ", ".join(b["bande"] for b in sur)
        print(f"  SUR-CONFIANCE sur {pires}.")
        print("  Le système promet plus qu'il ne tient dans ces bandes. C'est le")
        print("  régime à risque, puisque c'est lui qui autorise les actions")
        print("  automatiques sur des diagnostics qui se révèlent faux.")
    else:
        pires = ", ".join(b["bande"] for b in sous)
        ecart_max = max(abs(b["ecart"]) for b in sous)
        print(f"  SOUS-CONFIANCE sur {pires} (jusqu'à {ecart_max:.2f}).")
        print("  Le système a raison plus souvent qu'il ne l'annonce. Ce n'est")
        print("  pas dangereux — aucune action erronée n'en découle — mais c'est")
        print("  coûteux : des diagnostics corrects sont renvoyés à un humain")
        print("  alors qu'ils auraient pu être traités seuls. Les seuils")
        print("  pourraient être abaissés sans perte de sûreté.")

    if decomposition["resolution"] < 0.02:
        print("\n  Résolution faible : le score discrimine peu entre les cas")
        print("  corrects et incorrects. Il est fiable mais peu informatif.")
    elif decomposition["resolution"] > decomposition["fiabilite"] * 2:
        print("\n  Résolution élevée par rapport à la fiabilité : le score sépare")
        print("  bien les cas corrects des incorrects, mais ses valeurs absolues")
        print("  sont décalées. Un simple recalage suffirait à le rendre lisible")
        print("  comme une probabilité.")

    if len(jeu) < 50:
        print(f"\n  Réserve : {len(jeu)} incidents seulement. Les intervalles de")
        print("  confiance ci-dessus sont larges, et ces chiffres doivent être")
        print("  lus comme une tendance et non comme une mesure établie.")

    SORTIE_JSON.write_text(json.dumps(mesures, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    print(f"\nDétail écrit dans {SORTIE_JSON.name}")

    if tracer(bandes, mesures, SORTIE_PNG):
        print(f"Figure écrite dans {SORTIE_PNG.name}")
    else:
        print("Figure non produite — « pip install matplotlib » pour l'activer")


if __name__ == "__main__":
    main()