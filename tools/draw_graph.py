"""
Dessin de l'architecture du graphe multi-agents.

Produit trois représentations de la MÊME topologie, celle réellement
exécutée par LangGraph — et non un schéma dessiné à la main qui pourrait
diverger du code :

  - architecture.png   : rendu graphique (nécessite une connexion, le rendu
                         Mermaid étant délégué à un service en ligne)
  - architecture.mmd   : code Mermaid brut, à coller dans un rapport ou
                         dans mermaid.live — aucune connexion requise
  - sortie console     : rendu ASCII, utile en démonstration

Aucun service du projet n'a besoin de tourner : on ne fait que lire la
structure du graphe, sans l'exécuter.

Usage :  python tools/draw_graph.py
"""

import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _sub in ("agents", "detector", "guardrails"):
    sys.path.append(str(_ROOT / _sub))

from llm_client import OllamaClient                    # noqa: E402
from graph import build_graph                          # noqa: E402
from anomaly_detector import AnomalyDetector           # noqa: E402
from log_anomaly_detector import LogAnomalyDetector    # noqa: E402


def main() -> None:
    # Les clients et détecteurs sont construits mais jamais appelés : seule
    # la topologie du graphe est lue.
    llm = OllamaClient()
    graphe = build_graph(
        metrics_client=llm, metrics_model="qwen2.5:1.5b",
        logs_client=llm, logs_model="llama3.2:1b",
        arbiter_client=llm, arbiter_model="qwen2.5:1.5b",
        metric_detector=AnomalyDetector(),
        log_detector=LogAnomalyDetector(),
    )
    representation = graphe.get_graph()

    # Le code Mermaid d'abord : c'est la sortie qui ne peut pas échouer, et
    # celle qui sert au rapport.
    mermaid = representation.draw_mermaid()
    pathlib.Path("architecture.mmd").write_text(mermaid, encoding="utf-8")
    print("architecture.mmd écrit — à coller dans un rapport ou sur mermaid.live\n")
    print(mermaid)

    # Rendu ASCII : dépend du paquet `grandalf`, absent par défaut. Son
    # absence ne doit pas empêcher les autres sorties d'être produites.
    try:
        print("\n" + representation.draw_ascii())
    except ImportError:
        print("\n[rendu ASCII indisponible — « pip install grandalf » pour l'activer]")

    # Rendu PNG en dernier : c'est la seule sortie qui dépend d'un service
    # en ligne, donc la plus susceptible d'échouer.
    try:
        pathlib.Path("architecture.png").write_bytes(representation.draw_mermaid_png())
        print("\narchitecture.png écrit")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[PNG non généré : {exc}]")
        print("Le fichier .mmd ci-dessus reste utilisable sans connexion.")


if __name__ == "__main__":
    main()