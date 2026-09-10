from ctypes import CDLL, c_int, c_char_p
import json
import os
import sys

import configparser
import pwd
import mysql.connector

# アヴェリア用のセキュリティコード
# 改変された場合も、アヴェリアのアップデート時に上書きされるので、セキュリティ上の問題はない。
# ライブラリが書き換えられたら、アップデートによる上書き対象ではないため、セキュリティ上の問題がある。
# が、そうしないとテスト環境でのテストができないので、仕方ない。

DATABASE_CONF = "/opt/Anvelk-Mainframe/config/database.conf"

def check_library_exists():
    if not os.path.exists("/usr/lib64/libanv_core.so"):
        raise RuntimeError(
            "libanv_core.so が見つかりません。"
        )

check_library_exists()

def load_database_config():
    config = configparser.ConfigParser()
    config.read(DATABASE_CONF, encoding="utf-8")

    if "DATABASE" not in config:
        raise RuntimeError(
            "database.conf に [DATABASE] がありません。"
        )

    section = config["DATABASE"]

    return {
        "host": section.get("host", "127.0.0.1"),
        "port": section.getint("port", 3306),
        "user": section["user"],
        "password": section.get("password", ""),
        "database": section["database"],
    }


def get_database_connection():
    db = load_database_config()

    return mysql.connector.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )


def ensure_allow_users_table():
    """
    allow_users テーブルが無ければ作成する。
    """

    connection = None
    cursor = None

    try:
        connection = get_database_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS allow_users (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                username VARCHAR(64) NOT NULL,
                enabled TINYINT(1) NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (id),
                UNIQUE KEY uq_allow_users_username (username)
            )
            """
        )

        connection.commit()

    finally:
        if cursor is not None:
            cursor.close()

        if connection is not None:
            connection.close()


def get_current_username():
    return pwd.getpwuid(os.geteuid()).pw_name


def is_allowed_user():
    """
    現在のLinuxユーザーがallow_usersに登録されているか確認。
    """

    ensure_allow_users_table()

    username = get_current_username()

    connection = None
    cursor = None

    try:
        connection = get_database_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT 1
            FROM allow_users
            WHERE username = %s
              AND enabled = 1
            LIMIT 1
            """,
            (username,)
        )

        return cursor.fetchone() is not None

    finally:
        if cursor is not None:
            cursor.close()

        if connection is not None:
            connection.close()

def authorize_user():
    """
    起動ユーザーを判定する。
    許可されていなければFalseを返す。
    """

    username = get_current_username()

    try:
        allowed = is_allowed_user()

    except Exception as e:
        return {
            "ok": False,
            "reason": "user_authorization_failed",
            "username": username,
            "error": str(e),
        }

    if not allowed:
        return {
            "ok": False,
            "reason": "user_not_allowed",
            "username": username,
        }

    return {
        "ok": True,
        "reason": None,
        "username": username,
    }

# ====== runtime guard ======
LIB_PATH = os.path.join("/usr/lib64", "libanv_core.so")
EXPECTED_VERSION = "2.8.0"

lib = CDLL(LIB_PATH)

lib.anv_is_allowed_runtime.restype = c_int
lib.anv_get_last_reason.restype = c_char_p
lib.anv_get_last_details_json.restype = c_char_p

# 追加: バージョン取得API
lib.anv_get_version_major.restype = c_int
lib.anv_get_version_minor.restype = c_int
lib.anv_get_version_patch.restype = c_int
lib.anv_get_version_hex.restype = c_int
lib.anv_get_version_string.restype = c_char_p


def get_library_version():
    version_str = lib.anv_get_version_string().decode("utf-8", errors="replace")
    major = lib.anv_get_version_major()
    minor = lib.anv_get_version_minor()
    patch = lib.anv_get_version_patch()
    version_hex = lib.anv_get_version_hex()

    return {
        "string": version_str,
        "major": major,
        "minor": minor,
        "patch": patch,
        "hex": version_hex,
    }


def authorize_environment():
    allowed = bool(lib.anv_is_allowed_runtime())
    reason = lib.anv_get_last_reason().decode("utf-8", errors="replace")
    details_raw = lib.anv_get_last_details_json().decode("utf-8", errors="replace")

    try:
        details = json.loads(details_raw)
    except Exception:
        details = {"raw": details_raw}

    version_info = get_library_version()

    # runtime 不許可
    if not allowed:
        return {
            "ok": False,
            "reason": reason,
            "details": details,
            "version": version_info,
        }

    # バージョン不一致
    if version_info["string"] != EXPECTED_VERSION:
        return {
            "ok": False,
            "reason": "library_version_mismatch",
            "details": details,
            "version": version_info,
            "expected_version": EXPECTED_VERSION,
        }

    return {
        "ok": True,
        "reason": None,
        "details": details,
        "version": version_info,
    }




# バーコード認証
import hashlib
import mysql.connector

SETTING_SECTION = "AUTH_CARD"
SETTING_ENABLED_KEY = "enabled"


# ============================================================
# Database
# ============================================================


def connect_database():
    """
    DB接続を作成する。
    """

    return get_database_connection()


# ============================================================
# SHA-256
# ============================================================

def hash_barcode(barcode_value: str) -> str:
    """
    バーコード値をSHA-256化する。

    改行やバーコードリーダー由来の末尾空白を除去してから
    SHA-256へ変換する。
    """

    if barcode_value is None:
        raise ValueError(
            "バーコード値がNoneです。"
        )

    normalized = str(
        barcode_value
    ).strip()

    if not normalized:
        raise ValueError(
            "バーコード値が空です。"
        )

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# ============================================================
# Initialization
# ============================================================

def initialize_auth_card_system():
    """
    認証カードシステムを初期化する。

    ・auth_cards テーブルが無ければ作成
    ・settings に AUTH_CARD / enabled が無ければ追加
    ・enabled の初期値は 0
    ・既存設定は変更しない
    """

    conn = connect_database()
    cursor = conn.cursor()

    try:

        # ----------------------------------------------------
        # 認証カードテーブル
        # ----------------------------------------------------

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_cards (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

                barcode_hash CHAR(64) NOT NULL,

                card_name VARCHAR(255) DEFAULT NULL,

                enabled TINYINT(1) NOT NULL DEFAULT 1,

                created_at DATETIME NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                updated_at DATETIME NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

                PRIMARY KEY (id),

                UNIQUE KEY uq_auth_cards_barcode_hash (
                    barcode_hash
                )

            ) ENGINE=InnoDB
              DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_unicode_ci
            """
        )

        # ----------------------------------------------------
        # settings 初期値
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT 1
            FROM settings
            WHERE section_name = %s
              AND setting_key = %s
            LIMIT 1
            """,
            (
                SETTING_SECTION,
                SETTING_ENABLED_KEY,
            )
        )

        setting_exists = cursor.fetchone()

        if setting_exists is None:

            cursor.execute(
                """
                INSERT INTO settings (
                    section_name,
                    setting_key,
                    setting_value
                )
                VALUES (
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    SETTING_SECTION,
                    SETTING_ENABLED_KEY,
                    "0",
                )
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


# ============================================================
# Settings
# ============================================================

def get_setting_value(
    section_name: str,
    setting_key: str,
    default=None
):
    """
    settings テーブルから設定値を取得する。

    設定が存在しない場合は default を返す。
    """

    conn = connect_database()
    cursor = conn.cursor()

    try:

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
                setting_key,
            )
        )

        row = cursor.fetchone()

        if row is None:
            return default

        return row[0]

    finally:
        cursor.close()
        conn.close()


def is_setting_enabled(
    section_name: str,
    setting_key: str,
    default: bool = False
) -> bool:
    """
    settings の値を有効/無効として判定する。

    True:
        1
        true
        yes
        on
        enabled

    False:
        0
        false
        no
        off
        disabled

    不明な値または設定なしの場合は default。
    """

    value = get_setting_value(
        section_name,
        setting_key,
        None
    )

    if value is None:
        return default

    normalized = str(
        value
    ).strip().lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }:
        return False

    return default


def is_auth_card_system_enabled() -> bool:
    """
    認証カードシステムが有効か判定する。
    """

    return is_setting_enabled(
        SETTING_SECTION,
        SETTING_ENABLED_KEY,
        default=False
    )


# ============================================================
# Card lookup / Authentication
# ============================================================

def get_auth_card(
    barcode_value: str
):
    """
    入力されたバーコードをSHA-256化して、
    auth_cards の有効カードと照合する。

    一致した場合:
        {
            "id": ...,
            "card_name": ...,
            "enabled": ...
        }

    一致しない場合:
        None
    """

    try:
        barcode_hash = hash_barcode(
            barcode_value
        )

    except ValueError:
        return None

    conn = connect_database()

    cursor = conn.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            """
            SELECT
                id,
                card_name,
                enabled
            FROM auth_cards
            WHERE barcode_hash = %s
              AND enabled = 1
            LIMIT 1
            """,
            (
                barcode_hash,
            )
        )

        return cursor.fetchone()

    finally:
        cursor.close()
        conn.close()


def authenticate_card(
    barcode_value: str
):
    """
    認証カードによる認証を行う。

    システム無効:
        (False, None)

    未登録・無効カード:
        (False, None)

    認証成功:
        (True, card_data)
    """

    if not is_auth_card_system_enabled():
        return False, None

    card = get_auth_card(
        barcode_value
    )

    if card is None:
        return False, None

    return True, card


# ============================================================
# Card registration
# ============================================================

def register_card(
    barcode_value: str,
    card_name: str | None = None
):
    """
    カードを登録する。

    DBには平文バーコードを保存せず、
    SHA-256ハッシュのみ保存する。
    """

    barcode_hash = hash_barcode(
        barcode_value
    )

    conn = connect_database()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO auth_cards (
                barcode_hash,
                card_name,
                enabled
            )
            VALUES (
                %s,
                %s,
                1
            )
            """,
            (
                barcode_hash,
                card_name,
            )
        )

        conn.commit()

        return cursor.lastrowid

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


# ============================================================
# Card enable / disable
# ============================================================

def disable_card(
    barcode_value: str
) -> bool:
    """
    カードを無効化する。

    成功:
        True

    対象カードなし:
        False
    """

    barcode_hash = hash_barcode(
        barcode_value
    )

    conn = connect_database()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE auth_cards
            SET enabled = 0
            WHERE barcode_hash = %s
              AND enabled <> 0
            """,
            (
                barcode_hash,
            )
        )

        changed = (
            cursor.rowcount > 0
        )

        conn.commit()

        return changed

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


def enable_card(
    barcode_value: str
) -> bool:
    """
    カードを再有効化する。

    成功:
        True

    対象カードなし:
        False
    """

    barcode_hash = hash_barcode(
        barcode_value
    )

    conn = connect_database()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE auth_cards
            SET enabled = 1
            WHERE barcode_hash = %s
              AND enabled <> 1
            """,
            (
                barcode_hash,
            )
        )

        changed = (
            cursor.rowcount > 0
        )

        conn.commit()

        return changed

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


# ============================================================
# Auth card system setting
# ============================================================

def set_auth_card_system_enabled(
    enabled: bool
):
    """
    認証カードシステム全体を有効・無効にする。
    """

    value = "1" if enabled else "0"

    conn = connect_database()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE settings
            SET setting_value = %s
            WHERE section_name = %s
              AND setting_key = %s
            """,
            (
                value,
                SETTING_SECTION,
                SETTING_ENABLED_KEY,
            )
        )

        if cursor.rowcount == 0:

            cursor.execute(
                """
                INSERT INTO settings (
                    section_name,
                    setting_key,
                    setting_value
                )
                VALUES (
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    SETTING_SECTION,
                    SETTING_ENABLED_KEY,
                    value,
                )
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


initialize_auth_card_system()

def BarcodeAuthGuard():
    """
    認証カードシステムが有効な場合、起動時に認証カードの入力を要求する。
    """

    # 認証カード機能が有効な場合のみ実行
    if is_auth_card_system_enabled():

        barcode = input(
            "認証カードを読み取ってください >> "
        ).strip()

        success, card = authenticate_card(
            barcode
        )

        return success
    else:
        return None