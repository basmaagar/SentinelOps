"""
Verrou anti-double-action — Jour 9, en réponse directe à la remarque de
l'encadrante : "si le même problème redéclenche l'investigation avant que
la première action n'ait eu le temps de faire effet, le système ne doit
pas relancer une deuxième action automatique en parallèle".

Implémentation volontairement simple (dict en mémoire) pour le prototype.
Limite assumée : non persistant entre redémarrages, et non partagé entre
plusieurs instances du système (suffisant pour un prototype mono-process,
à remplacer par un verrou distribué — ex: Redis — en production).
"""

import time
from dataclasses import dataclass


@dataclass
class LockInfo:
    component: str
    started_at: float
    timeout_seconds: float


class RemediationLock:
    def __init__(self):
        self._locks: dict[str, LockInfo] = {}

    def is_locked(self, component: str) -> bool:
        lock = self._locks.get(component)
        if lock is None:
            return False
        if time.time() - lock.started_at > lock.timeout_seconds:
            # Le verrou a expiré (ex: action jamais vérifiée post-exécution
            # suite à un crash) -> on le libère automatiquement plutôt que
            # de bloquer indéfiniment les investigations futures.
            del self._locks[component]
            return False
        return True

    def acquire(self, component: str, timeout_seconds: float = 120.0) -> bool:
        """Retourne False si déjà verrouillé (échec d'acquisition)."""
        if self.is_locked(component):
            return False
        self._locks[component] = LockInfo(component, time.time(), timeout_seconds)
        return True

    def release(self, component: str) -> None:
        self._locks.pop(component, None)