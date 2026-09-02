"""
Dépendance externe simulée — service réel, Jour 14.

Pourquoi ce service existe
--------------------------
Jusqu'ici, `dependency-service` n'était qu'un nom dans l'inventaire des
composants : la latence de dépendance était simulée par un `sleep` à
l'intérieur de `target-app`. Deux conséquences gênantes :

1. L'action `scale_replica` était **inexécutable**. Elle visait un service
   qui n'existait pas, et la seule implémentation possible passait par
   l'API Docker Swarm, absente d'un environnement `docker compose`
   classique. Toute la branche « action à risque faible » de la politique
   de risque restait donc théorique.

2. Le composant `dependency-service` ne pouvait jamais être réellement
   distingué de `target-app`, puisqu'il n'avait ni processus, ni métriques,
   ni journaux propres.

Ce service est volontairement minimal : il répond à un appel avec une
latence de base réaliste, et sa capacité dépend du nombre de répliques.
C'est exactement ce qu'il faut pour que la mise à l'échelle produise un
effet mesurable plutôt que d'être un geste symbolique.

Contrainte de conception importante : **aucun port publié**. C'est ce qui
permet à `docker compose up -d --scale dependency-service=N` de fonctionner
sans conflit de port, contrairement à `target-app` qui expose 8000 sur
l'hôte. Le service n'est joignable que depuis le réseau interne Docker,
ce qui est de toute façon le comportement réaliste d'une dépendance.
"""

import os
import time
import random
import socket
import asyncio

from fastapi import FastAPI

app = FastAPI(title="SentinelOps — dépendance simulée")

# Identité de la réplique. Docker attribue un nom d'hôte distinct à chaque
# instance : c'est ce qui permet de vérifier, à la lecture des réponses,
# que la mise à l'échelle a bien pris effet et que les appels se
# répartissent entre les instances.
INSTANCE = socket.gethostname()

# Nombre d'appels traités simultanément par cette réplique. Au-delà, les
# appels attendent — c'est ce plafond qui rend la mise à l'échelle utile :
# doubler les répliques double la capacité de traitement parallèle.
CAPACITE_PAR_REPLIQUE = int(os.getenv("DEPENDENCY_CONCURRENCY", "4"))
_semaphore = asyncio.Semaphore(CAPACITE_PAR_REPLIQUE)

LATENCE_MIN_S = float(os.getenv("DEPENDENCY_BASE_LATENCY_MIN", "0.02"))
LATENCE_MAX_S = float(os.getenv("DEPENDENCY_BASE_LATENCY_MAX", "0.05"))


@app.get("/health")
async def health():
    return {"status": "ok", "instance": INSTANCE,
            "capacite": CAPACITE_PAR_REPLIQUE}


@app.get("/call")
async def call(extra_ms: int = 0):
    """
    Traite un appel.

    `extra_ms` permet à l'appelant de demander une latence supplémentaire :
    c'est le canal utilisé par l'injection de panne, qui reste pilotée
    depuis `target-app` afin de ne pas modifier les injecteurs existants et
    la vérité terrain qu'ils produisent.

    Le sémaphore reproduit une capacité finie. Sous charge, les appels
    au-delà de la capacité attendent leur tour, et la latence observée
    monte — ce qui est précisément la situation qu'une mise à l'échelle
    corrige.
    """
    extra_ms = max(0, min(5000, extra_ms))          # borne dure, comme dans l'app
    debut = time.perf_counter()

    async with _semaphore:
        base = random.uniform(LATENCE_MIN_S, LATENCE_MAX_S)
        await asyncio.sleep(base + extra_ms / 1000.0)

    total_ms = round((time.perf_counter() - debut) * 1000, 1)
    return {"status": "ok", "instance": INSTANCE, "latency_ms": total_ms,
            "attente_incluse": True}