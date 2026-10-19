# HaxorGPT Luna 7.1

Bot de Telegram com IA (Python 3.11+, `python-telegram-bot` assíncrono, SQLite). Planos com cota, painel
administrativo dentro do Telegram, grupos/tópicos de fórum e vários provedores de IA configuráveis.

## Estado de verificação (leia antes de implantar)
Desenvolvido em ambiente **sem rede e sem pytest/python-telegram-bot/httpx instalados**. Portanto:
- ✅ Executados: 44 testes das camadas config, banco, assinaturas/cotas, administração, grupos, IA (com HTTP falso),
  segurança e suporte — rodados com um executor mínimo (ele só implementa `pytest.raises` e `tmp_path`), não com o pytest real.
  Rode `pytest` no seu ambiente para confirmar.
- ✅ Um teste de fumaça de `bot.py` com um pacote `telegram` **falso** (fluxo de comandos, divisão de mensagens, roteamento por tópico).
- ⚠️ **Não validado**: `bot.py`/`main.py` contra o `python-telegram-bot` real (versões em `requirements.txt` não foram instaladas),
  chamadas reais aos provedores de IA, e as URLs base de cada provedor (veja "Validação pendente").

## Requisitos
Python 3.11+, acesso à internet, token do bot (@BotFather) e ao menos uma chave de provedor de IA.

## Instalação
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # edite e preencha
```
Criar o bot: converse com @BotFather → `/newbot` → copie o token para `TELEGRAM_BOT_TOKEN`.
Descubra seu ID numérico (ex.: com `/id` neste bot depois de iniciá-lo) e coloque em `OWNER_TELEGRAM_ID`.
Para usar o bot em grupos, no @BotFather ajuste o *Group Privacy* conforme necessário (o bot responde a menções e respostas a ele).

## Provedores de IA
Suportados: OpenRouter, Groq, Together AI, DeepSeek, Mistral AI, Fireworks AI, Cerebras, xAI, NVIDIA, Hugging Face, Inference.net.
Todos são chamados via `POST {base_url}/chat/completions` (formato compatível com OpenAI) usando `httpx` — o SDK da OpenAI **não** é usado.
- Configure `DEFAULT_PROVIDER`, `DEFAULT_MODEL` e a `*_API_KEY` correspondente. **Nenhum nome de modelo vem embutido.**
- Alternativo opcional: `FALLBACK_PROVIDER` (+ `FALLBACK_MODEL` ou `<PROVEDOR>_MODEL`). Só é usado em falhas de autenticação, configuração,
  indisponibilidade, timeout ou limite de requisições — nunca em erro 400 (pedido inválido).
- Retentativas: `API_MAX_RETRIES` com backoff exponencial + jitter; `Retry-After` é respeitado (se maior que `API_BACKOFF_MAX_SECONDS`, não insiste).
  Atenção: uma retentativa após timeout pode gerar cobrança dupla no provedor (chamada não idempotente) — por isso o padrão é 2.
- Modelo por plano: `STANDARD_MODEL`, `PRO_MODEL`, `ULTRA_MODEL`, `FREE_PLAN_MODEL` (valem só para o provedor principal).

### Validação pendente
Confirme na documentação oficial de cada provedor (URLs em `api_providers.py`): base URL, compatibilidade com `chat/completions`,
nomes de modelos e cabeçalhos exigidos. Se algum divergir, ajuste `PROVIDERS` (isolado em um único dicionário).

## Planos e cotas
- **Gratuito**: `FREE_MESSAGE_LIMIT` (padrão 10) mensagens **vitalícias**, persistidas no SQLite; `/start`, `/reset` e reinícios não zeram.
- **Standard / Pro / Ultra**: `STANDARD_QUOTA`, `PRO_QUOTA`, `ULTRA_QUOTA` por período de `PLAN_PERIOD_DAYS` dias. Sem preços embutidos.
- Concessão manual: `/grant <id> <standard|pro|ultra> <dias>`; renovação `/renew`; cancelamento `/revoke` (com confirmação).
- Expiração é verificada a cada geração. O histórico de alterações fica em `subscription_events`.
- **O que consome cota**: uma geração concluída com sucesso = 1 mensagem. A cota é reservada atomicamente antes de chamar a IA e devolvida
  se houver falha (timeout, erro do provedor, erro interno). Entradas inválidas, bloqueios e negações por cota não consomem.
- **Pagamentos**: nenhum plano pago é ativado por callback/mensagem de usuário. `SubscriptionService.activate_from_payment` só funciona
  com um `PaymentVerifier` que confirme o pagamento — nenhum existe ainda, então hoje só administradores concedem planos.

## Administração
`OWNER_TELEGRAM_ID` é o proprietário. Total de **2 administradores** (incluindo o proprietário): `ADMIN_TELEGRAM_IDS` aceita 1 ID extra;
`/addadmin` e `/rmadmin` são exclusivos do proprietário; o proprietário não pode ser removido nem bloqueado. Permissões são checadas no servidor
em cada comando e a cada clique de botão. Painel: `/admin`.

## Comandos
Usuário: `/start /help /reset /historico /plano /status /id /about /cancel /instrucoes /suporte`.
Grupos: `/grupo_ativar /grupo_desativar /grupo_topico [limpar] /topico on|off /grupo_status` (administradores do grupo ou do bot).
Admin: `/admin /grant /renew /revoke /block /unblock /addadmin /rmadmin /users /events /setting /grupo_premium`.
Configurações permitidas em `/setting`: `maintenance` (0/1), `group_min_members`, `support_note`.

## Grupos e tópicos
Grupos começam **desativados** (`GROUPS_ENABLED_BY_DEFAULT=false`). O histórico é isolado por `(chat_id, message_thread_id)`; a resposta vai ao
tópico de origem. Com `/grupo_topico` definido, o bot só responde nesse tópico (exceto tópicos liberados com `/topico on`).
Recurso premium de grupo: exige `GROUP_PREMIUM_FEATURE_ENABLED=true`, `/grupo_premium on` por um admin do bot, contagem de membros **verificada
pela API do Telegram** (`getChatMemberCount`, validade 24 h) ≥ `GROUP_MIN_MEMBERS`, e uma assinatura paga concedida ao ID do grupo
(`/grant <chat_id> ...`). Se a consulta falhar, o número é tratado como desconhecido e nada é concedido.

## Execução local e testes
```bash
./run.sh                  # valida Python, venv, dependências e .env; inicia main.py
.venv/bin/pytest          # testes (não usam rede, tokens nem IA paga)
```

## Implantação em Debian/Linux (systemd)
```bash
sudo apt install -y python3 python3-venv sqlite3
sudo useradd -r -m -d /opt/haxorgpt haxorgpt
sudo -u haxorgpt git clone <seu-repo> /opt/haxorgpt/app   # ou copie os arquivos
cd /opt/haxorgpt/app && sudo -u haxorgpt python3 -m venv .venv && sudo -u haxorgpt .venv/bin/pip install -r requirements.txt
sudo -u haxorgpt cp .env.example .env && sudo chmod 600 .env && sudoedit .env
```
`/etc/systemd/system/haxorgpt.service`:
```ini
[Unit]
Description=HaxorGPT Luna
After=network-online.target
Wants=network-online.target

[Service]
User=haxorgpt
WorkingDirectory=/opt/haxorgpt/app
ExecStart=/opt/haxorgpt/app/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now haxorgpt
journalctl -u haxorgpt -f
```
Atualização: `git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart haxorgpt` (faça backup antes).

## Backup e restauração
O banco usa WAL; **não copie o arquivo `.db` com `cp` com o bot rodando**. Backup consistente:
```bash
sqlite3 data/haxorgpt.db ".backup 'backup-$(date +%F).db'"
```
Restauração: `systemctl stop haxorgpt`, remova `data/haxorgpt.db-wal` e `-shm`, copie o backup para `data/haxorgpt.db`, `systemctl start haxorgpt`.
Backups contêm histórico de conversas: proteja-os (permissões 600, criptografia) e respeite a retenção.

## Segurança — resumo
Segredos só no `.env` (ignorado pelo Git); logs passam por redação (tokens/chaves); o conteúdo das mensagens não é logado; callbacks são validados
por padrão estrito; SQL sempre parametrizado; sem `eval/exec`; chaves nunca vão ao banco nem aos provedores. Código Python distribuído **não** é
inviolável por ofuscação ou criptografia: a proteção vem de manter segredos fora do código e validar tudo no servidor.

## Privacidade e termos
`privacy_policy.md` e `terms.md` são **modelos** que exigem revisão humana/jurídica antes de publicar (veja os marcadores `[REVISAR]`).

## Solução de problemas
- `Configuração inválida: VARIÁVEL (...)` → corrija a variável citada (o valor nunca é exibido).
- "serviço de IA não configurado" → falta `DEFAULT_MODEL` ou a chave do provedor; use `/status`.
- `InvalidToken` na inicialização → token do BotFather incorreto.
- Bot não responde em grupo → ative com `/grupo_ativar`, mencione o bot e verifique `/grupo_status`.
- `database is locked` → confirme que há uma única instância rodando.

## Limites conhecidos
Limitação de frequência e confirmações administrativas ficam em memória (reiniciam com o bot); polling (sem webhook); sem integração de pagamento;
sem streaming de respostas; respostas em texto puro (sem Markdown) para evitar erros de formatação; bot/handlers não testados contra a biblioteca real.

## GitHub
**Repositório:** `git init && git add . && git commit -m "HaxorGPT Luna 7.1"` — `.env`, `data/` e logs já estão no `.gitignore`. Nunca faça commit do `.env`.

**CI (`.github/workflows/ci.yml`):** roda `compileall` e `pytest` em Python 3.11 e 3.12 a cada push/PR. Não usa segredos. É a primeira verificação real com pytest e `python-telegram-bot` instalados — confira o resultado.

**Executar o bot no Actions (`.github/workflows/run-bot.yml`), melhor esforço:**
1. Em *Settings → Secrets and variables → Actions* crie os *secrets* `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID` (e `ADMIN_TELEGRAM_IDS`, chaves `*_API_KEY` que usar) e as *variables* `DEFAULT_PROVIDER`, `DEFAULT_MODEL` (opcional: `SUPPORT_USERNAME`, `FALLBACK_*`).
2. Em *Actions → Run bot → Run workflow* (ou aguarde o agendamento a cada 6 h).

Limitações importantes:
- Cada job dura no máximo ~6 h; há pequenas lacunas entre execuções (o bot fica offline nesses intervalos; as mensagens pendentes são entregues depois pelo Telegram por até ~24 h).
- O banco é salvo como **cache do Actions** (backup SQLite consistente). Caches podem ser removidos (inatividade de 7 dias, limite de 10 GB), o que **zeraria cotas e histórico** — não é armazenamento confiável. Quem tem acesso ao repositório pode acessar o cache, que contém conversas.
- Em repositórios públicos, workflows agendados são desativados após 60 dias sem atividade, e o uso de Actions para bots de longa duração pode estar sujeito aos termos do GitHub **[REVISAR]**.
- Use **repositório privado** e prefira a VPS para produção. Para desenvolvimento no navegador, o projeto também roda em um Codespace: `pip install -r requirements.txt && pytest`.
- Nunca imprima segredos nos logs do workflow (o projeto já redige tokens/chaves).
