import sys

sys.path.insert(0, "guardrails")
sys.path.insert(0, "remediation")
sys.path.insert(0, "detector")

from policy import evaluate
from action_selector import select_action
from actions import execute_action
from post_action_check import verify_and_rollback
from decisions_log import log_decision
from anomaly_detector import AnomalyDetector
import docker  # nécessite : pip install docker

# Client Docker RÉEL cette fois (à utiliser une fois docker compose up lancé)
docker_client = docker.from_env()

arbiter_verdict = {"agreement_status": "accord", "final_confidence": 0.9,
                    "diagnosis": "Saturation CPU", "composant_suspecte": "target-app"}
action = select_action(arbiter_verdict["composant_suspecte"])
decision = evaluate(action, arbiter_verdict)
print("Décision garde-fou:", decision)

if decision.decision == "autoriser_auto":
    result = execute_action(docker_client, action, "sentinelops-target-app")
    print("Action exécutée:", result)

    from decisions_log import log_decision

decision_id = log_decision(
    metrics_hypothesis={"hypothesis": "Saturation CPU", "confidence": 0.9},
    logs_hypothesis={"hypothesis": "aucune", "confidence": 0.0},
    arbiter_verdict=arbiter_verdict,
    guardrail_decision=decision.__dict__,
    action_executed=action,
)
print("Décision journalisée:", decision_id)