#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON="${LOCAL_IA_INSTALL_PYTHON:-$(command -v python3)}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Se necesita Python 3.10+"'

echo '[1/3] Preparando la API...'
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt

echo '[2/3] Buscando el motor MLX...'
MLX_PYTHON="${LOCAL_IA_MLX_PYTHON:-$PYTHON}"
if ! "$MLX_PYTHON" -c 'from importlib.metadata import version; assert tuple(int(p) for p in version("mlx-lm").split(".")[:3]) >= (0, 32, 2)' 2>/dev/null; then
    .venv/bin/python -m pip install -r requirements-mlx.txt
    MLX_PYTHON="$PROJECT_DIR/.venv/bin/python"
fi

echo '[3/3] Guardando configuración local...'
if [ ! -f .env ]; then
    cp .env.example .env
fi
# Preserve a Python path explicitly configured by the user.
LOCAL_IA_DETECTED_PYTHON="$MLX_PYTHON" .venv/bin/python - <<'PY'
import os
from dotenv import dotenv_values, set_key
if not dotenv_values('.env').get('LOCAL_IA_MLX_PYTHON'):
    set_key('.env', 'LOCAL_IA_MLX_PYTHON', os.environ['LOCAL_IA_DETECTED_PYTHON'])
PY
chmod +x local-ia
echo 'Listo. Inicia con ./local-ia start y conversa con ./local-ia chat'
echo 'El modelo se carga con la primera consulta. No se ha instalado un servicio de inicio automático.'
