# FrontierPulse Data

Unofficial digest data for the FrontierPulse iOS app — bilingual summaries of
what the major AI labs (OpenAI, Anthropic, Google DeepMind, Meta AI, xAI, and
the open-source frontier) announced, refreshed throughout the day, plus a cumulative
model-release tracker (`models.json`).

Not affiliated with any of these companies. Summaries are AI-generated with
links to original sources; see `legal/` for privacy policy and terms.

- `latest.json` — most recent day
- `index.json` — list of all days
- `digests/YYYY-MM-DD.json` — one day of stories
- `models.json` — model release tracker
- `backend/model_history_seed.json` — official-source-verified historical baseline merged on every run
- `backend/` — generator (`ai_digest_webgrok.py` via logged-in Grok Web by default;
  `ai_digest.py` retains CLI/API fallback and owns the output schema/merge logic)
