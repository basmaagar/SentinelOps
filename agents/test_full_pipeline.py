from llm_client import OllamaClient
from graph import build_graph

client = OllamaClient(base_url="http://localhost:11435", timeout_seconds=60.0)
graph = build_graph(client, "qwen2.5:1.5b", client, "llama3.2:1b")

for i in range(10):
    graph.invoke({"ts": float(i), "metrics_sample": {"cpu": 10.0}, "log_lines": []})

result = graph.invoke({"ts": 11.0, "metrics_sample": {"cpu": 95.0}, "log_lines": []})
print(result["arbiter_verdict"])