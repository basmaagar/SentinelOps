FROM python:3.11-slim

WORKDIR /app

# Dépendances minimales : ce service ne fait qu'une chose, il n'a pas
# besoin de prometheus_client ni du reste de la pile applicative.
RUN pip install --no-cache-dir fastapi==0.115.0 uvicorn==0.30.6

COPY main.py .

EXPOSE 8000

# Un seul worker : la capacité de traitement est volontairement portée par
# le sémaphore applicatif, et non par le serveur. C'est ce qui rend l'effet
# de la mise à l'échelle prévisible et mesurable.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]