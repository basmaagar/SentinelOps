from llm_client import OllamaClient
from graph import build_graph

metrics_client = OllamaClient()
logs_client = OllamaClient()
graph = build_graph(metrics_client, 'qwen2.5:1.5b', logs_client, 'llama3.2:1b')

# Ligne de base : CPU normal pendant 10 échantillons
for i in range(10):
    graph.invoke({'ts': float(i), 'metrics_sample': {'cpu': 10.0}, 'log_lines': []})

# Le pic, maintenant comparé à une vraie ligne de base
result = graph.invoke({'ts': 11.0, 'metrics_sample': {'cpu': 95.0}, 'log_lines': []})
print(result)