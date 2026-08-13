"""
Client Ollama.

Pourquoi un wrapper dédié plutôt qu'un appel requests.post() direct dans
chaque agent ? -> Centralise le timeout (obligatoire : cf. contrainte
"latence LLM local" du cahier des charges) et isole la dépendance réseau,
ce qui permet de la remplacer par un mock dans les tests.

--- Correctif Jour 12 : latence de démarrage à froid ---

Symptôme observé en conditions réelles : des appels de 90 à 150 secondes
avec `qwen2.5:1.5b`, un modèle qui infère normalement en quelques secondes,
puis des timeouts systématiques une fois le délai ramené à 45 s.

Cause : Ollama décharge un modèle de la mémoire après 5 minutes
d'inactivité (`keep_alive` par défaut). Comme les anomalies sont rares —
plusieurs minutes d'écart entre deux incidents — CHAQUE appel payait le
rechargement complet du modèle depuis le disque. Le temps mesuré n'était
donc pas celui de l'inférence, mais celui du chargement.

Trois corrections ici :

1. `keep_alive` porté à 1 heure : le modèle reste résident entre deux
   incidents. C'est le correctif principal.

2. `prewarm()` : chargement explicite au démarrage de la boucle, pour que
   le tout premier incident réel ne paye pas non plus ce coût. Sans cela,
   le premier diagnostic d'une campagne d'évaluation serait
   systématiquement le plus lent — ce qui fausserait la mesure du TTD.

3. `num_predict` borné : sans limite, un modèle peut partir en génération
   très longue sur une sortie JSON mal amorcée. On plafonne, et une
   température basse réduit ce risque sur une tâche d'extraction
   structurée où la créativité n'a aucune valeur.
"""

import time
import logging

import requests

logger = logging.getLogger("sentinelops.agents.llm")


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11435",
                 timeout_seconds: float = 90.0,
                 keep_alive: str = "1h",
                 num_predict: int = 220,
                 num_ctx: int = 2048,
                 temperature: float = 0.1):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive
        # Budget de génération. Mesuré en conditions réelles : sur une
        # machine sans GPU, un modèle 1.5B produit de l'ordre de 10 à 20
        # tokens/seconde. Un plafond à 512 tokens autorisait donc à lui
        # seul 25 à 50 secondes de génération, avant même le traitement du
        # prompt — ce qui expliquait les dépassements du délai de 45 s.
        # Les sorties attendues (hypothèse + preuves + confiance en JSON)
        # tiennent largement en 220 tokens.
        self.num_predict = num_predict
        # Fenêtre de contexte explicite : la valeur par défaut d'Ollama
        # peut être bien plus large que nécessaire, ce qui coûte de la
        # mémoire et du temps de préparation pour rien.
        self.num_ctx = num_ctx
        self.temperature = temperature

    def generate(self, model: str, prompt: str,
                 num_predict: int | None = None) -> str:
        """
        Retourne la sortie brute (texte) du modèle. Lève une exception si
        le serveur Ollama ne répond pas dans le délai imparti — c'est
        volontaire : l'agent appelant doit gérer explicitement ce cas
        (fallback), jamais attendre indéfiniment.

        `num_predict` permet à un agent d'augmenter son budget de sortie.
        Nécessaire pour l'Arbitre, qui doit produire cinq champs dont un
        rapport d'incident rédigé : avec le budget standard, sa réponse
        était tronquée en plein JSON, l'accolade fermante manquait, et les
        trois tentatives échouaient identiquement. Un plafond trop bas ne
        dégrade pas la réponse, il la rend inexploitable.
        """
        budget = num_predict if num_predict is not None else self.num_predict
        started = time.perf_counter()
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "keep_alive": self.keep_alive,
                "options": {
                    "num_predict": budget,
                    "num_ctx": self.num_ctx,
                    "temperature": self.temperature,
                },
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()

        # Journalisation du coût réel de l'appel. Sans cette mesure, il est
        # impossible de distinguer "le modèle est lent" de "le prompt est
        # trop long" — les deux se présentent comme un simple timeout.
        # Ces chiffres alimenteront directement la section performance du
        # rapport, et la comparaison petits modèles / gros modèles.
        elapsed = time.perf_counter() - started
        produced = payload.get("eval_count", 0) or 0
        # Une sortie qui atteint exactement le budget est presque
        # certainement tronquée : on le signale, car le symptôme visible
        # (JSON invalide) ne dit rien de la cause réelle.
        tronque = " [TRONQUÉ : budget atteint]" if produced >= budget else ""
        logger.info(
            f"{model} : {elapsed:.1f}s "
            f"(prompt {payload.get('prompt_eval_count', '?')} tokens, "
            f"sortie {produced} tokens){tronque}"
        )
        return payload["response"]

    def prewarm(self, models: list[str], timeout_seconds: float = 300.0) -> dict[str, bool]:
        """
        Charge les modèles en mémoire avant le début de la supervision.

        Timeout volontairement large (5 min) et distinct de celui des
        appels de diagnostic : charger un modèle depuis un disque lent est
        légitimement long, alors qu'un diagnostic qui dépasse 45 s est un
        échec au regard de l'objectif de TTD. Confondre les deux délais
        était précisément l'erreur d'origine.

        Un prompt vide avec num_predict=0 suffit : Ollama charge le modèle
        sans rien générer.
        """
        results: dict[str, bool] = {}
        for model in dict.fromkeys(models):  # dédoublonne en gardant l'ordre
            try:
                requests.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model, "prompt": "", "stream": False,
                          "keep_alive": self.keep_alive,
                          "options": {"num_predict": 0}},
                    timeout=timeout_seconds,
                ).raise_for_status()
                logger.info(f"modèle préchargé : {model}")
                results[model] = True
            except requests.RequestException as exc:
                logger.warning(f"préchargement de {model} échoué : {exc}")
                results[model] = False
        return results