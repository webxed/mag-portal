#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Управление MAG-приставкой по SSH: перезагрузка и произвольная команда.
Используется панелью (PHP) для кнопок «Перезагрузка» и подобных.

Примеры
-------
    python tools/mag_ctl.py --hosts 192.168.1.10 --reboot
    python tools/mag_ctl.py --hosts 192.168.1.10 --cmd "date; uptime"
    python tools/mag_ctl.py --hosts 192.168.1.10,192.168.1.11 --reboot

Зависимость: paramiko>=3.4,<4 (tools/requirements.txt).
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Sequence, Tuple

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write("Не найден paramiko. Установите: pip install -r tools/requirements.txt\n")
    raise SystemExit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("MAG_PASSWORD", "")
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 10.0


def enable_legacy_ssh() -> None:
    from paramiko.transport import Transport
    preferred = tuple(getattr(Transport, "_preferred_keys", ()))
    if "ssh-rsa" in preferred:
        legacy = tuple(k for k in ("ssh-rsa", "ssh-dss") if k in preferred)
        Transport._preferred_keys = legacy + tuple(k for k in preferred if k not in legacy)
    else:
        sys.stderr.write("ВНИМАНИЕ: paramiko не поддерживает ssh-rsa. Нужен 'paramiko>=3.4,<4'.\n")


def connect(host: str, user: str, password: str, port: int, timeout: float) -> "paramiko.SSHClient":
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=host, port=port, username=user, password=password,
              timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
              look_for_keys=False, allow_agent=False)
    return c


def act(host: str, user: str, password: str, port: int, timeout: float,
        reboot: bool, cmd: str) -> Tuple[str, bool, str]:
    try:
        c = connect(host, user, password, port, timeout)
    except paramiko.AuthenticationException:
        return host, False, "неверный логин/пароль"
    except Exception as exc:  # noqa: BLE001
        return host, False, f"нет связи: {exc}"
    try:
        if reboot:
            # reboot завершает соединение — это нормально
            try:
                c.exec_command("reboot", timeout=timeout)
            except Exception:
                pass
            return host, True, "команда перезагрузки отправлена"
        if cmd:
            _in, out, err = c.exec_command(cmd, timeout=timeout)
            data = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
            st = out.channel.recv_exit_status()
            return host, st == 0, data.strip()
        return host, False, "не задано действие (--reboot или --cmd)"
    finally:
        try:
            c.close()
        except Exception:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Управление MAG по SSH (reboot / произвольная команда).")
    p.add_argument("--hosts", required=True, help="хосты через запятую")
    p.add_argument("--reboot", action="store_true")
    p.add_argument("--cmd", default="")
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--jobs", type=int, default=8)
    args = p.parse_args(argv)

    if not args.password:
        p.error("пароль SSH не задан: укажите --password или env MAG_PASSWORD")

    enable_legacy_ssh()
    hosts: List[str] = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if not hosts:
        p.error("пустой список хостов")

    rc = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(act, h, args.user, args.password, args.port, args.timeout, args.reboot, args.cmd)
                for h in hosts]
        for f in as_completed(futs):
            host, ok, msg = f.result()
            print(f"[{'OK  ' if ok else 'FAIL'}] {host:<15} {msg}")
            if not ok:
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
