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
from pack.keyword_learning import (
    learn_from_conversation,
    learning_enabled
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


# =========================================================
# スケジュール管理
# =========================================================

def init_schedule_table():
    """schedulesテーブルが存在しない場合は作成する。"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                scheduled_at DATETIME NOT NULL,
                message_use TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                INDEX idx_schedule_due (message_use, scheduled_at)
            ) ENGINE=InnoDB
              DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_unicode_ci
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _normalize_schedule_datetime(scheduled_at):
    """スケジュール日時をdatetimeへ正規化する。"""
    from datetime import datetime

    if isinstance(scheduled_at, datetime):
        return scheduled_at

    if not isinstance(scheduled_at, str):
        raise ValueError("scheduled_at は日時文字列で指定してください。")

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(scheduled_at.strip(), fmt)
        except ValueError:
            pass

    raise ValueError(
        "日時形式が不正です。YYYY-MM-DD HH:MM または "
        "YYYY-MM-DD HH:MM:SS を使用してください。"
    )


def add_schedule(title, message, scheduled_at):
    """新しい通知予定を登録し、登録IDを返す。"""
    title = str(title).strip()
    message = str(message).strip()
    scheduled_at = _normalize_schedule_datetime(scheduled_at)

    if not title:
        raise ValueError("スケジュールタイトルが空です。")
    if not message:
        raise ValueError("通知メッセージが空です。")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO schedules
                (title, message, scheduled_at, message_use)
            VALUES
                (%s, %s, %s, 0)
            """,
            (title, message, scheduled_at),
        )
        schedule_id = cursor.lastrowid
        conn.commit()
        return schedule_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_schedules(include_used=False):
    """予定一覧を取得する。include_used=Falseなら未通知のみ。"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if include_used:
            cursor.execute(
                """
                SELECT id, title, message, scheduled_at,
                       message_use, created_at
                FROM schedules
                ORDER BY scheduled_at ASC, id ASC
                """
            )
        else:
            cursor.execute(
                """
                SELECT id, title, message, scheduled_at,
                       message_use, created_at
                FROM schedules
                WHERE message_use = 0
                ORDER BY scheduled_at ASC, id ASC
                """
            )
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def delete_schedule(schedule_id):
    """IDを指定して予定を削除する。存在した場合True。"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "DELETE FROM schedules WHERE id = %s",
            (int(schedule_id),),
        )
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
def delete_calendar_once(schedule_id):
    """単発予定を削除する。"""

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM calendar_once
            WHERE id = %s
            """,
            (
                int(schedule_id),
            )
        )

        deleted = (
            cursor.rowcount > 0
        )

        conn.commit()

        return deleted

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


def delete_calendar_weekly(schedule_id):
    """
    曜日条件付き予定を削除する。

    calendar_weekly_days と
    calendar_weekly_status は
    FOREIGN KEY ON DELETE CASCADE により
    自動削除される。
    """

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM calendar_weekly
            WHERE id = %s
            """,
            (
                int(schedule_id),
            )
        )

        deleted = (
            cursor.rowcount > 0
        )

        conn.commit()

        return deleted

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()



try:
    init_schedule_table()
except Exception as e:
    print("")
    print(" Schedule Databaseを初期化できませんでした。")
    print(f" {e}")

# =========================================================
# 通常予定管理
# =========================================================

def init_calendar_tables():
    """
    通知なしの通常予定を管理するテーブルを作成する。

    calendar_once
        単発予定

    calendar_weekly
        曜日条件付き予定本体

    calendar_weekly_days
        定期予定の曜日

    calendar_weekly_status
        定期予定の日ごとの完了状態
    """

    sql_list = [
        """
        CREATE TABLE IF NOT EXISTS calendar_once (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

            title VARCHAR(255) NOT NULL,
            description TEXT NULL,

            scheduled_date DATE NOT NULL,

            start_time TIME NULL,
            end_time TIME NULL,

            completed TINYINT(1) NOT NULL DEFAULT 0,
            completed_at DATETIME NULL,

            source VARCHAR(32) NOT NULL DEFAULT 'manual',

            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

            updated_at DATETIME NOT NULL
                DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,

            PRIMARY KEY (id),

            INDEX idx_calendar_once_date (
                scheduled_date,
                completed
            )

        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,

        """
        CREATE TABLE IF NOT EXISTS calendar_weekly (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

            title VARCHAR(255) NOT NULL,
            description TEXT NULL,

            start_time TIME NULL,
            end_time TIME NULL,

            enabled TINYINT(1) NOT NULL DEFAULT 1,

            source VARCHAR(32) NOT NULL DEFAULT 'manual',

            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

            updated_at DATETIME NOT NULL
                DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,

            PRIMARY KEY (id)

        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,

        """
        CREATE TABLE IF NOT EXISTS calendar_weekly_days (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

            schedule_id BIGINT UNSIGNED NOT NULL,

            weekday TINYINT UNSIGNED NOT NULL,

            PRIMARY KEY (id),

            UNIQUE KEY uq_calendar_weekly_day (
                schedule_id,
                weekday
            ),

            INDEX idx_calendar_weekday (
                weekday
            ),

            CONSTRAINT fk_calendar_weekly_days
                FOREIGN KEY (schedule_id)
                REFERENCES calendar_weekly(id)
                ON DELETE CASCADE

        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """,

        """
        CREATE TABLE IF NOT EXISTS calendar_weekly_status (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

            schedule_id BIGINT UNSIGNED NOT NULL,

            scheduled_date DATE NOT NULL,

            completed TINYINT(1) NOT NULL DEFAULT 0,

            completed_at DATETIME NULL,

            PRIMARY KEY (id),

            UNIQUE KEY uq_calendar_weekly_status (
                schedule_id,
                scheduled_date
            ),

            INDEX idx_calendar_weekly_status_date (
                scheduled_date,
                completed
            ),

            CONSTRAINT fk_calendar_weekly_status
                FOREIGN KEY (schedule_id)
                REFERENCES calendar_weekly(id)
                ON DELETE CASCADE

        ) ENGINE=InnoDB
          DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    ]

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        for sql in sql_list:
            cursor.execute(sql)

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


def _normalize_calendar_date(value):

    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if not isinstance(value, str):
        raise ValueError(
            "日付はYYYY-MM-DD形式で指定してください。"
        )

    try:

        return datetime.strptime(
            value.strip(),
            "%Y-%m-%d"
        ).date()

    except ValueError as e:

        raise ValueError(
            "日付形式が不正です。YYYY-MM-DDを使用してください。"
        ) from e


def _normalize_calendar_time(value):

    from datetime import (
        time as dt_time,
        datetime
    )

    if value is None or value == "":
        return None

    if isinstance(value, dt_time):
        return value

    if not isinstance(value, str):

        raise ValueError(
            "時刻はHH:MMまたはHH:MM:SS形式で指定してください。"
        )

    value = value.strip()

    if not value:
        return None

    for fmt in (
        "%H:%M:%S",
        "%H:%M"
    ):

        try:

            return datetime.strptime(
                value,
                fmt
            ).time()

        except ValueError:
            pass

    raise ValueError(
        "時刻形式が不正です。"
        "HH:MMまたはHH:MM:SSを使用してください。"
    )


def _validate_calendar_title(title):

    title = str(title).strip()

    if not title:
        raise ValueError(
            "予定タイトルが空です。"
        )

    return title


def _validate_calendar_time_range(
    start_time,
    end_time
):

    start_time = _normalize_calendar_time(
        start_time
    )

    end_time = _normalize_calendar_time(
        end_time
    )

    if (
        start_time is not None
        and end_time is not None
        and end_time < start_time
    ):

        raise ValueError(
            "end_timeはstart_time以降にしてください。"
        )

    return start_time, end_time


def add_calendar_once(
    title,
    scheduled_date,
    start_time=None,
    end_time=None,
    description=None,
    source="avelia"
):

    title = _validate_calendar_title(
        title
    )

    scheduled_date = _normalize_calendar_date(
        scheduled_date
    )

    start_time, end_time = (
        _validate_calendar_time_range(
            start_time,
            end_time
        )
    )

    if description is not None:

        description = (
            str(description).strip()
            or None
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO calendar_once
            (
                title,
                description,
                scheduled_date,
                start_time,
                end_time,
                source
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                title,
                description,
                scheduled_date,
                start_time,
                end_time,
                source
            )
        )

        schedule_id = (
            cursor.lastrowid
        )

        conn.commit()

        return schedule_id

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


def add_calendar_weekly(
    title,
    weekdays,
    start_time=None,
    end_time=None,
    description=None,
    source="avelia"
):

    title = _validate_calendar_title(
        title
    )

    if not isinstance(
        weekdays,
        (list, tuple, set)
    ):

        raise ValueError(
            "weekdaysは配列で指定してください。"
        )

    weekdays = sorted(
        {
            int(day)
            for day in weekdays
        }
    )

    if not weekdays:

        raise ValueError(
            "曜日が指定されていません。"
        )

    for day in weekdays:

        if day < 0 or day > 6:

            raise ValueError(
                "曜日番号は0〜6です。"
            )

    start_time, end_time = (
        _validate_calendar_time_range(
            start_time,
            end_time
        )
    )

    if description is not None:

        description = (
            str(description).strip()
            or None
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO calendar_weekly
            (
                title,
                description,
                start_time,
                end_time,
                enabled,
                source
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                1,
                %s
            )
            """,
            (
                title,
                description,
                start_time,
                end_time,
                source
            )
        )

        schedule_id = (
            cursor.lastrowid
        )

        cursor.executemany(
            """
            INSERT INTO calendar_weekly_days
            (
                schedule_id,
                weekday
            )
            VALUES
            (
                %s,
                %s
            )
            """,
            [
                (
                    schedule_id,
                    day
                )
                for day
                in weekdays
            ]
        )

        conn.commit()

        return schedule_id

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()


def complete_calendar_once(
    schedule_id
):

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE calendar_once

            SET
                completed = 1,
                completed_at = NOW()

            WHERE
                id = %s
                AND completed = 0
            """,
            (
                int(schedule_id),
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


def complete_calendar_weekly(
    schedule_id,
    scheduled_date=None
):

    from datetime import date

    if scheduled_date is None:

        target_date = date.today()

    else:

        target_date = (
            _normalize_calendar_date(
                scheduled_date
            )
        )

    schedule_id = int(
        schedule_id
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            SELECT id

            FROM calendar_weekly

            WHERE
                id = %s
                AND enabled = 1
            """,
            (
                schedule_id,
            )
        )

        if cursor.fetchone() is None:
            return False

        cursor.execute(
            """
            SELECT 1

            FROM calendar_weekly_days

            WHERE
                schedule_id = %s
                AND weekday = %s
            """,
            (
                schedule_id,
                target_date.weekday()
            )
        )

        if cursor.fetchone() is None:

            raise ValueError(
                "指定日はこの定期予定の対象曜日ではありません。"
            )

        cursor.execute(
            """
            INSERT INTO calendar_weekly_status
            (
                schedule_id,
                scheduled_date,
                completed,
                completed_at
            )

            VALUES
            (
                %s,
                %s,
                1,
                NOW()
            )

            ON DUPLICATE KEY UPDATE

                completed = 1,
                completed_at = NOW()
            """,
            (
                schedule_id,
                target_date
            )
        )

        conn.commit()

        return True

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()
        conn.close()

def _calendar_time_to_string(
    value
):

    if value is None:
        return None

    if hasattr(
        value,
        "total_seconds"
    ):

        seconds = int(
            value.total_seconds()
        )

        hours = (
            seconds // 3600
        )

        minutes = (
            seconds % 3600
        ) // 60

        seconds = (
            seconds % 60
        )

        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    if hasattr(
        value,
        "strftime"
    ):

        return value.strftime(
            "%H:%M:%S"
        )

    return str(value)


def get_calendar_date(
    target_date=None,
    unfinished_only=True
):

    from datetime import date

    if target_date is None:

        target = date.today()

    else:

        target = (
            _normalize_calendar_date(
                target_date
            )
        )

    weekday = (
        target.weekday()
    )

    conn = get_db_connection()

    once_cursor = conn.cursor(
        dictionary=True
    )

    weekly_cursor = conn.cursor(
        dictionary=True
    )

    try:

        once_sql = """
            SELECT
                id,
                title,
                description,
                scheduled_date,
                start_time,
                end_time,
                completed,
                completed_at,
                source

            FROM calendar_once

            WHERE scheduled_date = %s
        """

        if unfinished_only:

            once_sql += """
                AND completed = 0
            """

        once_sql += """
            ORDER BY
                start_time IS NULL,
                start_time,
                id
        """

        once_cursor.execute(
            once_sql,
            (
                target,
            )
        )

        once_rows = (
            once_cursor.fetchall()
        )

        weekly_sql = """
            SELECT
                cw.id,
                cw.title,
                cw.description,
                cw.start_time,
                cw.end_time,
                cw.source,

                COALESCE(
                    cws.completed,
                    0
                ) AS completed,

                cws.completed_at

            FROM calendar_weekly AS cw

            INNER JOIN calendar_weekly_days AS cwd

                ON cw.id =
                   cwd.schedule_id

            LEFT JOIN calendar_weekly_status AS cws

                ON cw.id =
                   cws.schedule_id

                AND cws.scheduled_date =
                    %s

            WHERE
                cw.enabled = 1

                AND cwd.weekday = %s
        """

        if unfinished_only:

            weekly_sql += """
                AND COALESCE(
                    cws.completed,
                    0
                ) = 0
            """

        weekly_sql += """
            ORDER BY
                cw.start_time IS NULL,
                cw.start_time,
                cw.id
        """

        weekly_cursor.execute(
            weekly_sql,
            (
                target,
                weekday
            )
        )

        weekly_rows = (
            weekly_cursor.fetchall()
        )

        schedules = []

        for row in once_rows:

            schedules.append(
                {
                    "schedule_type":
                        "once",

                    "id":
                        row["id"],

                    "title":
                        row["title"],

                    "description":
                        row["description"],

                    "date":
                        target.isoformat(),

                    "start_time":
                        _calendar_time_to_string(
                            row["start_time"]
                        ),

                    "end_time":
                        _calendar_time_to_string(
                            row["end_time"]
                        ),

                    "completed":
                        bool(
                            row["completed"]
                        )
                }
            )

        for row in weekly_rows:

            schedules.append(
                {
                    "schedule_type":
                        "weekly",

                    "id":
                        row["id"],

                    "title":
                        row["title"],

                    "description":
                        row["description"],

                    "date":
                        target.isoformat(),

                    "start_time":
                        _calendar_time_to_string(
                            row["start_time"]
                        ),

                    "end_time":
                        _calendar_time_to_string(
                            row["end_time"]
                        ),

                    "completed":
                        bool(
                            row["completed"]
                        )
                }
            )

        schedules.sort(
            key=lambda x: (
                x["start_time"]
                is None,

                x["start_time"]
                or "",

                x["id"]
            )
        )

        return {
            "date":
                target.isoformat(),

            "weekday":
                weekday,

            "count":
                len(schedules),

            "schedules":
                schedules
        }

    finally:

        once_cursor.close()
        weekly_cursor.close()
        conn.close()


def get_calendar_today():

    return get_calendar_date(
        target_date=None,
        unfinished_only=True
    )

# =========================================================
# CSVインポート
# =========================================================
def import_calendar_csv(
    csv_path
):

    import csv
    import os

    csv_path = os.path.abspath(
        os.path.expanduser(
            str(csv_path)
        )
    )

    if not os.path.isfile(
        csv_path
    ):

        raise FileNotFoundError(
            f"CSVファイルがありません: {csv_path}"
        )

    added = 0
    skipped = 0
    errors = []

    with open(
        csv_path,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as f:

        reader = csv.DictReader(
            f
        )

        for row_number, row in enumerate(
            reader,
            start=2
        ):

            try:

                schedule_type = str(
                    row.get(
                        "type",
                        ""
                    )
                ).strip().lower()

                title = str(
                    row.get(
                        "title",
                        ""
                    )
                ).strip()

                description = (
                    str(
                        row.get(
                            "description"
                        )
                        or ""
                    ).strip()
                    or None
                )

                start_time = (
                    str(
                        row.get(
                            "start_time"
                        )
                        or ""
                    ).strip()
                    or None
                )

                end_time = (
                    str(
                        row.get(
                            "end_time"
                        )
                        or ""
                    ).strip()
                    or None
                )

                if (
                    schedule_type
                    == "once"
                ):

                    scheduled_date = (
                        str(
                            row.get(
                                "date"
                            )
                            or ""
                        ).strip()
                    )

                    add_calendar_once(
                        title=title,
                        scheduled_date=scheduled_date,
                        start_time=start_time,
                        end_time=end_time,
                        description=description,
                        source="csv"
                    )

                elif (
                    schedule_type
                    == "weekly"
                ):

                    weekday_text = str(
                        row.get(
                            "weekdays"
                        )
                        or ""
                    ).strip()

                    weekdays = [
                        int(x)
                        for x
                        in weekday_text.split(
                            "|"
                        )
                        if x.strip()
                    ]

                    add_calendar_weekly(
                        title=title,
                        weekdays=weekdays,
                        start_time=start_time,
                        end_time=end_time,
                        description=description,
                        source="csv"
                    )

                else:

                    raise ValueError(
                        f"未対応type: "
                        f"{schedule_type}"
                    )

                added += 1

            except Exception as e:

                skipped += 1

                errors.append(
                    {
                        "row":
                            row_number,

                        "error":
                            str(e)
                    }
                )

    return {
        "success":
            True,

        "added":
            added,

        "skipped":
            skipped,

        "errors":
            errors
    }

# =========================================================
#カレンダーDB初期化
#========================================================

try:

    init_calendar_tables()

except Exception as e:

    print("")
    print(
        " Calendar Databaseを"
        "初期化できませんでした。"
    )

    print(
        f" {e}"
    )



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
CONFIG_DIR = "/opt/Anvelk-Mainframe/config"
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "Avelia"
LOG_DIR = DATA_DIR / "logs"

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


def _schedule_tools():
    """通常会話でアヴェリアに公開するResponses API用Tool。"""
    return [
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
            "description": "登録されているスケジュール予定を一覧表示するために取得します。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "include_used": {
                        "type": "boolean",
                        "description": "trueなら通知済み予定も含める"
                    }
                },
                "required": ["include_used"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "delete_schedule",
            "description": "指定IDのスケジュールを削除します。",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_id": {
                        "type": "integer",
                        "description": "削除するスケジュールID"
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
        "単発予定を完了済みにします。",

    "strict": True,

    "parameters": {

        "type": "object",

        "properties": {

            "schedule_id": {
                "type": "integer"
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
        "曜日条件付き定期予定の"
        "その日分だけを完了済みにします。"
    ),

    "strict": True,

    "parameters": {

        "type": "object",

        "properties": {

            "schedule_id": {
                "type": "integer"
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
                "type": "integer",
                "description":
                    "削除する単発予定ID"
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
                "type": "integer",
                "description":
                    "削除する定期予定ID"
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
    ]

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
    TASK_DB_CONFIG = load_database_config()
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
def main():
    c=0

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
    os.chdir(Path.home())
    if pre_clear:
        # 起動時に画面クリアする場合は少し待機してからクリア(その後簡単なメニュー表示)
        print("2秒後に画面をクリアします...")
        time.sleep(2)
        os.system("clear")
        try:
            print(pyfiglet.figlet_format("Avelia",font="slant"))
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
                    console.print(f" {BOT_NAME}:")
                    console.print(Markdown(display_response))

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



    
