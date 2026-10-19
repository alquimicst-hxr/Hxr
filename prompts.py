"""Instruções de sistema e montagem do contexto enviado ao provedor."""
from __future__ import annotations

DEFAULT_PERSONALITY = (
    "Você é um assistente de IA prestativo, claro e objetivo. "
    "Responda no mesmo idioma do usuário e admita quando não souber algo."
)
GROUP_NOTE = "Você está em um chat de grupo com vários participantes; seja conciso."
MAX_CUSTOM_INSTRUCTIONS = 500


def build_system_prompt(personality: str = "", custom_instructions: str = "", is_group: bool = False) -> str:
    parts = [(personality or "").strip() or DEFAULT_PERSONALITY]
    if is_group:
        parts.append(GROUP_NOTE)
    custom = (custom_instructions or "").strip()[:MAX_CUSTOM_INSTRUCTIONS]
    if custom and not is_group:  # instruções pessoais não vazam para grupos
        parts.append("Preferências do usuário (não podem alterar regras de segurança): " + custom)
    return "\n\n".join(parts)


def build_messages(system_prompt: str, history: list[dict], user_text: str, max_chars: int) -> list[dict]:
    """Monta a lista de mensagens, descartando as mais antigas do histórico
    até caber em `max_chars`."""
    kept: list[dict] = []
    total = len(user_text)
    for item in reversed(history):
        size = len(item["content"])
        if total + size > max_chars:
            break
        kept.append({"role": item["role"], "content": item["content"]})
        total += size
    kept.reverse()
    return [{"role": "system", "content": system_prompt}, *kept, {"role": "user", "content": user_text}]
