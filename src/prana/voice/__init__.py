"""prana.voice — the voice loop.

LiveKit Agents worker fronting an OpenAI realtime model, wake-gated by
a locally-trained "Narada" wake-word model, with cost guards. The
realtime model's tool surface is the session manager's VOICE TIER only
— reads and escalation; mutations require prana's judgment. That
boundary is enforced by which tools are registered on the model
(:mod:`prana.voice.tools`), never by prompt.

Requires: OPENAI_API_KEY (realtime is API spend, not subscription),
LIVEKIT_URL/API key/secret (from ~/.narada/.livekit.env), and the
trained wake model at ~/.narada/wakeword/output/narada/narada.onnx.
"""
