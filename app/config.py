import os

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "24"))
RUCAPTCHA_API_KEY = os.getenv("RUCAPTCHA_API_KEY", "")
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
