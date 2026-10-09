"""Pre-deploy check: does the native /api/generate path (engine._compat_options) give the SAME text as Ollama's
/v1/completions for the same job? Run against any Ollama serving the baked model — local, or the built image:

    python tools/parity_check.py [http://localhost:11434] [model]

Every case should print SAME (each side runs three times; see RUNS). The no-temperature case is the one that matters most: Alsion's main narrative call
sends no temperature or top_p, so the native path must reproduce the OpenAI layer's defaults. A DIFF means
_COMPAT_DEFAULTS no longer match this Ollama version — ship with OLLAMA_NATIVE_COMPLETIONS=0 until fixed.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from engine import _compat_options  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:11434"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "hf.co/TheDrummer/Cydonia-24B-v4.3-GGUF:Q4_K_M"


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


# Alsion's three shapes (default / conservative / diverse) plus a stop list. A seed makes the hot ones comparable.
CASES = [
    ("Write one sentence about rain.", {"max_tokens": 40, "frequency_penalty": 0.3, "seed": 3}),
    ("List three fruits, comma separated:", {"temperature": 0.1, "top_p": 0.55, "top_k": 15, "max_tokens": 30,
                                             "seed": 7}),
    ("Write one sentence about a lighthouse at night.", {"temperature": 1.15, "top_p": 0.95, "top_k": 80,
                                                         "repeat_penalty": 1.3, "frequency_penalty": 0.8,
                                                         "max_tokens": 40, "seed": 11}),
    ("Count: one, two,", {"temperature": 0.7, "max_tokens": 20, "seed": 5, "stop": ["five"]}),
]

# A seeded call is NOT always reproducible on the GPU: at temperature ~1, /v1/completions itself gave two different
# texts in three identical runs (Ollama 0.35.1, 2026-10-09). So each side runs RUNS times and a case passes when the
# two sets of texts overlap; with the model's own defaults instead of the compat ones they never did.
RUNS = 3

failed = 0
for prompt, params in CASES:
    # What the OpenAI route receives: top_k / repeat_penalty ride in the body and are ignored there.
    a = {post("/v1/completions", {"model": MODEL, "prompt": prompt, **params})["choices"][0]["text"]
         for _ in range(RUNS)}
    b = {post("/api/generate", {"model": MODEL, "prompt": prompt, "stream": False,
                                "options": _compat_options(params)})["response"] for _ in range(RUNS)}
    same = bool(a & b)
    failed += not same
    noisy = f"  (run-to-run noise: /v1 {len(a)} distinct, native {len(b)})" if len(a) > 1 or len(b) > 1 else ""
    print("SAME" if same else "DIFF", json.dumps(params) + noisy)
    if not same:
        print("   /v1  :", [t[:100] for t in a])
        print("   /api :", [t[:100] for t in b])

e1 = post("/v1/embeddings", {"model": "nomic-embed-text", "input": "The quick brown fox"})
e2 = post("/api/embed", {"model": "nomic-embed-text", "input": "The quick brown fox"})
same = e1["data"][0]["embedding"] == e2["embeddings"][0]
failed += not same
print("SAME" if same else "DIFF", "embeddings", {k: e2.get(k) for k in ("load_duration", "total_duration")})
sys.exit(1 if failed else 0)
