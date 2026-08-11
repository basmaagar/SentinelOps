"""
Injecteur de panne : saturation disque.

Usage: python injectors/disk_saturation.py [--size-mb 200] [--duration 15]
"""
import argparse
import time
import requests

from ground_truth import log_incident

TARGET_APP_URL = "http://localhost:8000"


def run(size_mb: int, duration_seconds: int):
    start_ts = time.time()
    print(f"[disk_saturation] Injection démarrée : {size_mb}Mo pendant {duration_seconds}s")

    resp = requests.get(
        f"{TARGET_APP_URL}/inject/disk",
        params={"size_mb": size_mb, "duration_seconds": duration_seconds},
        timeout=duration_seconds + 30,  # marge de sécurité sur le timeout HTTP
    )
    resp.raise_for_status()

    end_ts = time.time()
    incident_id = log_incident(
        incident_type="disk_saturation",
        start_ts=start_ts,
        end_ts=end_ts,
        composant_cible="target-app",
        params={"size_mb": size_mb, "duration_seconds": duration_seconds},
    )
    print(f"[disk_saturation] Terminé. incident_id={incident_id}")
    return incident_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=200)
    parser.add_argument("--duration", type=int, default=15)
    args = parser.parse_args()
    run(args.size_mb, args.duration)