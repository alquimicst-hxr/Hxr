"""Utilitários independentes do Telegram."""
from __future__ import annotations

TELEGRAM_SAFE_LIMIT = 4000  # limite real: 4096; margem de segurança


def split_message(text: str, limit: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    """Divide texto preservando a ordem; cada parte tem no máximo `limit`
    caracteres. Prefere quebrar em linhas, depois em espaços."""
    if limit < 1:
        raise ValueError("limit deve ser >= 1")
    text = (text or "").strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        part = text[:cut].rstrip()
        if part:
            chunks.append(part)
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
