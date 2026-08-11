"""
Détecteur d'anomalies sur métriques — couche déterministe, sans IA générative.

Deux méthodes complémentaires, volontairement combinées (cf. cahier des charges) :

1. Z-score glissant (par métrique, univarié) : rapide, interprétable,
   détecte bien les écarts nets sur UNE métrique isolée. Faiblesse : ne
   capte pas les corrélations entre métriques.

2. Isolation Forest (multivarié) : détecte des combinaisons de métriques
   individuellement "normales" mais anormales ensemble (ex: CPU légèrement
   élevé + latence légèrement élevée en même temps). Faiblesse : nécessite
   un ré-entraînement, moins interprétable seul.

Pourquoi combiner plutôt que choisir un seul ? Le z-score seul raterait les
anomalies multivariées subtiles ; Isolation Forest seul serait moins
réactif sur un pic franc et unique. On déclare une anomalie si au moins
UNE des deux méthodes la détecte (logique OR), pour privilégier le rappel
(éviter de rater un vrai incident) — le taux de faux positifs résiduel est
filtré en amont par le seuil de z-score et en aval par le score de
confiance de l'Agent Arbitre (Semaine 2).
"""

from dataclasses import dataclass, field
from collections import deque

import numpy as np
from sklearn.ensemble import IsolationForest


@dataclass
class AnomalyEvent:
    ts: float
    metric: str
    value: float
    z_score: float | None
    is_anomaly: bool
    severity: str  # "low" | "medium" | "high"
    method: str    # "z_score" | "isolation_forest" | "z_score+isolation_forest"


class RollingZScoreDetector:
    """
    Détecteur z-score sur fenêtre glissante, par métrique.

    Pourquoi une fenêtre glissante et pas une moyenne/écart-type fixes
    calculés une fois ? -> Le comportement "normal" d'une infra dérive
    dans le temps (charge qui augmente progressivement dans la journée).
    Une fenêtre glissante s'adapte à cette dérive sans fausse alerte,
    contrairement à un seuil statique calculé une seule fois au démarrage.
    """

    def __init__(self, window_size: int = 30, z_threshold: float = 3.0):
        self.window_size = window_size
        self.z_threshold = z_threshold
        self._windows: dict[str, deque] = {}

    def update(self, metric: str, ts: float, value: float) -> AnomalyEvent:
        if metric not in self._windows:
            self._windows[metric] = deque(maxlen=self.window_size)
        window = self._windows[metric]

        # Pas assez d'historique pour juger -> jamais d'anomalie déclarée
        # (évite les faux positifs de démarrage, cf. garde-fou Jour 5).
        if len(window) < max(5, self.window_size // 3):
            window.append(value)
            return AnomalyEvent(ts, metric, value, None, False, "low", "z_score")

        mean = float(np.mean(window))
        std = float(np.std(window))
        window.append(value)

        if std == 0:
            # Fenêtre parfaitement stable : tout écart est significatif par définition.
            z = 0.0 if value == mean else float("inf")
        else:
            z = (value - mean) / std

        is_anomaly = abs(z) >= self.z_threshold
        severity = self._severity_from_z(abs(z))
        return AnomalyEvent(ts, metric, value, z, is_anomaly, severity, "z_score")

    @staticmethod
    def _severity_from_z(abs_z: float) -> str:
        if abs_z >= 5.0:
            return "high"
        if abs_z >= 4.0:
            return "medium"
        return "low"


class MultivariateAnomalyDetector:
    """
    Isolation Forest sur un vecteur de métriques combinées.

    contamination=0.05 : hypothèse volontairement basse (5% d'anomalies
    attendues) -- cohérent avec un système de supervision où les incidents
    doivent rester rares par construction, sinon le modèle "normalise"
    des comportements réellement anormaux.
    """

    def __init__(self, contamination: float = 0.05, min_samples_to_fit: int = 50):
        self.contamination = contamination
        self.min_samples_to_fit = min_samples_to_fit
        self._model: IsolationForest | None = None
        self._training_buffer: list[list[float]] = []

    def fit(self, feature_vectors: list[list[float]]) -> None:
        self._model = IsolationForest(
            contamination=self.contamination,
            random_state=42,  # reproductibilité : même jeu de données -> même modèle
        )
        self._model.fit(feature_vectors)

    def observe_and_predict(self, feature_vector: list[float]) -> bool:
        """
        Alimente le buffer d'entraînement tant qu'aucun modèle n'est prêt,
        puis bascule en mode prédiction une fois le seuil atteint.
        Retourne True si le vecteur est jugé anormal.
        """
        if self._model is None:
            self._training_buffer.append(feature_vector)
            if len(self._training_buffer) >= self.min_samples_to_fit:
                self.fit(self._training_buffer)
            return False  # pas de verdict tant que le modèle n'est pas entraîné

        prediction = self._model.predict([feature_vector])[0]  # -1 = anomalie, 1 = normal
        return prediction == -1


class AnomalyDetector:
    """Façade combinant les deux méthodes (logique OR), utilisée par le reste du pipeline."""

    def __init__(self, window_size: int = 30, z_threshold: float = 3.0,
                 contamination: float = 0.05, min_samples_to_fit: int = 50):
        self.zscore = RollingZScoreDetector(window_size, z_threshold)
        self.multivariate = MultivariateAnomalyDetector(contamination, min_samples_to_fit)

    def process_sample(self, ts: float, metrics: dict[str, float]) -> list[AnomalyEvent]:
        """
        metrics: ex. {"cpu": 82.3, "latency_ms": 45.0, "memory_mb": 512.0}
        Retourne la liste des événements (un par métrique + un événement
        agrégé si Isolation Forest détecte une anomalie multivariée).
        """
        events = [self.zscore.update(name, ts, value) for name, value in metrics.items()]

        ordered_values = [metrics[k] for k in sorted(metrics.keys())]
        is_multivariate_anomaly = self.multivariate.observe_and_predict(ordered_values)
        if is_multivariate_anomaly:
            events.append(AnomalyEvent(
                ts=ts, metric="__multivariate__", value=0.0, z_score=None,
                is_anomaly=True, severity="medium", method="isolation_forest",
            ))
        return events