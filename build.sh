#!/usr/bin/env bash
set -e

echo "==> 1/3 Instalando lo basico"
pip install -r requirements.txt

echo "==> 2/3 Instalando Exnova (con el arreglo)"
rm -rf /tmp/ex
mkdir -p /tmp/ex
git clone --depth 1 https://github.com/CassDs/ExnovaAPI.git /tmp/ex
touch /tmp/ex/exnovaapi/http/__init__.py
echo "    __init__.py puesto"
pip install --no-deps /tmp/ex

echo "==> 3/3 Instalando IQ Option"
pip install --no-deps git+https://github.com/Lu-Yi-Hsun/iqoptionapi.git || echo "    (IQ Option no se instalo; Exnova funciona igual)"

echo "==> Comprobando"
python -c "import exnovaapi.http, exnovaapi.stable_api; print('    Exnova: ok')"

echo "==> Listo"
