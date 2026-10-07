#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автообновление файлов портала (services.html и т.п.) на MAG-приставках по SSH.

Зависимость: paramiko  ->  pip install -r tools/requirements.txt

Примеры
-------
    # 1) Проверить SSH-доступность всех устройств из devices.txt
    python tools/deploy_services.py --check

    # 2) Залить services.html и settings.json в /home/web на все устройства
    python tools/deploy_services.py

    # 3) Один хост, один файл, своя удалённая папка
    python tools/deploy_services.py --hosts 192.168.1.10 \
        --upload ai_config/services.html:/home/web/services.html

    # 4) Без параллелизма и с доп. командой после заливки
    python tools/deploy_services.py --jobs 1 --cmd "sync" --cmd "date"

    # 5) Только показать, что будет сделано (без заливки)
    python tools/deploy_services.py --dry-run

Пароль SSH задаётся аргументом --password или переменной окружения MAG_PASSWORD.
Значение по умолчанию не задано: без пароля подключение не выполняется.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import shlex
import socket
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Не найден модуль paramiko.\n"
        "Установите: pip install -r tools/requirements.txt\n"
    )
    raise SystemExit(2)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Заливки по умолчанию: (локальный путь, удалённый путь)
# На MAG250 портал лежит в /home/web (проверено на тестовой приставке).
# Конфиг — ЕДИНЫЙ файл ai_config/www/settings.json: его генерирует панель из
# devices.json (ai_config/www/) и его же отдаёт приставкам веб-сервер (GET /settings.json).
DEFAULT_UPLOADS: Sequence[Tuple[str, str]] = (
    ("ai_config/services.html", "/home/web/services.html"),
    ("ai_config/www/settings.json", "/home/web/settings.json"),
)

DEFAULT_DEVICES = "devices.txt"
DEFAULT_USER = "root"
DEFAULT_PASSWORD = os.environ.get("MAG_PASSWORD", "")
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 10.0
DEFAULT_JOBS = 4


@dataclass
class HostResult:
    host: str
    ok: bool
    message: str


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------
def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class C:
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREY = "\033[90m"
    RESET = "\033[0m"


def color(text: str, code: str) -> str:
    return f"{code}{text}{C.RESET}" if use_color() else text


def enable_legacy_ssh() -> None:
    """Старые MAG (OpenSSH 5.1) принимают только host key ssh-rsa/ssh-dss.

    В paramiko >= 4 поддержка ssh-rsa удалена — с ней подключиться нельзя.
    Здесь (для paramiko 3.x) поднимаем устаревшие алгоритмы в начало списка.
    """
    from paramiko.transport import Transport

    preferred = tuple(getattr(Transport, "_preferred_keys", ()))
    if "ssh-rsa" in preferred:
        legacy = tuple(k for k in ("ssh-rsa", "ssh-dss") if k in preferred)
        Transport._preferred_keys = legacy + tuple(k for k in preferred if k not in legacy)
    else:
        sys.stderr.write(
            "ВНИМАНИЕ: этот paramiko не поддерживает ssh-rsa (host key), нужный\n"
            "старым MAG (OpenSSH 5.1). Установите совместимую версию:\n"
            "    pip install 'paramiko>=3.4,<4'\n"
        )


def parse_devices_file(path: str) -> List[str]:
    """Читает список хостов: один в строке; '#' — комментарий; берём 1-й токен."""
    hosts: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            host = line.split()[0]
            if host not in hosts:
                hosts.append(host)
    return hosts


def parse_upload(spec: str) -> Tuple[str, str]:
    """'local:remote' -> (local_abs, remote). Локальный путь — от корня репозитория."""
    if ":" not in spec:
        raise argparse.ArgumentTypeError(
            f"Ожидается формат local:remote, получено: {spec!r}"
        )
    local, remote = spec.split(":", 1)
    local = local.strip()
    remote = remote.strip()
    if not local or not remote:
        raise argparse.ArgumentTypeError(f"Пустая часть пути в {spec!r}")
    if not os.path.isabs(local):
        local = os.path.join(REPO_ROOT, local)
    return os.path.abspath(local), remote


def run_command(client: "paramiko.SSHClient", cmd: str, timeout: float) -> Tuple[int, str, str]:
    """Выполняет команду на удалённом хосте. Возвращает (exit_status, stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    status = stdout.channel.recv_exit_status()
    return status, out, err


def ensure_remote_dir(client: "paramiko.SSHClient", remote_dir: str, timeout: float) -> None:
    if not remote_dir or remote_dir == "/":
        return
    status, _out, err = run_command(
        client, f"mkdir -p {shlex.quote(remote_dir)}", timeout
    )
    if status != 0:
        raise RuntimeError(f"не удалось создать {remote_dir}: {err.strip()}")


def put_bytes(client: "paramiko.SSHClient", data: bytes, remote: str, timeout: float) -> None:
    """Записывает файл на устройство через 'cat > file'.

    SFTP-подсистемы на старых прошивках MAG нет, поэтому используем exec-канал.
    """
    chan = client.get_transport().open_session(timeout=timeout)
    try:
        chan.settimeout(timeout)
        chan.exec_command("cat > " + shlex.quote(remote))
        chan.sendall(data)
        chan.shutdown_write()
        status = chan.recv_exit_status()
        if status != 0:
            raise RuntimeError(f"'cat > {remote}' завершился с кодом {status}")
    finally:
        chan.close()


def upload_one(
    client: "paramiko.SSHClient",
    local: str,
    remote: str,
    backup: bool,
    timeout: float,
) -> None:
    if not os.path.isfile(local):
        raise FileNotFoundError(local)

    with open(local, "rb") as fh:
        data = fh.read()

    ensure_remote_dir(client, posixpath.dirname(remote), timeout)

    if backup:
        # cp не жалуется фатально, если исходного файла ещё нет — просто пропускаем.
        run_command(
            client,
            f"cp -f {shlex.quote(remote)} {shlex.quote(remote)}.bak",
            timeout,
        )

    put_bytes(client, data, remote, timeout)
    run_command(client, f"chmod 644 {shlex.quote(remote)}", timeout)

    # Проверяем, что размер на устройстве совпал (защита от обрыва канала).
    status, out, _err = run_command(client, f"wc -c < {shlex.quote(remote)}", timeout)
    got = out.strip()
    if status == 0 and got.isdigit() and int(got) != len(data):
        raise RuntimeError(f"размер на устройстве не совпал: {got} != {len(data)}")


# --------------------------------------------------------------------------
# Обработка одного хоста
# --------------------------------------------------------------------------
def process_host(
    host: str,
    *,
    user: str,
    password: str,
    port: int,
    timeout: float,
    uploads: Sequence[Tuple[str, str]],
    commands: Sequence[str],
    backup: bool,
    check_only: bool,
    dry_run: bool,
) -> HostResult:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if dry_run:
        plan = ", ".join(f"{os.path.basename(l)}->{r}" for l, r in uploads)
        return HostResult(host, True, f"[dry-run] будет залито: {plan}")

    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.AuthenticationException:
        return HostResult(host, False, "неверный логин/пароль")
    except (socket.timeout, TimeoutError):
        return HostResult(host, False, "таймаут подключения")
    except Exception as exc:  # noqa: BLE001
        return HostResult(host, False, f"нет связи: {exc}")

    try:
        if check_only:
            status, out, _err = run_command(client, "uname -a", timeout)
            info = out.strip() if status == 0 else "команда uname не отработала"
            return HostResult(host, status == 0, f"SSH OK ({info})")

        uploaded = []
        for local, remote in uploads:
            upload_one(client, local, remote, backup, timeout)
            uploaded.append(posixpath.basename(remote))

        for cmd in commands:
            status, _out, err = run_command(client, cmd, timeout)
            if status != 0:
                return HostResult(
                    host, False, f"залито ({', '.join(uploaded)}), но команда '{cmd}' упала: {err.strip()}"
                )

        return HostResult(host, True, f"обновлено: {', '.join(uploaded)}")
    except Exception as exc:  # noqa: BLE001
        return HostResult(host, False, f"ошибка заливки: {exc}")
    finally:
        client.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Автообновление файлов портала на MAG-приставках по SSH.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_argument_group("Источники устройств")
    src.add_argument("--devices", default=DEFAULT_DEVICES,
                     help=f"файл со списком хостов (по умолчанию: {DEFAULT_DEVICES})")
    src.add_argument("--hosts", default="",
                     help="хосты через запятую (альтернатива файлу; имеет приоритет)")

    conn = p.add_argument_group("Подключение")
    conn.add_argument("--user", default=DEFAULT_USER, help=f"логин SSH (по умолчанию: {DEFAULT_USER})")
    conn.add_argument("--password", default=DEFAULT_PASSWORD,
                      help="пароль SSH (по умолчанию: env MAG_PASSWORD)")
    conn.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"порт SSH (по умолчанию: {DEFAULT_PORT})")
    conn.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                      help=f"таймаут подключения/команд, сек (по умолчанию: {DEFAULT_TIMEOUT})")
    conn.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                      help=f"число параллельных потоков (по умолчанию: {DEFAULT_JOBS})")

    up = p.add_argument_group("Что заливать")
    up.add_argument("--upload", action="append", type=parse_upload, metavar="LOCAL:REMOTE",
                    help="добавить пару для заливки (можно несколько раз). "
                         "Если не указано — используются значения по умолчанию.")
    up.add_argument("--remote-dir", default="/home/web",
                    help="удалённая папка для заливок по умолчанию (по умолчанию: /home/web)")
    up.add_argument("--no-backup", action="store_true",
                    help="не создавать remote.bak перед перезаписью")
    up.add_argument("--cmd", action="append", default=[], metavar="CMD",
                    help="команда на устройстве после заливки (можно несколько раз; по умолчанию 'sync')")

    mode = p.add_argument_group("Режимы")
    mode.add_argument("--check", action="store_true", help="только проверить SSH-доступность")
    mode.add_argument("--dry-run", action="store_true", help="показать план, ничего не делать")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.password:
        build_parser().error("пароль SSH не задан: укажите --password или env MAG_PASSWORD")

    enable_legacy_ssh()

    # --- список хостов ---
    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    else:
        devices_path = args.devices
        if not os.path.isabs(devices_path):
            candidate = os.path.join(REPO_ROOT, devices_path)
            if os.path.exists(candidate):
                devices_path = candidate
        if not os.path.isfile(devices_path):
            print(color(f"Файл со списком устройств не найден: {devices_path}", C.RED))
            return 2
        hosts = parse_devices_file(devices_path)

    if not hosts:
        print(color("Список устройств пуст.", C.RED))
        return 2

    # --- список заливок ---
    if args.upload:
        uploads = list(args.upload)
    else:
        uploads = []
        for local, remote in DEFAULT_UPLOADS:
            local_abs = local if os.path.isabs(local) else os.path.join(REPO_ROOT, local)
            uploads.append(
                (os.path.normpath(local_abs),
                 posixpath.join(args.remote_dir, posixpath.basename(remote)))
            )

    if not args.check:
        missing = [l for l, _r in uploads if not os.path.isfile(l)]
        if missing:
            for m in missing:
                print(color(f"Локальный файл не найден: {m}", C.RED))
            return 2

    commands = args.cmd if args.cmd else ([] if args.check else ["sync"])

    # --- шапка ---
    print(color(f"Устройств: {len(hosts)} | потоков: {args.jobs} | пользователь: {args.user}", C.GREY))
    if args.check:
        print(color("Режим: проверка SSH", C.GREY))
    else:
        for local, remote in uploads:
            print(color(f"  {local}  ->  {remote}", C.GREY))
        if commands:
            print(color(f"  команды после заливки: {', '.join(commands)}", C.GREY))
        if args.dry_run:
            print(color("Режим: dry-run (ничего не выполняется)", C.YELLOW))
    print()

    # --- обработка ---
    results: List[HostResult] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(
                process_host,
                host,
                user=args.user,
                password=args.password,
                port=args.port,
                timeout=args.timeout,
                uploads=uploads,
                commands=commands,
                backup=not args.no_backup,
                check_only=args.check,
                dry_run=args.dry_run,
            ): host
            for host in hosts
        }
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            mark = color("OK  ", C.GREEN) if res.ok else color("FAIL", C.RED)
            print(f"[{mark}] {res.host:<15} {res.message}")

    # --- итог ---
    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    print()
    print(f"Итого: {color(str(ok), C.GREEN)} успешно, "
          f"{color(str(fail), C.RED) if fail else '0'} с ошибкой, всего {len(results)}")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        raise SystemExit(130)
