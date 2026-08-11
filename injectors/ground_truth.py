"""
Journalisation de la vérité terrain (ground truth) des pannes injectées.

Pourquoi un fichier JSONL partagé plutôt qu'une base de données ?
-> Au Jour 2, on n'a pas encore besoin de requêtage complexe. Un JSONL est
   append-only (jamais corrompu par une écriture concurrente partielle),
   lisible par n'importe quel outil (pandas, jq), et suffisant pour calculer
   la précision du diagnostic en Semaine 3 (Jour 13).
"""

import json
import time
import uuid
import pathlib

GROUND_TRUTH_PATH = pathlib.Path(__file__).parent / "ground_truth.jsonl"


def log_incident(incident_type: str, start_ts: float, end_ts: float,
                  composant_cible: str, params: dict) -> str:
    """Enregistre un incident injecté et retourne son incident_id."""
    incident_id = str(uuid.uuid4())
    record = {
        "incident_id": incident_id,
        "type": incident_type,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "composant_cible": composant_cible,
        "params": params,
    }
    with GROUND_TRUTH_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return incident_id


def read_incidents() -> list[dict]:
    """Relit tous les incidents enregistrés (utilisé en Semaine 3 pour l'évaluation)."""
    if not GROUND_TRUTH_PATH.exists():
        return []
    with GROUND_TRUTH_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]