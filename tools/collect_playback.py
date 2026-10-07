#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сбор информации о том, какой поток сейчас воспроизводится на каждой MAG-приставке,
с сохранением результатов в базу SQLite.

Определение потока — по косвенным признакам (SSH, без доступа к gSTB внутри портала):
  * multicast-группа в /proc/net/igmp         -> udp-поток (сопоставляется со settings.json);
  * ESTABLISHED TCP к стрим-серверу           -> http/hls/rtmp-поток (по host:port);
  * битрейт на eth0                           -> подтверждение активности.

База: data/mag.db (SQLite), таблицы devices / playback.
Примеры:
    python tools/collect_playback.py
    python tools/collect_playback.py --hosts 192.168.1.10
    python tools/collect_playback.py --db data/mag.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write("Не найден paramiko. Установите: pip install -r tools/requirements.txt\n")
    raise SystemExit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEVICES = "devices.txt"
# Конфиг — единый файл панели (генерируется из ai_config/www/devices.json).
DEFAULT_SETTINGS = "ai_config/www/settings.json"
DEFAULT_DB = "data/mag.db"
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("MAG_PASSWORD", "")
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 8.0

_IGMP_IGNORE = {"010000E0", "FB0000E0", "FD0000E0"}


@dataclass
class StreamInfo:
    host: str
    ok: bool
    error: str = ""
    mac: str = ""
    portal: str = ""
    stream_id: str = ""
    kind: str = ""           # udp | http | rtmp | unknown | idle
    remote: str = ""         # ip:port удалённого сервера
    multicast: str = ""      # IP multicast-группы
    rx_kbps: float = 0.0
    playing: bool = False


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


def load_stream_map(settings_path: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Возвращает (multicast_ip -> stream_id, 'host:port' -> stream_id)."""
    with open(settings_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    mc: Dict[str, str] = {}
    http: Dict[str, str] = {}
    for sid, url in (cfg.get("streams") or {}).items():
        m = re.match(r"^udp://([0-9.]+):(\d+)", url)
        if m:
            mc[m.group(1)] = sid
            continue
        m = re.match(r"^(?:https?|rtmp)://([^/]+)", url)
        if m:
            hp = m.group(1)
            # несколько потоков могут делить host:port — берём первый (обычно main)
            if hp not in http:
                http[hp] = sid
    return mc, http


def _le_ip(hexaddr: str) -> str:
    try:
        b = bytes.fromhex(hexaddr)
        return f"{b[3]}.{b[2]}.{b[1]}.{b[0]}"
    except Exception:  # noqa: BLE001
        return hexaddr


def _le_port(h: str) -> int:
    try:
        b = bytes.fromhex(h)
        return (b[0] << 8) | b[1]
    except Exception:  # noqa: BLE001
        return 0


def _le_group_ip(hex8: str) -> str:
    return _le_ip(hex8)


def collect(host: str, user: str, password: str, port: int, timeout: float,
            mc_map: Dict[str, str], http_map: Dict[str, str], window: float) -> StreamInfo:
    info = StreamInfo(host=host, ok=False)
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

    def run(cmd: str, t: float = timeout) -> str:
        _in, out, err = c.exec_command(cmd, timeout=t)
        return out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")

    try:
        info.mac = run("/home/default/rdir.cgi MACAddress 2>/dev/null | tr -d '\\n'").strip()
        info.portal = run("tr '\\0' ' ' < /proc/$(pidof stbapp)/cmdline 2>/dev/null").strip()

        igmp = run("cat /proc/net/igmp 2>/dev/null")
        groups = []
        for line in igmp.splitlines()[1:]:
            f = line.split()
            if len(f) >= 2 and len(f[0]) == 8 and all(ch in "0123456789ABCDEFabcdef" for ch in f[0]):
                groups.append(f[0].upper())
        media = [g for g in groups if g not in _IGMP_IGNORE]
        media_ips = [_le_group_ip(g) for g in media]
        if media_ips:
            info.multicast = ", ".join(media_ips)

        tcp = run("awk 'NR>1{print $2, $3, $4}' /proc/net/tcp 2>/dev/null")
        peers: List[Tuple[str, int]] = []
        for line in tcp.splitlines():
            f = line.split()
            if len(f) < 3 or f[2] != "01":
                continue
            if _le_port(f[0].split(":")[1]) == 22:
                continue
            peers.append((_le_ip(f[1].split(":")[0]), _le_port(f[1].split(":")[1])))

        # трафик
        def rx() -> int:
            out = run("grep eth0 /proc/net/dev 2>/dev/null")
            p = out.split()
            return int(p[1]) if len(p) > 1 and p[1].isdigit() else 0
        r1 = rx(); time.sleep(window); r2 = rx()
        info.rx_kbps = (r2 - r1) * 8.0 / window / 1000.0

        # классификация
        if media_ips and media_ips[0] in mc_map:
            info.stream_id = mc_map[media_ips[0]]
            info.kind = "udp"
            info.remote = f"udp://{media_ips[0]}"
            info.playing = True
        else:
            for ip, pt in peers:
                sid = http_map.get(f"{ip}:{pt}")
                if sid:
                    info.stream_id = sid
                    info.remote = f"{ip}:{pt}"
                    info.kind = "http" if pt in (80, 8080, 8000) else ("rtmp" if pt == 1935 else "http")
                    info.playing = True
                    break
            if not info.playing and peers:
                info.remote = f"{peers[0][0]}:{peers[0][1]}"
                info.kind = "unknown"
                info.playing = info.rx_kbps >= 200
            if not info.playing and not peers:
                info.kind = "idle"
                info.playing = info.rx_kbps >= 200

        info.ok = True
    except Exception as exc:  # noqa: BLE001
        info.error = f"ошибка опроса: {exc}"
    finally:
        c.close()
    return info


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices (
            host TEXT PRIMARY KEY,
            mac TEXT, model TEXT, image_version TEXT, image_desc TEXT,
            last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS playback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            host TEXT NOT NULL,
            mac TEXT, portal TEXT,
            stream_id TEXT, kind TEXT, remote TEXT, multicast TEXT,
            rx_kbps REAL, playing INTEGER
        );
        """
    )
    conn.commit()


def save_db(conn: sqlite3.Connection, rows: Sequence[StreamInfo]) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    for r in rows:
        if not r.ok:
            continue
        conn.execute(
            "INSERT INTO devices(host, mac, last_seen) VALUES(?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET mac=excluded.mac, last_seen=excluded.last_seen",
            (r.host, r.mac, ts),
        )
        conn.execute(
            "INSERT INTO playback(ts, host, mac, portal, stream_id, kind, remote, multicast, rx_kbps, playing) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, r.host, r.mac, r.portal, r.stream_id, r.kind, r.remote, r.multicast, r.rx_kbps, int(r.playing)),
        )
    conn.commit()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Сбор текущего потока на MAG-приставках + запись в SQLite.")
    p.add_argument("--devices", default=DEFAULT_DEVICES)
    p.add_argument("--hosts", default="")
    p.add_argument("--settings", default=os.path.join(REPO_ROOT, DEFAULT_SETTINGS))
    p.add_argument("--db", default=os.path.join(REPO_ROOT, DEFAULT_DB))
    p.add_argument("--user", default=DEFAULT_USER)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--window", type=float, default=4.0, help="окно замера трафика, сек")
    p.add_argument("--jobs", type=int, default=8)
    args = p.parse_args(argv)

    if not args.password:
        p.error("пароль SSH не задан: укажите --password или env MAG_PASSWORD")

    enable_legacy_ssh()
    mc_map, http_map = load_stream_map(args.settings)

    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    else:
        path = args.devices if os.path.isabs(args.devices) else os.path.join(REPO_ROOT, args.devices)
        hosts = parse_devices(path)

    results: List[StreamInfo] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(collect, h, args.user, args.password, args.port, args.timeout,
                            mc_map, http_map, args.window) for h in hosts]
        for f in as_completed(futs):
            results.append(f.result())
    order = {h: i for i, h in enumerate(hosts)}
    results.sort(key=lambda r: order.get(r.host, 1 << 30))

    # таблица
    print("| IP | MAC | Поток | Тип | Источник | Играет | кбит/с |")
    print("|----|-----|-------|-----|----------|--------|--------|")
    for r in results:
        if not r.ok:
            print(f"| {r.host} | — | — | — | _{r.error}_ | — | — |")
            continue
        print(f"| {r.host} | {r.mac} | {r.stream_id or '—'} | {r.kind} | {r.remote or '—'} "
              f"| {'да' if r.playing else 'нет'} | {r.rx_kbps:.0f} |")

    # база
    db_path = args.db
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        save_db(conn, results)
        n = conn.execute("SELECT COUNT(*) FROM playback").fetchone()[0]
    finally:
        conn.close()
    sys.stderr.write(f"\nСохранено в {db_path} (всего записей playback: {n})\n")

    ok = sum(1 for r in results if r.ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
