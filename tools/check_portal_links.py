#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка файла портала /home/web/services.html на устройствах:
наличие, размер, md5 и найденные ссылки (URL потоков). Формирует markdown-отчёт.

Примеры
-------
    python tools/check_portal_links.py
    python tools/check_portal_links.py --out portal_links_report.md
    python tools/check_portal_links.py --hosts 192.168.1.11,192.168.1.12

Зависимость: paramiko>=3.4,<4.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write("Не найден paramiko. Установите: pip install -r tools/requirements.txt\n")
    raise SystemExit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEVICES = "devices.txt"
DEFAULT_PATH = "/home/web/services.html"
DEFAULT_LOCAL = "ai_config/services.html"
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("MAG_PASSWORD", "")
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 8.0

URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>)]+")
_NOISE = ("www.w3.org",)


@dataclass
class PortalInfo:
    host: str
    ok: bool = False
    error: str = ""
    exists: bool = False
    size: int = 0
    md5: str = ""
    links: List[str] = field(default_factory=list)


def enable_legacy_ssh() -> None:
    from paramiko.transport import Transport
    preferred = tuple(getattr(Transport, "_preferred_keys", ()))
    if "ssh-rsa" in preferred:
        legacy = tuple(k for k in ("ssh-rsa", "ssh-dss") if k in preferred)
        Transport._preferred_keys = legacy + tuple(k for k in preferred if k not in legacy)
    else:
        sys.stderr.write("ВНИМАНИЕ: paramiko не поддерживает ssh-rsa. Нужен 'paramiko>=3.4,<4'.\n")


def parse_devices(path: str) -> List[str]:
    hosts: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                h = line.split()[0]
                if h not in hosts:
                    hosts.append(h)
    return hosts


def run(client: "paramiko.SSHClient", cmd: str, timeout: float) -> str:
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    return out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")


def check(host: str, path: str, user: str, password: str, port: int, timeout: float) -> PortalInfo:
    info = PortalInfo(host=host)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(hostname=host, port=port, username=user, password=password,
                  timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                  look_for_keys=False, allow_agent=False)
    except paramiko.AuthenticationException:
        info.error = "неверный логин/пароль"; return info
    except Exception as exc:  # noqa: BLE001
        info.error = f"нет связи: {exc}"; return info
    try:
        exists = run(c, f"[ -f {path} ] && echo yes || echo no", timeout).strip()
        if exists != "yes":
            info.ok = True
            info.exists = False
            return info

        size = run(c, f"wc -c < {path} 2>/dev/null", timeout).strip()
        info.size = int(size) if size.isdigit() else 0
        info.md5 = run(c, f"md5sum {path} 2>/dev/null | awk '{{print $1}}'", timeout).strip()

        # Ссылки (URL) из файла: читаем содержимое и разбираем в Python
        content = run(c, f"cat {path} 2>/dev/null", timeout)
        found = set()
        for m in URL_RE.finditer(content):
            u = m.group(0).rstrip("'\";,.)")
            if u in ("file://",) or any(n in u for n in _NOISE):
                continue
            found.add(u)
        info.links = sorted(found)
        info.exists = True
        info.ok = True
    except Exception as exc:  # noqa: BLE001
        info.error = f"ошибка: {exc}"
    finally:
        c.close()
    return info


def local_md5(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return ""


def build_report(rows: Sequence[PortalInfo], local: str, local_hash: str, path: str) -> str:
    out: List[str] = []
    out.append("# Отчёт: ссылки в `/home/web/services.html`")
    out.append("")
    out.append(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    out.append(f"Эталон: `{local}` — md5 `{local_hash}`")
    out.append("")
    out.append("| IP | Файл | Размер | md5 | = эталон | Ссылок |")
    out.append("|----|------|--------|-----|----------|--------|")
    for r in rows:
        if not r.ok:
            out.append(f"| {r.host} | — | — | — | — | _{r.error}_ |")
            continue
        if not r.exists:
            out.append(f"| {r.host} | **нет** | — | — | — | 0 |")
            continue
        match = "✅" if r.md5 and r.md5 == local_hash else "—"
        out.append(f"| {r.host} | есть | {r.size} | `{r.md5}` | {match} | {len(r.links)} |")
    out.append("")
    out.append("## Ссылки по устройствам")
    out.append("")
    for r in rows:
        if not r.ok or not r.exists:
            continue
        out.append(f"### {r.host}" + ("  ✅ совпадает с эталоном" if r.md5 == local_hash else ""))
        if r.links:
            for u in r.links:
                out.append(f"- `{u}`")
        else:
            out.append("_ссылок не найдено_")
        out.append("")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Проверка ссылок в /home/web/services.html на MAG.")
    p.add_argument("--devices", default=DEFAULT_DEVICES)
    p.add_argument("--hosts", default="")
    p.add_argument("--path", default=DEFAULT_PATH)
    p.add_argument("--local", default=os.path.join(REPO_ROOT, DEFAULT_LOCAL))
    p.add_argument("--out", default="", help="сохранить markdown-отчёт в файл")
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--jobs", type=int, default=8)
    args = p.parse_args(argv)

    if not args.password:
        p.error("пароль SSH не задан: укажите --password или env MAG_PASSWORD")

    enable_legacy_ssh()
    local_hash = local_md5(args.local)

    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    else:
        path = args.devices if os.path.isabs(args.devices) else os.path.join(REPO_ROOT, args.devices)
        hosts = parse_devices(path)

    rows: List[PortalInfo] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(check, h, args.path, args.user, args.password, args.port, args.timeout)
                for h in hosts]
        for f in as_completed(futs):
            rows.append(f.result())
    order = {h: i for i, h in enumerate(hosts)}
    rows.sort(key=lambda r: order.get(r.host, 1 << 30))

    report = build_report(rows, args.local, local_hash, args.path)
    print(report)
    if args.out:
        out_path = args.out if os.path.isabs(args.out) else os.path.join(REPO_ROOT, args.out)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report + "\n")
        sys.stderr.write(f"\nОтчёт сохранён: {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
