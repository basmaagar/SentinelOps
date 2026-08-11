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
                  incident_id: str | None = None) -> str:
    """
    Journalise l'intégralité de la chaîne de décision pour un incident :
    ce que chaque agent a vu, ce que l'Arbitre a conclu, ce que le
    garde-fou a décidé, et l'action réellement exécutée (ou None).
    """
    decision_id = incident_id or str(uuid.uuid4())
    record = {
        "decision_id": decision_id,
        "ts": time.time(),
        "metrics_hypothesis": metrics_hypothesis,
        "logs_hypothesis": logs_hypothesis,
        "arbiter_verdict": arbiter_verdict,
        "guardrail_decision": guardrail_decision,
        "action_executed": action_executed,
        "post_action_outcome": None,  # rempli ultérieurement par le Jour 10 (vérification post-action)
    }
    with DECISIONS_LOG_PATH.open("a") as f:
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
    with DECISIONS_LOG_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def read_decisions() -> list[dict]:
    if not DECISIONS_LOG_PATH.exists():
        return []
    with DECISIONS_LOG_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]