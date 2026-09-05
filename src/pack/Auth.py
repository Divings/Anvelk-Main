from ctypes import CDLL, c_int, c_char_p
import json
import os
import sys

import configparser
import pwd
import mysql.connector

DATABASE_CONF = "/opt/Anvelk-Mainframe/config/database.conf"


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
