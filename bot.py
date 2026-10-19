"""Handlers do Telegram (python-telegram-bot v21+, API assíncrona)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

import keyboards as kb
from admin import PAGE_SIZE, AdminError, AdminService
from ai import AIError, AIService, GenerationFailed
from config import VERSION, Config
from database import Database
from groups import GroupService
from security import InvalidCallback, InputError, PermissionDenied, parse_callback, parse_user_id, safe_config_summary, sanitize_user_text
from subscriptions import QuotaExceeded, SubscriptionService
from support import SupportService
from utils import split_message

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _fmt_ts(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


def _thread(msg) -> int:
    return int(msg.message_thread_id) if msg is not None and getattr(msg, "is_topic_message", False) and msg.message_thread_id else 0


class LunaBot:
    def __init__(self, config: Config, db: Database, subs: SubscriptionService, admin: AdminService,
                 groups: GroupService, ai: AIService, client=None) -> None:
        self.config, self.db, self.subs, self.admin, self.groups, self.ai = config, db, subs, admin, groups, ai
        self.client = client  # ChatClient, fechado no encerramento
        self.support: Optional[SupportService] = None
        self.bot_username = ""
        self._bg: list[asyncio.Task] = []

    # ================= construção =================
    def build(self) -> Application:
        app = (ApplicationBuilder().token(self.config.telegram_token).concurrent_updates(True)
               .post_init(self._post_init).post_shutdown(self._post_shutdown).build())
        self.support = SupportService(self.config, self.db, lambda cid, text: app.bot.send_message(chat_id=cid, text=text))
        g = self._guard
        commands: dict[str, Handler] = {
            "start": g(self.cmd_start), "help": g(self.cmd_help), "reset": g(self.cmd_reset),
            "historico": g(self.cmd_historico), "plano": g(self.cmd_plano), "status": g(self.cmd_status),
            "id": g(self.cmd_id), "about": g(self.cmd_about), "cancel": g(self.cmd_cancel),
            "instrucoes": g(self.cmd_instrucoes), "suporte": g(self.cmd_suporte),
            "grupo_ativar": g(self.cmd_group_enable), "grupo_desativar": g(self.cmd_group_disable),
            "grupo_topico": g(self.cmd_group_topic), "topico": g(self.cmd_topic), "grupo_status": g(self.cmd_group_status),
            "grupo_premium": g(self.cmd_group_premium, admin_only=True),
            "admin": g(self.cmd_admin, admin_only=True), "grant": g(self.cmd_grant, admin_only=True),
            "renew": g(self.cmd_renew, admin_only=True), "revoke": g(self.cmd_revoke, admin_only=True),
            "block": g(self.cmd_block, admin_only=True), "unblock": g(self.cmd_unblock, admin_only=True),
            "addadmin": g(self.cmd_addadmin, admin_only=True), "rmadmin": g(self.cmd_rmadmin, admin_only=True),
            "users": g(self.cmd_users, admin_only=True), "events": g(self.cmd_events, admin_only=True),
            "setting": g(self.cmd_setting, admin_only=True),
        }
        for name, handler in commands.items():
            app.add_handler(CommandHandler(name, handler))
        app.add_handler(CallbackQueryHandler(g(self.on_callback)))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, g(self.on_text)))
        app.add_handler(ChatMemberHandler(self.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_error_handler(self.on_error)
        return app

    async def _post_init(self, app: Application) -> None:
        me = await app.bot.get_me()  # valida o token junto ao Telegram
        self.bot_username = me.username or ""
        log.info("bot iniciado como @%s", self.bot_username)
        self._bg.append(asyncio.create_task(self._retention_loop()))

    async def _post_shutdown(self, app: Application) -> None:
        for t in self._bg:
            t.cancel()
        if self.client is not None:
            await self.client.aclose()

    async def _retention_loop(self) -> None:
        while True:
            try:
                removed = await asyncio.to_thread(self.ai.purge_expired_history)
                if removed:
                    log.info("retenção: %d mensagens antigas removidas", removed)
            except Exception:  # noqa: BLE001
                log.exception("falha na limpeza de retenção")
            await asyncio.sleep(6 * 3600)

    # ================= infraestrutura =================
    def _guard(self, fn: Handler, admin_only: bool = False) -> Handler:
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user, chat = update.effective_user, update.effective_chat
            if user is None or chat is None or user.is_bot:
                return
            try:
                await asyncio.to_thread(self.db.upsert_user, user.id, user.username, user.first_name)
                if await asyncio.to_thread(self.db.is_blocked, user.id):
                    await self._say(update, context, "Seu acesso está bloqueado. Fale com o suporte se achar que é um engano.")
                    return
                if admin_only and not self.admin.is_admin(user.id):
                    await asyncio.to_thread(self.db.add_audit, user.id, "admin_denied", chat.id, False, "")
                    await self._say(update, context, "Comando restrito a administradores.")
                    return
                await fn(update, context)
            except PermissionDenied:
                await self._say(update, context, "Você não tem permissão para isso.")
            except AdminError as err:
                await self._say(update, context, str(err))
            except Exception:  # noqa: BLE001
                log.exception("erro em handler")
                await self._say(update, context, "Ocorreu um erro inesperado. Tente novamente mais tarde.")
        return handler

    async def _say(self, update: Update, context, text: str, markup: Optional[kb.Rows] = None) -> None:
        chat, msg = update.effective_chat, update.effective_message
        if chat is None:
            return
        chunks = split_message(text) or ["(sem conteúdo)"]
        thread = _thread(msg)
        try:
            for i, chunk in enumerate(chunks):  # ordem preservada
                await context.bot.send_message(chat_id=chat.id, text=chunk, message_thread_id=thread or None,
                                               reply_markup=kb.to_markup(markup) if markup and i == len(chunks) - 1 else None)
        except TelegramError:
            log.warning("falha ao enviar mensagem ao chat")

    async def _is_group_admin(self, context, chat_id: int, user_id: int) -> bool:
        if self.admin.is_admin(user_id):
            return True
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
        except TelegramError:
            return False
        return member.status in ("administrator", "creator")

    @staticmethod
    def _args(context) -> list[str]:
        return list(context.args or [])

    # ================= comandos gerais =================
    async def cmd_start(self, update, context):
        chat = update.effective_chat
        if chat.type in GROUP_TYPES:
            await self.asyncio_register(update)
            await self._say(update, context, "Olá! Um administrador do grupo pode ativar a IA com /grupo_ativar. "
                            "Depois, me mencione ou responda às minhas mensagens.")
            return
        free = self.config.free_message_limit
        await self._say(update, context, f"Olá! Sou o HaxorGPT Luna. No plano gratuito você tem {free} mensagens. "
                        "Escolha uma opção ou envie sua pergunta.", kb.main_menu())

    async def asyncio_register(self, update) -> None:
        chat, msg = update.effective_chat, update.effective_message
        await asyncio.to_thread(self.groups.register, chat.id, chat.title, _thread(msg))

    async def cmd_help(self, update, context):
        lines = ["/start – menu inicial", "/help – ajuda", "/reset – limpar histórico", "/historico – resumo do histórico",
                 "/plano – seu plano e saldo", "/status – estado do bot", "/id – seus IDs", "/about – sobre",
                 "/cancel – cancelar operação em andamento", "/instrucoes <texto> – instruções pessoais (privado)",
                 "/suporte – falar com o suporte",
                 "Grupos: /grupo_ativar, /grupo_desativar, /grupo_topico, /topico on|off, /grupo_status"]
        if self.admin.is_admin(update.effective_user.id):
            lines.append("Admin: /admin, /grant, /renew, /revoke, /block, /unblock, /addadmin, /rmadmin, /users, /events, /setting, /grupo_premium")
        await self._say(update, context, "\n".join(lines))

    async def cmd_reset(self, update, context):
        chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
        if chat.type in GROUP_TYPES and not await self._is_group_admin(context, chat.id, user.id):
            await self._say(update, context, "Só administradores do grupo podem limpar o histórico daqui.")
            return
        n = await self.ai.clear_history(chat.id, _thread(msg))
        await self._say(update, context, f"Histórico limpo ({n} mensagens). Seu saldo de mensagens não foi alterado.")

    async def cmd_historico(self, update, context):
        chat, msg = update.effective_chat, update.effective_message
        thread = _thread(msg)
        total = await asyncio.to_thread(self.db.count_messages, chat.id, thread)
        if chat.type in GROUP_TYPES:  # privacidade: não expõe conteúdo no grupo
            await self._say(update, context, f"Este tópico tem {total} mensagens no histórico (retenção: {self.config.history_retention_days} dias).")
            return
        rows = await asyncio.to_thread(self.db.recent_messages, chat.id, thread, 5)
        preview = "\n".join(f"{'Você' if r['role'] == 'user' else 'IA'}: {r['content'][:120]}" for r in rows) or "(vazio)"
        await self._say(update, context, f"Histórico: {total} mensagens (retenção: {self.config.history_retention_days} dias).\nÚltimas:\n{preview}")

    async def _plan_text(self, user_id: int) -> str:
        res = await asyncio.to_thread(self.subs.usage_status, user_id)
        if res.plan == "free":
            return (f"Plano: Gratuito\nUsadas: {res.used}/{res.quota} (limite vitalício)\nRestantes: {res.remaining}")
        return (f"Plano: {res.plan.capitalize()}\nUsadas neste período: {res.used}/{res.quota}\n"
                f"Restantes: {res.remaining}\nVence em: {_fmt_ts(res.expires_at)}")

    async def cmd_plano(self, update, context):
        await self._say(update, context, await self._plan_text(update.effective_user.id), kb.back_menu())

    def _plans_text(self) -> str:
        c = self.config
        lines = [f"Gratuito: {c.free_message_limit} mensagens (vitalício)"]
        for p in ("standard", "pro", "ultra"):
            lines.append(f"{p.capitalize()}: {c.plan_quotas[p]} mensagens a cada {c.plan_period_days} dias")
        lines.append("Planos pagos são ativados por um administrador. Fale com o suporte para contratar (valores: consulte o suporte).")
        return "\n".join(lines)

    async def cmd_status(self, update, context):
        s = safe_config_summary(self.config)
        maint = await asyncio.to_thread(self.db.get_setting, "maintenance", "0")
        await self._say(update, context, f"HaxorGPT Luna {VERSION}\nProvedor principal: {s['provider']} "
                        f"({'configurado' if s['provider'] in s['providers_configured'] else 'sem chave'})\n"
                        f"Alternativo: {s['fallback'] or 'nenhum'}\nModelo definido: {'sim' if s['model_configured'] else 'não'}\n"
                        f"Manutenção: {'sim' if maint == '1' else 'não'}")

    async def cmd_id(self, update, context):
        chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
        await self._say(update, context, f"Seu ID: {user.id}\nChat ID: {chat.id}\nTópico: {_thread(msg) or 'nenhum'}")

    async def cmd_about(self, update, context):
        await self._say(update, context, f"HaxorGPT Luna {VERSION} – assistente de IA para Telegram. "
                        "As respostas são geradas por provedores externos de IA e podem conter erros. Veja /help.")

    async def cmd_cancel(self, update, context):
        context.user_data.clear()
        await self._say(update, context, "Operação cancelada.")

    async def cmd_instrucoes(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE:
            await self._say(update, context, "Use /instrucoes em conversa privada comigo.")
            return
        text = " ".join(self._args(context)).strip()
        uid = update.effective_user.id
        if not text:
            cur = await asyncio.to_thread(self.db.get_custom_instructions, uid)
            await self._say(update, context, f"Instruções atuais: {cur or '(nenhuma)'}\nDefina com /instrucoes <texto> ou remova com /instrucoes limpar")
        elif text.lower() == "limpar":
            await asyncio.to_thread(self.db.set_custom_instructions, uid, None)
            await self._say(update, context, "Instruções removidas.")
        else:
            try:
                clean = sanitize_user_text(text, 500)
            except InputError:
                await self._say(update, context, "Instruções devem ter até 500 caracteres.")
                return
            await asyncio.to_thread(self.db.set_custom_instructions, uid, clean)
            await self._say(update, context, "Instruções salvas.")

    # ================= suporte / documentos =================
    async def cmd_suporte(self, update, context):
        await self._say(update, context, self.support.contact_text(), kb.support_menu(self.config.support_username))

    async def _send_doc(self, update, context, filename: str) -> None:
        try:
            text = (BASE_DIR / filename).read_text(encoding="utf-8")
        except OSError:
            text = "Documento indisponível no momento."
        await self._say(update, context, text, kb.back_menu())

    # ================= grupos =================
    async def _require_group(self, update, context) -> bool:
        if update.effective_chat.type not in GROUP_TYPES:
            await self._say(update, context, "Este comando só funciona em grupos.")
            return False
        await self.asyncio_register(update)
        return True

    async def cmd_group_enable(self, update, context):
        if not await self._require_group(update, context):
            return
        chat, user = update.effective_chat, update.effective_user
        is_gadmin = await self._is_group_admin(context, chat.id, user.id)
        await asyncio.to_thread(self.admin.set_group_enabled, user.id, chat.id, True, is_gadmin)
        await self._say(update, context, "IA ativada neste grupo. Me mencione ou responda às minhas mensagens.")

    async def cmd_group_disable(self, update, context):
        if not await self._require_group(update, context):
            return
        chat, user = update.effective_chat, update.effective_user
        is_gadmin = await self._is_group_admin(context, chat.id, user.id)
        await asyncio.to_thread(self.admin.set_group_enabled, user.id, chat.id, False, is_gadmin)
        await self._say(update, context, "IA desativada neste grupo.")

    async def cmd_group_topic(self, update, context):
        if not await self._require_group(update, context):
            return
        chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
        is_gadmin = await self._is_group_admin(context, chat.id, user.id)
        if self._args(context)[:1] == ["limpar"]:
            await asyncio.to_thread(self.admin.set_group_default_topic, user.id, chat.id, None, is_gadmin)
            await self._say(update, context, "Tópico padrão removido: respondo em qualquer tópico ativo.")
            return
        thread = _thread(msg)
        if not thread:
            await self._say(update, context, "Envie /grupo_topico dentro do tópico que deseja definir como padrão.")
            return
        await asyncio.to_thread(self.admin.set_group_default_topic, user.id, chat.id, thread, is_gadmin)
        await self._say(update, context, "Este tópico agora é o padrão: só respondo aqui (exceto tópicos ativados com /topico on).")

    async def cmd_topic(self, update, context):
        if not await self._require_group(update, context):
            return
        chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
        arg = (self._args(context) or [""])[0].lower()
        thread = _thread(msg)
        if arg not in ("on", "off") or not thread:
            await self._say(update, context, "Use /topico on ou /topico off dentro de um tópico do fórum.")
            return
        if not await self._is_group_admin(context, chat.id, user.id):
            raise PermissionDenied("tópico")
        await asyncio.to_thread(self.groups.enable_topic, chat.id, thread, arg == "on")
        await asyncio.to_thread(self.db.add_audit, user.id, f"topic_{arg}", chat.id, True, str(thread))
        await self._say(update, context, f"IA {'ativada' if arg == 'on' else 'desativada'} neste tópico.")

    async def _bot_permission_note(self, context, chat_id: int) -> str:
        try:
            m = await context.bot.get_chat_member(chat_id, context.bot.id)
        except TelegramError:
            return "não foi possível verificar"
        if m.status in ("administrator", "creator"):
            return "administrador"
        if m.status == "member":
            return "membro (envio permitido, salvo restrições do grupo)"
        if m.status == "restricted":
            return "ok" if getattr(m, "can_send_messages", False) else "SEM permissão para enviar mensagens"
        return "fora do grupo"

    async def _refresh_members(self, context, chat_id: int) -> Optional[int]:
        return await self.groups.refresh_member_count(chat_id, context.bot.get_chat_member_count)

    async def cmd_group_status(self, update, context):
        if not await self._require_group(update, context):
            return
        chat = update.effective_chat
        count = await self._refresh_members(context, chat.id)
        g = await asyncio.to_thread(self.db.get_group, chat.id)
        elig = await asyncio.to_thread(self.groups.premium_eligible, chat.id)
        await self._say(update, context,
                        f"Grupo ativo: {'sim' if g['enabled'] else 'não'}\nTópico padrão: {g['default_thread_id'] or 'nenhum'}\n"
                        f"Membros (verificado): {count if count is not None else 'desconhecido'}\n"
                        f"Recurso premium: {'elegível' if elig else 'não elegível'} (mínimo {self.groups.min_members()})\n"
                        f"Permissão do bot: {await self._bot_permission_note(context, chat.id)}")

    async def cmd_group_premium(self, update, context):
        if not await self._require_group(update, context):
            return
        arg = (self._args(context) or [""])[0].lower()
        if arg not in ("on", "off"):
            await self._say(update, context, "Use /grupo_premium on ou /grupo_premium off.")
            return
        await asyncio.to_thread(self.admin.set_group_premium, update.effective_user.id, update.effective_chat.id, arg == "on")
        if arg == "on":
            await self._refresh_members(context, update.effective_chat.id)
        await self._say(update, context, f"Recurso premium {'habilitado' if arg == 'on' else 'desabilitado'} para este grupo "
                        "(só vale se houver contagem verificada suficiente e política ativa).")

    # ================= administração =================
    async def cmd_admin(self, update, context):
        await self._say(update, context, "Painel administrativo:", kb.admin_panel())

    def _uid_arg(self, context, idx: int = 0) -> int:
        args = self._args(context)
        if len(args) <= idx:
            raise AdminError("Argumento ausente. Veja /help.")
        try:
            return parse_user_id(args[idx])
        except InputError:
            raise AdminError("ID de usuário inválido.") from None

    @staticmethod
    def _days(args: list[str], idx: int) -> int:
        try:
            return int(args[idx])
        except (IndexError, ValueError):
            raise AdminError("Informe a duração em dias (número).") from None

    async def cmd_grant(self, update, context):
        args, actor = self._args(context), update.effective_user.id
        target = self._uid_arg(context)
        if len(args) < 3:
            raise AdminError("Uso: /grant <user_id> <standard|pro|ultra> <dias>")
        await asyncio.to_thread(self.admin.grant, actor, target, args[1].lower(), self._days(args, 2))
        await self._say(update, context, f"Plano {args[1].lower()} concedido a {target} por {self._days(args, 2)} dias.")

    async def cmd_renew(self, update, context):
        args, target = self._args(context), self._uid_arg(context)
        await asyncio.to_thread(self.admin.renew, update.effective_user.id, target, self._days(args, 1))
        await self._say(update, context, "Assinatura renovada.")

    async def _ask_confirm(self, update, context, action: str, args: tuple, text: str) -> None:
        token = self.admin.confirmations.create(update.effective_user.id, action, args)
        await self._say(update, context, text, kb.confirm(token))

    async def cmd_revoke(self, update, context):
        target = self._uid_arg(context)
        await self._ask_confirm(update, context, "revoke", (target,), f"Confirmar revogação da assinatura de {target}?")

    async def cmd_rmadmin(self, update, context):
        target = self._uid_arg(context)
        await self._ask_confirm(update, context, "rmadmin", (target,), f"Confirmar remoção do administrador {target}?")

    async def cmd_block(self, update, context):
        target = self._uid_arg(context)
        reason = " ".join(self._args(context)[1:])
        await asyncio.to_thread(self.admin.block, update.effective_user.id, target, reason)
        await self._say(update, context, f"Usuário {target} bloqueado.")

    async def cmd_unblock(self, update, context):
        target = self._uid_arg(context)
        done = await asyncio.to_thread(self.admin.unblock, update.effective_user.id, target)
        await self._say(update, context, "Usuário desbloqueado." if done else "Esse usuário não estava bloqueado.")

    async def cmd_addadmin(self, update, context):
        target = self._uid_arg(context)
        await asyncio.to_thread(self.admin.add_admin, update.effective_user.id, target)
        await self._say(update, context, f"{target} agora é administrador.")

    async def cmd_users(self, update, context):
        first = (self._args(context) or ["0"])[0]
        await self._show_users(update, context, int(first) if first.isdigit() else 0)

    async def _show_users(self, update, context, page: int):
        rows, total = await asyncio.to_thread(self.admin.list_users, update.effective_user.id, page)
        body = "\n".join(f"{r['user_id']} @{r['username'] or '-'} [{r['plan']}]{' BLOQ' if r['blocked'] else ''}" for r in rows) or "(sem usuários)"
        await self._say(update, context, f"Usuários (pág. {page + 1}, total {total}):\n{body}", kb.pagination("adm:users", page, total, PAGE_SIZE))

    async def cmd_events(self, update, context):
        await self._show_events(update, context, 0)

    async def _show_events(self, update, context, page: int):
        rows, total = await asyncio.to_thread(self.admin.list_events, update.effective_user.id, page)
        body = "\n".join(f"{_fmt_ts(r['ts'])} {r['actor']} {r['action']} {r['target']} {r['detail']}" for r in rows) or "(sem eventos)"
        await self._say(update, context, f"Eventos (pág. {page + 1}, total {total}):\n{body}", kb.pagination("adm:events", page, total, PAGE_SIZE))

    async def cmd_setting(self, update, context):
        args, actor = self._args(context), update.effective_user.id
        if len(args) < 2:
            cur = await asyncio.to_thread(self.admin.get_settings, actor)
            await self._say(update, context, "Configurações:\n" + "\n".join(f"{k}={v or '-'}" for k, v in cur.items()) + "\nUso: /setting <chave> <valor>")
            return
        await asyncio.to_thread(self.admin.set_setting, actor, args[0], " ".join(args[1:]))
        await self._say(update, context, "Configuração atualizada.")

    async def _run_confirmed(self, actor: int, action: str, args: tuple) -> str:
        if action == "revoke":
            done = await asyncio.to_thread(self.admin.revoke, actor, args[0])
            return "Assinatura revogada." if done else "O usuário não tinha assinatura ativa."
        if action == "rmadmin":
            await asyncio.to_thread(self.admin.remove_admin, actor, args[0])
            return "Administrador removido."
        raise AdminError("Ação desconhecida.")

    # ================= callbacks =================
    async def on_callback(self, update, context):
        query = update.callback_query
        if query is None:
            return
        try:
            await query.answer()
        except TelegramError:
            pass
        try:
            parts = parse_callback(query.data)
        except InvalidCallback:
            return
        user, chat, msg = update.effective_user, update.effective_chat, update.effective_message
        head = parts[0]
        if head == "menu" and len(parts) == 2:
            await self._menu(update, context, parts[1])
        elif head == "clear" and len(parts) == 2:
            if parts[1] == "yes":
                if chat.type in GROUP_TYPES and not await self._is_group_admin(context, chat.id, user.id):
                    raise PermissionDenied("clear")
                n = await self.ai.clear_history(chat.id, _thread(msg))
                await self._say(update, context, f"Histórico limpo ({n} mensagens).", kb.back_menu())
            else:
                await self._say(update, context, "Cancelado.", kb.main_menu())
        elif head == "adm":
            self.admin.require_admin(user.id)  # revalidado a cada clique
            await self._admin_cb(update, context, parts)

    async def _menu(self, update, context, item: str) -> None:
        uid = update.effective_user.id
        if item == "home":
            await self._say(update, context, "Menu principal:", kb.main_menu())
        elif item == "chat":
            await self._say(update, context, "Pode enviar sua mensagem agora. (Em grupos, me mencione.)", kb.back_menu())
        elif item == "plan":
            await self._say(update, context, await self._plan_text(uid), kb.back_menu())
        elif item == "plans":
            await self._say(update, context, self._plans_text(), kb.back_menu())
        elif item == "support":
            await self._say(update, context, self.support.contact_text(), kb.support_menu(self.config.support_username))
        elif item == "ticket":
            context.user_data["awaiting_support"] = True
            await self._say(update, context, "Escreva sua mensagem para o suporte (ou /cancel).")
        elif item == "privacy":
            await self._send_doc(update, context, "privacy_policy.md")
        elif item == "terms":
            await self._send_doc(update, context, "terms.md")
        elif item == "clear":
            await self._say(update, context, "Limpar o histórico desta conversa? Seu saldo de mensagens não muda.", kb.clear_confirm())

    async def _admin_cb(self, update, context, parts: list[str]) -> None:
        actor = update.effective_user.id
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "stats":
            s = await asyncio.to_thread(self.admin.stats, actor)
            await self._say(update, context, "\n".join(f"{k}: {v}" for k, v in s.items()), kb.admin_panel())
        elif sub == "users" and len(parts) == 3 and parts[2].isdigit():
            await self._show_users(update, context, int(parts[2]))
        elif sub == "events" and len(parts) == 3 and parts[2].isdigit():
            await self._show_events(update, context, int(parts[2]))
        elif sub == "settings":
            cur = await asyncio.to_thread(self.admin.get_settings, actor)
            await self._say(update, context, "\n".join(f"{k}={v or '-'}" for k, v in cur.items()) + "\nAltere com /setting <chave> <valor>", kb.admin_panel())
        elif sub == "cf" and len(parts) == 3:
            action, args = self.admin.confirmations.consume(parts[2], actor)
            await self._say(update, context, await self._run_confirmed(actor, action, args))
        elif sub == "cx" and len(parts) == 3:
            self.admin.confirmations.cancel(parts[2], actor)
            await self._say(update, context, "Operação cancelada.")

    # ================= mensagens de texto =================
    def _addressed(self, msg, bot_id: int) -> bool:
        text = (msg.text or "").lower()
        if self.bot_username and f"@{self.bot_username.lower()}" in text:
            return True
        rep = msg.reply_to_message
        return bool(rep and rep.from_user and rep.from_user.id == bot_id)

    async def on_text(self, update, context):
        msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if msg is None or not (msg.text or "").strip():
            await self._say(update, context, "Envie uma mensagem de texto.")
            return
        thread = _thread(msg)
        text = msg.text
        if context.user_data.get("awaiting_support"):
            context.user_data.pop("awaiting_support", None)
            res = await self.support.forward_to_admins(user.id, user.username, text)
            await self._say(update, context, "Mensagem enviada à equipe de suporte." if res.ok else
                            "Não consegui encaminhar agora. " + self.support.contact_text())
            return
        is_group = chat.type in GROUP_TYPES
        if is_group:
            await self.asyncio_register(update)
            if not self._addressed(msg, context.bot.id):
                return
            if not await asyncio.to_thread(self.groups.can_respond, chat.id, thread):
                g = await asyncio.to_thread(self.db.get_group, chat.id)
                if g is not None and not g["enabled"]:
                    await self._say(update, context, "A IA ainda não está ativada neste grupo. Um administrador pode usar /grupo_ativar.")
                return
            if self.bot_username:
                text = re.sub(f"@{re.escape(self.bot_username)}", "", text, flags=re.I)
            if self.config.group_premium_feature_enabled:
                g = await asyncio.to_thread(self.db.get_group, chat.id)
                stale = g is not None and (g["member_count_checked_at"] is None or self.groups.clock() - g["member_count_checked_at"] > 3600)
                if g and g["premium_enabled"] and stale:
                    await self._refresh_members(context, chat.id)
        try:
            await context.bot.send_chat_action(chat.id, ChatAction.TYPING, message_thread_id=thread or None)
        except TelegramError:
            pass
        try:
            result = await self.ai.generate(user.id, chat.id, thread, text, is_group)
        except QuotaExceeded as err:
            await self._say(update, context, f"Você usou todas as mensagens do plano {err.plan}. Veja /plano e os planos disponíveis.",
                            [[kb.Button("💎 Planos", "menu:plans"), kb.Button("🛟 Suporte", "menu:support")]])
            return
        except GenerationFailed as err:
            await self._say(update, context, err.user_message)
            return
        except AIError as err:
            await self._say(update, context, err.user_message)
            return
        reply = result.text
        if result.reservation.plan == "free":
            reply += f"\n\n(Mensagens restantes no plano gratuito: {result.reservation.remaining})"
        await self._say(update, context, reply)

    # ================= eventos =================
    async def on_my_chat_member(self, update, context):
        ev = update.my_chat_member
        if ev and ev.chat.type in GROUP_TYPES and ev.new_chat_member.status in ("member", "administrator"):
            await asyncio.to_thread(self.groups.register, ev.chat.id, ev.chat.title)
            await asyncio.to_thread(self.db.add_audit, ev.from_user.id if ev.from_user else None, "bot_added", ev.chat.id, True, "")

    async def on_error(self, update, context):
        log.error("erro não tratado: %s", type(context.error).__name__, exc_info=context.error)
