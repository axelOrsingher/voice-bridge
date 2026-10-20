"""Select bilingual status messages using VOZTRAD_LANG (en/es)."""
import os


def text(en: str, es: str) -> str:
    return es if os.environ.get("VOZTRAD_LANG", "en").lower().startswith("es") else en
