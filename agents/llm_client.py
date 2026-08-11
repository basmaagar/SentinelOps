"""
Client Ollama minimal.

Pourquoi un wrapper dédié plutôt qu'un appel requests.post() direct dans
chaque agent ? -> Centralise le timeout (obligatoire : cf. contrainte
"latence LLM local" du cahier des charges) et isole la dépendance réseau,
ce qui permet de la remplacer par un mock dans les tests (cf. tests plus
bas, exécutés sans Ollama réel).
"""

import requests

class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11435", timeout_seconds: float = 120.0):
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def generate(self, model: str, prompt: str) -> str:
        """
        Retourne la sortie brute (texte) du modèle. Lève une exception si
        le serveur Ollama ne répond pas dans le délai imparti — c'est
        volontaire : l'agent appelant doit gérer explicitement ce cas
        (fallback), jamais attendre indéfiniment (cf. contrainte latence).
        """
        response = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()["response"]