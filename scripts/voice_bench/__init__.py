"""Voice command-routing model benchmark (issue #234).

A thin HTTP client of the local-llm-hub that measures, per candidate model,
how reliably and how fast it turns a spoken utterance into a schema-valid
Tier-2 intent classification (intent + slots). It does not serve models or
wrap ``claude -p`` — it drives the hub's existing OpenAI-shape endpoint and
admin start/stop routes, so the hub stays the single owner of model serving.

Run: ``python -m scripts.voice_bench`` (see ``runner.py`` for CLI flags).
"""
