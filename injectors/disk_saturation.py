"""
Injecteur de panne : saturation disque.

Usage: python injectors/disk_saturation.py [--size-mb 200] [--duration 15]

--- Robustesse ajoutée au Jour 13 ---

L'injection est déclenchée par une requête HTTP maintenue ouverte pendant
toute sa durée. Or le système supervisé peut, pendant ce temps, décider de
REDÉMARRER le conteneur en guise de remédiation — c'est précisément le
comportement recherché. La connexion est alors coupée et la requête lève
une exception.

Traiter ce cas comme un échec d'injection serait doublement faux : la
panne a bien été injectée (le système l'a détectée, puisqu'il a réagi), et
l'interruption est le signe que la remédiation a fonctionné. Lors de la
première campagne, 11 injections disque sur 15 ont ainsi été comptées en
échec et perdues pour l'évaluation, alors qu'elles s'étaient déroulées
correctement.

L'incident est donc journalisé dans tous les cas, avec un indicateur
`interrompu` permettant de distinguer les deux situations à l'analyse.
"""
import argparse
import time
import requests

from ground_truth import log_incident

TARGET_APP_URL = "http://localhost:8000"


def run(size_mb: int, duration_seconds: int):
    start_ts = time.time()
    print(f"[disk_saturation] Injection démarrée : {size_mb}Mo pendant {duration_seconds}s")

    interrompu = False
    raison_interruption = None
    try:
        resp = requests.get(
            f"{TARGET_APP_URL}/inject/disk",
            params={"size_mb": size_mb, "duration_seconds": duration_seconds},
            timeout=duration_seconds + 30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        # La connexion a été rompue en cours d'injection : très probablement
        # un redémarrage du conteneur déclenché par la remédiation
        # automatique. L'injection a eu lieu ; on le journalise comme tel.
        interrompu = True
        raison_interruption = str(exc)
        print(f"[disk_saturation] Injection interrompue (probable remédiation) : {exc}")

    end_ts = time.time()
    incident_id = log_incident(
        incident_type="disk_saturation",
        start_ts=start_ts,
        end_ts=end_ts,
        composant_cible="target-app",
        params={**{"size_mb": size_mb, "duration_seconds": duration_seconds},
                "interrompu": interrompu,
                "raison_interruption": raison_interruption},
    )
    statut = "interrompu" if interrompu else "terminé"
    print(f"[disk_saturation] {statut}. incident_id={incident_id}")
    return incident_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=200)
    parser.add_argument("--duration", type=int, default=15)
    args = parser.parse_args()
    run(args.size_mb, args.duration)