"""影音轉逐字稿核心套件"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"
