"""
SentinelOps - Application cible instrumentée.

Cette app simule un service applicatif réel. Elle expose :
- /health          : liveness check simple
- /load            : endpoint qui consomme du CPU de façon contrôlée (pour tester
                      la détection d'anomalie de charge CPU en Semaine 1/2)
- /metrics         : endpoint Prometheus (scrapé par prometheus.yml)

Pourquoi exposer un /load plutôt que d'attendre les scripts d'injection du Jour 2 ?
-> Cela permet dès aujourd'hui de vérifier que le pipeline de scraping Prometheus
   réagit correctement à une variation de charge, sans dépendre du travail de demain.
"""

import os
import time
import random
import logging
from contextlib import contextmanager

from fastapi import FastAPI, Response
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

app = FastAPI(title="SentinelOps Target App")

# --- Logging applicatif ---
# Pourquoi un fichier dédié (app.log) plutôt que se contenter des logs uvicorn ?
# -> Les logs uvicorn (accès HTTP) sont bruyants et peu informatifs sur la
#    cause métier d'un incident. On émet ici des messages ciblés (ex: latence
#    dépendance dépassant un seuil) qui sont exactement ce que Drain3 doit
#    détecter comme anormal au Jour 4.
logger = logging.getLogger("sentinelops.app")
logger.setLevel(logging.INFO)

# Jour 12 : le fichier est écrit dans logs/, monté en volume vers l'hôte
# (cf. docker-compose.yml). C'est ce qui permet à la boucle de supervision,
# qui tourne hors du conteneur, de lire ces lignes au fil de l'eau.
# LOG_DIR reste paramétrable pour pouvoir lancer l'app hors Docker (tests).
LOG_DIR = os.getenv("SENTINELOPS_LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_handler = logging.FileHandler(os.path.join(LOG_DIR, "app.log"))
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
logger.addHandler(_handler)

# --- Métriques applicatives ---
# Un Counter pour le nombre total de requêtes (utile pour détecter un pic d'erreurs).
REQUEST_COUNT = Counter(
    "app_requests_total",
    "Nombre total de requêtes reçues",
    ["endpoint", "status"],
)

# Un Histogram pour la latence : c'est la métrique clé pour détecter
# les incidents de type "latence réseau/dépendance".
REQUEST_LATENCY = Histogram(
    "app_request_latency_seconds",
    "Latence des requêtes en secondes",
    ["endpoint"],
)

# Une Gauge pour simuler une charge CPU "logique" pilotable, indépendante
# du CPU réel de la machine hôte (utile en environnement de démo partagé/limité).
SIMULATED_CPU_LOAD = Gauge(
    "app_simulated_cpu_load_percent",
    "Charge CPU simulée en pourcentage (0-100)",
)
SIMULATED_CPU_LOAD.set(5.0)  # valeur de repos réaliste


@contextmanager
def track_latency(endpoint: str):
    start = time.perf_counter()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = time.perf_counter() - start
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
        REQUEST_COUNT.labels(endpoint=endpoint, status=status).inc()


@app.get("/health")
def health():
    with track_latency("/health"):
        return {"status": "ok"}


@app.get("/load")
def load(intensity: int = 50, duration_seconds: int = 5):
    """
    Simule une charge CPU contrôlée.

    intensity: 0-100, intensité de charge simulée
    duration_seconds: durée pendant laquelle la charge est appliquée

    On borne les paramètres pour éviter qu'un appel malformé ne fasse planter
    le conteneur (garde-fou basique, même à ce stade précoce du projet).
    """
    intensity = max(0, min(100, intensity))
    duration_seconds = max(1, min(30, duration_seconds))

    with track_latency("/load"):
        SIMULATED_CPU_LOAD.set(intensity)
        logger.info(f"LoadTest starting intensity={intensity} duration={duration_seconds}s")
        end_time = time.perf_counter() + duration_seconds
        # Boucle de calcul réelle et bornée, pour générer une vraie
        # consommation CPU visible par cAdvisor, proportionnelle à `intensity`.
        while time.perf_counter() < end_time:
            if intensity > 0:
                _ = sum(random.random() for _ in range(intensity * 1000))
            else:
                time.sleep(0.05)
        SIMULATED_CPU_LOAD.set(5.0)  # retour à l'état de repos
        return {"status": "load_applied", "intensity": intensity, "duration_seconds": duration_seconds}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# =========================================================================
# JOUR 2 — Endpoints d'injection de pannes contrôlées et réversibles.
#
# Choix : l'injection se fait via l'app elle-même (pas via docker exec externe)
# pour garantir la réversibilité (try/finally systématique) et permettre de
# tester toute la logique sans avoir Docker/Prometheus en marche.
# =========================================================================

import gc

DISK_INJECTION_DIR = "/tmp/sentinelops_disk_injection"
DISK_INJECTION_GAUGE = Gauge(
    "app_disk_injection_active_mb", "Taille du fichier d'injection disque actif (Mo)"
)
MEMORY_INJECTION_GAUGE = Gauge(
    "app_memory_injection_active_mb", "Mémoire retenue par l'injection active (Mo)"
)
LATENCY_INJECTION_GAUGE = Gauge(
    "app_latency_injection_active_ms", "Latence artificielle actuellement active (ms)"
)

# Etat global minimal pour l'injection de latence (fenêtre temporelle).
_latency_injection_state = {"end_ts": 0.0, "extra_ms": 0}


@app.get("/inject/disk")
def inject_disk(size_mb: int = 100, duration_seconds: int = 10):
    """
    Sature l'espace disque du conteneur de façon bornée et réversible :
    écrit un fichier de `size_mb` Mo, attend `duration_seconds`, puis le
    supprime dans un bloc `finally` (garantie de rollback même en cas
    d'erreur).
    """
    size_mb = max(1, min(2000, size_mb))          # borne dure : jamais plus de 2 Go
    duration_seconds = max(1, min(120, duration_seconds))

    os.makedirs(DISK_INJECTION_DIR, exist_ok=True)
    filepath = os.path.join(DISK_INJECTION_DIR, "injected.bin")

    try:
        with open(filepath, "wb") as f:
            f.write(b"0" * (size_mb * 1024 * 1024))
        DISK_INJECTION_GAUGE.set(size_mb)
        logger.warning(f"DiskUsageHigh: injected file size_mb={size_mb} path={filepath}")
        time.sleep(duration_seconds)
        return {"status": "disk_injection_completed", "size_mb": size_mb, "duration_seconds": duration_seconds}
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
        DISK_INJECTION_GAUGE.set(0)


@app.get("/inject/memory")
def inject_memory(size_mb: int = 100, duration_seconds: int = 10, step_mb: int = 10):
    """
    Simule une fuite mémoire progressive : alloue par paliers de `step_mb`
    jusqu'à `size_mb`, en espaçant les paliers sur `duration_seconds`, puis
    libère explicitement (garantie de rollback).
    """
    size_mb = max(1, min(1000, size_mb))           # borne dure : jamais plus de 1 Go
    duration_seconds = max(1, min(120, duration_seconds))
    step_mb = max(1, min(size_mb, step_mb))

    steps = max(1, size_mb // step_mb)
    delay_per_step = duration_seconds / steps
    held_blocks = []

    try:
        for i in range(steps):
            held_blocks.append(bytearray(step_mb * 1024 * 1024))
            MEMORY_INJECTION_GAUGE.set((i + 1) * step_mb)
            logger.warning(f"MemoryUsageHigh: allocated_mb={(i + 1) * step_mb} step={i+1}/{steps}")
            time.sleep(delay_per_step)
        return {"status": "memory_injection_completed", "size_mb": size_mb, "duration_seconds": duration_seconds}
    finally:
        held_blocks.clear()
        gc.collect()
        MEMORY_INJECTION_GAUGE.set(0)


@app.get("/inject/latency/start")
def inject_latency_start(extra_ms: int = 300, duration_seconds: int = 30):
    """
    Active une fenêtre de latence artificielle sur /dependency-call.
    Non bloquant : retourne immédiatement, la fenêtre s'auto-désactive
    à expiration (vérifiée à chaque appel de /dependency-call).
    """
    extra_ms = max(0, min(5000, extra_ms))          # borne dure : jamais plus de 5s
    duration_seconds = max(1, min(120, duration_seconds))

    _latency_injection_state["end_ts"] = time.time() + duration_seconds
    _latency_injection_state["extra_ms"] = extra_ms
    LATENCY_INJECTION_GAUGE.set(extra_ms)
    return {"status": "latency_injection_started", "extra_ms": extra_ms, "duration_seconds": duration_seconds}


@app.get("/dependency-call")
def dependency_call():
    """
    Simule un appel à une dépendance externe. Latence de base réaliste
    (20-50ms) + latence injectée si la fenêtre est active.
    """
    with track_latency("/dependency-call"):
        base_latency = random.uniform(0.02, 0.05)
        extra = 0.0
        if time.time() < _latency_injection_state["end_ts"]:
            extra = _latency_injection_state["extra_ms"] / 1000.0
        else:
            LATENCY_INJECTION_GAUGE.set(0)  # fenêtre expirée : on remet le gauge à 0
        time.sleep(base_latency + extra)
        total_ms = round((base_latency + extra) * 1000, 1)
        if total_ms > 200:
            logger.error(f"DependencyTimeout: call exceeded SLA latency_ms={total_ms}")
        return {"status": "dependency_ok", "latency_ms": total_ms}