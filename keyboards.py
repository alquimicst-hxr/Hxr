"""Menus inline. Os builders retornam estruturas simples (testáveis sem
Telegram); `to_markup` converte para InlineKeyboardMarkup."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Button:
    text: str
    callback: Optional[str] = None
    url: Optional[str] = None


Rows = list[list[Button]]


def to_markup(rows: Rows):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # import tardio
    return InlineKeyboardMarkup([[InlineKeyboardButton(b.text, callback_data=b.callback, url=b.url) for b in r] for r in rows])


def main_menu() -> Rows:
    return [
        [Button("💬 Conversar com a IA", "menu:chat")],
        [Button("📊 Meu plano", "menu:plan"), Button("💎 Planos", "menu:plans")],
        [Button("🛟 Suporte", "menu:support")],
        [Button("🔒 Privacidade", "menu:privacy"), Button("📄 Termos", "menu:terms")],
        [Button("🧹 Limpar histórico", "menu:clear")],
    ]


def back_menu() -> Rows:
    return [[Button("⬅️ Menu", "menu:home")]]


def clear_confirm() -> Rows:
    return [[Button("✅ Sim, limpar", "clear:yes"), Button("✖️ Cancelar", "clear:no")]]


def support_menu(username: str = "") -> Rows:
    rows: Rows = []
    if username:
        rows.append([Button("Abrir chat do suporte", url=f"https://t.me/{username}")])
    rows.append([Button("✉️ Enviar mensagem", "menu:ticket")])
    rows.append([Button("⬅️ Menu", "menu:home")])
    return rows


def admin_panel() -> Rows:
    return [
        [Button("📈 Estatísticas", "adm:stats"), Button("👥 Usuários", "adm:users:0")],
        [Button("🧾 Eventos", "adm:events:0"), Button("⚙️ Configurações", "adm:settings")],
    ]


def pagination(prefix: str, page: int, total: int, page_size: int) -> Rows:
    row: list[Button] = []
    if page > 0:
        row.append(Button("◀️", f"{prefix}:{page - 1}"))
    if (page + 1) * page_size < total:
        row.append(Button("▶️", f"{prefix}:{page + 1}"))
    return [row] if row else []


def confirm(token: str) -> Rows:
    return [[Button("✅ Confirmar", f"adm:cf:{token}"), Button("✖️ Cancelar", f"adm:cx:{token}")]]
