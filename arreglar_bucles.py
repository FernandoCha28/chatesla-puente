# -*- coding: utf-8 -*-
import os, re, sys

WHILE = re.compile(r'^(\s*)while\b.*:\s*(#.*)?$')
PASS = re.compile(r'^(\s*)pass\s*(#.*)?$')
UNA_LINEA = re.compile(r'^(\s*)(while\b.*:)\s*pass\s*(#.*)?$')
PAUSA = "__import__('time').sleep(0.02)"


def arreglar(ruta):
    with open(ruta, encoding='utf-8', errors='ignore') as f:
        lineas = f.read().split('\n')

    cambios = 0
    for i, linea in enumerate(lineas):
        m = UNA_LINEA.match(linea)
        if m:
            lineas[i] = m.group(1) + m.group(2) + ' ' + PAUSA
            cambios += 1
            continue
        mw = WHILE.match(linea)
        if not mw or i + 1 >= len(lineas):
            continue
        mp = PASS.match(lineas[i + 1])
        if not mp or len(mp.group(1)) <= len(mw.group(1)):
            continue
        lineas[i + 1] = mp.group(1) + PAUSA
        cambios += 1

    if cambios:
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lineas))
    return cambios


def main(raiz):
    total, ficheros = 0, 0
    for base, _, nombres in os.walk(raiz):
        for n in nombres:
            if not n.endswith('.py'):
                continue
            c = arreglar(os.path.join(base, n))
            if c:
                total += c
                ficheros += 1
    print("    bucles arreglados: %d (en %d archivos)" % (total, ficheros))
    return total


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
