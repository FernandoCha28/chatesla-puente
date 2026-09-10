#!/usr/bin/env bash
set -e

echo "==> 1/4 Instalando lo basico"
pip install -r requirements.txt

echo "==> 2/4 Instalando Exnova (con los arreglos)"
rm -rf /tmp/ex
mkdir -p /tmp/ex
git clone --depth 1 https://github.com/CassDs/ExnovaAPI.git /tmp/ex
touch /tmp/ex/exnovaapi/http/__init__.py
echo "    __init__.py puesto"
python arreglar_bucles.py /tmp/ex/exnovaapi
pip install --no-deps /tmp/ex

echo "==> 3/4 Instalando IQ Option (con los mismos arreglos)"
rm -rf /tmp/iq
mkdir -p /tmp/iq
if git clone --depth 1 https://github.com/Lu-Yi-Hsun/iqoptionapi.git /tmp/iq; then
  python arreglar_bucles.py /tmp/iq/iqoptionapi
  pip install --no-deps /tmp/iq
else
  echo "    (IQ Option no se pudo bajar; Exnova funciona igual)"
fi

echo "==> 4/4 Comprobando"
python -c "
import exnovaapi.http, exnovaapi.stable_api
print('    Exnova: ok')
try:
    import iqoptionapi.stable_api
    print('    IQ Option: ok')
except Exception as e:
    print('    IQ Option: no disponible (%s)' % e)
import websocket
print('    websocket-client:', getattr(websocket, '__version__', '?'))
"

echo "==> Listo"
