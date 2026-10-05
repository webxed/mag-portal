#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка «воспроизводится ли видео» на MAG-приставке по SSH.

Прямого доступа к gSTB.IsPlaying() снаружи нет (это API внутри портала),
поэтому смотрим косвенные признаки на уровне ОС:
  * процесс портала (stbapp) и загруженный URL;
  * ESTABLISHED TCP-соединения (кроме SSH) и их удалённые адреса;
  * членство в multicast-группах (/proc/net/igmp);
  * скорость трафика на eth0 (медиапоток даёт заметный битрейт);
  * активность декодеров (kernel-задачи STVID/STAUD).

Вердикт: PLAYING, если есть multicast-группа медиа, ЛИБО битрейт выше порога.

Примеры
-------
    python tools/check_playback.py --hosts 192.168.1.10
    python tools/check_playback.py --hosts 192.168.1.10 --window 8 --min-kbps 300
    python tools/check_playback.py                 # все из devices.txt

Зависимость: paramiko>=3.4,<4 (см. tools/requirements.txt).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write("Не найден модуль paramiko. Установите: pip install -r tools/requirements.txt\n")
    raise SystemExit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEVICES = "devices.txt"
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("MAG_PASSWORD", "")
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 10.0

# Группы, которые не являются медиапотоком
_IGMP_IGNORE = {
    "010000E0",           # 224.0.0.1  all-hosts
    "FB0000E0",           # 224.0.0.251 mDNS
    "FD0000E0",           # 224.0.0.253
    "000000EF",           # 239.0.0.0 (зарезервировано)
}


@dataclass
class PlaybackReport:
    host: str
    ok: bool                 # удалось ли получить диагностику
    playing: bool
    verdict: str
    details: Dict[str, str]


def use_color() -> bool:
    return not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def color(text: str, code: str) -> str:
    return f"{code}{text}\033[0m" if use_color() else text


def enable_legacy_ssh() -> None:
    """MAG (OpenSSH 5.1) принимает только ssh-rsa/ssh-dss; в paramiko<4 они есть."""
    from paramiko.transport import Transport

    preferred = tuple(getattr(Transport, "_preferred_keys", ()))
    if "ssh-rsa" in preferred:
        legacy = tuple(k for k in ("ssh-rsa", "ssh-dss") if k in preferred)
        Transport._preferred_keys = legacy + tuple(k for k in preferred if k not in legacy)
    else:
        sys.stderr.write(
            "ВНИМАНИЕ: этот paramiko не поддерживает ssh-rsa. Нужен 'paramiko>=3.4,<4'.\n"
        )


def parse_devices_file(path: str) -> List[str]:
    hosts: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                host = line.split()[0]
                if host not in hosts:
                    hosts.append(host)
    return hosts


def _le_ip(hexaddr: str) -> str:
    """'0100A8C0' -> '192.168.0.1' (little-endian по 32 бита, как в /proc/net/tcp)."""
    try:
        b = bytes.fromhex(hexaddr)
        return f"{b[3]}.{b[2]}.{b[1]}.{b[0]}"
    except Exception:  # noqa: BLE001
        return hexaddr


def _le_port(hexport: str) -> int:
    try:
        b = bytes.fromhex(hexport)
        return (b[0] << 8) | b[1]
    except Exception:  # noqa: BLE001
        return 0


def run(client: "paramiko.SSHClient", cmd: str, timeout: float) -> str:
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    return out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")


def _parse_igmp(raw: str) -> List[str]:
    groups = []
    for line in raw.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 8 and all(c in "0123456789ABCDEFabcdef" for c in parts[0]):
            groups.append(parts[0].upper())
    return [g for g in groups if g not in _IGMP_IGNORE]


def _parse_tcp_established(raw: str) -> List[Tuple[str, int]]:
    """Возвращает [(ip, port)] для ESTABLISHED-соединений, исключая SSH (к нам)."""
    peers: List[Tuple[str, int]] = []
    for line in raw.splitlines()[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        loc, rem, state = f[1], f[2], f[3]
        if state != "01":  # ESTABLISHED
            continue
        loc_hex, loc_port_hex = loc.split(":")
        if _le_port(loc_port_hex) == 22:  # наш собственный SSH-сеанс
            continue
        ip_hex, port_hex = rem.split(":")
        ip, port = _le_ip(ip_hex), _le_port(port_hex)
        if ip == "0.0.0.0":
            continue
        peers.append((ip, port))
    return peers


def check_host(
    host: str, *, user: str, password: str, port: int, timeout: float,
    window: float, min_kbps: float,
) -> PlaybackReport:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    details: Dict[str, str] = {}
    try:
        client.connect(
            hostname=host, port=port, username=user, password=password,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            look_for_keys=False, allow_agent=False,
        )
    except paramiko.AuthenticationException:
        return PlaybackReport(host, False, False, "неверный логин/пароль", details)
    except Exception as exc:  # noqa: BLE001
        return PlaybackReport(host, False, False, f"нет связи: {exc}", details)

    try:
        details["portal"] = run(client, "tr '\\0' ' ' < /proc/$(pidof stbapp)/cmdline 2>/dev/null; echo", timeout).strip()

        # трафик
        def rx_bytes() -> int:
            out = run(client, "grep eth0 /proc/net/dev 2>/dev/null", timeout)
            parts = out.split()
            return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        rx1 = rx_bytes()
        time.sleep(window)
        rx2 = rx_bytes()
        kbps = (rx2 - rx1) * 8.0 / window / 1000.0
        details["rx_kbps"] = f"{kbps:.1f}"

        groups = _parse_igmp(run(client, "cat /proc/net/igmp 2>/dev/null", timeout))
        details["multicast"] = ", ".join(groups) if groups else "нет"

        peers = _parse_tcp_established(run(client, "cat /proc/net/tcp 2>/dev/null", timeout))
        details["tcp_peers"] = ", ".join(f"{ip}:{p}" for ip, p in peers) if peers else "нет"

        decoders = run(client, "ps 2>/dev/null | grep -E 'STVID|STAUD' | grep -v grep", timeout)
        details["decoders"] = "активны" if decoders.strip() else "нет"

        playing = bool(groups) or (kbps >= min_kbps)
        if playing:
            reason = []
            if groups:
                reason.append(f"multicast {', '.join(groups)}")
            if kbps >= min_kbps:
                reason.append(f"битрейт {kbps:.0f} кбит/с")
            return PlaybackReport(host, True, True, "ВОСПРОИЗВОДИТСЯ (" + "; ".join(reason) + ")", details)
        return PlaybackReport(
            host, True, False,
            f"НЕ воспроизводится (битрейт {kbps:.0f} кбит/с < {min_kbps:.0f}, multicast нет)",
            details,
        )
    finally:
        client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Проверка воспроизведения видео на MAG по SSH.")
    p.add_argument("--devices", default=DEFAULT_DEVICES, help=f"файл со списком хостов (по умолчанию: {DEFAULT_DEVICES})")
    p.add_argument("--hosts", default="", help="хосты через запятую (приоритетнее файла)")
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASSWORD, help="пароль SSH (или переменная окружения MAG_PASSWORD)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--window", type=float, default=6.0, help="окно измерения трафика, сек (по умолчанию: 6)")
    p.add_argument("--min-kbps", type=float, default=200.0, help="порог битрейта 'играет', кбит/с (по умолчанию: 200)")
    args = p.parse_args(argv)

    if not args.password:
        p.error("пароль SSH не задан: укажите --password или env MAG_PASSWORD")

    enable_legacy_ssh()

    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    else:
        path = args.devices if os.path.isabs(args.devices) else os.path.join(REPO_ROOT, args.devices)
        if not os.path.isfile(path):
            print(color(f"Файл не найден: {path}", "\033[31m"))
            return 2
        hosts = parse_devices_file(path)

    for host in hosts:
        rep = check_host(
            host, user=args.user, password=args.password, port=args.port,
            timeout=args.timeout, window=args.window, min_kbps=args.min_kbps,
        )
        if not rep.ok:
            print(f"[\033[31m!!\033[0m] {host:<15} {rep.verdict}")
            continue
        tag = "\033[32mPLAY\033[0m" if rep.playing else "\033[33mIDLE\033[0m"
        print(f"[{tag}] {host:<15} {rep.verdict}")
        if rep.details.get("portal"):
            print(f"          портал : {rep.details['portal']}")
        print(f"          трафик : {rep.details.get('rx_kbps','?')} кбит/с | "
              f"multicast: {rep.details.get('multicast','?')} | "
              f"TCP: {rep.details.get('tcp_peers','?')}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
        raise SystemExit(130)
