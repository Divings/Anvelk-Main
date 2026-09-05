# -*- coding: utf-8 -*-

import os
import time
import hashlib
import configparser
from pathlib import Path

import mysql.connector
import requests
from dotenv import load_dotenv


# ============================================================
# 基本設定
# ============================================================

ANVELK_DB_CONF = Path(
    "/opt/Anvelk-Mainframe/config/database.conf"
)

HASH_FILE = Path(
    "/var/log/Anvelk-Mainframe/notification_hash.txt"
)

LOG_FILE = Path(
    "/var/log/Anvelk-Mainframe/notification_log.txt"
)

NOTIFY_COOLDOWN_SECONDS = 60


# .env 読み込み
# Telegramを使う場合に使用
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# DB設定読み込み
# ============================================================

def load_database_config(
    path: Path = ANVELK_DB_CONF
) -> dict:
    """
    /opt/Anvelk-Mainframe/config/database.conf
    からMariaDB接続設定を読み込む。

    例:

    [database]
    host = localhost
    port = 3306
    user = mainframe
    password = password
    database = mainframe
    """

    if not path.exists():
        raise RuntimeError(
            f"DB設定ファイルが見つかりません: {path}"
        )

    config = configparser.ConfigParser(
        interpolation=None
    )

    loaded = config.read(
        path,
        encoding="utf-8"
    )

    if not loaded:
        raise RuntimeError(
            f"DB設定ファイルを読み込めません: {path}"
        )

    section_name = None

    for candidate in (
        "database",
        "DB",
        "mysql",
        "mariadb"
    ):
        if config.has_section(candidate):
            section_name = candidate
            break

    if section_name is None:
        raise RuntimeError(
            "database.conf に "
            "[database] セクションがありません。"
        )

    section = config[section_name]

    host = section.get(
        "host",
        fallback="127.0.0.1"
    )

    port = section.getint(
        "port",
        fallback=3306
    )

    user = section.get(
        "user",
        fallback=None
    )

    password = section.get(
        "password",
        fallback=""
    )

    database = section.get(
        "database",
        fallback=None
    )

    if not user:
        raise RuntimeError(
            "database.conf に user がありません。"
        )

    if not database:
        raise RuntimeError(
            "database.conf に database がありません。"
        )

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database
    }


# ============================================================
# DB接続
# ============================================================

def get_database_connection():
    """
    mysql.connector を使って Anvelk Mainframe のMariaDBへ接続する。
    """

    config = load_database_config()

    try:
        connection = mysql.connector.connect(
            host=config["host"],
            port=config["port"],
            user=config["user"],
            password=config["password"],
            database=config["database"]
        )

        connection.autocommit = True
        return connection

    except mysql.connector.Error as e:
        raise RuntimeError(
            f"データベース接続に失敗しました: {e}"
        ) from e


# ============================================================
# settings テーブル読み込み
# ============================================================

def get_setting(
    section_name: str,
    setting_key: str
) -> str:
    """
    settings テーブルから setting_value を取得。

    カラム構成:

    section_name
    setting_key
    setting_value
    """

    connection = get_database_connection()

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT setting_value
            FROM settings
            WHERE section_name = %s
              AND setting_key = %s
            LIMIT 1
            """,
            (
                section_name,
                setting_key
            )
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "設定が見つかりません: "
                f"{section_name}/{setting_key}"
            )

        value = row[0]

        if value is None:
            raise RuntimeError(
                "設定値が空です: "
                f"{section_name}/{setting_key}"
            )

        return str(value).strip()

    finally:
        connection.close()


# ============================================================
# Slack設定
# ============================================================

def get_slack_webhook_url() -> str:
    """
    DBからSlack Webhook URLを取得する。

    section_name = SLACK
    setting_key  = webhook_url

    setting_value は平文でそのまま使用。
    復号化処理は行わない。
    """

    return get_setting(
        "SLACK",
        "webhook_url"
    )


# ============================================================
# Mainframe通知設定
# ============================================================

def get_default_service() -> str:
    """
    通知先をDBから取得。

    設定が無ければSlackを使用。

    将来的に、

    section_name = NOTIFICATION
    setting_key  = default_service

    として slack / telegram を切り替え可能。
    """

    try:
        return get_setting(
            "NOTIFICATION",
            "default_service"
        ).lower()

    except Exception:
        return "slack"


def get_debug_mode() -> bool:
    """
    DebugモードをDBから取得。

    設定が無ければFalse。
    """

    try:
        value = get_setting(
            "NOTIFICATION",
            "debug"
        )

        return value.lower() in (
            "1",
            "true",
            "yes",
            "on"
        )

    except Exception:
        return False


# ============================================================
# 通知状態
# ============================================================

_last_notify_times = {}

msg_history = None
# ============================================================
# ログディレクトリ
# ============================================================

def _prepare_log_directory():
    """
    通知ログ用ディレクトリを作成。
    """

    try:
        LOG_FILE.parent.mkdir(
            parents=True,
            exist_ok=True
        )

    except Exception:
        pass


#_prepare_log_directory()




# ============================================================
# Slack メッセージ色
# ============================================================

def _message_color_for_slack(
    message: str
) -> str:

    if (
        "[タイムアウト]" in message
        or "[エラー]" in message
        or "[⚠️アラート]" in message
    ):
        return "#ff4d4d"

    if (
        "[完了]" in message
        or "[終了]" in message
    ):
        return "#36a64f"

    if "[INFO]" in message:
        return "#888888"

    return "#dddddd"


# ============================================================
# Slack通知
# ============================================================

def _notify_slack_impl(
    message: str
):
    """
    Slack Incoming Webhookへ通知。

    Webhook URLは毎回DBから取得するため、
    DB側でURLを変更してもプロセス再起動不要。
    """

    webhook_url = get_slack_webhook_url()

    if not webhook_url:
        raise ValueError(
            "Slack Webhook URLが設定されていません。"
        )

    color = _message_color_for_slack(
        message
    )

    payload = {
        "attachments": [
            {
                "color": color,
                "text": message
            }
        ]
    }

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=10
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Slack通知に失敗しました: "
            f"{response.status_code} "
            f"{response.text}"
        )


# ============================================================
# .env 更新
# ============================================================

def _append_env_if_needed(
    key: str,
    value: str,
    env_path: str = ".env"
):

    try:
        path = Path(env_path)

        if not path.exists():
            path.write_text(
                f"{key}={value}\n",
                encoding="utf-8"
            )
            return

        lines = path.read_text(
            encoding="utf-8"
        ).splitlines()

        updated = False

        for index, line in enumerate(lines):

            if line.strip().startswith(
                f"{key}="
            ):
                lines[index] = (
                    f"{key}={value}"
                )

                updated = True
                break

        if not updated:
            lines.append(
                f"{key}={value}"
            )

        path.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8"
        )

    except Exception as e:
        print(
            "[ENV書き込み警告] "
            f"{key} の保存に失敗: {e}"
        )


# ============================================================
# Telegram chat_id
# ============================================================

def _get_telegram_chat_id() -> str:

    global TELEGRAM_CHAT_ID

    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID

    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_TOKENが設定されていません。"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/getUpdates"
    )

    response = requests.get(
        url,
        timeout=10
    )

    try:
        data = response.json()

    except Exception as e:
        raise RuntimeError(
            "Telegram chat_id の取得に失敗しました。"
        ) from e

    results = data.get(
        "result",
        []
    )

    if not results:
        raise RuntimeError(
            "Telegram Botにメッセージがありません。"
        )

    last = results[-1]

    chat = None

    if (
        "message" in last
        and "chat" in last["message"]
    ):
        chat = last["message"]["chat"]

    elif (
        "channel_post" in last
        and "chat" in last["channel_post"]
    ):
        chat = last["channel_post"]["chat"]

    elif (
        "edited_message" in last
        and "chat" in last["edited_message"]
    ):
        chat = last["edited_message"]["chat"]

    if not chat or "id" not in chat:
        raise RuntimeError(
            "Telegram chat.id が取得できません。"
        )

    TELEGRAM_CHAT_ID = str(
        chat["id"]
    )

    _append_env_if_needed(
        "TELEGRAM_CHAT_ID",
        TELEGRAM_CHAT_ID
    )

    return TELEGRAM_CHAT_ID


# ============================================================
# Telegram通知
# ============================================================

def _notify_telegram_impl(
    message: str
):

    if not TELEGRAM_TOKEN:
        raise ValueError(
            "TELEGRAM_TOKENが設定されていません。"
        )

    chat_id = _get_telegram_chat_id()

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": chat_id,
        "text": message
    }

    response = requests.post(
        url,
        data=payload,
        timeout=10
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Telegram通知に失敗しました: "
            f"{response.status_code} "
            f"{response.text}"
        )


# ============================================================
# 通知エントリポイント
# ============================================================

def notify_slack(
    message: str,
    mode=None
):
    """
    通知用エントリポイント。

    既存コードとの互換性のため
    関数名 notify_slack を維持。

    default_service が telegram の場合はTelegram、
    それ以外はSlackへ送信。

    mode が指定された場合は
    重複通知抑止を無効化。
    """
    global msg_history

    # API連続アクセス対策
    time.sleep(1.2)

    now = time.time()

    message_hash = hashlib.sha256(
        message.encode(
            "utf-8"
        )
    ).hexdigest()

    # --------------------------------------------------------
    # Debug
    # --------------------------------------------------------

    debug = get_debug_mode()

    if debug:
        send_text = (
            "[Debug モード] "
            + message
        )

    else:
        send_text = message

    # --------------------------------------------------------
    # 通知
    # --------------------------------------------------------

    try:

        default_service = (
            get_default_service()
        )

        if (
            default_service
            == "telegram"
        ):
            _notify_telegram_impl(
                send_text
            )

        else:
            _notify_slack_impl(
                send_text
            )

        # 通知成功後にハッシュ保存
        msg_history = message_hash
        return True
    except Exception as e:
        print(f"[通知例外] {e}")
        return False


# ============================================================
# 単体テスト
# ============================================================

if __name__ == "__main__":

    notify_slack(
        "[INFO] Anvelk Mainframe "
        "Slack通知テスト"
    )