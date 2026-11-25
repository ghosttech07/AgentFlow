"""List Gemini models available to GEMINI_API_KEY that support generateContent.

Usage:  python scripts/list_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402

from app.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    if settings.gemini_api_key is None:
        print("GEMINI_API_KEY is not set (put it in .env).", file=sys.stderr)
        return 1
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    print(f"{'model':45} {'input':>9} {'output':>7}  display name")
    for model in client.models.list():
        if "generateContent" not in (model.supported_actions or []):
            continue
        name = (model.name or "").removeprefix("models/")
        print(f"{name:45} {model.input_token_limit or 0:>9} {model.output_token_limit or 0:>7}  {model.display_name}")
    print(f"\nConfigured GEMINI_MODEL: {settings.gemini_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
