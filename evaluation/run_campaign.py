"""
Campagne d'évaluation automatisée — Jour 13.

Rôle et périmètre
-----------------
Ce script DÉCLENCHE des injections de pannes selon un plan reproductible.
Il n'intervient à aucun moment dans la chaîne de décision : il n'appelle
ni les détecteurs, ni les agents, ni le garde-fou. La boucle de
supervision tourne en parallèle, exactement comme en usage normal, et
c'est elle qui détecte et diagnostique. Le script se contente d'injecter
puis d'attendre.

C'est une propriété importante pour la validité de l'évaluation : le code
évalué est le même que celui qui tourne hors campagne, et il ignore
totalement qu'une campagne est en cours.

Ce que le script apporte par rapport à des injections manuelles
--------------------------------------------------------------
  - REPRODUCTIBILITÉ : même plan, même ordre, mêmes délais, graine fixée.
    Quelqu'un d'autre peut rejouer la campagne et comparer ses chiffres.
  - INDÉPENDANCE DES INCIDENTS : un délai de retour au calme est imposé
    entre deux injections. Sans lui, une panne démarrerait alors que la
    fenêtre glissante du détecteur n'est pas revenue à son état normal, et
    les incidents s'influenceraient mutuellement.
  - EXHAUSTIVITÉ : tous les incidents sont journalisés, y compris ceux qui
    échouent. Sélectionner les incidents réussis serait le seul vrai biais
    à craindre ici ; c'est écarté par construction.

Limite à documenter dans le rapport
-----------------------------------
Les pannes sont injectées par un processus déterministe : les métriques
varient de façon nette et prévisible. Une infrastructure réelle produit
des dégradations progressives, bruitées, parfois simultanées. Les chiffres
obtenus sont donc OPTIMISTES par rapport à un déploiement réel. Cette
limite tient à la nature du banc d'essai, pas au script : elle serait
identique avec des injections manuelles.
"""

import sys
import time
import json
import random
import logging
import pathlib
import argparse
from dataclasses import dataclass, asdict

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.append(str(_ROOT / "injectors"))

import disk_saturation      # noqa: E402
import memory_leak          # noqa: E402
import latency_injection    # noqa: E402

logger = logging.getLogger("sentinelops.campagne")

CAMPAIGN_LOG_PATH = pathlib.Path(__file__).parent / "campaign_runs.jsonl"


@dataclass
class ScenarioSpec:
    """
    Un type de panne et ses paramètres.

    `composant_attendu` est la vérité terrain utilisée au moment du calcul
    de la précision : c'est le composant que le système DEVRAIT désigner.
    Il est fixé ici, à l'avance, et jamais dérivé de ce que le système a
    répondu — sans quoi la mesure n'aurait aucune valeur.
    """
    nom: str
    composant_attendu: str
    params: dict


SCENARIOS = [
    ScenarioSpec(
        nom="saturation_disque",
        composant_attendu="target-app",
        params={"size_mb": 200, "duration_seconds": 45},
    ),
    ScenarioSpec(
        nom="fuite_memoire",
        composant_attendu="target-app",
        params={"size_mb": 150, "duration_seconds": 45, "step_mb": 30},
    ),
    ScenarioSpec(
        nom="latence_dependance",
        composant_attendu="dependency-service",
        params={"extra_ms": 400, "duration_seconds": 45, "call_interval_seconds": 1.0},
    ),
]

RUNNERS = {
    "saturation_disque": disk_saturation.run,
    "fuite_memoire": memory_leak.run,
    "latence_dependance": latency_injection.run,
}


def build_plan(repetitions: int, seed: int = 42) -> list[ScenarioSpec]:
    """
    Construit le plan d'injection : chaque scénario répété N fois, puis
    l'ensemble mélangé avec une graine FIXE.

    Le mélange évite qu'un effet d'ordre soit confondu avec un effet de
    scénario (par exemple : les trois pannes disque exécutées d'affilée en
    début de campagne, quand les fenêtres sont encore vierges). La graine
    est fixée pour que la campagne reste rejouable à l'identique.
    """
    plan = [spec for spec in SCENARIOS for _ in range(repetitions)]
    random.Random(seed).shuffle(plan)
    return plan


def run_campaign(repetitions: int = 10, cooldown_seconds: int = 90,
                 seed: int = 42, dry_run: bool = False) -> dict:
    plan = build_plan(repetitions, seed)
    started_at = time.time()
    results: list[dict] = []

    duree_estimee = sum(s.params["duration_seconds"] + cooldown_seconds for s in plan)
    logger.info(
        f"campagne : {len(plan)} incidents, graine={seed}, "
        f"retour au calme={cooldown_seconds}s — durée estimée "
        f"{duree_estimee // 60} min {duree_estimee % 60}s"
    )
    logger.info("la boucle de supervision doit tourner en parallèle, sinon rien ne sera diagnostiqué")

    for index, spec in enumerate(plan, start=1):
        logger.info(f"[{index}/{len(plan)}] injection : {spec.nom}")
        entry = {
            "ordre": index,
            "scenario": spec.nom,
            "composant_attendu": spec.composant_attendu,
            "params": spec.params,
            "ts_debut": time.time(),
        }

        if dry_run:
            entry.update({"incident_id": None, "statut": "dry_run"})
        else:
            try:
                incident_id = RUNNERS[spec.nom](**spec.params)
                entry.update({"incident_id": incident_id, "statut": "ok"})
            except Exception as exc:  # noqa: BLE001
                # Un échec d'injection est journalisé et la campagne
                # continue. L'écarter silencieusement fausserait le compte
                # d'incidents et donc toutes les proportions calculées.
                logger.error(f"échec de l'injection {spec.nom} : {exc}")
                entry.update({"incident_id": None, "statut": "echec_injection",
                              "erreur": str(exc)})

        entry["ts_fin"] = time.time()
        results.append(entry)
        _append_jsonl(entry)

        # Retour au calme : laisse au système le temps de terminer son
        # diagnostic, d'exécuter une éventuelle action, de faire sa
        # vérification post-action (60 s par défaut), et aux fenêtres
        # glissantes de revenir à leur état normal. C'est ce délai qui rend
        # les incidents indépendants les uns des autres.
        if index < len(plan):
            logger.info(f"retour au calme : {cooldown_seconds}s")
            if not dry_run:
                time.sleep(cooldown_seconds)

    resume = {
        "debut": started_at,
        "fin": time.time(),
        "incidents_planifies": len(plan),
        "incidents_injectes": sum(1 for r in results if r["statut"] == "ok"),
        "echecs_injection": sum(1 for r in results if r["statut"] == "echec_injection"),
        "graine": seed,
        "cooldown_seconds": cooldown_seconds,
        "scenarios": [asdict(s) for s in SCENARIOS],
    }
    logger.info(
        f"campagne terminée : {resume['incidents_injectes']}/{resume['incidents_planifies']} "
        f"injections réussies"
    )
    return resume


def _append_jsonl(record: dict) -> None:
    with CAMPAIGN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="Campagne d'évaluation SentinelOps")
    parser.add_argument("--repetitions", type=int, default=10,
                        help="répétitions par scénario (3 scénarios : 10 -> 30 incidents)")
    parser.add_argument("--cooldown", type=int, default=90,
                        help="délai de retour au calme entre deux injections (s)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche le plan sans rien injecter")
    args = parser.parse_args()

    resume = run_campaign(args.repetitions, args.cooldown, args.seed, args.dry_run)
    print(json.dumps(resume, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()