"""
Métriques d'évaluation — précision, temps de diagnostic, hallucination.

L'appariement entre décisions et incidents n'est PAS implémenté ici : il
provient de `dataset.py`, source unique partagée avec les analyses de
calibration et de rejeu contrefactuel.

C'est une correction importante. Les trois analyses avaient chacune leur
propre logique d'appariement et produisaient trois effectifs différents sur
les mêmes journaux — 23, 29 et 21 décisions selon le script. Des chiffres
incompatibles issus des mêmes données discréditent l'ensemble d'une
évaluation, quelle que soit la qualité de chaque mesure prise isolément.

Trois catégories rapportées séparément
--------------------------------------
  DIAGNOSTIQUÉS  — le système a produit un diagnostic exploitable ;
                   c'est la seule population sur laquelle une précision
                   a un sens.
  EN ÉCHEC       — l'appel au modèle a échoué, le repli s'est appliqué ;
                   ce n'est pas un mauvais diagnostic mais son absence.
  NON DÉTECTÉS   — aucune décision produite ; relève de la couverture.

Les confondre produirait un chiffre qui ne mesure rien de précis.

Usage :  python evaluation/compute_metrics.py
"""

import sys
import json
import pathlib
import argparse
import statistics

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.append(str(pathlib.Path(__file__).resolve().parent))

from dataset import charger, diagnostiquees, resume_effectifs, en_dict  # noqa: E402

GROUND_TRUTH_PATH = _ROOT / "injectors" / "ground_truth.jsonl"
DECISIONS_PATH = _ROOT / "guardrails" / "decisions_log.jsonl"
SORTIE = _ROOT / "evaluation" / "evaluation_results.json"


def calculer(appariees, non_detectes) -> dict:
    effectifs = resume_effectifs(appariees, non_detectes)
    utiles = diagnostiquees(appariees)
    corrects = [d for d in utiles if d.correct]
    ttds = [d.ttd for d in utiles]

    preuves_totales = sum(d.preuves_totales for d in utiles)
    preuves_ancrees = sum(d.preuves_ancrees for d in utiles)

    total_injectes = effectifs["incidents_injectes"] or 1

    # Précision calculée sur les incidents DIAGNOSTIQUÉS. Un incident non
    # détecté, ou dont l'analyse a échoué, ne relève pas d'une erreur de
    # diagnostic : les mélanger produirait un chiffre qui ne mesure ni la
    # qualité du diagnostic ni la couverture.
    precision = (len(corrects) / len(utiles)) if utiles else None

    par_type: dict[str, dict] = {}
    for d in appariees:
        seau = par_type.setdefault(d.type_panne,
                                   {"total": 0, "diagnostiques": 0, "corrects": 0,
                                    "en_echec": 0, "non_detectes": 0})
        seau["total"] += 1
        if d.en_echec:
            seau["en_echec"] += 1
        else:
            seau["diagnostiques"] += 1
            seau["corrects"] += int(d.correct)
    for incident in non_detectes:
        seau = par_type.setdefault(incident.get("type", "?"),
                                   {"total": 0, "diagnostiques": 0, "corrects": 0,
                                    "en_echec": 0, "non_detectes": 0})
        seau["total"] += 1
        seau["non_detectes"] += 1

    return {
        **effectifs,
        "couverture": round(len(utiles) / total_injectes, 4),
        "precision_diagnostic": round(precision, 4) if precision is not None else None,
        "objectif_precision": 0.70,
        "ttd_median_s": round(statistics.median(ttds), 2) if ttds else None,
        "ttd_moyen_s": round(statistics.fmean(ttds), 2) if ttds else None,
        "ttd_max_s": round(max(ttds), 2) if ttds else None,
        "ttd_sous_60s": (round(sum(1 for t in ttds if t < 60) / len(ttds), 4)
                         if ttds else None),
        "objectif_ttd_s": 60,
        "preuves_citees": preuves_totales,
        "preuves_ancrees": preuves_ancrees,
        "taux_hallucination": (round(1 - preuves_ancrees / preuves_totales, 4)
                               if preuves_totales else None),
        "actions_executees": sum(1 for d in utiles if d.action_executee),
        "validations_humaines": sum(1 for d in utiles
                                    if d.decision_garde_fou == "validation_humaine"),
        "par_type": par_type,
    }


def main() -> None:
    parseur = argparse.ArgumentParser(description="Métriques d'évaluation SentinelOps")
    parseur.add_argument("--decisions", default=str(DECISIONS_PATH))
    parseur.add_argument("--ground-truth", default=str(GROUND_TRUTH_PATH))
    arguments = parseur.parse_args()

    appariees, non_detectes = charger(pathlib.Path(arguments.decisions),
                                      pathlib.Path(arguments.ground_truth))
    resultats = calculer(appariees, non_detectes)

    print(json.dumps(resultats, indent=2, ensure_ascii=False))

    if resultats["analyses_en_echec"]:
        print(f"\n[!] {resultats['analyses_en_echec']} analyse(s) en échec écartée(s)")
        print("    du calcul de précision : le modèle n'a pas répondu, il ne")
        print("    s'agit donc pas d'un diagnostic erroné mais de son absence.")

    if resultats["non_detectes"]:
        part = resultats["non_detectes"] / (resultats["incidents_injectes"] or 1)
        print(f"\n[!] {resultats['non_detectes']} incident(s) non détecté(s) "
              f"({part:.0%}).")
        print("    Relève de la couverture, pas de la précision. Une couverture")
        print("    faible après une longue campagne signale généralement une")
        print("    contamination de la fenêtre glissante — redémarrer la boucle.")

    with SORTIE.open("w", encoding="utf-8") as f:
        json.dump({**resultats, "detail": [en_dict(d) for d in appariees]},
                  f, indent=2, ensure_ascii=False)
    print(f"\nDétail complet écrit dans {SORTIE}")


if __name__ == "__main__":
    main()