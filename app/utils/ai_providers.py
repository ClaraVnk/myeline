"""Shared LLM provider catalogue for BYOK (bring-your-own-key).

Single source of truth used by both org-level BYOK (``app/routes/org.py``)
and user-level BYOK (``app/routes/user.py`` + ``app/services/rag_service.py``).
Keeping the lists here avoids the two surfaces drifting apart when a new
provider or default model is added.
"""

# Providers a customer can bring their own API key for.
ALLOWED_PROVIDERS = ("mistral", "openai", "gemini", "claude")

# Providers a FREE user may pick: their key must power BOTH indexing
# (embeddings) AND answers, since Free has no platform fallback. Mistral,
# OpenAI and Gemini all expose an embedding API; Anthropic Claude does NOT,
# so Claude is Pro-only (Pro indexes with the included platform Mistral).
FREE_PROVIDERS = ("mistral", "openai", "gemini")

# Only Mistral can run on the platform-supplied key. Every other provider
# requires the customer's own key.
PLATFORM_KEY_ENV = {
    "mistral": "MISTRAL_API_KEY",
}

# Default model per provider when the customer doesn't override it.
PROVIDER_DEFAULT_MODELS = {
    "mistral": "mistral-medium-latest",
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "claude": "claude-haiku-4-5-20251001",
}

# Human-readable labels for flash messages and UI.
PROVIDER_LABELS = {
    "mistral": "Mistral",
    "gemini": "Google Gemini",
    "openai": "OpenAI",
    "claude": "Anthropic Claude",
}
