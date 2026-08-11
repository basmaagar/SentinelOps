FROM python:3.11-slim

WORKDIR /app

# Copier uniquement requirements.txt d'abord : exploite le cache Docker
# pour éviter de réinstaller les dépendances à chaque modification de main.py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

# --host 0.0.0.0 est obligatoire : sans cela, l'app n'écoute que sur
# localhost à l'intérieur du conteneur et Prometheus (autre conteneur)
# ne pourra jamais la scraper.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]