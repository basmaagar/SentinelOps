# Console de supervision — service Docker.
#
# Ce conteneur a besoin de deux accès particuliers, absents d'un service
# applicatif classique :
#
# 1. Les journaux JSONL du projet (decisions_log.jsonl, ground_truth.jsonl,
#    pending_validations.jsonl, evaluation_results.json...) sont montés en
#    volume depuis l'hôte dans docker-compose.yml. Le dashboard les lit
#    directement, comme il le fait aujourd'hui hors conteneur.
#
# 2. Le socket Docker de l'hôte est monté pour permettre l'exécution des
#    actions de remédiation (restart_container) approuvées depuis la
#    console : `docker.from_env()` dans actions.py en a besoin pour parler
#    au démon Docker de l'hôte, pas à un démon interne au conteneur.
FROM python:3.11-slim

WORKDIR /app

# La console dépend de plusieurs modules du projet (remediation,
# guardrails) en plus de Streamlit lui-même : la liste reflète les imports
# réels de dashboard/app.py.
COPY dashboard_ctx/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Seul le code de la console est copié dans l'image : les journaux qu'il
# lit arrivent par les volumes déclarés dans docker-compose.yml, pas par
# une copie figée au moment du build.
COPY dashboard ./dashboard

EXPOSE 8501

# --server.address 0.0.0.0 est indispensable en conteneur : sans lui,
# Streamlit n'écoute que sur localhost à l'intérieur du conteneur, ce qui
# le rend injoignable depuis l'extérieur malgré le port publié.
CMD ["streamlit", "run", "dashboard/app.py", \
     "--server.address", "0.0.0.0", \
     "--server.port", "8501", \
     "--server.headless", "true"]