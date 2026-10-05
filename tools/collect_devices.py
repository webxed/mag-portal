#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сбор данных о MAG-приставках по SSH: модель, вендор, железо, версия прошивки,
серийник, MAC. Печатает markdown-таблицу (её можно вставить в network_devices.md).

Примеры
-------
    python tools/collect_devices.py                      # все из devices.txt
    python tools/collect_devices.py --hosts 192.168.1.10
    python tools/collect_devices.py --json               # ещё и JSON

Зависимость: paramiko>=3.4,<4 (см. tools/requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Sequence

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

# Поля, которые умеет отдавать /home/default/rdir.cgi
RDIR_FIELDS = ["Model", "Vendor", "HardwareVersion", "ImageVersion",
               "ImageDescription", "SerialNumber", "MACAddress"]


@dataclass
class DeviceInfo:
    host: str
    ok: bool = False
    error: str = ""
    model: str = ""
    vendor: str = ""
    hardware: str = ""
    image_version: str = ""
    image_desc: str = ""
    serial: str = ""
    mac: str = ""
    kernel: str = ""
    image_date: str = ""


def enable_legacy_ssh() -> None:
    from paramiko.transport import Transport
    preferred = tuple(getattr(Transport, "_preferred_keys", ()))
    if "ssh-rsa" in preferred:
        legacy = tuple(k for k in ("ssh-rsa", "ssh-dss") if k in preferred)
        Transport._preferred_keys = legacy + tuple(k for k in preferred if k not in legacy)
    else:
        sys.stderr.write("ВНИМАНИЕ: paramiko не поддерживает ssh-rsa. Нужен 'paramiko>=3.4,<4'.\n")


def parse_devices_file(path: str) -> List[str]:
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


def collect(host: str, user: str, password: str, port: int, timeout: float) -> DeviceInfo:
    info = DeviceInfo(host=host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, port=port, username=user, password=password,
                       timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                       look_for_keys=False, allow_agent=False)
    except paramiko.AuthenticationException:
        info.error = "неверный логин/пароль"
        return info
    except Exception as exc:  # noqa: BLE001
        info.error = f"нет связи: {exc}"
        return info

    try:
        # rdir.cgi отдаёт все поля сразу
        loop = "; ".join(f"printf '%s=' {f}; /home/default/rdir.cgi {f} 2>/dev/null; echo" for f in RDIR_FIELDS)
        raw = run(client, loop, timeout)
        vals: Dict[str, str] = {}
        for line in raw.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()

        info.model = vals.get("Model", "")
        info.vendor = vals.get("Vendor", "")
        info.hardware = vals.get("HardwareVersion", "")
        info.image_version = vals.get("ImageVersion", "")
        info.image_desc = vals.get("ImageDescription", "")
        info.serial = vals.get("SerialNumber", "")
        info.mac = vals.get("MACAddress", "")

        # fallback через fw_printenv, если rdir пуст
        if not info.image_version or not info.image_desc:
            env = run(client, "fw_printenv 2>/dev/null | grep -iE 'Image_Version|Image_Desc|Image_Date'", timeout)
            for line in env.splitlines():
                if line.startswith("Image_Version=") and not info.image_version:
                    info.image_version = line.split("=", 1)[1].strip()
                elif line.startswith("Image_Desc=") and not info.image_desc:
                    info.image_desc = line.split("=", 1)[1].strip()
                elif line.startswith("Image_Date="):
                    info.image_date = line.split("=", 1)[1].strip()

        kernel = run(client, "uname -a", timeout).strip()
        info.kernel = kernel
        info.ok = True
    finally:
        client.close()
    return info


def to_markdown(rows: Sequence[DeviceInfo]) -> str:
    lines = [
        "| IP | MAC | Модель | Вендор | Железо | Прошивка (Image) | Описание | Serial | Ядро |",
        "|----|-----|--------|--------|--------|------------------|----------|--------|------|",
    ]
    for r in rows:
        if r.ok:
            lines.append(
                f"| {r.host} | {r.mac} | {r.model} | {r.vendor} | {r.hardware} | "
                f"{r.image_version} | {r.image_desc} | {r.serial} | {r.kernel} |"
            )
        else:
            lines.append(f"| {r.host} | — | — | — | — | — | — | — | _{r.error}_ |")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Сбор данных о MAG-приставках по SSH.")
    p.add_argument("--devices", default=DEFAULT_DEVICES)
    p.add_argument("--hosts", default="")
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--json", action="store_true", help="дополнительно вывести JSON")
    args = p.parse_args(argv)

    if not args.password:
        p.error("пароль SSH не задан: укажите --password или env MAG_PASSWORD")

    enable_legacy_ssh()

    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    else:
        path = args.devices if os.path.isabs(args.devices) else os.path.join(REPO_ROOT, args.devices)
        if not os.path.isfile(path):
            sys.stderr.write(f"Файл не найден: {path}\n")
            return 2
        hosts = parse_devices_file(path)

    results: List[DeviceInfo] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(collect, h, args.user, args.password, args.port, args.timeout): h for h in hosts}
        for f in as_completed(futs):
            results.append(f.result())

    order = {h: i for i, h in enumerate(hosts)}
    results.sort(key=lambda r: order.get(r.host, 1 << 30))

    print(to_markdown(results))
    if args.json:
        print("\n" + json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))

    ok = sum(1 for r in results if r.ok)
    sys.stderr.write(f"\nОпрошено: {ok}/{len(results)}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
