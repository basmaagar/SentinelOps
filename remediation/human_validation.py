"""
File d'attente de validation humaine — Jour 10.

Quand le garde-fou décide "validation_humaine" (Jour 9), la décision est
placée ici plutôt qu'exécutée. Le dashboard (Jour 11) lira cette file
pour afficher les boutons Valider/Rejeter ; en attendant qu'il existe,
cette file est déjà fonctionnelle et testable en isolation.

Persistance simple sur fichier JSONL (même pattern que le reste du
projet) : pas de base de données pour un prototype de cette taille.
"""

import json
import time
import uuid
import pathlib

PENDING_QUEUE_PATH = pathlib.Path(__file__).parent / "pending_validations.jsonl"


def enqueue_for_validation(decision_id: str, action: str, target: str,
                            arbiter_verdict: dict, guardrail_decision: dict) -> str:
    validation_id = str(uuid.uuid4())
    record = {
        "validation_id": validation_id,
        "decision_id": decision_id,
        "action": action,
        "target": target,
        "arbiter_verdict": arbiter_verdict,
        "guardrail_decision": guardrail_decision,
        "status": "pending",  # "pending" | "approved" | "rejected"
        "ts": time.time(),
    }
    with PENDING_QUEUE_PATH.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return validation_id


def _read_all() -> list[dict]:
    if not PENDING_QUEUE_PATH.exists():
        return []
    with PENDING_QUEUE_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def list_pending() -> list[dict]:
    """
    Reconstitue l'état courant : pour chaque validation_id, ne garde que
    la DERNIÈRE entrée (append-only, cf. decisions_log.py), puis filtre
    celles encore "pending".
    """
    latest_by_id: dict[str, dict] = {}
    for record in _read_all():
        latest_by_id[record["validation_id"]] = record
    return [r for r in latest_by_id.values() if r["status"] == "pending"]


def resolve(validation_id: str, approved: bool) -> bool:
    """
    Enregistre la décision humaine. Append-only : on ajoute une nouvelle
    entrée avec le nouveau statut plutôt que de réécrire l'ancienne (même
    principe d'immuabilité que decisions_log.py).
    """
    existing = [r for r in _read_all() if r["validation_id"] == validation_id]
    if not existing:
        return False
    latest = existing[-1]
    latest["status"] = "approved" if approved else "rejected"
    latest["ts"] = time.time()
    with PENDING_QUEUE_PATH.open("a") as f:
        f.write(json.dumps(latest, ensure_ascii=False) + "\n")
    return True