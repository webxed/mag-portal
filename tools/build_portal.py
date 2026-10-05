#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка портала для приставок из шаблона.

Берёт ai_config/portal.template.html, подставляет базовый адрес сервера
(из ai_config/www/config.php или из --host/--port) и пишет:
  * ai_config/services.html
  * ai_config/www/services.html

Примеры
-------
    python tools/build_portal.py                      # адрес из config.php
    python tools/build_portal.py --host 10.0.0.5 --port 8080
"""

from __future__ import annotations

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO_ROOT, "ai_config", "portal.template.html")
CONFIG_PHP = os.path.join(REPO_ROOT, "ai_config", "www", "config.php")
OUTPUTS = [
    os.path.join(REPO_ROOT, "ai_config", "services.html"),
    os.path.join(REPO_ROOT, "ai_config", "www", "services.html"),
]


def read_config_php() -> tuple[str, int]:
    """Достаёт SERVER_HOST/SERVER_PORT из config.php."""
    host, port = "192.168.1.100", 8088
    try:
        text = open(CONFIG_PHP, "r", encoding="utf-8").read()
    except OSError:
        return host, port
    m = re.search(r"SERVER_HOST'\s*,\s*getenv\('MAG_SERVER_HOST'\)\s*\?:\s*'([^']+)'", text)
    if not m:
        m = re.search(r"SERVER_HOST'\s*,\s*'([^']+)'", text)
    if m:
        host = m.group(1)
    m = re.search(r"SERVER_PORT'\s*,\s*\(int\)\s*\(getenv\('MAG_SERVER_PORT'\)\s*\?:\s*(\d+)\)", text)
    if not m:
        m = re.search(r"SERVER_PORT'\s*,\s*(\d+)", text)
    if m:
        port = int(m.group(1))
    return host, port


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Собрать ai_config/services.html из шаблона.")
    p.add_argument("--host", default="", help="переопределить хост сервера")
    p.add_argument("--port", type=int, default=0, help="переопределить порт сервера")
    args = p.parse_args(argv)

    host, port = read_config_php()
    if args.host:
        host = args.host
    if args.port:
        port = args.port
    base = f"http://{host}:{port}"

    if not os.path.isfile(TEMPLATE):
        sys.stderr.write(f"Шаблон не найден: {TEMPLATE}\n")
        return 2

    tpl = open(TEMPLATE, "r", encoding="utf-8").read()
    if "__SERVER_BASE__" not in tpl:
        sys.stderr.write("В шаблоне нет плейсхолдера __SERVER_BASE__\n")
        return 2

    out = tpl.replace("__SERVER_BASE__", base)
    for path in OUTPUTS:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
    print(f"Собрано: {base}")
    for path in OUTPUTS:
        print(f"  -> {os.path.relpath(path, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
