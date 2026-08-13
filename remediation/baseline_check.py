"""
Comparaison post-action contre une ligne de base saine — Jour 12.

CORRECTIF d'un défaut de `post_action_check.verify_and_rollback` (Jour 10) :
cette fonction faisait passer l'échantillon post-action dans LE MÊME
détecteur que celui qui avait repéré l'anomalie. Deux effets pervers :

1. Contamination : l'échantillon post-action était ajouté à la fenêtre
   glissante, faussant toutes les détections suivantes.
2. Faux négatif systématique : pendant un incident, la fenêtre glissante
   s'est "adaptée" aux valeurs anormales (c'est le défaut déjà identifié
   au Jour 3). Un retour à la normale apparaît alors comme un ÉCART par
   rapport à cette fenêtre contaminée — donc le système peut conclure
   "toujours anormal" précisément quand l'action a réussi, et déclencher
   un rollback inutile.

Solution : comparer non pas à la fenêtre courante, mais à une ligne de
base SAINE, capturée pendant les périodes sans anomalie, et jamais
modifiée par la lecture (comparaison en lecture seule).
"""

import statistics
from collections import deque
from dataclasses import dataclass, field


@dataclass
class BaselineComparison:
    is_anomalous: bool
    deviations: dict[str, float] = field(default_factory=dict)
    reason: str = ""


class HealthyBaseline:
    """
    Mémorise les échantillons observés PENDANT les périodes saines, et
    permet de comparer un échantillon post-action à cet état de référence.

    `observe()` n'est appelé que lorsque le détecteur n'a rien signalé —
    c'est ce qui garantit que la référence reste "propre" même si un
    incident dure longtemps.
    """

    def __init__(self, window_size: int = 60, z_threshold: float = 3.0,
                 min_samples: int = 10, relative_floor: float = 0.05,
                 absolute_floor: float = 1e-6):
        self.window_size = window_size
        self.z_threshold = z_threshold
        self.min_samples = min_samples
        # Plancher d'écart-type. Sans lui, une ligne de base parfaitement
        # plate (écart-type = 0, cas FRÉQUENT ici : une gauge d'injection
        # vaut 0 en permanence hors incident) donne un z-score infini au
        # moindre écart. Conséquence concrète : toute action RÉUSSIE serait
        # classée "négative" et déclencherait un rollback ou une escalade
        # inutile — l'inverse exact du comportement voulu.
        # On considère donc qu'un écart doit dépasser 5% de la moyenne pour
        # être significatif, même si l'historique est parfaitement stable.
        self.relative_floor = relative_floor
        self.absolute_floor = absolute_floor
        self._windows: dict[str, deque] = {}

    def observe(self, metrics_sample: dict[str, float]) -> None:
        """À n'appeler QUE sur un échantillon jugé sain."""
        for name, value in metrics_sample.items():
            if name not in self._windows:
                self._windows[name] = deque(maxlen=self.window_size)
            self._windows[name].append(value)

    def is_ready(self) -> bool:
        if not self._windows:
            return False
        return all(len(w) >= self.min_samples for w in self._windows.values())

    def compare(self, metrics_sample: dict[str, float]) -> BaselineComparison:
        """
        Compare SANS modifier la ligne de base (lecture seule) — c'est le
        cœur du correctif : vérifier n'altère jamais la référence.
        """
        if not self.is_ready():
            return BaselineComparison(
                is_anomalous=False,
                reason="ligne_de_base_insuffisante",
            )

        deviations: dict[str, float] = {}
        for name, value in metrics_sample.items():
            window = self._windows.get(name)
            if window is None or len(window) < self.min_samples:
                continue
            mean = statistics.fmean(window)
            stdev = max(
                statistics.pstdev(window),
                abs(mean) * self.relative_floor,
                self.absolute_floor,
            )
            deviations[name] = abs(value - mean) / stdev

        if not deviations:
            return BaselineComparison(False, {}, "aucune_metrique_comparable")

        worst_metric = max(deviations, key=deviations.get)
        is_anomalous = deviations[worst_metric] >= self.z_threshold
        reason = (
            f"ecart_max sur {worst_metric} = {deviations[worst_metric]:.2f}"
            f" (seuil {self.z_threshold})"
        )
        return BaselineComparison(is_anomalous, deviations, reason)