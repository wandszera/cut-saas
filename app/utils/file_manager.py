from app.services.storage import get_storage


def ensure_directories():
    get_storage().ensure_default_prefixes(["downloads", "transcripts", "clips", "temp", "exports", "uploads"])
