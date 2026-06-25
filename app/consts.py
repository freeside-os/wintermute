# --- Model Configuration ---
MODEL_TRIAGE = "gemini-3.5-flash"
MODEL_SCAFFOLD = "gemini-3.5-flash"
MODEL_REFINER = "gemini-3.5-flash"
MODEL_BUILDER = "gemini-3.1-pro-preview"
MODEL_MEM_SUMMARY = "gemini-3.5-flash"
MODEL_COMPACTION = "gemini-3.5-flash"
MODEL_RETRIES = 3

# --- Context Caching Settings ---
# Minimum tokens required to activate caching
CONTEXT_CACHE_MIN_TOKENS = 2048
# Caching Time-to-Live (TTL) in seconds
CONTEXT_CACHE_TTL_SECONDS = 3600  # 1 hour
# Interval of turns to refresh/re-cache
CONTEXT_CACHE_INTERVALS = 10

# --- Context Compaction Settings ---
# Number of turns before triggering history compaction
COMPACTION_INTERVAL = 30
# Number of turns retained in raw detail (overlap)
COMPACTION_OVERLAP = 5

# --- Tool Self-Healing Plugin Settings ---
# Max retry count for ReflectAndRetryToolPlugin
TOOL_MAX_RETRIES = 2

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
