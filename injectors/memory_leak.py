"""
Injecteur de panne : fuite mémoire progressive.

Usage: python injectors/memory_leak.py [--size-mb 150] [--duration 20] [--step-mb 15]
"""
import argparse
import time
import requests

from ground_truth import log_incident

TARGET_APP_URL = "http://localhost:8000"


def run(size_mb: int, duration_seconds: int, step_mb: int):
    start_ts = time.time()
    print(f"[memory_leak] Injection démarrée : {size_mb}Mo par paliers de {step_mb}Mo sur {duration_seconds}s")

    resp = requests.get(
        f"{TARGET_APP_URL}/inject/memory",
        params={"size_mb": size_mb, "duration_seconds": duration_seconds, "step_mb": step_mb},
        timeout=duration_seconds + 30,
    )
    resp.raise_for_status()

    end_ts = time.time()
    incident_id = log_incident(
        incident_type="memory_leak",
        start_ts=start_ts,
        end_ts=end_ts,
        composant_cible="target-app",
        params={"size_mb": size_mb, "duration_seconds": duration_seconds, "step_mb": step_mb},
    )
    print(f"[memory_leak] Terminé. incident_id={incident_id}")
    return incident_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=150)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--step-mb", type=int, default=15)
    args = parser.parse_args()
    run(args.size_mb, args.duration, args.step_mb)