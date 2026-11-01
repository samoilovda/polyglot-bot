import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Тесты не должны ходить в сеть или запускать claude CLI: по умолчанию — один провайдер, который мокается через requests.post.
os.environ["LLM_PROVIDERS"] = "openrouter"
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
