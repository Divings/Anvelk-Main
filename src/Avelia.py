#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Avelia all-in-one runtime.

Usage (systemd前提):
  python3 Avelia.py
      常駐TCPサーバーとして起動します。通常はsystemdから実行します。
      既定は 127.0.0.1:47500 です。

  python3 Avelia.py --connect
      常駐Aveliaへ接続します。

  python3 Avelia.py --connect --host 127.0.0.1 --port 47500
      接続先を明示します。

設計:
- systemdのType=simpleで前景常駐する前提です。fork/daemonizeは行いません。
- HMAC認証は使用しません。
- TCPソケットをsys.stdin/sys.stdoutへ直接接続し、PTYや子プロセスは使用しません。
- 既存のバーコード/ユーザー認証はTCPセッション開始時にそのまま使用します。
- 同時に1セッションだけ受け付け、memory.vlm等への競合書き込みを避けます。
- 既定ではlocalhostにのみbindします。外部からはSSHポートフォワード推奨です。
"""

import argparse
import builtins
import os
import signal
import socket
import sys
import time
import traceback
from pathlib import Path

DEFAULT_HOST = os.getenv("AVELIA_DAEMON_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("AVELIA_DAEMON_PORT", "47500"))


GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_CALENDAR_TIMEZONE = "Asia/Tokyo"
GOOGLE_CALENDAR_AUTH_PORT = 8765


def _google_calendar_data_dir():
    data_dir = Path.home() / ".local" / "share" / "Avelia"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _ensure_google_calendar_credentials_file(credentials_file: Path):
    """OAuthクライアントJSONが無ければAvelia自身が対話作成する。"""
    if credentials_file.is_file():
        return credentials_file

    import json

    print("")
    print(" Google Calendar OAuth設定ファイルがありません。")
    print(" 初回設定を開始します。")
    print("")
    print(" Google Cloud Consoleで作成した『デスクトップ アプリ』の")
    print(" OAuth Client ID / Client Secret を入力してください。")
    print("")

    client_id = input(" Google OAuth Client ID >> ").strip()
    if not client_id:
        raise RuntimeError("Google OAuth Client ID が入力されていません。")

    client_secret = input(" Google OAuth Client Secret >> ").strip()
    if not client_secret:
        raise RuntimeError("Google OAuth Client Secret が入力されていません。")

    data = {
        "installed": {
            "client_id": client_id,
            "project_id": "avelia-google-calendar",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
        }
    }

    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = credentials_file.with_suffix(".json.tmp")
    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, credentials_file)
    try:
        credentials_file.chmod(0o600)
    except OSError:
        pass

    print("")
    print(" Google Calendar OAuth設定ファイルを作成しました。")
    print(f" {credentials_file}")
    print("")
    return credentials_file


def _run_google_calendar_auth():
    """
    Avelia本体内部でGoogle Calendar OAuth認証を完結させる。

    認証用の外部Pythonスクリプトは不要。
    GUIがある場合はブラウザを開き、GUIがない場合は認証URLを
    現在のAveliaセッションへ表示する。
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise RuntimeError(
            "Google Calendar依存ライブラリがありません。 "
            "python3 -m pip install google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib"
        ) from e

    data_dir = _google_calendar_data_dir()
    credentials_file = data_dir / "google_calendar_credentials.json"
    token_file = data_dir / "google_calendar_token.json"

    _ensure_google_calendar_credentials_file(credentials_file)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_file),
        GOOGLE_CALENDAR_SCOPES,
    )

    # DISPLAYなどの環境変数には依存しない。
    # Avelia自身は常駐サーバーとして動くため、自動ブラウザ起動は行わず、
    # 認証URLを必ず現在のセッションへ表示する。
    print("")
    print(" Google Calendar OAuth認証を開始します。")
    print(
        " Aveliaサーバーが別PCの場合は、必要に応じて次のSSHポートフォワードを使用してください。"
    )
    print(
        f" ssh -L {GOOGLE_CALENDAR_AUTH_PORT}:127.0.0.1:{GOOGLE_CALENDAR_AUTH_PORT} <server>"
    )
    print("")

    creds = flow.run_local_server(
        host="127.0.0.1",
        port=GOOGLE_CALENDAR_AUTH_PORT,
        open_browser=False,
        authorization_prompt_message=(
            " 次のURLをブラウザで開いてGoogle認証してください:\n{url}\n"
        ),
        success_message=(
            "AveliaのGoogle Calendar認証が完了しました。"
            "このタブは閉じて構いません。"
        ),
    )

    token_file.write_text(
        creds.to_json(),
        encoding="utf-8",
    )
    try:
        token_file.chmod(0o600)
    except OSError:
        pass

    print("")
    print(" Google Calendar認証が完了しました。")

    return {
        "success": True,
        "authenticated": True,
        "token_file": str(token_file),
    }


def _configure_stdio():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _check_host_environment():
    from pack import Auth

    auth_response = Auth.authorize_user()
    if not auth_response["ok"]:
        print("")
        print(" アヴェリアを起動できません。")
        print(f" 理由: {auth_response['reason']}")
        print(f" ユーザー: {auth_response.get('username', '不明')}")
        if "error" in auth_response:
            print(f" 詳細: {auth_response['error']}")
        return False

    auth_result = Auth.authorize_environment()
    if not auth_result["ok"]:
        print("")
        print(" アヴェリアを起動できません。")
        print(f" 理由: {auth_result['reason']}")
        if "username" in auth_result:
            print(f" ユーザー: {auth_result['username']}")
        if auth_result["reason"] == "library_version_mismatch":
            print(
                " 必要なライブラリバージョン: "
                f"{auth_result['expected_version']}"
            )
            print(
                " 現在のライブラリバージョン: "
                f"{auth_result['version']['string']}"
            )
        if "error" in auth_result:
            print(f" 詳細: {auth_result['error']}")
        return False

    return True


def _authenticate_startup_user():
    from pack import Auth

    if not _check_host_environment():
        raise SystemExit(1)

    try:
        startup_auth = Auth.authenticate_startup_user()
    except KeyboardInterrupt:
        print("")
        print(" バーコード認証をキャンセルしました。")
        time.sleep(2)
        raise SystemExit(1)

    if not startup_auth["ok"]:
        print("")
        print(" アヴェリアを起動できません。")
        print(" 理由: バーコード認証に失敗しました。")
        raise SystemExit(1)

    return startup_auth["user"]


def _serve_client(conn: socket.socket, addr):
    """1本のTCP接続をAveliaの標準入出力へ直接接続する。"""
    global console, process_uuid, sys_msg

    peer = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr)

    daemon_stdin = sys.stdin
    daemon_stdout = sys.stdout
    daemon_stderr = sys.stderr
    original_input = builtins.input

    print(f"[Avelia] 接続: {peer}", file=daemon_stdout, flush=True)

    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    class _SocketInput:
        """
        TCPソケットをテキストstdinとして扱う。

        input() だけでなく、pack側が sys.stdin.readline() を直接使っても
        同じTCP接続から読めるようにする。
        """

        def __init__(self, sock):
            self.sock = sock
            self._buffer = bytearray()
            self._closed = False

        @property
        def encoding(self):
            return "utf-8"

        @property
        def errors(self):
            return "replace"

        @property
        def closed(self):
            return self._closed

        def isatty(self):
            return False

        def readable(self):
            return True

        def fileno(self):
            return self.sock.fileno()

        def _recv_more(self):
            if self._closed:
                return False

            try:
                chunk = self.sock.recv(4096)
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                self._closed = True
                raise EOFError("client disconnected") from e

            if not chunk:
                self._closed = True
                return False

            self._buffer.extend(chunk)

            if os.getenv("AVELIA_SOCKET_DEBUG", "0") == "1":
                try:
                    print(
                        f"[Avelia][socket-debug] RX {len(chunk)} bytes",
                        file=daemon_stdout,
                        flush=True,
                    )
                except Exception:
                    pass

            return True

        def readline(self, size=-1):
            while True:
                newline_pos = self._buffer.find(b"\n")

                if newline_pos >= 0:
                    take = newline_pos + 1
                    if size is not None and size >= 0:
                        take = min(take, size)

                    raw = bytes(self._buffer[:take])
                    del self._buffer[:take]
                    return raw.decode("utf-8", errors="replace")

                if size is not None and size >= 0 and len(self._buffer) >= size:
                    raw = bytes(self._buffer[:size])
                    del self._buffer[:size]
                    return raw.decode("utf-8", errors="replace")

                if not self._recv_more():
                    if self._buffer:
                        raw = bytes(self._buffer)
                        self._buffer.clear()
                        return raw.decode("utf-8", errors="replace")
                    return ""

        def read(self, size=-1):
            if size == 0:
                return ""

            if size is None or size < 0:
                chunks = []
                if self._buffer:
                    chunks.append(bytes(self._buffer))
                    self._buffer.clear()

                while self._recv_more():
                    if self._buffer:
                        chunks.append(bytes(self._buffer))
                        self._buffer.clear()

                return b"".join(chunks).decode("utf-8", errors="replace")

            while len(self._buffer) < size:
                if not self._recv_more():
                    break

            raw = bytes(self._buffer[:size])
            del self._buffer[:size]
            return raw.decode("utf-8", errors="replace")

        def close(self):
            self._closed = True

    class _SocketOutput:
        """TCPソケットをテキストstdout/stderrとして扱う。"""

        def __init__(self, sock):
            self.sock = sock
            self._closed = False

        @property
        def encoding(self):
            return "utf-8"

        @property
        def errors(self):
            return "replace"

        @property
        def closed(self):
            return self._closed

        def isatty(self):
            return False

        def writable(self):
            return True

        def fileno(self):
            return self.sock.fileno()

        def write(self, text):
            if self._closed:
                raise EOFError("client disconnected")

            if text is None:
                return 0

            text = str(text)
            data = text.encode("utf-8", errors="replace")

            if not data:
                return 0

            try:
                self.sock.sendall(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
                self._closed = True
                raise EOFError("client disconnected") from e

            return len(text)

        def flush(self):
            return None

        def close(self):
            self._closed = True

    socket_input_stream = _SocketInput(conn)
    socket_output_stream = _SocketOutput(conn)

    def tcp_input(prompt=""):
        # builtins.input() と同じ挙動をTCP上で再現する。
        if prompt:
            socket_output_stream.write(prompt)
            socket_output_stream.flush()

        line = socket_input_stream.readline()

        if line == "":
            raise EOFError("client disconnected")

        # input() と同じく末尾の改行だけ除く。
        return line.rstrip("\r\n")

    normal_exit = False

    try:
        # input() と sys.stdin.readline() の両方をTCPへ向ける。
        sys.stdin = socket_input_stream
        sys.stdout = socket_output_stream
        sys.stderr = socket_output_stream
        builtins.input = tcp_input

        console = Console(
            file=socket_output_stream,
            force_terminal=False,
            width=120,
        )

        # 既存のユーザー認証。pack.Auth側がinputでもstdin.readlineでも動く。
        user = _authenticate_startup_user()

        process_uuid = str(uuid.uuid4())
        if Check_previous_session() == 1:
            sys_msg = "前回のセッションが正常に閉じられませんでした。"
        else:
            sys_msg = ""

        Create_session(process_uuid)

        avelia_core_main(user)
        normal_exit = True

        card_id = user.get("card_id")
        if card_id is not None:
            from pack import Auth as _AveliaAuth
            _AveliaAuth.update_last_login(card_id)

    except (EOFError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    except KeyboardInterrupt:
        pass
    except SystemExit:
        # セッション側のsys.exit()でdaemon本体を落とさない。
        pass
    except Exception as e:
        try:
            socket_output_stream.write("\n")
            socket_output_stream.write(" 予期せぬエラーが発生しました。\n")
            socket_output_stream.write(f" {type(e).__name__}: {e}\n")
        except Exception:
            pass

        print(
            f"[Avelia] session error: {type(e).__name__}: {e}",
            file=daemon_stderr,
            flush=True,
        )
        traceback.print_exc(file=daemon_stderr)

    finally:
        # グローバル標準入出力は必ずdaemon側へ戻す。
        builtins.input = original_input
        sys.stdin = daemon_stdin
        sys.stdout = daemon_stdout
        sys.stderr = daemon_stderr

        socket_input_stream.close()
        socket_output_stream.close()

        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        state = "正常終了" if normal_exit else "切断/中断"
        print(f"[Avelia] 切断: {peer} ({state})", flush=True)


def _run_daemon(host: str, port: int):
    _configure_stdio()

    if not _check_host_environment():
        raise SystemExit(1)

    stopping = False

    def _stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        server.settimeout(1.0)

        print(f"[Avelia] daemon listening on {host}:{port}", flush=True)
        print("[Avelia] pure TCP / HMACなし / 既存ユーザー認証を使用", flush=True)
        print("[Avelia] 1セッションずつ受付します。", flush=True)

        while not stopping:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if stopping:
                    break
                raise

            try:
                _serve_client(conn, addr)
            except Exception as e:
                print(
                    f"[Avelia] session error: {type(e).__name__}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                traceback.print_exc()
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    print("[Avelia] daemon stopped", flush=True)


def _interactive_client(sock: socket.socket):
    """
    TCP -> 端末stdout は受信スレッド、端末stdin -> TCP はメインスレッド。

    Windows Console / PowerShell / cmd.exe では、標準入力をワーカースレッドから
    読む構成が不安定になり得るため、キーボード入力は必ずメインスレッドで処理する。
    """
    import threading

    stop = threading.Event()
    output_lock = threading.Lock()

    def receive_loop():
        try:
            while not stop.is_set():
                try:
                    data = sock.recv(65536)
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    break

                if not data:
                    break

                text = data.decode("utf-8", errors="replace")

                with output_lock:
                    sys.stdout.write(text)
                    sys.stdout.flush()

        finally:
            stop.set()

    receiver = threading.Thread(
        target=receive_loop,
        name="avelia-client-recv",
        daemon=True,
    )
    receiver.start()

    try:
        # 重要: stdinはメインスレッドで読む。
        while not stop.is_set():
            try:
                line = input()
            except EOFError:
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                break
            except KeyboardInterrupt:
                break

            if stop.is_set():
                break

            payload = (line + "\n").encode("utf-8", errors="replace")

            try:
                sock.sendall(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                break

    finally:
        stop.set()

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            sock.close()
        except OSError:
            pass

        receiver.join(timeout=0.5)


def _run_client(host: str, port: int):
    _configure_stdio()

    with socket.create_connection((host, port), timeout=10) as sock:
        sock.settimeout(None)

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        _interactive_client(sock)

def _build_parser():
    parser = argparse.ArgumentParser(description="Avelia systemd runtime")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--connect", action="store_true", help="常駐AveliaへTCP接続")
    mode.add_argument("--google-calendar-auth", action="store_true", help="Google Calendarの初回OAuth認証")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


_ARGS = _build_parser().parse_args()

if _ARGS.connect:
    _AVELIA_MODE = "connect"
elif _ARGS.google_calendar_auth:
    _AVELIA_MODE = "google_calendar_auth"
else:
    # systemd Type=simple 前提: 引数なし = 常駐サーバー。
    _AVELIA_MODE = "daemon"

# クライアント側ではCoreを読み込まない。daemon側では一度だけ初期化し、
# 接続ごとに同一プロセス内でTCP stdioセッションを開始する。
if _AVELIA_MODE == "daemon":

    # Copyright (c) 2026 Anvelk Innovations
    # Licensed under the GPL v3.0 License.
    # See LICENSE for details.
    import requests
    import os
    import sys
    import configparser
    import shutil
    import subprocess
    from pathlib import Path
    import pyfiglet
    import traceback
    import time
    from pack.pdf_reader import (
        read_pdf,
        get_pdf_info
    )
    from pack.run_System import run_system_command,block_cmd
    from pack.read_file import read_local_file
    from pack.slack_notify import notify_slack
    from pack.write_file import write_local_file
    from pack.schedule_pdf import (
        tool_create_schedule_pdf,
        tool_create_general_pdf
    )
    from pack.file_Sort import sort_file_by_importance
    from pack.OCR_read import ocr_image
    from pack.task_tools import (
        add_one_shot_task,
        add_weekly_task,
        list_tasks,
        get_task,
        disable_task,
        enable_task,
        retry_task,
        delete_task,
    )
    from pack.sessions import (
        Create_session,
        End_session,
        Check_previous_session
    )
    from pack.memory_crypto import (
        encrypt_memory,
        decrypt_memory
    )
    from pack.hash_utils import sha256_file
    from pack import file_signature
    from pack.keyword_learning import (
        learn_from_conversation,
        learning_enabled,
        load_learning_config
    )

    from pack.knowledge import (
        init_knowledge_table,
        get_knowledge_context
    )
    from pack.Auth import authorize_environment
    from rich.console import Console
    from rich.markdown import Markdown

    console = Console()

    import uuid

    result = authorize_environment()
    if not result["ok"]:
        reason = result["reason"]

        notify_slack("実行停止: 実行環境が許可されていません: " + str(reason))
        print("実行環境が許可されていません: " + str(reason))
        sys.exit()

    # セッションUUIDと前回セッション状態はTCP接続時に設定する。
    process_uuid = ""
    sys_msg = ""

    learned = load_learning_config()
    if learned["enabled"] == True:
        learning_enabled_keyword = True
    else:
        learning_enabled_keyword = False
    try:
        init_knowledge_table()
    except Exception as e:
        print("")
        print(" Knowledge Databaseを初期化できませんでした。")
        print(f" {e}")


    sys.stdin.reconfigure(
        encoding="utf-8",
        errors="replace"
    )

    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace"
    )

    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="replace"
    )

    # =========================================================
    # MySQL 設定
    # =========================================================
    # DB接続情報は /opt/Anvelk-Mainframe/config/database.conf から取得する。
    #
    # [DATABASE]
    # host = localhost
    # port = 3306
    # user = Dail
    # password = xxxxxxxx
    # database = dail
    DATABASE_CONFIG_FILE = "/opt/Anvelk-Mainframe/config/database.conf"




    def load_database_config():
        """/opt/Anvelk-Mainframe/config/database.conf からMySQL接続情報を読み込む。"""
        if not os.path.isfile(DATABASE_CONFIG_FILE):
            raise RuntimeError(
                f"{DATABASE_CONFIG_FILE} が見つかりません。"
            )

        config = configparser.ConfigParser()
        with open(DATABASE_CONFIG_FILE, "r", encoding="utf-8") as configfile:
            config.read_file(configfile)

        if "DATABASE" not in config:
            raise RuntimeError(
                f"{DATABASE_CONFIG_FILE} に [DATABASE] セクションがありません。"
            )

        section = config["DATABASE"]

        try:
            port = section.getint("port", fallback=3306)
        except ValueError as e:
            raise RuntimeError(
                f"{DATABASE_CONFIG_FILE} の port が不正です。"
            ) from e

        db_config = {
            "host": section.get("host", "localhost").strip() or "localhost",
            "port": port,
            "user": section.get("user", "Dail").strip() or "Dail",
            "password": section.get("password", ""),
            "database": section.get("database", "dail").strip() or "dail",
        }

        if not db_config["password"]:
            raise RuntimeError(
                f"{DATABASE_CONFIG_FILE} の password が設定されていません。"
            )

        return db_config


    def get_db_connection():
        """Dail設定DBへ接続する。"""
        try:
            import mysql.connector
        except ImportError as e:
            raise RuntimeError(
                "mysql-connector-python がインストールされていません。"
            ) from e

        db_config = load_database_config()

        return mysql.connector.connect(**db_config)

    card_id = ""
    real_name = ""
    last_login = ""

    # =========================================================
    # 予定管理互換レイヤー（Google Calendar）
    # =========================================================
    # 予定本体はGoogle Calendarを正本とする。
    # MySQLはsettings等のAvelia設定用途のみで、予定データには使用しない。

    def _normalize_schedule_datetime(scheduled_at):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if isinstance(scheduled_at, datetime):
            dt = scheduled_at
        elif isinstance(scheduled_at, str):
            text = scheduled_at.strip()
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    pass
            if dt is None:
                try:
                    dt = datetime.fromisoformat(text.replace(" ", "T", 1))
                except ValueError as e:
                    raise ValueError("日時形式が不正です。YYYY-MM-DD HH:MM またはISO 8601形式を使用してください。") from e
        else:
            raise ValueError("scheduled_at は日時文字列で指定してください。")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(GOOGLE_CALENDAR_TIMEZONE))
        return dt

    def _normalize_calendar_date(value):
        from datetime import date, datetime
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except ValueError as e:
            raise ValueError("日付はYYYY-MM-DD形式で指定してください。") from e

    def _normalize_calendar_time(value):
        from datetime import datetime, time as dt_time
        if value is None or value == "":
            return None
        if isinstance(value, dt_time):
            return value
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                pass
        raise ValueError("時刻はHH:MMまたはHH:MM:SS形式で指定してください。")

    def _validate_calendar_title(title):
        title = str(title).strip()
        if not title:
            raise ValueError("予定タイトルが空です。")
        return title

    def _validate_calendar_time_range(start_time, end_time):
        start_time = _normalize_calendar_time(start_time)
        end_time = _normalize_calendar_time(end_time)
        if start_time is not None and end_time is not None and end_time <= start_time:
            raise ValueError("end_timeはstart_timeより後にしてください。")
        return start_time, end_time

    def add_schedule(title, message, scheduled_at):
        """旧Tool互換。通知予定をGoogle Calendarイベントとして登録する。"""
        dt = _normalize_schedule_datetime(scheduled_at)
        event = google_calendar_create_event(
            title=_validate_calendar_title(title),
            start=dt.isoformat(),
            duration_minutes=30,
            description=str(message).strip(),
            calendar_id="primary",
        )
        return event["id"]

    def get_schedules(include_used=False):
        """
        旧Tool互換。

        Google Calendarから今後1年の予定を取得する。
        include_used は旧Tool互換名として残し、
        TrueならAvelia完了済み予定も含める。
        Falseなら未完了予定だけを返す。

        通知済み状態そのものはworkerの
        google_calendar_notify_state.jsonが管理するため、
        旧message_use列は互換用の値として返す。
        """
        events = google_calendar_get_upcoming(
            days=365,
            calendar_id="primary",
            max_results=10000,
            include_completed=bool(
                include_used
            ),
        )

        rows = []

        for event in events:
            completed = bool(
                event.get(
                    "completed",
                    False,
                )
            )

            rows.append(
                {
                    "id":
                        event["id"],

                    "title":
                        event["title"],

                    "message":
                        event.get(
                            "description",
                            "",
                        ),

                    "scheduled_at":
                        event.get(
                            "start",
                            "",
                        ),

                    # 旧レスポンス互換。
                    # Google Calendar移行後は
                    # 「完了済み」を1として返す。
                    "message_use":
                        1
                        if completed
                        else 0,

                    "created_at":
                        None,

                    "calendar_id":
                        event.get(
                            "calendar_id",
                            "primary",
                        ),

                    "all_day":
                        event.get(
                            "all_day",
                            False,
                        ),

                    "recurring_event_id":
                        event.get(
                            "recurring_event_id",
                            "",
                        ),

                    "completed":
                        completed,

                    "html_link":
                        event.get(
                            "html_link",
                            "",
                        ),
                }
            )

        return rows


    def delete_schedule(schedule_id):
        return bool(
            google_calendar_delete_event(
                str(schedule_id),
                calendar_id="primary",
            ).get("success")
        )

    def delete_calendar_once(schedule_id):
        # 単発予定はそのイベント自身を削除する。
        return delete_schedule(
            schedule_id
        )

    def _google_calendar_resolve_series_id(
        event_id,
        calendar_id="primary",
    ):
        """instance IDならrecurringEventIdを返し、master IDならそのまま返す。"""
        event_id = str(
            event_id
        ).strip()

        if not event_id:
            raise ValueError(
                "event_idが空です。"
            )

        service = (
            _get_google_calendar_service()
        )

        event = (
            service.events()
            .get(
                calendarId=calendar_id,
                eventId=event_id,
            )
            .execute()
        )

        return (
            event.get(
                "recurringEventId"
            )
            or event_id
        )

    def delete_calendar_weekly(schedule_id):
        # 旧DB版はweekly本体を削除していたため、
        # expanded instance IDが渡されてもseries masterを削除する。
        master_id = (
            _google_calendar_resolve_series_id(
                schedule_id,
                calendar_id="primary",
            )
        )

        return bool(
            google_calendar_delete_event(
                master_id,
                calendar_id="primary",
            ).get("success")
        )

    def add_calendar_once(title, scheduled_date, start_time=None, end_time=None, description=None, source="avelia"):
        title = _validate_calendar_title(title)
        scheduled_date = _normalize_calendar_date(scheduled_date)
        start_time, end_time = _validate_calendar_time_range(start_time, end_time)
        if start_time is None:
            event = google_calendar_create_all_day(
                title=title, scheduled_date=scheduled_date.isoformat(),
                description=description, calendar_id="primary"
            )
        else:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(GOOGLE_CALENDAR_TIMEZONE)
            start_dt = datetime.combine(scheduled_date, start_time, tzinfo=tz)
            end_dt = datetime.combine(scheduled_date, end_time, tzinfo=tz) if end_time else start_dt + timedelta(hours=1)
            event = google_calendar_create_event(
                title=title, start=start_dt.isoformat(), end=end_dt.isoformat(),
                description=description, calendar_id="primary"
            )
        return event["id"]

    def add_calendar_weekly(title, weekdays, start_time=None, end_time=None, description=None, source="avelia"):
        event = google_calendar_create_weekly(
            title=title, weekdays=weekdays, start_time=start_time, end_time=end_time,
            description=description, calendar_id="primary"
        )
        return event["id"]

    def complete_calendar_once(schedule_id):
        return bool(
            google_calendar_mark_completed(
                str(schedule_id),
                calendar_id="primary",
            ).get("success")
        )

    def complete_calendar_weekly(
        schedule_id,
        scheduled_date=None,
    ):
        """
        定期予定のその1回だけを完了扱いにする。

        get_calendar_date() が返すexpanded instance IDなら
        そのinstanceだけをpatchする。

        series master IDが渡された場合はscheduled_dateから
        対象instanceを探してpatchする。
        """
        event_id = str(
            schedule_id
        ).strip()

        if not event_id:
            raise ValueError(
                "schedule_idが空です。"
            )

        service = (
            _get_google_calendar_service()
        )

        event = (
            service.events()
            .get(
                calendarId="primary",
                eventId=event_id,
            )
            .execute()
        )

        # recurringEventIdがあるなら既に個別instance。
        if event.get(
            "recurringEventId"
        ):
            target_event_id = event_id

        else:
            # master eventなら対象日を使ってinstanceを解決する。
            if scheduled_date is None:
                raise ValueError(
                    "定期予定のmaster IDを完了する場合は"
                    "scheduled_dateが必要です。"
                )

            target_date = (
                _normalize_calendar_date(
                    scheduled_date
                )
            )

            events = google_calendar_get_date(
                target_date.isoformat(),
                calendar_id="primary",
            )

            target_event_id = None

            for item in events:
                if (
                    item.get(
                        "recurring_event_id"
                    )
                    == event_id
                ):
                    target_event_id = (
                        item.get("id")
                    )
                    break

            if not target_event_id:
                raise ValueError(
                    "指定日に該当する定期予定の"
                    "発生回が見つかりません。"
                )

        return bool(
            google_calendar_mark_completed(
                target_event_id,
                calendar_id="primary",
            ).get("success")
        )

    def get_calendar_date(target_date=None, unfinished_only=True):
        from datetime import date
        target = date.today() if target_date is None else _normalize_calendar_date(target_date)
        events = google_calendar_get_date(
            target.isoformat(),
            calendar_id="primary",
            include_completed=not bool(
                unfinished_only
            ),
        )
        schedules = []
        for e in events:
            start = e.get("start", "")
            end = e.get("end", "")
            start_time = None if e.get("all_day") else (start[11:19] if len(start) >= 19 else start[11:])
            end_time = None if e.get("all_day") else (end[11:19] if len(end) >= 19 else end[11:])
            schedules.append({
                "schedule_type": "weekly" if e.get("recurring_event_id") else "once",
                "id": e["id"], "title": e["title"], "description": e.get("description", ""),
                "date": target.isoformat(), "start_time": start_time, "end_time": end_time,
                "completed": bool(e.get("completed", False)),
            })
        return {"date": target.isoformat(), "weekday": target.weekday(), "count": len(schedules), "schedules": schedules}

    def get_calendar_today():
        return get_calendar_date(target_date=None, unfinished_only=True)

    def import_calendar_csv(csv_path):
        """旧CSV形式をGoogle Calendarへ取り込む。"""
        import csv
        csv_path = os.path.abspath(os.path.expanduser(str(csv_path)))
        if not os.path.isfile(csv_path):
            raise FileNotFoundError(f"CSVファイルがありません: {csv_path}")
        added, skipped, errors = 0, 0, []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            for row_number, row in enumerate(csv.DictReader(f), start=2):
                try:
                    typ = str(row.get("type", "")).strip().lower()
                    title = str(row.get("title", "")).strip()
                    description = str(row.get("description") or "").strip() or None
                    start_time = str(row.get("start_time") or "").strip() or None
                    end_time = str(row.get("end_time") or "").strip() or None
                    if typ == "once":
                        add_calendar_once(title, str(row.get("date") or "").strip(), start_time, end_time, description, "csv")
                    elif typ == "weekly":
                        weekdays = [int(x) for x in str(row.get("weekdays") or "").split("|") if x.strip()]
                        add_calendar_weekly(title, weekdays, start_time, end_time, description, "csv")
                    else:
                        raise ValueError(f"未対応type: {typ}")
                    added += 1
                except Exception as e:
                    skipped += 1
                    errors.append({"row": row_number, "error": str(e)})
        return {"success": True, "added": added, "skipped": skipped, "errors": errors}


    def load_settings_from_db(section_name):
        """settingsテーブルから指定セクションをConfigParser形式で返す。"""
        config = configparser.ConfigParser()
        config.optionxform = str

        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                SELECT setting_key, setting_value
                FROM settings
                WHERE section_name = %s
                """,
                (section_name,),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()

        values = {
            str(key): "" if value is None else str(value)
            for key, value in rows
        }

        if section_name.upper() == "DEFAULT":
            config["DEFAULT"] = values
        elif values:
            config[section_name] = values

        return config


    def save_setting_to_db(section_name, setting_key, setting_value):
        """設定値をINSERTまたはUPDATEする。"""
        conn = get_db_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """
                INSERT INTO settings
                    (section_name, setting_key, setting_value)
                VALUES
                    (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    setting_value = VALUES(setting_value)
                """,
                (
                    section_name,
                    setting_key,
                    "" if setting_value is None else str(setting_value),
                ),
            )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    # =========================================================
    # 画面クリア
    # =========================================================

    # systemd + TCP 常駐モードではサーバー側の clear は
    # クライアント端末へ反映されないため、起動時には実行しない。

    def load_config():
        return load_settings_from_db("DEFAULT")


    def load_memory_config():
        return load_settings_from_db("MEMORY")


    def load_pre_clear():
        config = load_config()

        try:
            return int(config["DEFAULT"].getboolean("pre_clear"))
        except (KeyError, ValueError):
            return 0

    def load_memory_conf():
        """
        メモリ(記憶)の最大保存件数を取得
        """
        config = load_memory_config()

        try:
            return int(config["MEMORY"]["max_memory"])
        except (KeyError, ValueError):
            return 5


    def load_DVDmode():
        """
        DVDモードの有効無効を変更
        """
        config = load_config()

        try:
            if config["DEFAULT"]["dvd_mode"] not in ["0", "1"]:
                print("")
                print(" config.iniのdvd_modeの値が不正です。")
                print(" 0 または 1 を設定してください。")
                print("")
                return 0
            return int(config["DEFAULT"]["dvd_mode"])
        except KeyError:
            return 0  

    def setup_dropbox_oauth():
        import dropbox

        config = load_dropbox_config()

        app_key = config["DROPBOX"]["app_key"].strip()

        auth_flow = dropbox.DropboxOAuth2FlowNoRedirect(
            app_key,
            token_access_type="offline",
            use_pkce=True
        )

        authorize_url = auth_flow.start()

        print("")
        print(" Dropbox認証URL:")
        print(f" {authorize_url}")
        print("")
        print(" このLinux環境にGUIブラウザが無い場合は、")
        print(" 上のURLを別の端末のブラウザで開いて認証してください。")

        try:
            import webbrowser
            if os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"):
                webbrowser.open(authorize_url)
        except Exception:
            pass

        auth_code = input(
            " 表示された認証コードを入力してください >> "
        ).strip()

        result = auth_flow.finish(auth_code)

        refresh_token = result.refresh_token

        save_setting_to_db(
            "DROPBOX",
            "refresh_token",
            refresh_token
        )

        print("")
        print(" Dropbox認証が完了しました。")

        return refresh_token

    def get_appdata_dir():
        """
        Linux向けユーザーデータ保存先。
        XDG_DATA_HOME が設定されていればそれを使用し、
        未設定なら ~/.local/share/Velwether-API を使用する。
        """
        base = os.getenv("XDG_DATA_HOME")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".local", "share")

        folder = os.path.join(base, "Velwether-API")
        os.makedirs(folder, exist_ok=True)
        return folder

    # =========================================================
    # 定数
    # =========================================================

    APP_DATA=get_appdata_dir()
    CONFIG_DIR = "/opt/Anvelk-Mainframe/config"
    from pathlib import Path

    DATA_DIR = Path.home() / ".local" / "share" / "Avelia"
    LOG_DIR = DATA_DIR / "logs"


    # =========================================================
    # Google Calendar
    # =========================================================

    GOOGLE_CALENDAR_CREDENTIALS_FILE = DATA_DIR / "google_calendar_credentials.json"
    GOOGLE_CALENDAR_TOKEN_FILE = DATA_DIR / "google_calendar_token.json"

    _google_calendar_service_cache = None


    def _get_google_calendar_service():
        """保存済みOAuth tokenからGoogle Calendar API clientを返す。"""
        global _google_calendar_service_cache

        if _google_calendar_service_cache is not None:
            return _google_calendar_service_cache

        try:
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2.credentials import Credentials as GoogleCredentials
            from googleapiclient.discovery import build as google_build
        except ImportError as e:
            raise RuntimeError(
                "Google Calendar依存ライブラリがありません。"
                " python3 -m pip install google-api-python-client "
                "google-auth-httplib2 google-auth-oauthlib"
            ) from e

        if not GOOGLE_CALENDAR_TOKEN_FILE.is_file():
            raise RuntimeError(
                "Google Calendarが未認証です。"
                " Google Calendar認証を実行してください。"
            )

        try:
            creds = GoogleCredentials.from_authorized_user_file(
                str(GOOGLE_CALENDAR_TOKEN_FILE),
                GOOGLE_CALENDAR_SCOPES,
            )
        except Exception as e:
            raise RuntimeError(f"Google Calendar tokenを読み込めません: {e}") from e

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleAuthRequest())
                GOOGLE_CALENDAR_TOKEN_FILE.write_text(
                    creds.to_json(),
                    encoding="utf-8",
                )
                try:
                    GOOGLE_CALENDAR_TOKEN_FILE.chmod(0o600)
                except OSError:
                    pass
            except Exception as e:
                raise RuntimeError(f"Google Calendar token更新に失敗しました: {e}") from e

        if not creds.valid:
            raise RuntimeError(
                "Google Calendar認証情報が無効です。"
                " Google Calendarを再認証してください。"
            )

        _google_calendar_service_cache = google_build(
            "calendar",
            "v3",
            credentials=creds,
            cache_discovery=False,
        )
        return _google_calendar_service_cache


    def _google_calendar_datetime(value):
        """ISO日時文字列をAsia/Tokyoのaware datetimeへ正規化する。"""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if not isinstance(value, str) or not value.strip():
            raise ValueError("日時文字列が空です。")

        text = value.strip()
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)

        try:
            dt = datetime.fromisoformat(text)
        except ValueError as e:
            raise ValueError(
                "日時は YYYY-MM-DD HH:MM またはISO 8601形式で指定してください。"
            ) from e

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(GOOGLE_CALENDAR_TIMEZONE))
        return dt


    def _google_calendar_event_to_dict(event, calendar_id="primary"):
        start_data = event.get("start", {})
        end_data = event.get("end", {})
        private_props = event.get("extendedProperties", {}).get("private", {})
        return {
            "id": event.get("id", ""),
            "title": event.get("summary", "(タイトルなし)"),
            "description": event.get("description", ""),
            "location": event.get("location", ""),
            "start": start_data.get("dateTime") or start_data.get("date") or "",
            "end": end_data.get("dateTime") or end_data.get("date") or "",
            "all_day": "date" in start_data and "dateTime" not in start_data,
            "status": event.get("status", ""),
            "html_link": event.get("htmlLink", ""),
            "calendar_id": calendar_id,
            "recurring_event_id": event.get("recurringEventId", ""),
            "recurrence": event.get("recurrence", []),
            "completed": private_props.get("avelia_completed") == "1",
        }


    def google_calendar_list_events(
        start=None,
        end=None,
        query=None,
        calendar_id="primary",
        max_results=100,
        include_completed=True,
    ):
        """
        Google Calendarから期間内イベントを取得する。

        - singleEvents=True で繰り返し予定を各発生回へ展開
        - nextPageToken を最後まで処理
        - max_results はAvelia側の総取得上限として扱う
        - include_completed=False の場合、
          extendedProperties.private.avelia_completed=1 を除外
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        service = _get_google_calendar_service()
        tz = ZoneInfo(GOOGLE_CALENDAR_TIMEZONE)

        start_dt = (
            _google_calendar_datetime(start)
            if start
            else datetime.now(tz)
        )

        end_dt = (
            _google_calendar_datetime(end)
            if end
            else start_dt + timedelta(days=30)
        )

        if end_dt <= start_dt:
            raise ValueError(
                "endはstartより後にしてください。"
            )

        limit = max(
            1,
            min(
                int(max_results),
                10000,
            ),
        )

        events = []
        page_token = None

        while len(events) < limit:
            per_page = min(
                2500,
                max(
                    1,
                    limit - len(events),
                ),
            )

            params = {
                "calendarId": calendar_id,
                "timeMin": start_dt.isoformat(),
                "timeMax": end_dt.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
                "showDeleted": False,
                "maxResults": per_page,
                "timeZone": GOOGLE_CALENDAR_TIMEZONE,
            }

            if query:
                params["q"] = str(
                    query
                )

            if page_token:
                params["pageToken"] = (
                    page_token
                )

            result = (
                service.events()
                .list(
                    **params
                )
                .execute()
            )

            for item in result.get(
                "items",
                [],
            ):
                if (
                    item.get("status")
                    == "cancelled"
                ):
                    continue

                converted = (
                    _google_calendar_event_to_dict(
                        item,
                        calendar_id,
                    )
                )

                if (
                    not include_completed
                    and converted.get(
                        "completed",
                        False,
                    )
                ):
                    continue

                events.append(
                    converted
                )

                if len(events) >= limit:
                    break

            page_token = result.get(
                "nextPageToken"
            )

            if not page_token:
                break

        return events


    def google_calendar_get_event(
        event_id,
        calendar_id="primary",
    ):
        """
        Google CalendarイベントをイベントIDで1件取得する。
        """
        event_id = str(
            event_id
        ).strip()

        if not event_id:
            raise ValueError(
                "event_idが空です。"
            )

        event = (
            _get_google_calendar_service()
            .events()
            .get(
                calendarId=calendar_id,
                eventId=event_id,
            )
            .execute()
        )

        return _google_calendar_event_to_dict(
            event,
            calendar_id,
        )


    def google_calendar_get_upcoming(
        days=30,
        calendar_id="primary",
        max_results=100,
        include_completed=False,
    ):
        """
        現在時刻から指定日数先までの予定を取得する。
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        days = int(
            days
        )

        if days < 1:
            raise ValueError(
                "daysは1以上にしてください。"
            )

        tz = ZoneInfo(
            GOOGLE_CALENDAR_TIMEZONE
        )

        now = datetime.now(
            tz
        )

        return google_calendar_list_events(
            start=now.isoformat(),
            end=(
                now
                + timedelta(days=days)
            ).isoformat(),
            calendar_id=calendar_id,
            max_results=max_results,
            include_completed=include_completed,
        )


    def google_calendar_get_date(
        target_date,
        calendar_id="primary",
        include_completed=True,
    ):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        try:
            d = datetime.strptime(
                str(target_date).strip(),
                "%Y-%m-%d",
            ).date()

        except ValueError as e:
            raise ValueError(
                "日付はYYYY-MM-DD形式で指定してください。"
            ) from e

        tz = ZoneInfo(
            GOOGLE_CALENDAR_TIMEZONE
        )

        start = datetime(
            d.year,
            d.month,
            d.day,
            tzinfo=tz,
        )

        end = (
            start
            + timedelta(days=1)
        )

        return google_calendar_list_events(
            start=start.isoformat(),
            end=end.isoformat(),
            calendar_id=calendar_id,
            max_results=2500,
            include_completed=include_completed,
        )


    def google_calendar_create_event(
        title,
        start,
        end=None,
        duration_minutes=60,
        description=None,
        location=None,
        calendar_id="primary",
    ):
        from datetime import timedelta

        title = str(title).strip()
        if not title:
            raise ValueError("予定タイトルが空です。")

        start_dt = _google_calendar_datetime(start)
        end_dt = (
            _google_calendar_datetime(end)
            if end
            else start_dt + timedelta(minutes=int(duration_minutes))
        )
        if end_dt <= start_dt:
            raise ValueError("終了時刻は開始時刻より後にしてください。")

        body = {
            "summary": title,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": GOOGLE_CALENDAR_TIMEZONE,
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": GOOGLE_CALENDAR_TIMEZONE,
            },
        }
        if description:
            body["description"] = str(description)
        if location:
            body["location"] = str(location)

        event = _get_google_calendar_service().events().insert(
            calendarId=calendar_id,
            body=body,
        ).execute()
        return _google_calendar_event_to_dict(event, calendar_id)


    def google_calendar_create_all_day(
        title,
        scheduled_date,
        end_date=None,
        description=None,
        location=None,
        calendar_id="primary",
    ):
        from datetime import datetime, timedelta

        title = str(title).strip()
        if not title:
            raise ValueError("予定タイトルが空です。")

        try:
            start_d = datetime.strptime(str(scheduled_date).strip(), "%Y-%m-%d").date()
            end_d = (
                datetime.strptime(str(end_date).strip(), "%Y-%m-%d").date()
                if end_date
                else start_d + timedelta(days=1)
            )
        except ValueError as e:
            raise ValueError("日付はYYYY-MM-DD形式で指定してください。") from e

        if end_d <= start_d:
            raise ValueError("end_dateはscheduled_dateより後にしてください。")

        body = {
            "summary": title,
            "start": {"date": start_d.isoformat()},
            "end": {"date": end_d.isoformat()},
        }
        if description:
            body["description"] = str(description)
        if location:
            body["location"] = str(location)

        event = _get_google_calendar_service().events().insert(
            calendarId=calendar_id,
            body=body,
        ).execute()
        return _google_calendar_event_to_dict(event, calendar_id)


    def google_calendar_create_weekly(
        title, weekdays, start_time=None, end_time=None, description=None,
        location=None, calendar_id="primary", start_date=None,
    ):
        """曜日指定の毎週繰り返し予定をGoogle Calendarへ作成する。"""
        from datetime import date, datetime, timedelta
        from zoneinfo import ZoneInfo

        title = _validate_calendar_title(title)

        weekdays = sorted({int(x) for x in weekdays})
        if not weekdays or any(x < 0 or x > 6 for x in weekdays):
            raise ValueError("weekdaysは0(月)〜6(日)を1つ以上指定してください。")

        st, et = _validate_calendar_time_range(start_time, end_time)

        base = (
            _normalize_calendar_date(start_date)
            if start_date
            else date.today()
        )

        first_date = base + timedelta(
            days=min(
                (wd - base.weekday()) % 7
                for wd in weekdays
            )
        )

        codes = [
            "MO", "TU", "WE", "TH",
            "FR", "SA", "SU"
        ]

        recurrence = [
            "RRULE:FREQ=WEEKLY;BYDAY="
            + ",".join(
                codes[x]
                for x in weekdays
            )
        ]

        # start_time=None は旧calendar_weeklyの仕様に合わせ、
        # 終日の毎週予定としてGoogle Calendarへ登録する。
        if st is None:
            body = {
                "summary": title,
                "start": {
                    "date": first_date.isoformat()
                },
                "end": {
                    "date": (
                        first_date
                        + timedelta(days=1)
                    ).isoformat()
                },
                "recurrence": recurrence,
            }

        else:
            tz = ZoneInfo(
                GOOGLE_CALENDAR_TIMEZONE
            )

            start_dt = datetime.combine(
                first_date,
                st,
                tzinfo=tz,
            )

            end_dt = (
                datetime.combine(
                    first_date,
                    et,
                    tzinfo=tz,
                )
                if et
                else start_dt
                + timedelta(hours=1)
            )

            body = {
                "summary": title,
                "start": {
                    "dateTime":
                        start_dt.isoformat(),
                    "timeZone":
                        GOOGLE_CALENDAR_TIMEZONE,
                },
                "end": {
                    "dateTime":
                        end_dt.isoformat(),
                    "timeZone":
                        GOOGLE_CALENDAR_TIMEZONE,
                },
                "recurrence": recurrence,
            }

        if description:
            body["description"] = str(
                description
            )

        if location:
            body["location"] = str(
                location
            )

        event = (
            _get_google_calendar_service()
            .events()
            .insert(
                calendarId=calendar_id,
                body=body,
            )
            .execute()
        )

        return _google_calendar_event_to_dict(
            event,
            calendar_id,
        )

    def google_calendar_mark_completed(event_id, completed=True, calendar_id="primary"):
        """Avelia独自の完了状態をGoogle Calendar private extendedPropertiesへ保存する。"""
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("event_idが空です。")
        service = _get_google_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        ext = dict(event.get("extendedProperties", {}))
        private = dict(ext.get("private", {}))
        private["avelia_completed"] = "1" if bool(completed) else "0"
        ext["private"] = private
        updated = service.events().patch(
            calendarId=calendar_id, eventId=event_id, body={"extendedProperties": ext}
        ).execute()
        return {"success": True, "event": _google_calendar_event_to_dict(updated, calendar_id)}


    def google_calendar_update_event(
        event_id,
        title=None,
        start=None,
        end=None,
        description=None,
        location=None,
        calendar_id="primary",
    ):
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("event_idが空です。")

        body = {}
        if title is not None:
            title = str(title).strip()
            if not title:
                raise ValueError("予定タイトルが空です。")
            body["summary"] = title
        if description is not None:
            body["description"] = str(description)
        if location is not None:
            body["location"] = str(location)
        if start is not None:
            start_dt = _google_calendar_datetime(start)
            body["start"] = {
                "dateTime": start_dt.isoformat(),
                "timeZone": GOOGLE_CALENDAR_TIMEZONE,
            }
        if end is not None:
            end_dt = _google_calendar_datetime(end)
            body["end"] = {
                "dateTime": end_dt.isoformat(),
                "timeZone": GOOGLE_CALENDAR_TIMEZONE,
            }
        if not body:
            raise ValueError("変更内容がありません。")

        event = _get_google_calendar_service().events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
        ).execute()
        return _google_calendar_event_to_dict(event, calendar_id)


    def google_calendar_delete_event(event_id, calendar_id="primary"):
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("event_idが空です。")
        _get_google_calendar_service().events().delete(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()
        return {"success": True, "deleted_event_id": event_id}


    def google_calendar_list_calendars():
        service = _get_google_calendar_service()
        result_items = []
        page_token = None
        while True:
            result = service.calendarList().list(pageToken=page_token).execute()
            for item in result.get("items", []):
                result_items.append({
                    "id": item.get("id"),
                    "summary": item.get("summary"),
                    "primary": bool(item.get("primary", False)),
                    "access_role": item.get("accessRole"),
                    "timezone": item.get("timeZone"),
                })
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return result_items


    def google_calendar_auth_status():
        """Google Calendarの認証状態を返す。"""
        if not GOOGLE_CALENDAR_TOKEN_FILE.is_file():
            return {
                "success": True,
                "authenticated": False,
                "reason": "token_not_found",
            }

        try:
            _get_google_calendar_service()
            return {
                "success": True,
                "authenticated": True,
            }
        except Exception as e:
            return {
                "success": True,
                "authenticated": False,
                "reason": str(e),
            }


    def google_calendar_authenticate():
        """現在のAveliaセッション内でGoogle OAuth認証を実行する。"""
        global _google_calendar_service_cache

        result = _run_google_calendar_auth()

        # 認証前のserviceを保持していた場合に備えて破棄する。
        _google_calendar_service_cache = None

        # 保存直後にAPI clientを構築してtokenの利用可否まで確認する。
        _get_google_calendar_service()

        return result


    def _google_calendar_tool_schemas():
        nullable_string = {"type": ["string", "null"]}
        return [
            {
                "type": "function",
                "name": "google_calendar_auth_status",
                "description": (
                    "Google CalendarのOAuth認証状態を確認する。"
                    "認証操作は行わない。"
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_authenticate",
                "description": (
                    "ユーザーがGoogle Calendarの接続、認証、再認証を明示的に依頼した場合のみ使用する。"
                    "Avelia本体内部でOAuth認証URLを表示し、認証完了後にtokenを保存する。"
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_get_event",
                "description": (
                    "Google CalendarからイベントIDを指定して"
                    "予定を1件取得する。"
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "Google CalendarイベントID",
                        },
                        "calendar_id": {
                            "type": "string",
                            "description": "通常はprimary",
                        },
                    },
                    "required": [
                        "event_id",
                        "calendar_id",
                    ],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_get_upcoming",
                "description": (
                    "Google Calendarから現在以降の予定を取得する。"
                    "『今後の予定』『次の予定』『1週間の予定』などに使用する。"
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3650,
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10000,
                        },
                        "include_completed": {
                            "type": "boolean",
                        },
                        "calendar_id": {
                            "type": "string",
                        },
                    },
                    "required": [
                        "days",
                        "max_results",
                        "include_completed",
                        "calendar_id",
                    ],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_get_date",
                "description": "Google Calendarから指定日の予定を取得する。今日や明日も実際の日付YYYY-MM-DDに変換して指定する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scheduled_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "calendar_id": {"type": "string", "description": "通常はprimary"},
                    },
                    "required": ["scheduled_date", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_search",
                "description": "Google Calendarから期間内の予定をキーワード検索する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "start": {"type": "string", "description": "ISO日時"},
                        "end": {"type": "string", "description": "ISO日時"},
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["query", "start", "end", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_create",
                "description": "Google Calendarへ時刻付き予定を追加する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "YYYY-MM-DD HH:MMまたはISO日時"},
                        "end": nullable_string,
                        "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                        "description": nullable_string,
                        "location": nullable_string,
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["title", "start", "end", "duration_minutes", "description", "location", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_create_all_day",
                "description": "Google Calendarへ終日予定を追加する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "scheduled_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "end_date": nullable_string,
                        "description": nullable_string,
                        "location": nullable_string,
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["title", "scheduled_date", "end_date", "description", "location", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_create_weekly",
                "description": "Google Calendarへ曜日指定の毎週繰り返し予定を追加する。曜日は月0〜日6。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
                        "start_time": {"type": ["string", "null"]},
                        "end_time": nullable_string,
                        "description": nullable_string,
                        "location": nullable_string,
                        "start_date": nullable_string,
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["title", "weekdays", "start_time", "end_time", "description", "location", "start_date", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_mark_completed",
                "description": "Google Calendar予定のAvelia完了状態を変更する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "completed": {"type": "boolean"},
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["event_id", "completed", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_update",
                "description": "Google Calendarの既存予定をevent_idで部分更新する。削除や変更前に検索で対象を特定する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "title": nullable_string,
                        "start": nullable_string,
                        "end": nullable_string,
                        "description": nullable_string,
                        "location": nullable_string,
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["event_id", "title", "start", "end", "description", "location", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_delete",
                "description": "Google Calendarの予定をevent_idで削除する。対象が曖昧な場合は先に検索して特定する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "calendar_id": {"type": "string"},
                    },
                    "required": ["event_id", "calendar_id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "google_calendar_list_calendars",
                "description": "Googleアカウントから利用可能なカレンダー一覧を取得する。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        ]


    def _execute_google_calendar_tool(tool_name, arguments):
        if tool_name == "google_calendar_auth_status":
            return google_calendar_auth_status()

        if tool_name == "google_calendar_authenticate":
            return google_calendar_authenticate()

        calendar_id = arguments.get("calendar_id") or "primary"

        if tool_name == "google_calendar_get_event":
            event = google_calendar_get_event(
                arguments["event_id"],
                calendar_id=calendar_id,
            )
            return {
                "success": True,
                "event": event,
            }

        if tool_name == "google_calendar_get_upcoming":
            events = google_calendar_get_upcoming(
                days=arguments["days"],
                calendar_id=calendar_id,
                max_results=arguments["max_results"],
                include_completed=arguments[
                    "include_completed"
                ],
            )
            return {
                "success": True,
                "count": len(events),
                "events": events,
            }

        if tool_name == "google_calendar_get_date":
            events = google_calendar_get_date(
                arguments["scheduled_date"],
                calendar_id=calendar_id,
                include_completed=True,
            )
            return {
                "success": True,
                "count": len(events),
                "events": events,
            }

        if tool_name == "google_calendar_search":
            events = google_calendar_list_events(
                start=arguments["start"],
                end=arguments["end"],
                query=arguments["query"],
                calendar_id=calendar_id,
                max_results=10000,
                include_completed=True,
            )
            return {"success": True, "count": len(events), "events": events}

        if tool_name == "google_calendar_create":
            event = google_calendar_create_event(
                title=arguments["title"],
                start=arguments["start"],
                end=arguments.get("end"),
                duration_minutes=arguments.get("duration_minutes", 60),
                description=arguments.get("description"),
                location=arguments.get("location"),
                calendar_id=calendar_id,
            )
            return {"success": True, "event": event}

        if tool_name == "google_calendar_create_all_day":
            event = google_calendar_create_all_day(
                title=arguments["title"],
                scheduled_date=arguments["scheduled_date"],
                end_date=arguments.get("end_date"),
                description=arguments.get("description"),
                location=arguments.get("location"),
                calendar_id=calendar_id,
            )
            return {"success": True, "event": event}

        if tool_name == "google_calendar_create_weekly":
            event = google_calendar_create_weekly(
                title=arguments["title"], weekdays=arguments["weekdays"],
                start_time=arguments["start_time"], end_time=arguments.get("end_time"),
                description=arguments.get("description"), location=arguments.get("location"),
                calendar_id=calendar_id, start_date=arguments.get("start_date"),
            )
            return {"success": True, "event": event}

        if tool_name == "google_calendar_mark_completed":
            return google_calendar_mark_completed(
                arguments["event_id"], completed=arguments["completed"], calendar_id=calendar_id
            )

        if tool_name == "google_calendar_update":
            event = google_calendar_update_event(
                event_id=arguments["event_id"],
                title=arguments.get("title"),
                start=arguments.get("start"),
                end=arguments.get("end"),
                description=arguments.get("description"),
                location=arguments.get("location"),
                calendar_id=calendar_id,
            )
            return {"success": True, "event": event}

        if tool_name == "google_calendar_delete":
            return google_calendar_delete_event(
                arguments["event_id"],
                calendar_id=calendar_id,
            )

        if tool_name == "google_calendar_list_calendars":
            calendars = google_calendar_list_calendars()
            return {"success": True, "count": len(calendars), "calendars": calendars}

        raise ValueError(f"未対応のGoogle Calendar Toolです: {tool_name}")

    MEMORY_KEY_FILE = os.path.join(
        DATA_DIR,
        "memory.key"
    )

    MEMORY_HASH_FILE = None

    CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")
    SYSTEM_PROMPT_FILE = os.path.join(CONFIG_DIR, "Sys_Prompt.txt")
    MEMORY_CONFIG_FILE = os.path.join(CONFIG_DIR, "memory.ini")

    DVD_MODE = load_DVDmode()

    if DVD_MODE==0:
        DROPBOX_CONFIG_FILE = os.path.join(CONFIG_DIR, "dropbox.ini")
        CHAT_LOG_FILE = os.path.join(DATA_DIR, "memory.vlm")
        LOG_FILE = os.path.join(LOG_DIR, "message.log")
    else:
    
        LOG_FILE = os.path.join(APP_DATA, "message.log")
        CHAT_LOG_FILE = os.path.join(APP_DATA, "memory.vlm")
        if os.path.isfile(os.path.join(CONFIG_DIR, "dropbox.ini")) and not os.path.isfile(os.path.join(APP_DATA, "dropbox.ini")):
            shutil.copyfile(
                os.path.join(CONFIG_DIR, "dropbox.ini"),
                os.path.join(APP_DATA, "dropbox.ini")
            )

        if (os.path.isfile(os.path.join(CONFIG_DIR, "config.ini")) and not os.path.isfile(os.path.join(APP_DATA, "config.ini"))):
            shutil.copyfile(
                os.path.join(CONFIG_DIR, "config.ini"),
                os.path.join(APP_DATA, "config.ini")
                )
        if os.path.isfile(os.path.join(DATA_DIR, "memory.vlm")) and not os.path.isfile(os.path.join(APP_DATA, "memory.vlm")):
            shutil.copyfile(
                os.path.join(DATA_DIR, "memory.vlm"),
                os.path.join(APP_DATA, "memory.vlm")
            )
        if os.path.isfile(os.path.join(DATA_DIR, "memory.key")) and not os.path.isfile(os.path.join(APP_DATA, "memory.key")):
                shutil.copyfile(
                    os.path.join(DATA_DIR, "memory.key"),
                    os.path.join(APP_DATA, "memory.key")
                )
        if os.path.isfile(os.path.join(CONFIG_DIR, "Sys_Prompt.txt")) and not os.path.isfile(os.path.join(APP_DATA, "Sys_Prompt.txt")):
                shutil.copyfile(
                    os.path.join(CONFIG_DIR, "Sys_Prompt.txt"),
                    os.path.join(APP_DATA, "Sys_Prompt.txt")
                )
        if os.path.isfile(os.path.join(CONFIG_DIR, "memory.ini")) and not os.path.isfile(os.path.join(APP_DATA, "memory.ini")):
                    shutil.copyfile(
                        os.path.join(CONFIG_DIR, "memory.ini"),
                        os.path.join(APP_DATA, "memory.ini")
                    )
        MEMORY_CONFIG_FILE = os.path.join(APP_DATA, "memory.ini")
        DROPBOX_CONFIG_FILE = os.path.join(APP_DATA, "dropbox.ini")
        CONFIG_FILE = os.path.join(APP_DATA, "config.ini")
        SYSTEM_PROMPT_FILE = os.path.join(APP_DATA, "Sys_Prompt.txt")

    MEMORY_HASH_FILE = CHAT_LOG_FILE + ".sha256"
    EXEC_CONF_FILE = DATA_DIR / "exec_conf.ini"


    MAX_MEMORY = load_memory_conf()
    pre_clear = load_pre_clear()

    os.makedirs(CONFIG_DIR, exist_ok=True)
    if DVD_MODE==0:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)

    os.makedirs(DATA_DIR, exist_ok=True)
    if not EXEC_CONF_FILE.exists():
        config = configparser.ConfigParser()

        config["EXEC"] = {
            "enabled": "0"
        }

        with open(
            EXEC_CONF_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            config.write(f)
    if not os.path.isfile(DATA_DIR / "Sys_Prompt.txt"):

        shutil.copyfile(
            "/opt/Anvelk-Mainframe/config/Sys_Prompt_Template.txt",
            os.path.join(DATA_DIR, "Sys_Prompt.txt")
        )
        SYSTEM_PROMPT_FILE = DATA_DIR / "Sys_Prompt.txt"
    else:
        SYSTEM_PROMPT_FILE = DATA_DIR / "Sys_Prompt.txt"



    # =========================================================
    # Tool実行履歴
    # =========================================================

    EXEC_HISTORY_FILE = DATA_DIR / "execution_history.jsonl"


    def save_execution_history(tool_name, arguments, result):
        """
        Toolの実行結果をユーザー個別領域へ保存する。

        保存先:
        ~/.local/share/Avelia/execution_history.jsonl
        """
        import json
        from datetime import datetime

        try:
            DATA_DIR.mkdir(
                parents=True,
                exist_ok=True
            )

            record = {
                "executed_at": datetime.now().isoformat(
                    timespec="seconds"
                ),
                "tool": str(tool_name),
                "arguments": arguments,
                "result": result
            }

            with open(
                EXEC_HISTORY_FILE,
                "a",
                encoding="utf-8"
            ) as f:
                f.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        default=str
                    )
                    + "\n"
                )

            return True

        except Exception as e:
            # 履歴保存失敗だけでTool本体を失敗扱いにしない
            print("")
            print(" Tool実行履歴を保存できませんでした。")
            print(f" {type(e).__name__}: {e}")
            return False


    def purge_schedule_calendar_execution_history():
        """
        execution_history.jsonl から
        予定・カレンダー系Toolの過去履歴を削除する。

        Google Calendar移行前のDB由来予定を
        後から参照してしまう事故を防ぐ。
        """
        import json

        if not EXEC_HISTORY_FILE.is_file():
            return {
                "success": True,
                "removed": 0,
                "kept": 0,
            }

        kept = []
        removed = 0

        try:
            with open(
                EXEC_HISTORY_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                for line in f:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        record = json.loads(
                            line
                        )
                    except json.JSONDecodeError:
                        # 壊れた行はそのまま捨てる。
                        continue

                    if not isinstance(
                        record,
                        dict,
                    ):
                        continue

                    if _is_schedule_or_calendar_tool(
                        record.get(
                            "tool",
                            ""
                        )
                    ):
                        removed += 1
                        continue

                    kept.append(
                        record
                    )

            temp_file = (
                EXEC_HISTORY_FILE
                .with_suffix(
                    ".jsonl.tmp"
                )
            )

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as f:

                for record in kept:
                    f.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )

                f.flush()
                os.fsync(
                    f.fileno()
                )

            os.replace(
                temp_file,
                EXEC_HISTORY_FILE,
            )

            return {
                "success": True,
                "removed": removed,
                "kept": len(kept),
            }

        except Exception as e:
            return {
                "success": False,
                "removed": removed,
                "kept": len(kept),
                "error":
                    f"{type(e).__name__}: {e}",
            }


    def load_execution_history(limit=20):
        """
        最近のTool実行履歴を取得する。
        """
        import json

        if not EXEC_HISTORY_FILE.is_file():
            return []

        records = []

        try:
            with open(
                EXEC_HISTORY_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                for line in f:
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        record = json.loads(line)

                        if isinstance(record, dict):
                            records.append(record)

                    except json.JSONDecodeError:
                        continue

        except Exception:
            return []

        if limit <= 0:
            return []

        return records[-limit:]


    def _is_schedule_or_calendar_tool(tool_name):
        """
        予定・カレンダー系Toolか判定する。

        Google Calendar移行前のDB取得結果が
        execution_history.jsonl に残っていても、
        現在の予定情報としてSystem Promptへ再注入しないために使う。
        """
        name = str(
            tool_name
            or ""
        ).strip()

        if name.startswith(
            "google_calendar_"
        ):
            return True

        return name in {
            "add_schedule",
            "get_schedules",
            "delete_schedule",
            "calendar_add_once",
            "calendar_add_weekly",
            "calendar_get_today",
            "calendar_get_date",
            "calendar_complete_once",
            "calendar_complete_weekly",
            "calendar_delete_once",
            "calendar_delete_weekly",
            "calendar_import_csv",
        }


    def get_execution_history_context(limit=20):
        """
        System Promptへ渡すための実行履歴文字列を作成する。

        予定・カレンダー系の履歴は除外する。
        予定情報は鮮度が重要であり、過去のTool結果を
        現在の予定として再利用してはいけない。

        現在の予定を回答するときは必ずGoogle Calendar Toolを
        その場で実行して取得する。
        """
        import json

        # 予定系履歴を除いたうえでlimit件確保できるよう
        # 少し多めに読む。
        raw_records = load_execution_history(
            max(
                int(limit) * 5,
                50,
            )
        )

        records = [
            record
            for record in raw_records
            if not _is_schedule_or_calendar_tool(
                record.get(
                    "tool",
                    ""
                )
            )
        ]

        if limit > 0:
            records = records[
                -int(limit):
            ]
        else:
            records = []

        schedule_policy = (
            "重要: 予定・Google Calendar情報については、"
            "過去のTool実行履歴や会話中の古い予定情報を"
            "現在の予定として使用してはいけません。"
            "予定の追加・取得・検索・更新・削除・完了状態を"
            "確認する必要がある場合は、必ず現在の"
            "Google Calendar Toolを実行し、その結果だけを"
            "現在の予定情報として扱ってください。\n"
        )

        if not records:
            return (
                schedule_policy
                + "予定系以外のTool実行履歴はありません。"
            )

        return (
            schedule_policy
            + "以下は予定系を除外した実際のTool実行履歴です。\n"
            "過去のコマンド・Tool実行について判断する場合は、"
            "会話上の推測よりこの記録を優先してください。\n"
            "履歴に存在しない場合は「実行記録からは確認できません」"
            "と表現してください。\n\n"
            + json.dumps(
                records,
                ensure_ascii=False,
                indent=2,
                default=str
            )
        )

    # パーミッションエラーの防止
    if os.path.isfile("/opt/Anvelk-Mainframe/data/memory.vlm"):
        for file in os.listdir("/opt/Anvelk-Mainframe/data/"):
            if file.startswith("memory."):
                shutil.copyfile(
                    os.path.join("/opt/Anvelk-Mainframe/data/", file),
                    os.path.join(DATA_DIR, file)
                )
        os.remove("/opt/Anvelk-Mainframe/data/memory.vlm")
        os.remove("/opt/Anvelk-Mainframe/data/memory.key")
        os.remove("/opt/Anvelk-Mainframe/data/memory.vlm.sha256")
    
    # =========================================================
    # Google Calendar移行後の旧予定Tool履歴を掃除
    # =========================================================

    try:
        _schedule_history_cleanup = (
            purge_schedule_calendar_execution_history()
        )

        if (
            _schedule_history_cleanup.get(
                "success"
            )
            and _schedule_history_cleanup.get(
                "removed",
                0
            ) > 0
        ):
            print(
                "[Avelia] 旧予定Tool履歴を削除: "
                f"{_schedule_history_cleanup['removed']}件",
                flush=True,
            )

    except Exception as e:
        print(
            "[Avelia] 旧予定Tool履歴の削除に失敗: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


    # =========================================================
    # MySQL設定チェック
    # =========================================================

    try:
        _startup_config = load_config()
        if not _startup_config.defaults():
            raise RuntimeError(
                "settingsテーブルにDEFAULTセクションの設定がありません。"
            )
    except Exception as e:
        print("")
        print(" MySQLから設定を読み込めませんでした。")
        print(f" {type(e).__name__}: {e}")
        print("")
        input(" >> ")
        sys.exit(1)


    # =========================================================
    # 設定ファイル読み込み
    # =========================================================

    def load_exec_conf():
        """
        システムコマンド実行機能の有効・無効を取得する。

        [EXEC]
        enabled = 0  無効
        enabled = 1  有効
        """

        config = configparser.ConfigParser()

        try:
            config.read(
                EXEC_CONF_FILE,
                encoding="utf-8"
            )

            return config.getint(
                "EXEC",
                "enabled",
                fallback=0
            )

        except (ValueError, configparser.Error):
            return 0


    def load_voice():
        """
        合成音声の有効無効を変更
        """
        config = load_config()

        try:
            return int(config["DEFAULT"]["voice_enable"])
        except KeyError:
            return 0

    def load_log():
        """
        ログの有効無効を変更
        """
        config = load_config()

        try:
            return int(config["DEFAULT"]["log_enable"])
        except KeyError:
            return 0

    def load_model():
        """
        OpenAI APIで使用するモデル名を取得
        """
        config = load_config()

        try:
            return config["DEFAULT"]["model"]
        except KeyError:
            return "gpt-4"


    def load_reasoning_effort():
        """
        Responses APIで使用するreasoning effortを取得する。

        settingsテーブルの DEFAULT.reasoning_effort が存在する場合は
        その値を使用し、未設定の場合は medium を使用する。
        """
        config = load_config()

        try:
            effort = str(
                config["DEFAULT"].get(
                    "reasoning_effort",
                    "medium"
                )
            ).strip().lower()
        except (KeyError, AttributeError):
            effort = "medium"

        allowed = {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }

        if effort not in allowed:
            return "medium"

        return effort


    def load_openai_api_key():
        """
        MySQLのsettingsテーブル DEFAULT.api_key からOpenAI APIキーを取得する。
        旧Velwetherの設定キー名 api_key をそのまま使用する。
        """
        config = load_config()

        try:
            return config["DEFAULT"]["api_key"].strip()
        except KeyError:
            return ""


    def load_BotName():
        """
        Bot Name
        """
        config = load_config()
        app=DATA_DIR
        if os.path.isfile(os.path.join(app, "bot_name.conf")):
            bot_config = configparser.ConfigParser()
            bot_config.read(os.path.join(app, "bot_name.conf"), encoding="utf-8")

            bot_name = bot_config.get("DEFAULT", "bot_name", fallback="").strip()
            if bot_name:
                return bot_name
        try:
            return config["DEFAULT"]["bot_name"]
        except KeyError:
            return "ボット"

    def load_preload():
        """
        記憶機能の有効無効
        1 = memory.vlmを保存・読み込みする
        0 = memory.vlmを保存・読み込みしない
        """
        config = load_config()

        try:
            return int(config["DEFAULT"]["preload"])
        except (KeyError, ValueError):
            return 0


    def load_token():
        """
        OpenAIの最大生成トークン数
        """
        config = load_config()

        try:
            return int(config["DEFAULT"]["Max_Token"])
        except (KeyError, ValueError):
            return 1024
    from datetime import datetime

    def load_system_prompt():
        global sys_msg, real_name, last_login

        bot_name = load_BotName()

        try:
            with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
                base_prompt = f.read().strip()
        
            if not base_prompt:
                base_prompt = "あなたは自然な日本語を話すAIアシスタントです。"

        except FileNotFoundError:
            base_prompt = "あなたは自然な日本語を話すAIアシスタントです。"
        current_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execution_context = get_execution_history_context(limit=5)
        if sys_msg!="":
            session_msg=sys_msg
            sys_msg=""
        else:
            session_msg="現在、セッションエラーはありません"
        if real_name=="":
            real_name="ユーザー"
        if last_login=="":
            last_login="不明"
        return (
            f"あなたの名前は「{bot_name}」です。"
            f"ユーザーはあなたを「{bot_name}」として扱います。"
            f"自分自身について話すときも、その名前と人格設定を維持してください。"
            f"{base_prompt}"
            f"ユーザーの名前は「{real_name}」です。"
            f"ユーザーはあなたに対して敬語を使うことがあります。"
            f"ユーザーの名前を呼ぶときは、必ず「{real_name}さん」と呼んでください。"
            f"最終ログイン日時は{last_login}です。"
            f"学習機能は{('有効' if learning_enabled_keyword else '無効')} です。"
            "現在の予定・スケジュール・Google Calendarの内容を尋ねられた場合、"
            "記憶・過去会話・過去のTool結果だけで回答せず、"
            "必ずGoogle Calendar取得Toolを実行して最新情報を確認してください。"
            "Google Calendar Toolが返していない予定を、現在存在する予定として"
            "補完・推測・復元してはいけません。"
            f"{execution_context}"
            f"現在時刻は{current_date}です。"
            f"{session_msg}"
        )

    LOG_ENABLD=load_log()
    VOICE_ENABLD=load_voice()
    # ========================================================
    ## ログ出力
    #========================================================
    def write_log(messages):
        if LOG_ENABLD==0:
            return 
        import json

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n--- messages[-3:] ---\n")
            f.write(json.dumps(messages[-3:], ensure_ascii=False, indent=2))
            f.write("\n")

    #========================================================
    # ボイス設定
    #========================================================

    engine = None

    if VOICE_ENABLD == 1:
        try:
            import pyttsx3
            engine = pyttsx3.init()

            voices = engine.getProperty("voices")
            if voices:
                engine.setProperty("voice", voices[0].id)

        except Exception as e:
            print("")
            print(" 音声合成を初期化できませんでした。")
            print(f" {e}")
            print(" 音声なしで起動します。")
            engine = None

    # =========================================================
    # Dropbox設定 / 記憶同期
    # =========================================================

    def load_dropbox_config():
        """
        MySQLのsettingsテーブルからDROPBOXセクションを読み込む。
        設定が無い場合はDropbox同期を無効として扱う。
        """
        return load_settings_from_db("DROPBOX")

    def load_dropbox_key_path():
        """
        memory.key のDropbox保存先を取得する。
        key_path が未設定の場合は memory_path と同じフォルダに
        memory.key として保存する。
        """
        config = load_dropbox_config()

        try:
            path = config["DROPBOX"]["key_path"].strip()
            if path:
                return path
        except KeyError:
            pass

        memory_path = load_dropbox_memory_path()
        normalized = memory_path.replace("\\", "/")

        parent = normalized.rsplit("/", 1)[0]

        if not parent:
            return "/memory.key"

        return parent + "/memory.key"

    def load_dropbox_enabled():
        local_file = os.path.join(DATA_DIR, "memory.conf")

        if os.path.isfile(local_file):
            config = configparser.ConfigParser()
            config.read(local_file, encoding="utf-8")

            if "DROPBOX" in config:
                try:
                    return config["DROPBOX"].getboolean("enabled")
                except ValueError:
                    pass
                except KeyError:
                    pass

        config = load_dropbox_config()

        if "DROPBOX" in config:
            try:
                return config["DROPBOX"].getboolean("enabled")
            except ValueError:
                pass
            except KeyError:
                pass

        return False


    def load_dropbox_memory_path():
        local_file = os.path.join(DATA_DIR, "memory.conf")

        if os.path.isfile(local_file):
            config = configparser.ConfigParser()
            config.read(local_file, encoding="utf-8")

            try:
                path = config["DROPBOX"]["memory_path"].strip()
                return path if path else f"/{BOT_NAME}/memory.vlm"
            except KeyError:
            
                return f"/{BOT_NAME}/memory.vlm"

        config = load_dropbox_config()

        try:
            path = config["DROPBOX"]["memory_path"].strip()
            return path if path else f"/{BOT_NAME}/memory.vlm"
        except KeyError:
            return f"/{BOT_NAME}/memory.vlm"


    def get_dropbox_client():
        import dropbox

        config = load_dropbox_config()

        try:
            app_key = config["DROPBOX"]["app_key"].strip()
        except KeyError:
            app_key = ""

        try:
            refresh_token = config["DROPBOX"]["refresh_token"].strip()
        except KeyError:
            refresh_token = ""

        if not app_key:
            print("")
            print(" Dropbox App Keyが設定されていません。")
            app_key = input(
                " Dropbox App Keyを入力してください >> "
            ).strip()

            if not app_key:
                raise RuntimeError(
                    "Dropbox App Keyが入力されませんでした。"
                )

            save_setting_to_db(
                "DROPBOX",
                "app_key",
                app_key
            )

            print("")
            print(" Dropbox App Keyを保存しました。")

        if not refresh_token:
            refresh_token = setup_dropbox_oauth()

        return dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key
        )


    def ensure_dropbox_parent(dbx, remote_path):
        """
        /Velwether/memory.vlm のような保存先について、
        必要な親フォルダをDropbox側に作成する。
        """
        normalized = remote_path.replace("\\", "/")

        if not normalized.startswith("/"):
            normalized = "/" + normalized

        parts = [part for part in normalized.split("/") if part]

        # 最後はファイル名なので除外
        if len(parts) <= 1:
            return

        current = ""

        for part in parts[:-1]:
            current += "/" + part

            try:
                dbx.files_create_folder_v2(current)
            except Exception:
                # 既存フォルダの場合などはそのまま続行
                pass


    def _to_utc(dt):
        """
        Dropbox SDKが返すnaive datetimeをUTCとして正規化する。
        """
        from datetime import timezone

        if dt is None:
            return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)


    def get_local_memory_modified():
        if not os.path.isfile(CHAT_LOG_FILE):
            return None

        from datetime import datetime, timezone

        return datetime.fromtimestamp(
            os.path.getmtime(CHAT_LOG_FILE),
            tz=timezone.utc
        )


    def check_internet_connection():
        try:
            requests.get("https://www.google.com", timeout=5)
            return True
        except requests.RequestException:
            return False

    if not check_internet_connection():
        print("")
        print(" インターネットに接続できません。")
        print(" API呼び出しやDropbox同期は行えません。")
        input(" >> ")
        sys.exit(1)


    def save_memory_hash():
        """
        memory.vlmのSHA-256を保存する。
        """
        if not os.path.isfile(CHAT_LOG_FILE):
            return None

        digest = sha256_file(CHAT_LOG_FILE)

        with open(MEMORY_HASH_FILE, "w", encoding="ascii") as f:
            f.write(digest)

        return digest


    def verify_memory_hash():
        """
        保存済みSHA-256がある場合のみmemory.vlmを検証する。
        旧データなどSHA-256ファイルが無い場合はTrue。
        """
        if not os.path.isfile(CHAT_LOG_FILE):
            return False

        if not os.path.isfile(MEMORY_HASH_FILE):
            return True

        with open(MEMORY_HASH_FILE, "r", encoding="ascii") as f:
            expected = f.read().strip().lower()

        actual = sha256_file(CHAT_LOG_FILE).lower()

        return expected == actual


    def sha256_bytes(data):
        """
        bytesのSHA-256を取得する。
        """
        import hashlib
        return hashlib.sha256(data).hexdigest()


    def get_dropbox_memory_metadata(dbx):
        try:
            return dbx.files_get_metadata(
                load_dropbox_memory_path()
            )
        except Exception:
            return None

    def upload_memory_to_dropbox(show_error=False):
        """
        memory.vlm と memory.key をDropboxへアップロードする。
        memory.vlm はアップロード後に再取得し、
        SHA-256が一致することを確認する。
        """
        if not load_dropbox_enabled():
            return False

        if not os.path.isfile(CHAT_LOG_FILE):
            return False

        try:
            import dropbox

            dbx = get_dropbox_client()

            remote_path = load_dropbox_memory_path()
            remote_key_path = load_dropbox_key_path()

            ensure_dropbox_parent(dbx, remote_path)
            ensure_dropbox_parent(dbx, remote_key_path)

            local_modified = get_local_memory_modified()

            # ==============================
            # memory.vlm
            # ==============================

            with open(CHAT_LOG_FILE, "rb") as f:
                local_data = f.read()

            local_hash = sha256_bytes(local_data)

            dbx.files_upload(
                local_data,
                remote_path,
                mode=dropbox.files.WriteMode.overwrite,
                client_modified=(
                    local_modified.replace(tzinfo=None)
                    if local_modified is not None
                    else None
                ),
                mute=True
            )

            # 転送後に再取得して確認
            _, verify_response = dbx.files_download(remote_path)

            remote_hash = sha256_bytes(
                verify_response.content
            )

            if local_hash != remote_hash:
                raise RuntimeError(
                    "Dropbox転送後のSHA-256が一致しません。"
                )

            # ==============================
            # memory.key
            # ==============================

            if os.path.isfile(MEMORY_KEY_FILE):

                with open(MEMORY_KEY_FILE, "rb") as f:
                    key_data = f.read()

                dbx.files_upload(
                    key_data,
                    remote_key_path,
                    mode=dropbox.files.WriteMode.overwrite,
                    mute=True
                )

            return True

        except Exception as e:

            if show_error:
                print("")
                print(
                    " Dropboxへの記憶データ同期に失敗しました。"
                )
                print(
                    f" {type(e).__name__}: {e}"
                )
                traceback.print_exc()

            return False

    def download_memory_from_dropbox(dbx, metadata):
        """
        Dropboxのmemory.vlmとmemory.keyを取得する。

        memory.keyがDropbox側に存在する場合は先に取得し、
        memory.vlmを一時ファイルへ取得して検証後、
        ローカルへ置換する。
        """
        import json

        remote_path = load_dropbox_memory_path()
        remote_key_path = load_dropbox_key_path()

        temp_file = CHAT_LOG_FILE + ".tmp"

        # ==============================
        # memory.key を先に取得
        # ==============================

        try:
            _, key_response = dbx.files_download(
                remote_key_path
            )

            os.makedirs(
                os.path.dirname(
                    os.fspath(MEMORY_KEY_FILE)
                ),
                exist_ok=True
            )

            key_temp_file = (
                os.fspath(MEMORY_KEY_FILE) + ".tmp"
            )

            with open(key_temp_file, "wb") as f:
                f.write(
                    key_response.content
                )

            os.replace(
                key_temp_file,
                MEMORY_KEY_FILE
            )

        except Exception:
            # Dropbox側にキーが無い場合は
            # 既存のローカルキーを使用する
            pass

        # ==============================
        # memory.vlm を取得
        # ==============================

        _, response = dbx.files_download(
            remote_path
        )

        remote_hash = sha256_bytes(
            response.content
        )

        with open(temp_file, "wb") as f:
            f.write(
                response.content
            )

        try:
            temp_hash = sha256_file(
                temp_file
            )

            if remote_hash != temp_hash:
                raise RuntimeError(
                    "Dropboxから取得したmemory.vlmの"
                    "SHA-256が一致しません。"
                )

            # 旧形式の平文JSONか確認
            try:
                with open(
                    temp_file,
                    "r",
                    encoding="utf-8"
                ) as f:
                    test_messages = json.load(f)

                if not isinstance(
                    test_messages,
                    list
                ):
                    raise ValueError(
                        "Dropbox上のmemory.vlmの形式が"
                        "正しくありません。"
                    )

                for message in test_messages:

                    if not isinstance(
                        message,
                        dict
                    ):
                        raise ValueError(
                            "Dropbox上のmemory.vlmの"
                            "メッセージ形式が正しくありません。"
                        )

            except Exception:

                # 平文JSONでなければ暗号化形式
                test_messages = decrypt_memory(
                    temp_file,
                    MEMORY_KEY_FILE
                )

                if not isinstance(
                    test_messages,
                    list
                ):
                    raise ValueError(
                        "Dropbox上の暗号化memory.vlmの"
                        "形式が正しくありません。"
                    )

            os.replace(
                temp_file,
                CHAT_LOG_FILE
            )

            save_memory_hash()

        except Exception:

            if os.path.isfile(temp_file):
                os.remove(temp_file)

            raise

        remote_modified = _to_utc(
            getattr(
                metadata,
                "client_modified",
                None
            )
        )

        if remote_modified is not None:

            timestamp = (
                remote_modified.timestamp()
            )

            os.utime(
                CHAT_LOG_FILE,
                (timestamp, timestamp)
            )


    def sync_memory():
        """
        起動時にローカルとDropboxのmemory.vlmを比較する。

        Dropboxが新しい:
            Dropbox -> ローカル
        ローカルが新しい:
            ローカル -> Dropbox
        片方だけ存在:
            存在する側をもう片方へ同期

        ネット未接続などでDropboxへ接続できない場合は
        ローカルだけでそのまま起動する。
        """

        if not load_dropbox_enabled():
            return

        try:
            dbx = get_dropbox_client()

            local_modified = get_local_memory_modified()
            remote_metadata = get_dropbox_memory_metadata(dbx)

            remote_modified = None

            if remote_metadata is not None:
                remote_modified = _to_utc(
                    getattr(
                        remote_metadata,
                        "client_modified",
                        None
                    )
                )

            # 両方ない
            if local_modified is None and remote_metadata is None:
                return

            # Dropboxだけある
            if local_modified is None and remote_metadata is not None:
                print(" Dropboxから記憶データを取得しています...")
                download_memory_from_dropbox(
                    dbx,
                    remote_metadata
                )
                print(" Dropboxの記憶データを取得しました。")
                return

            # ローカルだけある
            if local_modified is not None and remote_metadata is None:
                print(" ローカルの記憶データをDropboxへ同期しています...")

                if upload_memory_to_dropbox(show_error=True):
                    print(" Dropboxへの同期が完了しました。")

                return

            # Dropboxの更新日時が取れない場合はローカル優先
            if remote_modified is None:
                upload_memory_to_dropbox(
                    show_error=True
                )
                return

            # 更新日時の差
            time_diff = abs(
                (local_modified - remote_modified).total_seconds()
            )

            # 1秒以内なら同じものとして扱う
            if time_diff <= 1:
                print(" Dropboxとの記憶データは同期済みです。")
                return

            # Dropboxの方が新しい
            if remote_modified > local_modified:
                print(" Dropboxに新しい記憶データがあります。")

                download_memory_from_dropbox(
                    dbx,
                    remote_metadata
                )

                print(" Dropboxの記憶データを使用します。")

            # ローカルの方が新しい
            else:
                print(
                    " ローカルの記憶データの方が新しいため"
                    "Dropboxへ同期します。"
                )

                upload_memory_to_dropbox(
                    show_error=True
                )

        except Exception as e:
            print("")
            print(
                " Dropboxに接続できないため"
                "ローカルの記憶データを使用します。"
            )
            print(f" {e}")


    # =========================================================
    # 会話履歴
    # =========================================================
    def save_chat(messages):
        """
        会話履歴を暗号化してmemory.vlmへ保存する。
        保存後に復号テストとSHA-256記録を行う。
        preload=0の場合は保存もDropbox同期もしない。
        """

        if load_preload() == 0:
            return

        try:
            encrypt_memory(
                messages,
                CHAT_LOG_FILE,
                MEMORY_KEY_FILE
            )

            # 保存直後に復号して内容確認
            check_messages = decrypt_memory(
                CHAT_LOG_FILE,
                MEMORY_KEY_FILE
            )

            if check_messages != messages:
                raise RuntimeError(
                    "保存後のmemory.vlmの内容が一致しません。"
                )

            save_memory_hash()

        except Exception as e:
            print("")
            print(" 会話履歴の保存中にエラーが発生しました。")
            print(f" {e}")
            return

        upload_memory_to_dropbox(
            show_error=False
        )


    def load_chat():
        """
        平文JSONならそのまま読み込み、
        読めなければ暗号化データとして復号する。
        SHA-256記録がある場合は読み込み前に破損確認する。
        """

        memory_file = CHAT_LOG_FILE
        key_file = MEMORY_KEY_FILE

        import json

        if not os.path.isfile(memory_file):
            return []

        if not verify_memory_hash():
            print("")
            print(" memory.vlmのSHA-256が一致しません。")
            print(" 記憶データの破損を検出しました。")
            return []

        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                return data

        except Exception:
            pass

        try:
            return decrypt_memory(
                memory_file,
                key_file
            )

        except Exception as e:
            print("")
            print(" memory.vlmの読み込みに失敗しました。")
            print(f" {e}")
            print("")
            print(" 新しい会話として開始します。")
            return []


    def new_chat():
        """
        新規会話
        """
        return [
            # {
            #     "role": "system",
            #     "content": load_system_prompt()
            # }
        ]


    # =========================================================
    # OpenAI API
    # =========================================================

    def check_openai():
        """
        OpenAI APIキーが設定されているか確認
        """
        return bool(load_openai_api_key())


    def learning_openai(prompt):

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        return chat_with_openai(
            messages
        )


    def build_context(messages):

        conversation_messages = [
            m for m in messages
            if m.get("role") != "system"
        ]

        last_user_message = ""

        for message in reversed(conversation_messages):
            if message.get("role") == "user":
                last_user_message = message.get(
                    "content",
                    ""
                )
                break

        system_prompt = load_system_prompt()

        if last_user_message:
            try:
                knowledge_context = get_knowledge_context(
                    last_user_message,
                    limit=5
                )

                if knowledge_context:
                    system_prompt += (
                        "\n\n"
                        + knowledge_context
                    )

            except Exception:
                pass

        system_message = {
            "role": "system",
            "content": system_prompt
        }

        if MAX_MEMORY < 0:
            return [system_message]

        recent_messages = conversation_messages[-MAX_MEMORY:]

        return [system_message] + recent_messages


    def build_context_old(messages):
        conversation_messages = [
            m for m in messages
            if m.get("role") != "system"
        ]

        system_message = {
                "role": "system",
                "content": load_system_prompt()
            }

        if MAX_MEMORY <= 0:
            return [system_message]

        recent_messages = conversation_messages[-MAX_MEMORY:]

        return [system_message] + recent_messages


    def write_API_log(messages, response):
        """
        OpenAI APIのエラーをログに出力する。
        DVDモードの場合はAPP_DATAに、通常モードの場合はLOG_DIRに出力する。
        なお、DVDに書き込んでいる場合は、DVDが書き込み禁止のためエラーになる可能性がある。
        """
        error_message = messages
        try:
            if DVD_MODE == 1:
                ERROR_LOG_FILE = os.path.join(APP_DATA, "error.log")
                with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"OpenAI API Error: {response.status_code}\n")
                    f.write(f"{error_message}\n")
                    f.write("\n")
            if DVD_MODE == 0:
                ERROR_LOG_FILE = os.path.join(LOG_DIR, "error.log")
                with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"OpenAI API Error: {response.status_code}\n")
                    f.write(f"{error_message}\n")
                    f.write("\n")
        except Exception as e:
            print("")
            print(" OpenAI APIのエラーをログに出力できませんでした。")
            print(f" {e}")


    def should_use_web_search(messages):
        """
        最新のユーザーメッセージを確認し、
        Web検索を明示的に要求している場合のみ True を返す。
        """

        if not messages:
            return False

        last_user_message = ""

        for message in reversed(messages):
            if message.get("role") == "user":
                last_user_message = str(message.get("content", ""))
                break

        search_keywords = [
            "検索して",
            "検索してみて",
            "について調べて",
            "web検索",
            "Web検索",
            "WEB検索",
            "ウェブ検索",
            "ネット検索",
            "ネットで検索",
            "ネットで調べて",
            "webで調べて",
            "Webで調べて",
            "WEBで調べて",
            "ウェブで調べて",
            "最新情報を検索",
            "最新情報を調べて",
            "最新の情報を検索",
            "最新の情報を調べて",
            "最新ニュースを検索",
            "最新ニュースを調べて",
            "今日の情報を検索",
            "今日の情報を調べて",
            "現在の情報を検索",
            "現在の情報を調べて",
        ]

        return any(keyword in last_user_message for keyword in search_keywords)


    def _openai_headers(api_key):
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }


    def _handle_openai_http_error(response):
        print("")
        print(f" OpenAI API Error: {response.status_code}")

        try:
            error_data = response.json()
            error_message = (
                error_data
                .get("error", {})
                .get("message", response.text)
            )
            print(f" {error_message}")
            write_API_log(error_message, response)
        except Exception:
            error_message = response.text
            print(error_message)
            write_API_log(error_message, response)

        if response.status_code == 429:
            print("")
            print(" APIの利用上限・残高・レート制限などを確認してください。")

        return None


    def _post_openai(api_url, headers, data):
        try:
            return requests.post(
                api_url,
                headers=headers,
                json=data,
                timeout=120
            )
        except requests.exceptions.ConnectionError:
            print("")
            print(" OpenAI APIに接続できませんでした。")
            return None
        except requests.exceptions.Timeout:
            print("")
            print(" OpenAI APIとの通信がタイムアウトしました。")
            return None
        except requests.exceptions.RequestException as e:
            print("")
            print(" OpenAI APIとの通信中にエラーが発生しました。")
            print(f" {e}")
            return None


    def _extract_responses_text(result):
        """
        Responses APIの output 配列から最終テキストを取り出す。
        web_search_call などのtool itemは読み飛ばす。
        """

        texts = []

        for item in result.get("output", []):
            if item.get("type") != "message":
                continue

            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text")
                    if text:
                        texts.append(text)

        if not texts:
            return None

        return "\n".join(texts)


    def chat_with_openai_web_search(messages):
        """
        Web検索専用処理。

        Responses API + built-in Web Searchを使う。
        検索モデルは費用を抑えるため gpt-5.6-luna を使用する。
        """

        api_key = load_openai_api_key()

        if not api_key:
            print("")
            print(" OpenAI APIキーが設定されていません。")
            return None

        model = "gpt-5.6-luna"
        api_url = "https://api.openai.com/v1/responses"
        context_messages = build_context(messages)

        print("")
        print(" Web検索モード")
        print(f" 使用モデル: {model}")

        data = {
            "model": model,
            "input": context_messages,
            "tools": [
                {
                    "type": "web_search_preview"
                }
            ],
            "tool_choice": "auto",
            "max_output_tokens": load_token()
        }

        response = _post_openai(
            api_url,
            _openai_headers(api_key),
            data
        )

        if response is None:
            return None

        if response.status_code != 200:
            return _handle_openai_http_error(response)

        try:
            result = response.json()
            content = _extract_responses_text(result)

            if not content:
                print("")
                print(" OpenAIからテキスト応答が返されませんでした。")
                return None

            return content

        except Exception as e:
            print("")
            print(" OpenAI Responses APIの応答を解析できませんでした。")
            print(f" {e}")
            return None


    def _ensec_tools():
        """RSA file encryption/decryption via the installed ensec CLI."""
        return [{
            "type": "function",
            "name": "ensec_rsa_file",
            "description": "ENSECのRSAモードでファイルを暗号化または復号する。0=encrypt、1=decrypt。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "integer", "enum": [0, 1], "description": "0=encrypt、1=decrypt"},
                    "file_path": {"type": "string", "description": "実行ユーザーのホーム配下にある対象ファイルの絶対パス。復号時は.rdecファイル。"},
                },
                "required": ["mode", "file_path"],
                "additionalProperties": False,
            },
        }]


    def _exec_ensec_rsa(arguments):
        if (not isinstance(arguments, dict) or set(arguments) != {"mode", "file_path"}
                or type(arguments["mode"]) is not int or arguments["mode"] not in (0, 1)
                or not isinstance(arguments["file_path"], str) or not arguments["file_path"]):
            return {"success": False, "error": "invalid_arguments"}

        mode = arguments["mode"]
        supplied = Path(arguments["file_path"])
        if not supplied.is_absolute():
            return {"success": False, "error": "absolute_path_required"}
        home = Path.home().resolve()
        target = supplied.resolve()
        if not target.is_relative_to(home) or not target.is_file():
            return {"success": False, "error": "file_must_be_regular_and_inside_home"}
        if mode == 1 and target.suffix != ".rdec":
            return {"success": False, "error": "rdec_file_required"}
        output = Path(str(target) + ".rdec") if mode == 0 else Path(str(target)[:-5])
        if output.exists() or output.is_symlink():
            return {"success": False, "error": "output_already_exists"}
        # Services may launch the venv Python directly without adding its bin to PATH.
        executable = Path(sys.executable)
        candidates = [executable.with_name("ensec.exe" if os.name == "nt" else "ensec")]
        if os.name == "nt":
            candidates.append(executable.parent / "Scripts" / "ensec.exe")
        else:
            candidates.append(Path.home() / ".local" / "bin" / "ensec")
        command = next((str(path) for path in candidates if path.is_file()), None)
        if command is None:
            command = shutil.which("ensec")
        if command is None:
            return {"success": False, "error": "ensec_not_found"}
        try:
            result = subprocess.run(
                [command, "encrypt" if mode == 0 else "decrypt", str(target), "--rsa"],
                capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "ensec_timeout"}
        except OSError as error:
            return {"success": False, "error": type(error).__name__}
        if result.returncode != 0 or not output.is_file():
            return {"success": False, "error": "ensec_failed", "returncode": result.returncode,
                    "message": (result.stderr or result.stdout)[-1000:]}
        return {"success": True, "mode": mode, "output_path": str(output)}


    def _schedule_tools():
        """通常会話でアヴェリアに公開するResponses API用Tool。"""
        return _google_calendar_tool_schemas() + [{
        "type": "function",
        "name": "ocr_image",
        "description": (
            "ローカルに保存されている画像ファイルをOCRで読み取り、"
            "画像内の日本語・英語テキストを抽出します。"
            "画像に書かれている文章、文字、帳票、スクリーンショットなどを"
            "読み取る必要がある場合に使用してください。"
            "OCRで読み取った内容から重要度を判断できる場合は、"
            "Normal、Important、Critical のいずれかへの"
            "フォルダ割り振りをユーザーに提案してください。"
            "ユーザーが明示的に許可または指示するまでは、"
            "ファイルを移動してはいけません。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": (
                        "OCRで読み取る画像ファイルのパス。"
                        "PNG、JPEGなどの画像ファイルを指定します。"
                    )
                }
            },
            "required": [
                "image_path"
            ],
            "additionalProperties": False
        }
    },{
        "type": "function",
        "name": "sort_file_by_importance",
        "description": (
            "ファイルを重要度に応じて /mnt/Folders 内の分類フォルダへ移動します。"
            "Normal は通常の文書、Important は重要な文書、"
            "Critical は最重要の文書です。"
            "OCRなどで内容を確認し、ユーザーがファイルの分類を"
            "明示的に許可または指示した場合のみ使用してください。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "分類して移動するファイルのパス"
                },
                "importance": {
                    "type": "string",
                    "enum": [
                        "normal",
                        "important",
                        "critical"
                    ],
                    "description": (
                        "分類先の重要度。"
                        "normal=Normal、"
                        "important=Important、"
                        "critical=Critical"
                    )
                }
            },
            "required": [
                "file_path",
                "importance"
            ],
            "additionalProperties": False
        }
    },
            {
        "type": "function",
        "name": "read_pdf",
        "description": (
            "ローカルに保存されているPDFファイルからテキストを読み取ります。"
            "PDFの内容確認、要約、分析などを行う場合に使用してください。"
            "必要に応じて読み取るページ範囲を指定できます。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "pdf_path": {
                    "type": "string",
                    "description": "読み取るPDFファイルのパス"
                },
                "start_page": {
                    "type": ["integer", "null"],
                    "description": "読み取り開始ページ。省略時は1ページ目"
                },
                "end_page": {
                    "type": ["integer", "null"],
                    "description": "読み取り終了ページ。省略時は最終ページ"
                }
            },
            "required": [
                "pdf_path",
                "start_page",
                "end_page"
            ],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "get_pdf_info",
        "description": (
            "ローカルに保存されているPDFファイルの基本情報を取得します。"
            "ページ数、ファイルサイズ、タイトル、作成者などを確認する場合に"
            "使用してください。PDF本文は読み取りません。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "pdf_path": {
                    "type": "string",
                    "description": "情報を取得するPDFファイルのパス"
                }
            },
            "required": [
                "pdf_path"
            ],
            "additionalProperties": False
        }
    },
            {
        "type": "function",
        "name": "create_pdf",
        "description": (
            "ユーザーが指定した内容をPDFファイルとして作成します。"
            "予定表専用ではなく、文章、レポート、説明資料、"
            "一覧、表などをPDFとして保存したい場合に使用してください。"
            "ユーザーが今日の予定をPDF化したい場合は、"
            "create_today_schedule_pdfを優先してください。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "PDFのタイトル"
                },
                "content": {
                    "type": "string",
                    "description": "PDF本文。プレーンテキストで指定する"
                },
                "filename": {
                    "type": [
                        "string",
                        "null"
                    ],
                    "description": (
                        "出力ファイル名。"
                        "指定がなければ自動生成する。"
                        "拡張子.pdfは省略可能"
                    )
                }
            },
            "required": [
                "title",
                "content",
                "filename"
            ],
            "additionalProperties": False
        }
    },

        {
        "type": "function",
        "name": "create_today_schedule_pdf",
        "description": (
            "今日の予定をPDFファイルとして出力します。"
            "ユーザーが『今日の予定をPDFにして』"
            "『今日のスケジュールをPDF出力して』など、"
            "今日の予定のPDF生成を依頼した場合に使用してください。"
            "出力先はユーザーのホームディレクトリ内のout_pdfです。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        }
    },
            {
        "type": "function",
        "name": "send_slack_notification",
        "description": (
            "Slackへ通知メッセージを送信します。"
            "ユーザーが明示的にSlackへの通知を依頼した場合、"
            "またはユーザーが依頼した作業の完了・失敗をSlackへ通知するよう明示した場合に使用してください。"
            "単なる会話内容を勝手に通知してはいけません。"
            "通知に失敗した場合には、内部的にFalseが返されますが、ユーザーには通知失敗の旨を伝えてください。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Slackへ送信する通知本文"
                }
            },
            "required": [
                "message"
            ],
            "additionalProperties": False
        }
    },
            {
        "type": "function",
        "name": "run_system",
        "description": (
            "Linuxサーバー上でシェルコマンドを実行します。"
            "ユーザーが現在のメッセージで明示的にコマンド実行を依頼した場合のみ使用してください。"
            "単なる質問、コマンド例の作成、説明、確認では使用しないでください。"
            "「実行したらどうなる？」などの質問の場合は(実行例として実行した場合はそれを明記して)使用しないでください。"
            "実行にはタイムアウトと出力サイズ制限があります。"
            "また、実行結果はユーザーに返すため、機密情報やパスワードなどを含むコマンドは使用しないでください。"
            "一部コマンドは実行制限がかかっています。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "実行するLinuxシェルコマンド"
                },
                "timeout": {
                    "type": "integer",
                    "description": "コマンド実行のタイムアウト秒数。通常は30秒"
                },
                "max_output": {
                    "type": "integer",
                    "description": "取得する最大出力サイズ。通常は65536バイト"
                }
            },
            "required": [
                "command",
                "timeout",
                "max_output"
            ],
            "additionalProperties": False
        }
    },
            {
        "type": "function",
        "name": "read_file",
        "description": (
            "ユーザーが指定したLinuxサーバー上のテキストファイルを読み込みます。"
            "ログ、設定ファイル、ソースコードなどの内容確認に使用します。"
            "ファイルの書き込みやプログラム実行は行いません。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "読み込むファイルのパス"
                }
            },
            "required": ["path"],
            "additionalProperties": False
        }
    },{
        "type": "function",
        "name": "write_file",
        "description": (
            "Linuxサーバー上のホームディレクトリ配下へ"
            "UTF-8テキストファイルを書き込みます。"
            "ユーザーが明示的にファイル作成または変更を依頼した場合のみ使用してください。"
            "ホームディレクトリ配下以外には書き込めません。"
            "既存ファイルを上書きする場合は overwrite=true を指定してください。"
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "書き込み先ファイルパス。"
                        "ホームディレクトリ配下のみ使用可能です。"
                    )
                },
                "content": {
                    "type": "string",
                    "description": "ファイルへ書き込むUTF-8テキスト内容"
                },
                "overwrite": {
                    "type": "boolean",
                    "description": (
                        "既存ファイルを上書きする場合はtrue。"
                        "新規作成または上書きしない場合はfalse"
                    )
                }
            },
            "required": [
                "path",
                "content",
                "overwrite"
            ],
            "additionalProperties": False
        }
    },
            {
                "type": "function",
                "name": "add_schedule",
                "description": (
                    "ユーザーが指定した日時に通知する予定を登録します。"
                    "『明日12時』『9月5日の18時』などの相対・自然言語日時は、"
                    "system promptにある現在時刻を基準に絶対日時へ変換してください。"
                    "タイトルの指定がない場合は、関連する任意のタイトルにしてください"
                ),
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "短い予定名"
                        },
                        "message": {
                            "type": "string",
                            "description": "通知時に送信する本文"
                        },
                        "scheduled_at": {
                            "type": "string",
                            "description": "YYYY-MM-DD HH:MM:SS形式の通知日時"
                        }
                    },
                    "required": ["title", "message", "scheduled_at"],
                    "additionalProperties": False
                }
            },
            {
                "type": "function",
                "name": "get_schedules",
                "description": "Google Calendarから今後のスケジュール予定を取得します。旧互換Toolです。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_used": {
                            "type": "boolean",
                            "description": "旧互換引数。trueならAvelia完了済み予定も含める"
                        }
                    },
                    "required": ["include_used"],
                    "additionalProperties": False
                }
            },
            {
                "type": "function",
                "name": "delete_schedule",
                "description": "指定したGoogle CalendarイベントIDのスケジュールを削除します。",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_id": {
                            "type": "string",
                            "description": "削除するGoogle CalendarイベントID"
                        }
                    },
                    "required": ["schedule_id"],
                    "additionalProperties": False
                }
            },
            {
        "type": "function",

        "name":
            "calendar_add_once",

        "description": (
            "通知なしの単発予定を登録します。"
            "特定の日だけ存在する予定に使用します。"
        ),

        "strict": True,

        "parameters": {
            "type": "object",

            "properties": {

                "title": {
                    "type": "string"
                },

                "scheduled_date": {
                    "type": "string",
                    "description":
                        "YYYY-MM-DD"
                },

                "start_time": {
                    "type": [
                        "string",
                        "null"
                    ]
                },

                "end_time": {
                    "type": [
                        "string",
                        "null"
                    ]
                },

                "description": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },

            "required": [
                "title",
                "scheduled_date",
                "start_time",
                "end_time",
                "description"
            ],

            "additionalProperties":
                False
        }
    },{
        "type": "function",

        "name":
            "calendar_add_weekly",

        "description": (
            "曜日条件付きの定期予定を登録します。"
            "曜日番号は"
            "月0、火1、水2、木3、"
            "金4、土5、日6です。"
        ),

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "title": {
                    "type": "string"
                },

                "weekdays": {

                    "type": "array",

                    "items": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 6
                    }
                },

                "start_time": {
                    "type": [
                        "string",
                        "null"
                    ]
                },

                "end_time": {
                    "type": [
                        "string",
                        "null"
                    ]
                },

                "description": {
                    "type": [
                        "string",
                        "null"
                    ]
                }
            },

            "required": [
                "title",
                "weekdays",
                "start_time",
                "end_time",
                "description"
            ],

            "additionalProperties":
                False
        }
    },{
        "type": "function",

        "name":
            "calendar_get_today",

        "description":
            "今日の未完了予定を取得します。",

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {},

            "required": [],

            "additionalProperties":
                False
        }
    },{
        "type": "function",

        "name":
            "calendar_complete_once",

        "description":
            "Google Calendar上の単発予定を完了済みにします。",

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "schedule_id": {
                    "type": "string",
                    "description": "Google CalendarイベントID"
                }
            },

            "required": [
                "schedule_id"
            ],

            "additionalProperties":
                False
        }
    },{
        "type": "function",

        "name":
            "calendar_complete_weekly",

        "description": (
            "Google Calendar上の曜日条件付き定期予定の"
            "その日分だけを完了済みにします。"
        ),

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "schedule_id": {
                    "type": "string",
                    "description": "Google CalendarイベントID"
                },

                "scheduled_date": {
                    "type": [
                        "string",
                        "null"
                    ],

                    "description":
                        "YYYY-MM-DD。"
                        "今日ならnull"
                }
            },

            "required": [
                "schedule_id",
                "scheduled_date"
            ],

            "additionalProperties":
                False
        }
    },{
        "type": "function",

        "name":
            "calendar_import_csv",

        "description":
            "CSVから予定を一括登録します。",

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "path": {
                    "type": "string"
                }
            },

            "required": [
                "path"
            ],

            "additionalProperties":
                False
        }
    },
    {
        "type": "function",

        "name":
            "calendar_delete_once",

        "description": (
            "単発予定を削除します。"
            "ユーザーが予定の削除を明示的に依頼した場合に使用してください。"
        ),

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "schedule_id": {
                    "type": "string",
                    "description":
                        "削除する単発予定のGoogle CalendarイベントID"
                }
            },

            "required": [
                "schedule_id"
            ],

            "additionalProperties":
                False
        }
    },
        {
        "type": "function",

        "name":
            "calendar_delete_weekly",

        "description": (
            "曜日条件付きの定期予定を削除します。"
            "ユーザーが定期予定そのものの削除を明示的に依頼した場合に使用してください。"
            "今日の分だけ取り消す場合には使用しません。"
        ),

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "schedule_id": {
                    "type": "string",
                    "description":
                        "削除する定期予定のGoogle CalendarイベントID"
                }
            },

            "required": [
                "schedule_id"
            ],

            "additionalProperties":
                False
        }
    },{
        "type": "function",

        "name":
            "calendar_get_date",

        "description": (
            "指定日の通常予定を取得します。"
            "単発予定と、その日に該当する曜日条件付き定期予定の両方を返します。"
            "『明日』『明後日』『9月10日』などの自然言語の日付は、"
            "system promptにある現在時刻を基準にYYYY-MM-DDへ変換してください。"
        ),

        "strict": True,

        "parameters": {

            "type": "object",

            "properties": {

                "scheduled_date": {
                    "type": "string",
                    "description":
                        "取得する日付。YYYY-MM-DD形式"
                },

                "unfinished_only": {
                    "type": "boolean",
                    "description":
                        "trueなら未完了予定のみ取得する"
                }
            },

            "required": [
                "scheduled_date",
                "unfinished_only"
            ],

            "additionalProperties":
                False
        }
    },{
            "type": "function",
            "name": "add_one_shot_task",
            "description": (
                "指定した日付と時刻に一度だけ"
                "プログラムを実行するタスクを登録する"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {
                        "type": "string",
                        "description": "タスク名"
                    },
                    "program_path": {
                        "type": "string",
                        "description": "実行するプログラムの絶対パス"
                    },
                    "arguments": {
                        "type": ["string", "null"],
                        "description": "プログラム引数"
                    },
                    "working_directory": {
                        "type": ["string", "null"],
                        "description": "作業ディレクトリ"
                    },
                    "run_date": {
                        "type": "string",
                        "description": "実行日。YYYY-MM-DD形式"
                    },
                    "run_time": {
                        "type": "string",
                        "description": "実行時刻。HH:MM または HH:MM:SS形式"
                    },
                },
                "required": [
                    "task_name",
                    "program_path",
                    "run_date",
                    "run_time",
                ],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "add_weekly_task",
            "description": (
                "指定曜日と時刻に毎週実行する"
                "プログラムタスクを登録する"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {
                        "type": "string",
                        "description": "タスク名"
                    },
                    "program_path": {
                        "type": "string",
                        "description": "実行するプログラムの絶対パス"
                    },
                    "arguments": {
                        "type": ["string", "null"],
                        "description": "プログラム引数"
                    },
                    "working_directory": {
                        "type": ["string", "null"],
                        "description": "作業ディレクトリ"
                    },
                    "weekday": {
                        "type": "string",
                        "description": (
                            "曜日。月曜日、火曜日などの日本語、"
                            "または monday などの英語"
                        )
                    },
                    "run_time": {
                        "type": "string",
                        "description": "実行時刻。HH:MM または HH:MM:SS形式"
                    },
                },
                "required": [
                    "task_name",
                    "program_path",
                    "weekday",
                    "run_time",
                ],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "list_tasks",
            "description": (
                "登録されているスケジュールタスク一覧を取得する"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_disabled": {
                        "type": "boolean",
                        "description": "無効化済みタスクも含めるか"
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "get_task",
            "description": (
                "指定したIDのスケジュールタスク詳細を取得する"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "タスクID"
                    }
                },
                "required": [
                    "task_id"
                ],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "disable_task",
            "description": (
                "指定したタスクを無効化する。"
                "実行中のタスクは無効化しない"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "タスクID"
                    }
                },
                "required": [
                    "task_id"
                ],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "enable_task",
            "description": (
                "無効化されたタスクを再度pending状態に戻す"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "タスクID"
                    }
                },
                "required": [
                    "task_id"
                ],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "retry_task",
            "description": (
                "failed状態のタスクをpendingに戻し、"
                "再実行可能な状態にする"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "タスクID"
                    }
                },
                "required": [
                    "task_id"
                ],
                "additionalProperties": False,
            },
        },

        {
            "type": "function",
            "name": "delete_task",
            "description": (
                "指定したタスクを削除する。"
                "実行中のタスクは削除しない"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "タスクID"
                    }
                },
                "required": [
                    "task_id"
                ],
                "additionalProperties": False,
            },
        },
        ] + file_signature.tools() + _ensec_tools()

    def _json_safe_schedule_rows(rows):
        """DB取得結果をTool応答用JSONへ変換する。"""
        result = []

        for row in rows:
            item = dict(row)
            for key in ("scheduled_at", "created_at"):
                value = item.get(key)
                if value is not None and hasattr(value, "strftime"):
                    item[key] = value.strftime("%Y-%m-%d %H:%M:%S")
            result.append(item)

        return result

    def execute_avelia_tool(tool_name, arguments):
        """OpenAIから要求されたローカルToolを実行する。"""
        if tool_name.startswith("google_calendar_"):
            return _execute_google_calendar_tool(tool_name, arguments)
        if tool_name in {tool["name"] for tool in file_signature.tools()}:
            return file_signature.exec(tool_name, arguments)
        if tool_name == "ensec_rsa_file":
            return _exec_ensec_rsa(arguments)

        TASK_DB_CONFIG = load_database_config()
        if tool_name == "sort_file_by_importance":

            return sort_file_by_importance(
                file_path=arguments["file_path"],
                importance=arguments["importance"]
            )
        if tool_name == "ocr_image":

            return ocr_image(
                image_path=arguments["image_path"]
            )
        if tool_name == "read_pdf":

            return read_pdf(
                pdf_path=arguments["pdf_path"],
                start_page=(
                    arguments.get("start_page")
                    if arguments.get("start_page") is not None
                    else 1
                ),
                end_page=arguments.get("end_page")
            )

        if tool_name == "get_pdf_info":

            return get_pdf_info(
                pdf_path=arguments["pdf_path"]
            )
        if tool_name == "create_pdf":

            title = arguments["title"]
            content = arguments["content"]
            filename = arguments.get("filename")

            return tool_create_general_pdf(
                title=title,
                content=content,
                filename=filename
            )
        if tool_name == "create_today_schedule_pdf":

            result = tool_create_schedule_pdf()

            return result
        if tool_name == "add_one_shot_task":

            return add_one_shot_task(
                TASK_DB_CONFIG,
                task_name=arguments["task_name"],
                program_path=arguments["program_path"],
                run_date=arguments["run_date"],
                run_time=arguments["run_time"],
                arguments=arguments.get(
                    "arguments"
                ),
                working_directory=arguments.get(
                    "working_directory"
                ),
            )

        elif tool_name == "add_weekly_task":

            return add_weekly_task(
                TASK_DB_CONFIG,
                task_name=arguments["task_name"],
                program_path=arguments["program_path"],
                weekday=arguments["weekday"],
                run_time=arguments["run_time"],
                arguments=arguments.get(
                    "arguments"
                ),
                working_directory=arguments.get(
                    "working_directory"
                ),
            )

        elif tool_name == "list_tasks":

            return list_tasks(
                TASK_DB_CONFIG,
                include_disabled=arguments.get(
                    "include_disabled",
                    False
                ),
            )

        elif tool_name == "get_task":

            return get_task(
                TASK_DB_CONFIG,
                arguments["task_id"]
            )

        elif tool_name == "disable_task":

            return disable_task(
                TASK_DB_CONFIG,
                arguments["task_id"]
            )

        elif tool_name == "enable_task":

            return enable_task(
                TASK_DB_CONFIG,
                arguments["task_id"]
            )

        elif tool_name == "retry_task":

            return retry_task(
                TASK_DB_CONFIG,
                arguments["task_id"]
            )

        elif tool_name == "delete_task":

            return delete_task(
                TASK_DB_CONFIG,
                arguments["task_id"]
            )
        if tool_name == "write_file":
            return write_local_file(
                arguments["path"],
                arguments["content"],
                arguments["overwrite"]
            )
        if tool_name == "send_slack_notification":
            response = notify_slack(
                arguments["message"],
                mode="tool"
            )

            return {
                "success": response,
                "message": arguments["message"]
            }
        if tool_name == "read_file":
            return read_local_file(arguments["path"])

        if tool_name == "run_system":
            if load_exec_conf() != 1:
                return {
                    "success": False,
                    "error": "system_execution_disabled"
                }
            if block_cmd(arguments["command"]):
                return {
                    "success": False,
                    "error": "blocked_command"
                }
            return run_system_command(
                command=arguments["command"],
                timeout=arguments["timeout"],
                max_output=arguments["max_output"]
            )

        if tool_name == "add_schedule":
            schedule_id = add_schedule(
                arguments["title"],
                arguments["message"],
                arguments["scheduled_at"],
            )
            return {
                "success": True,
                "schedule_id": schedule_id,
                "title": arguments["title"],
                "scheduled_at": arguments["scheduled_at"],
            }

        if tool_name == "get_schedules":
            rows = get_schedules(
                include_used=bool(arguments["include_used"])
            )
            return {
                "success": True,
                "schedules": _json_safe_schedule_rows(rows),
            }

        if tool_name == "delete_schedule":
            deleted = delete_schedule(arguments["schedule_id"])
            return {
                "success": deleted,
                "schedule_id": arguments["schedule_id"],
                "deleted": deleted,
            }

        if tool_name == "calendar_add_once":

            schedule_id = (
                add_calendar_once(
                    title=arguments[
                        "title"
                    ],

                    scheduled_date=arguments[
                        "scheduled_date"
                    ],

                    start_time=arguments[
                        "start_time"
                    ],

                    end_time=arguments[
                        "end_time"
                    ],

                    description=arguments[
                        "description"
                    ]
                )
            )

            return {
                "success": True,

                "schedule_type":
                    "once",

                "schedule_id":
                    schedule_id
            }


        if tool_name == "calendar_add_weekly":

            schedule_id = (
                add_calendar_weekly(
                    title=arguments[
                        "title"
                    ],

                    weekdays=arguments[
                        "weekdays"
                    ],

                    start_time=arguments[
                        "start_time"
                    ],

                    end_time=arguments[
                        "end_time"
                    ],

                    description=arguments[
                        "description"
                    ]
                )
            )

            return {
                "success": True,

                "schedule_type":
                    "weekly",

                "schedule_id":
                    schedule_id
            }


        if tool_name == "calendar_get_today":

            result = (
                get_calendar_today()
            )

            return {
                "success": True,
                **result
            }


        if tool_name == "calendar_complete_once":

            completed = (
                complete_calendar_once(
                    arguments[
                        "schedule_id"
                    ]
                )
            )

            return {
                "success":
                    completed,

                "schedule_id":
                    arguments[
                        "schedule_id"
                    ],

                "completed":
                    completed
            }


        if tool_name == "calendar_complete_weekly":

            completed = (
                complete_calendar_weekly(
                    schedule_id=arguments[
                        "schedule_id"
                    ],

                    scheduled_date=arguments[
                        "scheduled_date"
                    ]
                )
            )

            return {
                "success":
                    completed,

                "schedule_id":
                    arguments[
                        "schedule_id"
                    ],

                "completed":
                    completed
            }


        if tool_name == "calendar_import_csv":

            return import_calendar_csv(
                arguments[
                    "path"
                ]
            )

        if tool_name == "calendar_delete_once":

            deleted = (
                delete_calendar_once(
                    arguments[
                        "schedule_id"
                    ]
                )
            )

            return {
                "success":
                    deleted,

                "schedule_type":
                    "once",

                "schedule_id":
                    arguments[
                        "schedule_id"
                    ],

                "deleted":
                    deleted
            }


        if tool_name == "calendar_delete_weekly":

            deleted = (
                delete_calendar_weekly(
                    arguments[
                        "schedule_id"
                    ]
                )
            )

            return {
                "success":
                    deleted,

                "schedule_type":
                    "weekly",

                "schedule_id":
                    arguments[
                        "schedule_id"
                    ],

                "deleted":
                    deleted
            }
        if tool_name == "calendar_get_date":

            result = (
                get_calendar_date(
                    target_date=arguments[
                        "scheduled_date"
                    ],

                    unfinished_only=bool(
                        arguments[
                            "unfinished_only"
                        ]
                    )
                )
            )

            return {
                "success": True,
                **result
            }
        raise ValueError(f"未対応のToolです: {tool_name}")


    def chat_with_openai_responses(messages):
        """
        通常会話用。

        Responses APIを使用し、reasoningとFunction Toolを同時に利用する。
        Tool呼び出しが返された場合はローカルToolを実行し、
        function_call_outputをprevious_response_id付きでモデルへ返す。
        """
        import json

        api_key = load_openai_api_key()

        if not api_key:
            print("")
            print(" OpenAI APIキーが設定されていません。")
            return None

        model = load_model()
        api_url = "https://api.openai.com/v1/responses"
        tools = _schedule_tools()

        # 最初のリクエストでは通常の会話履歴とSystem Promptを送る。
        next_input = list(build_context(messages))
        previous_response_id = None

        # Tool実行後に別のToolが必要になるケースにも対応する。
        # 暴走防止のため最大4ラウンドまで。
        for _ in range(4):
            data = {
                "model": model,
                "input": next_input,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "reasoning": {
                    "effort": load_reasoning_effort()
                },
                "max_output_tokens": load_token()
            }

            if previous_response_id:
                data["previous_response_id"] = previous_response_id

            response = _post_openai(
                api_url,
                _openai_headers(api_key),
                data
            )

            if response is None:
                return None

            if response.status_code != 200:
                return _handle_openai_http_error(response)

            try:
                result = response.json()
                response_id = result.get("id")

                if not response_id:
                    print("")
                    print(" OpenAI Responses APIからresponse idが返されませんでした。")
                    return None

                function_calls = [
                    item
                    for item in result.get("output", [])
                    if item.get("type") == "function_call"
                ]

                # Function Callが無ければ最終テキストを返す。
                if not function_calls:
                    content = _extract_responses_text(result)

                    if not content:
                        print("")
                        print(" OpenAIからテキスト応答が返されませんでした。")
                        return None

                    return content

                tool_outputs = []

                for function_call in function_calls:
                    call_id = function_call.get("call_id")
                    tool_name = function_call.get("name", "")
                    raw_arguments = function_call.get("arguments", "{}")

                    try:
                        arguments = json.loads(raw_arguments)

                        if not isinstance(arguments, dict):
                            raise ValueError(
                                "Tool argumentsがJSONオブジェクトではありません。"
                            )

                        tool_result = execute_avelia_tool(
                            tool_name,
                            arguments
                        )

                        save_execution_history(
                            tool_name,
                            arguments,
                            tool_result
                        )
                    except Exception as e:
                        tool_result = {
                            "success": False,
                            "error": f"{type(e).__name__}: {e}"
                        }

                    if not call_id:
                        print("")
                        print(" Tool Callのcall_idがありません。")
                        return None

                    tool_outputs.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            tool_result,
                            ensure_ascii=False
                        )
                    })

                # Responses APIの状態を引き継ぎ、Tool結果だけを次の入力にする。
                previous_response_id = response_id
                next_input = tool_outputs

            except Exception as e:
                print("")
                print(" OpenAI Responses APIの応答を解析できませんでした。")
                print(f" {e}")
                return None

        print("")
        print(" Tool Callingの最大実行回数に達しました。")
        return None


    def chat_with_openai(messages):
        """
        OpenAI APIへの統合入口。

        通常会話:
            Responses API + DB設定モデル + reasoning + Function Tool

        「検索して」「ネットで調べて」などを含む場合:
            Responses API + gpt-5.6-luna + Web Search
        """

        if should_use_web_search(messages):
            return chat_with_openai_web_search(messages)

        return chat_with_openai_responses(messages)


    # =========================================================
    # メイン処理
    # =========================================================


    BOT_NAME = load_BotName()
    def avelia_core_main(user):
        c=0
        global card_id,real_name,last_login
        card_id = user["card_id"]
        real_name = user["real_name"]
        last_login = user["last_login"]
        try:
            print(pyfiglet.figlet_format("Avelia",font="slant"))
            print(" OpenAI API Hybrid Web Search Edition")
            print(" Anvelk Mainframe AI")
            print("")
            if DVD_MODE == 1:
                print("For DVD Mode")
            print("")
            c=1
        except:
            pass
    
        if c==0:
            print("")
            print(" ========================================")
            print("              Avelia")
            print("          OpenAI API Hybrid Web Search Edition")
            print(" ========================================")
        print("")

        model = load_model()

        print(f" 使用モデル : {model}")
        print("")

        # -----------------------------------------------------
        # OpenAI API設定確認
        # -----------------------------------------------------

        print(" OpenAI API設定を確認しています...")

        if not check_openai():

            print("")
            print(" OpenAI APIキーが設定されていません。")
            print("")
            print(" settingsテーブル DEFAULT セクションの api_key を設定してから再起動してください。")
            print("")

            input(" >> ")
            sys.exit()

        print(" OpenAI API設定: OK")
        print("")


        # -----------------------------------------------------
        # Dropbox記憶同期
        # -----------------------------------------------------

        if load_preload() == 1:
            sync_memory()

        # -----------------------------------------------------
        # 会話履歴
        # -----------------------------------------------------

        if load_preload() == 1:

            messages = load_chat()

        else:

            messages = new_chat()
            print(" 記憶機能は無効です。")


        # -----------------------------------------------------
        # 起動後処理
        # -----------------------------------------------------

        # systemd + TCP 常駐モードではサーバー側の clear は
        # クライアント端末に反映されないため、起動時クリアは行わない。
        os.chdir(Path.home())

        # -----------------------------------------------------
        # チャットループ
        # -----------------------------------------------------

        while True:

            try:

                user_input = input("\n あなた: ").strip()

            except EOFError:

                # TCPクライアントが切断された。
                try:
                    End_session(process_uuid)
                except Exception:
                    pass
                break

            except KeyboardInterrupt:

                print("")
                print("")
                print(" 会話を終了します。")

                break
            except Exception as e:
                print(f"入力エラー:{type(e).__name__}:{e}")
                traceback.print_exc()
                continue


            bad_chars = ["�", "□"]

            if any(bad in user_input for bad in bad_chars):
                for bad in bad_chars:
                    user_input = user_input.replace(bad, "")
                user_input += "\n[Software Warning:ユーザーの入力時に文字化けが発生しました。一部の文字が欠損しています]"

            # -------------------------------------------------
            # 空入力
            # -------------------------------------------------

            if not user_input:
                continue


            # -------------------------------------------------
            # 終了
            # -------------------------------------------------

            if user_input.lower() == "exit":

                print("")
                print(" 会話を終了します。")
                End_session(process_uuid)
                break


            # -------------------------------------------------
            # 履歴削除
            # -------------------------------------------------

            if user_input.lower() == "clear":

                messages = new_chat()

                # 空の会話履歴を保存してDropbox側にも反映する。
                # ローカルだけ削除すると次回起動時にDropboxから
                # 古い履歴が復元されてしまうため。
                save_chat(messages)

                print("")
                print(" 会話履歴を削除しました。")

                continue


            # -------------------------------------------------
            # 履歴表示
            # -------------------------------------------------

            if user_input.lower() == "history":

                print("")
                print(" ===== 会話履歴 =====")

                for message in messages:

                    role = message.get("role", "")
                    content = message.get("content", "")

                    if role == "system":
                        continue

                    if role == "user":
                        print("")
                        print(f" あなた: {content}")

                    elif role == "assistant":
                        print("")
                        console.print(f" {BOT_NAME}:")
                        console.print(Markdown(str(content)))

                print("")
                print(" ====================")

                continue


            # -------------------------------------------------
            # モデル情報
            # -------------------------------------------------

            if user_input.lower() == "model":

                print("")
                print(
                    f" 使用中モデル: {load_model()}"
                )

                continue


            # -------------------------------------------------
            # ユーザーメッセージ追加
            # -------------------------------------------------

            messages.append(
                {
                    "role": "user",
                    "content": user_input
                }
            )


            # -------------------------------------------------
            # OpenAI API呼び出し
            # -------------------------------------------------

            print("")
            print(" 考え中...")

            write_log(messages) # 会話履歴の生データを保存

            response = chat_with_openai(messages)

            if response is None:

                # APIエラー時はユーザー入力を履歴から外す
                if (
                    len(messages) > 0
                    and messages[-1]["role"] == "user"
                ):
                    messages.pop()

                continue


            # -------------------------------------------------
            # AI応答を履歴へ追加
            # -------------------------------------------------

            messages.append(
                {
                    "role": "assistant",
                    "content": response
                }
            )
        
            # -------------------------------------------------
            # 会話履歴保存
            # -------------------------------------------------

            save_chat(messages)
        
            if learning_enabled():

                try:
                    learn_from_conversation(
                    user_input,
                    response,
                    learning_openai
                    )
      
                except Exception:
                    # 自動学習が壊れても会話本体は止めない
                    pass

            # -------------------------------------------------
            # 表示
            # -------------------------------------------------

            display_response = response.replace(
                "。",
                "。\n "
            )
        

            print("")
            console.print(f" {BOT_NAME}:")
            console.print(Markdown(display_response))

            if VOICE_ENABLD == 1 and engine is not None:
                engine.say(display_response)
                engine.runAndWait()



    

# =========================================================
# Avelia one-file runtime dispatch
# =========================================================

if _AVELIA_MODE == "daemon":
    _run_daemon(_ARGS.host, _ARGS.port)

elif _AVELIA_MODE == "connect":
    _run_client(_ARGS.host, _ARGS.port)

elif _AVELIA_MODE == "google_calendar_auth":
    _configure_stdio()
    _run_google_calendar_auth()
