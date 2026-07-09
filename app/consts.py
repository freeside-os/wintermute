# --- Model Configuration ---
MODEL_TRIAGE = "gemini-3.5-flash"
MODEL_SCAFFOLD = "gemini-3.5-flash"
MODEL_REFINER = "gemini-3.5-flash"
MODEL_BUILDER = "gemini-3.1-pro-preview"
MODEL_RETRIES = 3

# --- Network Retry Settings ---
# Number of retry attempts for network/API calls
NETWORK_RETRY_TRIES = 3
# Initial delay in seconds before retrying
NETWORK_RETRY_DELAY = 1.0
# Backoff multiplier for delay
NETWORK_RETRY_BACKOFF = 2.0

# --- Cache Windows ---
# Security feeds local cache TTL
SECURITY_FEED_CACHE_TTL_SECONDS = 86400  # 24 hours

