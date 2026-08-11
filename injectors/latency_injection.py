"""
Injecteur de panne : latence de dépendance.

Contrairement aux deux autres injecteurs (bloquants côté app), celui-ci
active une fenêtre non-bloquante puis génère lui-même des appels répétés
à /dependency-call pendant la fenêtre, pour produire plusieurs échantillons
de latence exploitables par le détecteur (Jour 3-4).

Usage: python injectors/latency_injection.py [--extra-ms 400] [--duration 20]
"""
import argparse
import time
import requests

from ground_truth import log_incident

TARGET_APP_URL = "http://localhost:8000"


def run(extra_ms: int, duration_seconds: int, call_interval_seconds: float = 1.0):
    start_ts = time.time()
    print(f"[latency_injection] Fenêtre démarrée : +{extra_ms}ms pendant {duration_seconds}s")

    resp = requests.get(
        f"{TARGET_APP_URL}/inject/latency/start",
        params={"extra_ms": extra_ms, "duration_seconds": duration_seconds},
        timeout=10,
    )
    resp.raise_for_status()

    # Génère des appels réguliers à /dependency-call pendant toute la fenêtre,
    # pour produire des échantillons de latence anormaux dans l'Histogram Prometheus.
    end_window = time.time() + duration_seconds
    call_count = 0
    while time.time() < end_window:
        try:
            requests.get(f"{TARGET_APP_URL}/dependency-call", timeout=10)
            call_count += 1
        except requests.RequestException as exc:
            print(f"[latency_injection] Appel échoué (ignoré): {exc}")
        time.sleep(call_interval_seconds)

    end_ts = time.time()
    incident_id = log_incident(
        incident_type="latency_injection",
        start_ts=start_ts,
        end_ts=end_ts,
        composant_cible="target-app",
        params={"extra_ms": extra_ms, "duration_seconds": duration_seconds, "calls_made": call_count},
    )
    print(f"[latency_injection] Terminé ({call_count} appels). incident_id={incident_id}")
    return incident_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-ms", type=int, default=400)
    parser.add_argument("--duration", type=int, default=20)
    args = parser.parse_args()
    run(args.extra_ms, args.duration)