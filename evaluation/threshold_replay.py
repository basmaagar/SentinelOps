"""
Rejeu contrefactuel des seuils — que se serait-il passé avec un autre réglage ?

La question à laquelle ce script répond
---------------------------------------
Le seuil de confiance qui autorise une action automatique est un choix.
Jusqu'ici, à la question « pourquoi 0.80 et pas 0.70 ? », la seule réponse
possible était « c'est un choix documenté ». Ce script transforme ce choix
en arbitrage chiffré.

Ce que le seuil détermine RÉELLEMENT
------------------------------------
Une précision importante, car le sujet prête à confusion : le seuil ne fait
pas basculer le système entre prévention et diagnostic. Le système est
purement RÉACTIF — la détection repose sur un écart déjà constaté, il n'y a
aucune prédiction.

Ce que le seuil fait basculer, c'est le partage entre AUTONOMIE et
PRUDENCE :

  - seuil bas  -> le système agit souvent seul, et se trompe plus souvent
  - seuil haut -> il se trompe rarement, mais renvoie à l'humain des
                  décisions qui étaient correctes

Il existe donc bien un point de bascule, mais entre ces deux régimes-là. Ce
script le localise et le chiffre.

Pourquoi le rejeu est exact et non estimé
-----------------------------------------
Aucune inférence n'est refaite. Pour chaque décision passée, on connaît
déjà le score de confiance, le risque intrinsèque de l'action, et — grâce à
la vérité terrain — si le diagnostic était correct. Déplacer le seuil ne
change que la frontière de décision. Le résultat n'est donc pas une
projection : c'est ce qui se serait produit.

Usage :  python evaluation/threshold_replay.py
"""

import sys
import json
import pathlib
import argparse

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.append(str(pathlib.Path(__file__).resolve().parent))

from dataset import charger, diagnostiquees, resume_effectifs  # noqa: E402

GROUND_TRUTH_PATH = _ROOT / "injectors" / "ground_truth.jsonl"
DECISIONS_PATH = _ROOT / "guardrails" / "decisions_log.jsonl"
SORTIE_JSON = _ROOT / "evaluation" / "threshold_replay.json"
SORTIE_PNG = _ROOT / "evaluation" / "threshold_replay.png"

ATTRIBUTION_WINDOW = 240

EXPECTED_COMPONENT = {
    "disk_saturation": "target-app", "saturation_disque": "target-app",
    "memory_leak": "target-app", "fuite_memoire": "target-app",
    "latency_injection": "dependency-service",
    "latence_dependance": "dependency-service",
}

# Écart entre le seuil des actions à risque faible et celui des actions à
# risque modéré, tel que défini dans les profils de risque (0.60 / 0.80 en
# production, 0.45 / 0.65 en évaluation). On déplace les deux ensemble pour
# préserver la structure de la politique : ce qu'on fait varier, c'est le
# niveau global d'exigence, pas la hiérarchie entre les actions.
ECART_RISQUE = 0.20

# Seuils de référence, pour situer les profils actuels sur la courbe.
PROFILS = {"production": 0.80, "evaluation": 0.65}


def rejouer(jeu: list[dict], seuil_modere: float) -> dict:
    """
    Rejoue toutes les décisions sous un seuil donné.

    On reproduit fidèlement la règle du garde-fou, y compris le passage
    forcé en validation humaine sur désaccord : sans cela, on mesurerait
    une politique qui n'est pas celle du système.
    """
    seuils = {"faible": max(0.0, seuil_modere - ECART_RISQUE),
              "modere": seuil_modere, "eleve": 1.01}

    auto_juste = auto_faux = humain_juste = humain_faux = 0
    for d in jeu:
        seuil = seuils.get(d["risque"], seuil_modere)
        # Une décision sans action candidate ne peut jamais s'exécuter
        # automatiquement, quel que soit le seuil : il n'y a rien à faire.
        automatique = (d.get("action_possible", True)
                       and not d["desaccord"]
                       and d["confiance"] >= seuil)
        if automatique:
            if d["correct"]:
                auto_juste += 1
            else:
                auto_faux += 1
        else:
            if d["correct"]:
                humain_juste += 1
            else:
                humain_faux += 1

    total = len(jeu) or 1
    auto = auto_juste + auto_faux
    return {
        "seuil": round(seuil_modere, 3),
        "total": len(jeu),
        "auto": auto,
        "auto_juste": auto_juste,
        # Actions exécutées seules sur un diagnostic FAUX : c'est le coût
        # de l'autonomie, et la seule catégorie réellement dommageable.
        "auto_faux": auto_faux,
        "humain": humain_juste + humain_faux,
        # Décisions correctes renvoyées à un humain : c'est le coût de la
        # prudence — une occasion d'automatisation manquée.
        "humain_juste": humain_juste,
        "humain_faux": humain_faux,
        "taux_automatisation": round(auto / total, 4),
        "precision_auto": round(auto_juste / auto, 4) if auto else None,
    }


def point_de_bascule(courbe: list[dict]) -> dict:
    """
    Localise le seuil au-delà duquel plus aucune action erronée n'est
    exécutée automatiquement.

    C'est la lecture la plus directe de l'arbitrage : en deçà, le système
    agit à tort ; au-delà, il ne se trompe plus mais renonce à des actions
    correctes. Le coût de ce renoncement est chiffré par `humain_juste`.
    """
    sans_erreur = [p for p in courbe if p["auto_faux"] == 0]
    if not sans_erreur:
        return {"existe": False}

    premier = min(sans_erreur, key=lambda p: p["seuil"])
    # Le seuil qui maximise les actions justes tout en n'en ratant aucune.
    return {
        "existe": True,
        "seuil": premier["seuil"],
        "actions_auto": premier["auto"],
        "occasions_manquees": premier["humain_juste"],
    }


def tracer(courbe: list[dict], bascule: dict, chemin: pathlib.Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    seuils = [p["seuil"] for p in courbe]
    fig, ax = plt.subplots(figsize=(7.2, 4.8), dpi=140)
    fig.patch.set_facecolor("white")

    ax.plot(seuils, [p["auto"] for p in courbe], color="#3D6FD9",
            linewidth=2, label="actions exécutées seules")
    ax.plot(seuils, [p["auto_faux"] for p in courbe], color="#D95F7B",
            linewidth=2, label="dont erronées")
    ax.plot(seuils, [p["humain_juste"] for p in courbe], color="#E8A33D",
            linewidth=1.8, linestyle="--",
            label="correctes renvoyées à l'humain")

    for nom, valeur in PROFILS.items():
        ax.axvline(valeur, color="#999999", linewidth=1, linestyle=":")
        ax.text(valeur, ax.get_ylim()[1] * 0.96, f" {nom}", fontsize=8,
                color="#666666", rotation=90, va="top")

    if bascule.get("existe"):
        ax.axvline(bascule["seuil"], color="#2E9E6B", linewidth=1.4)
        ax.text(bascule["seuil"], ax.get_ylim()[1] * 0.55,
                f"  zéro erreur\n  à partir de {bascule['seuil']:.2f}",
                fontsize=8.5, color="#2E9E6B")

    ax.set_xlabel("Seuil de confiance exigé pour une action de risque modéré",
                  fontsize=10)
    ax.set_ylabel("Nombre de décisions", fontsize=10)
    ax.set_title("Arbitrage autonomie / prudence\n"
                 f"Rejeu de {courbe[0]['total']} décisions déjà prises",
                 fontsize=11, pad=12)
    ax.grid(alpha=0.22, linewidth=0.6)
    ax.legend(fontsize=8.5, loc="upper right")
    fig.tight_layout()
    fig.savefig(chemin)
    plt.close(fig)
    return True


def main() -> None:
    parseur = argparse.ArgumentParser(description="Rejeu contrefactuel des seuils")
    parseur.add_argument("--decisions", default=str(DECISIONS_PATH))
    parseur.add_argument("--ground-truth", default=str(GROUND_TRUTH_PATH))
    arguments = parseur.parse_args()

    appariees, non_detectes = charger(pathlib.Path(arguments.decisions),
                                      pathlib.Path(arguments.ground_truth))
    effectifs = resume_effectifs(appariees, non_detectes)
    utiles = diagnostiquees(appariees)

    # CORRECTIF d'un biais de sélection. La version initiale écartait les
    # décisions sans action candidate — or ce sont précisément celles dont
    # le composant n'a pas été reconnu, c'est-à-dire les diagnostics ratés.
    # Le rejeu ne voyait donc que les bons cas et affichait 100 % de
    # précision à tous les seuils, ce qui rendait la courbe inutile.
    #
    # Elles sont désormais conservées et traitées comme ce qu'elles sont :
    # des diagnostics incorrects qui, faute de cible, n'auraient de toute
    # façon pas déclenché d'action. Elles pèsent donc dans le dénominateur
    # sans jamais compter comme action automatique.
    jeu = [{"confiance": d.confiance,
            "correct": d.correct,
            "risque": d.risque,
            "desaccord": d.statut_accord == "desaccord",
            "action_possible": d.action is not None,
            "type": d.type_panne} for d in utiles]

    print(f"\nEffectifs : {effectifs['incidents_injectes']} incidents injectés · "
          f"{effectifs['diagnostiquees']} diagnostiqués · "
          f"{effectifs['analyses_en_echec']} analyses en échec · "
          f"{effectifs['non_detectes']} non détectés")
    sans_cible = sum(1 for d in jeu if not d["action_possible"])
    if sans_cible:
        print(f"Dont {sans_cible} sans action candidate (composant non reconnu) : "
              f"comptés comme non automatisables.")

    if len(jeu) < 5:
        print(f"\nSeulement {len(jeu)} diagnostic(s) exploitable(s) — insuffisant.")
        return

    courbe = [rejouer(jeu, s / 100) for s in range(40, 101, 5)]
    bascule = point_de_bascule(courbe)

    print(f"\nREJEU CONTREFACTUEL — {len(jeu)} décisions rejouées\n")
    print("Ce que le seuil fait varier : la part d'autonomie du système.")
    print("Aucune inférence n'est refaite ; seule la frontière de décision bouge.\n")
    print(f"{'seuil':>7}{'auto':>7}{'justes':>8}{'erronées':>10}"
          f"{'humain':>8}{'dont correctes':>16}{'précision':>11}")
    print("-" * 70)
    for p in courbe:
        precision = f"{p['precision_auto']:.0%}" if p["precision_auto"] is not None else "—"
        repere = ""
        for nom, valeur in PROFILS.items():
            if abs(p["seuil"] - valeur) < 0.001:
                repere = f"  <- {nom}"
        print(f"{p['seuil']:>7.2f}{p['auto']:>7}{p['auto_juste']:>8}"
              f"{p['auto_faux']:>10}{p['humain']:>8}{p['humain_juste']:>16}"
              f"{precision:>11}{repere}")

    print("\nLECTURE")
    bas, haut = courbe[0], courbe[-1]
    print(f"  Au seuil le plus permissif ({bas['seuil']:.2f}) : {bas['auto']} actions "
          f"automatiques, dont {bas['auto_faux']} sur un diagnostic faux.")
    print(f"  Au seuil le plus strict ({haut['seuil']:.2f})   : {haut['auto']} actions "
          f"automatiques, mais {haut['humain_juste']} décisions correctes")
    print("  renvoyées inutilement à un humain.")

    if bascule.get("existe"):
        print(f"\n  POINT DE BASCULE : à partir de {bascule['seuil']:.2f}, plus aucune")
        print("  action erronée n'est exécutée automatiquement. Ce seuil coûte")
        print(f"  {bascule['occasions_manquees']} occasion(s) d'automatisation, "
              f"pour {bascule['actions_auto']} action(s) exécutée(s) seule(s).")
    else:
        print("\n  Aucun seuil de la plage étudiée n'élimine complètement les")
        print("  actions erronées — le score ne sépare pas suffisamment les")
        print("  diagnostics corrects des incorrects sur cet échantillon.")

    for nom, valeur in PROFILS.items():
        proche = min(courbe, key=lambda p: abs(p["seuil"] - valeur))
        print(f"\n  Profil {nom} (seuil {valeur:.2f}) : {proche['auto']} actions "
              f"automatiques, {proche['auto_faux']} erronée(s),")
        print(f"  {proche['humain_juste']} décision(s) correcte(s) renvoyée(s) à l'humain.")

    if len(jeu) < 50:
        print(f"\n  Réserve : {len(jeu)} décisions seulement. Ces chiffres indiquent")
        print("  une tendance, pas une mesure établie.")

    SORTIE_JSON.write_text(
        json.dumps({"effectif": len(jeu), "point_de_bascule": bascule,
                    "courbe": courbe}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"\nDétail écrit dans {SORTIE_JSON.name}")

    if tracer(courbe, bascule, SORTIE_PNG):
        print(f"Figure écrite dans {SORTIE_PNG.name}")
    else:
        print("Figure non produite — « pip install matplotlib » pour l'activer")


if __name__ == "__main__":
    main()