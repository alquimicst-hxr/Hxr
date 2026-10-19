#!/usr/bin/env bash
# Inicia o HaxorGPT Luna. Não imprime segredos e não instala nada globalmente.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERRO: python3 não encontrado. Instale Python 3.11+ (ex.: sudo apt install python3 python3-venv)." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "ERRO: é necessário Python 3.11 ou superior." >&2
  exit 1
fi

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . ".venv/bin/activate"
else
  echo "AVISO: .venv não encontrado; usando o Python do sistema. Recomendado: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
fi

missing=()
for mod in telegram httpx dotenv; do
  python3 -c "import $mod" >/dev/null 2>&1 || missing+=("$mod")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERRO: dependências ausentes: ${missing[*]}. Execute: pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERRO: arquivo .env não encontrado. Copie .env.example para .env e preencha." >&2
  exit 1
fi

exec python3 main.py
