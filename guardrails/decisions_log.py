"""
Journalisation structurée et immuable de chaque décision — Jour 9, en
réponse directe à la remarque de l'encadrante : "chaque décision doit être
journalisée de façon structurée et immuable, pas juste dans le rapport
final envoyé à l'humain".

Même pattern que injectors/ground_truth.py (Jour 2) : JSONL append-only,
jamais réécrit ni corrompu par une écriture partielle.
"""

import json
import time
import uuid
import pathlib

DECISIONS_LOG_PATH = pathlib.Path(__file__).parent / "decisions_log.jsonl"


def log_decision(metrics_hypothesis: dict, logs_hypothesis: dict, arbiter_verdict: dict,
                  guardrail_decision: dict, action_executed: str | None,
                  incident_id: str | None = None,
                  metrics_observed: list | None = None,
                  logs_observed: list | None = None) -> str:
    """
    Journalise l'intégralité de la chaîne de décision pour un incident :
    ce que chaque agent a vu, ce que l'Arbitre a conclu, ce que le
    garde-fou a décidé, et l'action réellement exécutée (ou None).

    `metrics_observed` / `logs_observed` : les événements d'anomalie
    RÉELLEMENT transmis à chaque agent.

    Ajout du Jour 14. Le journal contenait jusqu'ici les conclusions des
    agents mais pas les données sur lesquelles elles reposaient. Un audit
    ne pouvait donc pas vérifier qu'une preuve citée existait vraiment :
    il fallait croire le score d'ancrage sur parole, alors que tout le
    projet consiste à ne rien croire sur parole. Conserver l'entrée des
    agents rend la vérification refaisable à la main, et permet à la
    console de confronter chaque preuve à sa source.
    """
    decision_id = incident_id or str(uuid.uuid4())
    record = {
        "decision_id": decision_id,
        "ts": time.time(),
        "metrics_observed": metrics_observed or [],
        "logs_observed": logs_observed or [],
        "metrics_hypothesis": metrics_hypothesis,
        "logs_hypothesis": logs_hypothesis,
        "arbiter_verdict": arbiter_verdict,
        "guardrail_decision": guardrail_decision,
        "action_executed": action_executed,
        "post_action_outcome": None,  # rempli ultérieurement par le Jour 10 (vérification post-action)
    }
    with DECISIONS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return decision_id


def append_post_action_outcome(decision_id: str, outcome: dict) -> bool:
    """
    Ajoute le résultat de la vérification post-action (Jour 10) à une
    décision déjà journalisée. Le JSONL étant append-only par principe
    (immuabilité), on n'édite PAS la ligne existante : on ajoute une
    nouvelle ligne de type "update" référençant le decision_id d'origine.
    Le lecteur (dashboard, Semaine 3) doit donc reconstituer l'état final
    en prenant la DERNIÈRE entrée par decision_id.
    """
    record = {
        "decision_id": decision_id,
        "ts": time.time(),
        "record_type": "post_action_outcome_update",
        "post_action_outcome": outcome,
    }
    with DECISIONS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def read_decisions() -> list[dict]:
    if not DECISIONS_LOG_PATH.exists():
        return []
    with DECISIONS_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]