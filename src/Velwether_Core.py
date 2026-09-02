#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2026 Anvelk Innovations
# Licensed under the GPL v3.0 License.
# See LICENSE for details.
import requests
import os
import sys
import configparser
import shutil
import pyfiglet
import traceback
import time
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
from pack.keyword_learning import (
    learn_from_conversation,
    learning_enabled
)

from pack.knowledge import (
    init_knowledge_table,
    get_knowledge_context
)

import uuid

# UUID生成
process_uuid = str(uuid.uuid4())

if Check_previous_session() == 1:
    sys_msg="前回のセッションが正常に閉じられませんでした。"
else:
    sys_msg=""


Create_session(process_uuid)
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
# DB接続情報は config/database.conf から取得する。
#
# [DATABASE]
# host = localhost
# port = 3306
# user = Dail
# password = xxxxxxxx
# database = dail
DATABASE_CONFIG_FILE = os.path.join("config", "database.conf")




def load_database_config():
    """config/database.conf からMySQL接続情報を読み込む。"""
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

os.system("clear")



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
CONFIG_DIR = "config"
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "Dail"
LOG_DIR ="logs"

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

MAX_MEMORY = load_memory_conf()
pre_clear = load_pre_clear()

os.makedirs(CONFIG_DIR, exist_ok=True)
if DVD_MODE==0:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.isfile(DATA_DIR / "Sys_Prompt.txt"):

    shutil.copyfile(
        "/opt/Anvelk-Mainframe/config/Sys_Prompt_Template.txt",
        os.path.join(DATA_DIR, "Sys_Prompt.txt")
    )
    SYSTEM_PROMPT_FILE = DATA_DIR / "Sys_Prompt.txt"
else:
    SYSTEM_PROMPT_FILE = DATA_DIR / "Sys_Prompt.txt"

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
    global sys_msg
    bot_name = load_BotName()

    try:
        with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
            base_prompt = f.read().strip()
        
        if not base_prompt:
            base_prompt = "あなたは自然な日本語を話すAIアシスタントです。"

    except FileNotFoundError:
        base_prompt = "あなたは自然な日本語を話すAIアシスタントです。"
    current_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if sys_msg!="":
        session_msg=sys_msg
        sys_msg=""
    else:
        session_msg="現在、セッションエラーはありません"
    return (
        f"あなたの名前は「{bot_name}」です。"
        f"ユーザーはあなたを「{bot_name}」として扱います。"
        f"自分自身について話すときも、その名前と人格設定を維持してください。"
        f"{base_prompt}"
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


def chat_with_openai_chat_completions(messages):
    """
    通常会話用。

    従来通り Chat Completions APIを使い、
    DBの DEFAULT.model に設定されたモデルを使用する。
    """

    api_key = load_openai_api_key()

    if not api_key:
        print("")
        print(" OpenAI APIキーが設定されていません。")
        return None

    model = load_model()
    api_url = "https://api.openai.com/v1/chat/completions"
    context_messages = build_context(messages)

    data = {
        "model": model,
        "messages": context_messages,
        "max_completion_tokens": load_token()
        # "temperature": 0.8
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
        choices = result.get("choices", [])

        if not choices:
            print("")
            print(" OpenAIから応答候補が返されませんでした。")
            return None

        message = choices[0].get("message", {})
        content = message.get("content")

        if not content:
            print("")
            print(" OpenAIからテキスト応答が返されませんでした。")
            return None

        return content

    except Exception as e:
        print("")
        print(" OpenAI Chat Completions APIの応答を解析できませんでした。")
        print(f" {e}")
        return None


def chat_with_openai(messages):
    """
    OpenAI APIへの統合入口。

    通常会話:
        Chat Completions API + DB設定モデル

    「検索して」「ネットで調べて」などを含む場合:
        Responses API + gpt-5.6-luna + Web Search
    """

    if should_use_web_search(messages):
        return chat_with_openai_web_search(messages)

    return chat_with_openai_chat_completions(messages)


# =========================================================
# メイン処理
# =========================================================

BOT_NAME = load_BotName()
def main():
    c=0

    try:
        print(pyfiglet.figlet_format("Avelia",font="slant"))
        print("OpenAI API Hybrid Web Search Edition")
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
    # 操作説明
    # -----------------------------------------------------

    print("")
    print(f" {BOT_NAME}に話しかけてみてください！")
    print("")
    print(" 終了       : exit")
    print(" 履歴削除   : clear")
    print(" 履歴表示   : history")
    print(" モデル表示 : model")
    print("")
    if load_preload() == 1:
        print(
            " 会話履歴は memory.vlm に保存され、Dropbox同期が有効な場合はクラウドにも保存されます。"
        )
    else:
        print(
            " 記憶機能は無効です。この会話は終了時に保存されません。"
        )
    print("")

    if pre_clear:
        # 起動時に画面クリアする場合は少し待機してからクリア(その後簡単なメニュー表示)
        print("2秒後に画面をクリアします...")
        time.sleep(2)
        os.system("clear")
        try:
            print(pyfiglet.figlet_format("Velwether",font="slant"))
            print("OpenAI API Hybrid Web Search Edition")
            if DVD_MODE == 1:
                print("For DVD Mode")
            c=1
        except:
            pass
        print("")
        print(f" {BOT_NAME}に話しかけてみてください！")
        print("")

    # -----------------------------------------------------
    # チャットループ
    # -----------------------------------------------------

    while True:

        try:

            user_input = input("\n あなた: ").strip()

        except KeyboardInterrupt:

            print("")
            print("")
            print(" 会話を終了します。")

            break
        except Exception as e:
            print(f"入力エラー:{type(e).__name__}:{e}")
            traceback.print_exc()


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
                    print(f" {BOT_NAME}: {content}")

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
        
        display_response = display_response.replace(
                    "**",
                    ""
                )

        print("")
        print(
            f" {BOT_NAME}: {display_response}"
        )
        if VOICE_ENABLD == 1 and engine is not None:
            engine.say(display_response)
            engine.runAndWait()



    
