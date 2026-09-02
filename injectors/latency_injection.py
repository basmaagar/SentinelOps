"""
Injection de latence par SATURATION de la dépendance — Jour 15.

Ce qui change par rapport à la version précédente
-------------------------------------------------
L'ancienne version ajoutait un délai artificiel (`sleep`) à l'intérieur de
`target-app`. La dégradation était donc réelle du point de vue de la
métrique, mais **aucune action corrective ne pouvait l'améliorer** : la
latence disparaissait à l'expiration de la fenêtre d'injection, quoi que
fasse le système.

La vérification post-action constatait donc un retour à la normale sans
lien de cause à effet avec l'action exécutée. Et la mise à l'échelle de la
dépendance, elle, n'avait littéralement aucun effet.

Cette version provoque la dégradation par la charge : plusieurs appels
concurrents vers `/dependency-call`, au-delà de la capacité de traitement
de la dépendance. Les appels excédentaires attendent leur tour, et la
latence mesurée monte.

Pourquoi c'est important
------------------------
La chaîne causale devient complète et vérifiable :

    charge concurrente -> file d'attente -> latence mesurée en hausse
    -> détection -> diagnostic -> mise à l'échelle
    -> capacité doublée -> latence en baisse -> vérification positive

C'est le seul scénario du projet où la remédiation **corrige réellement**
le problème, au lieu d'accompagner sa disparition spontanée. La
vérification post-action mesure alors un effet, pas une coïncidence.

Deux modes conservés
--------------------
  `--mode charge` (défaut) : saturation réelle, améliorable par scale
  `--mode delai`           : ancien comportement, dégradation non corrigible

Le second reste utile pour tester la détection isolément, sans dépendre du
conteneur de dépendance.
"""

import time
import argparse
import threading
import urllib.error
import urllib.request

from ground_truth import log_incident

TARGET_APP_URL = "http://localhost:8000"

# Nombre d'appels concurrents.
#
# La dépendance traite 2 appels simultanés par réplique. Avec 20 appels
# concurrents et une seule réplique, la file atteint dix profondeurs : la
# latence mesurée dépasse 300 ms, contre 35 à 85 ms en régime normal.
#
# Ce dimensionnement vient d'une mesure et non d'une estimation : la
# variation naturelle de la latence est large, et un écart trop faible
# serait indiscernable du bruit par le détecteur statistique. Un premier
# essai à 12 appels concurrents pour une capacité de 4 produisait une
# latence à peine supérieure à la normale.
CONCURRENCE_DEFAUT = 20


def _appeler(url: str, compteur: dict, verrou: threading.Lock,
             fin: float, timeout: float) -> None:
    """Boucle d'appels d'un seul travailleur, jusqu'à la fin de la fenêtre."""
    while time.time() < fin:
        try:
            with urllib.request.urlopen(url, timeout=timeout):
                pass
            with verrou:
                compteur["ok"] += 1
        except Exception:  # noqa: BLE001
            # Un appel échoué est compté mais n'interrompt pas l'injection.
            # Pendant une remédiation, le conteneur peut redémarrer : les
            # appels en cours échouent alors, et c'est le signe attendu que
            # le système a réagi — pas un échec de l'injection.
            with verrou:
                compteur["echecs"] += 1
        time.sleep(0.05)


def run(duration_seconds: int = 45, concurrence: int = CONCURRENCE_DEFAUT,
        extra_ms: int = 0, mode: str = "charge",
        call_interval_seconds: float = 1.0) -> str:
    """
    Provoque une dégradation de latence, puis journalise la vérité terrain.

    `extra_ms` reste accepté pour compatibilité avec les campagnes
    existantes ; en mode `charge` il vaut 0 par défaut, la dégradation
    venant de la concurrence et non d'un délai déclaré.
    """
    start_ts = time.time()
    fin = start_ts + duration_seconds
    compteur = {"ok": 0, "echecs": 0}
    verrou = threading.Lock()

    if mode == "delai":
        # Ancien comportement : fenêtre d'injection déclarée dans l'app.
        print(f"[latency_injection] Mode délai : +{extra_ms}ms pendant {duration_seconds}s")
        try:
            url = (f"{TARGET_APP_URL}/inject/latency/start"
                   f"?extra_ms={extra_ms}&duration_seconds={duration_seconds}")
            with urllib.request.urlopen(url, timeout=10):
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"[latency_injection] Démarrage de la fenêtre échoué : {exc}")

        while time.time() < fin:
            try:
                with urllib.request.urlopen(
                        f"{TARGET_APP_URL}/dependency-call", timeout=15):
                    compteur["ok"] += 1
            except Exception:  # noqa: BLE001
                compteur["echecs"] += 1
            time.sleep(call_interval_seconds)
    else:
        print(f"[latency_injection] Mode charge : {concurrence} appels concurrents "
              f"pendant {duration_seconds}s")
        url = f"{TARGET_APP_URL}/dependency-call"
        travailleurs = [
            threading.Thread(target=_appeler,
                             args=(url, compteur, verrou, fin, 20.0),
                             daemon=True)
            for _ in range(concurrence)
        ]
        for t in travailleurs:
            t.start()
        for t in travailleurs:
            # Marge au-delà de la fenêtre : les derniers appels peuvent
            # encore attendre leur tour dans la file de la dépendance.
            t.join(timeout=duration_seconds + 30)

    end_ts = time.time()
    interrompu = compteur["echecs"] > 0

    incident_id = log_incident(
        incident_type="latency_injection",
        start_ts=start_ts,
        end_ts=end_ts,
        composant_cible="dependency-service",
        params={
            "mode": mode,
            "concurrence": concurrence if mode == "charge" else None,
            "extra_ms": extra_ms if mode == "delai" else 0,
            "duration_seconds": duration_seconds,
            "appels_reussis": compteur["ok"],
            "appels_echoues": compteur["echecs"],
            # Des appels échoués pendant l'injection indiquent le plus
            # souvent un redémarrage de conteneur déclenché par la
            # remédiation — c'est-à-dire que le système a réagi.
            "interrompu": interrompu,
        },
    )
    statut = "interrompu" if interrompu else "terminé"
    print(f"[latency_injection] {statut}. {compteur['ok']} appels réussis, "
          f"{compteur['echecs']} échoués. incident_id={incident_id}")
    return incident_id


def main() -> None:
    parseur = argparse.ArgumentParser(description="Injection de latence de dépendance")
    parseur.add_argument("--duration", type=int, default=45)
    parseur.add_argument("--concurrence", type=int, default=CONCURRENCE_DEFAUT)
    parseur.add_argument("--extra-ms", type=int, default=0)
    parseur.add_argument("--mode", choices=["charge", "delai"], default="charge")
    arguments = parseur.parse_args()
    run(duration_seconds=arguments.duration, concurrence=arguments.concurrence,
        extra_ms=arguments.extra_ms, mode=arguments.mode)


if __name__ == "__main__":
    main()