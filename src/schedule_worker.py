#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import configparser
import os
import time

import mysql.connector
import requests


# =========================================================
# Config
# =========================================================

DATABASE_CONF = "/opt/Anvelk-Mainframe/config/database.conf"

DEFAULT_CHECK_INTERVAL = 30
DEFAULT_NOTIFY_BEFORE_MINUTES = 5


# =========================================================
# Database
# =========================================================

def load_database_config():
    """
    /opt/Anvelk-Mainframe/config/database.conf から
    MySQL/MariaDB接続情報を取得する。

    想定形式:

    [DATABASE]
    host = 127.0.0.1
    port = 3306
    user = user_name
    password = password
    database = mainframe
    """

    config = configparser.ConfigParser()

    read_files = config.read(
        DATABASE_CONF,
        encoding="utf-8"
    )

    if not read_files:
        raise RuntimeError(
            f"{DATABASE_CONF} が見つかりません。"
        )

    if "DATABASE" not in config:
        raise RuntimeError(
            f"{DATABASE_CONF} に "
            "[DATABASE] セクションがありません。"
        )

    db = config["DATABASE"]

    host = db.get(
        "host",
        "127.0.0.1"
    ).strip()

    port = db.getint(
        "port",
        fallback=3306
    )

    user = db.get(
        "user",
        ""
    ).strip()

    password = db.get(
        "password",
        ""
    )

    database = db.get(
        "database",
        ""
    ).strip()

    if not user:
        raise RuntimeError(
            f"{DATABASE_CONF} の user が設定されていません。"
        )

    if not password:
        raise RuntimeError(
            f"{DATABASE_CONF} の password が設定されていません。"
        )

    if not database:
        raise RuntimeError(
            f"{DATABASE_CONF} の database が設定されていません。"
        )

    return {
        "host": host or "127.0.0.1",
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "autocommit": False,
    }


def get_connection():
    return mysql.connector.connect(
        **load_database_config()
    )


# =========================================================
# Settings
# =========================================================

def ensure_scheduler_settings():
    """
    SCHEDULER設定が存在しなければ追加する。

    check_interval:
        DBを確認する間隔（秒）

    notify_before_minutes:
        予定開始何分前に通知するか
    """

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # check_interval
        cursor.execute(
            """
            SELECT 1
            FROM settings
            WHERE section_name = %s
              AND setting_key = %s
            LIMIT 1
            """,
            (
                "SCHEDULER",
                "check_interval",
            )
        )

        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO settings (
                    section_name,
                    setting_key,
                    setting_value
                )
                VALUES (%s, %s, %s)
                """,
                (
                    "SCHEDULER",
                    "check_interval",
                    str(DEFAULT_CHECK_INTERVAL),
                )
            )

            print(
                "[Avelia Schedule] "
                "settingsへ "
                "SCHEDULER/check_interval="
                f"{DEFAULT_CHECK_INTERVAL} を追加",
                flush=True
            )

        # notify_before_minutes
        cursor.execute(
            """
            SELECT 1
            FROM settings
            WHERE section_name = %s
              AND setting_key = %s
            LIMIT 1
            """,
            (
                "SCHEDULER",
                "notify_before_minutes",
            )
        )

        if cursor.fetchone() is None:
            cursor.execute(
                """
                INSERT INTO settings (
                    section_name,
                    setting_key,
                    setting_value
                )
                VALUES (%s, %s, %s)
                """,
                (
                    "SCHEDULER",
                    "notify_before_minutes",
                    str(DEFAULT_NOTIFY_BEFORE_MINUTES),
                )
            )

            print(
                "[Avelia Schedule] "
                "settingsへ "
                "SCHEDULER/notify_before_minutes="
                f"{DEFAULT_NOTIFY_BEFORE_MINUTES} を追加",
                flush=True
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        cursor.close()
        conn.close()


def load_integer_setting(
    section_name,
    setting_key,
    default_value,
    minimum=0
):
    """
    settingsテーブルから整数設定を取得する。

    未設定または不正値の場合はdefault_valueを返す。
    """

    conn = get_connection()
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
            return default_value

        try:
            value = int(row[0])

        except (TypeError, ValueError):
            return default_value

        if value < minimum:
            return default_value

        return value

    finally:
        cursor.close()
        conn.close()


def load_scheduler_interval():
    return load_integer_setting(
        "SCHEDULER",
        "check_interval",
        DEFAULT_CHECK_INTERVAL,
        minimum=1
    )


def load_notify_before_minutes():
    return load_integer_setting(
        "SCHEDULER",
        "notify_before_minutes",
        DEFAULT_NOTIFY_BEFORE_MINUTES,
        minimum=0
    )


# =========================================================
# Slack
# =========================================================

def load_slack_webhook_url(conn):
    """
    Slack Webhook URLを取得する。

    優先順位:
        1. 環境変数 SLACK_WEBHOOK_URL
        2. settingsテーブル
           SLACK / webhook_url
    """

    env_url = os.getenv(
        "SLACK_WEBHOOK_URL",
        ""
    ).strip()

    if env_url:
        return env_url

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
                "SLACK",
                "webhook_url",
            )
        )

        row = cursor.fetchone()

    finally:
        cursor.close()

    if not row:
        raise RuntimeError(
            "Slack Webhook URL が設定されていません。"
            " settingsテーブルに "
            "SLACK / webhook_url を設定してください。"
        )

    webhook_url = str(
        row[0] or ""
    ).strip()

    if not webhook_url:
        raise RuntimeError(
            "Slack Webhook URL が空です。"
        )

    return webhook_url


def notify_slack(
    conn,
    message
):
    """
    Slack Incoming Webhookへ通知する。
    """

    webhook_url = load_slack_webhook_url(
        conn
    )

    try:
        response = requests.post(
            webhook_url,
            json={
                "text": message
            },
            timeout=10
        )

    except requests.RequestException as e:
        raise RuntimeError(
            f"Slackへの接続に失敗しました: {e}"
        ) from e

    if not (
        200 <= response.status_code < 300
    ):
        raise RuntimeError(
            "Slack通知に失敗しました。"
            f" HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )


# =========================================================
# schedules
# =========================================================

def init_table():
    """
    schedulesテーブルが存在しない場合は作成する。
    """

    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id BIGINT UNSIGNED
                    NOT NULL AUTO_INCREMENT,

                title VARCHAR(255)
                    NOT NULL,

                message TEXT
                    NOT NULL,

                scheduled_at DATETIME
                    NOT NULL,

                message_use TINYINT(1)
                    NOT NULL DEFAULT 0,

                created_at DATETIME
                    NOT NULL DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (id),

                INDEX idx_schedule_due (
                    message_use,
                    scheduled_at
                )

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


def get_due_messages(
    conn,
    notify_before_minutes
):
    """
    通知時刻のN分前を迎えた
    未通知schedulesを取得する。
    """

    cursor = conn.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                id,
                title,
                message,
                scheduled_at,
                message_use

            FROM schedules

            WHERE
                message_use = 0

                AND scheduled_at <=
                    DATE_ADD(
                        NOW(),
                        INTERVAL %s MINUTE
                    )

            ORDER BY
                scheduled_at ASC,
                id ASC
            """,
            (
                notify_before_minutes,
            )
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_message_used(
    conn,
    message_id
):
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE schedules

            SET
                message_use = 1

            WHERE
                id = %s
                AND message_use = 0
            """,
            (
                message_id,
            )
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "message_use の更新に失敗しました。"
                f" ID={message_id}"
            )

    finally:
        cursor.close()


def build_message(
    schedule,
    notify_before_minutes
):
    return (
        "[アヴェリア スケジュール]\n"
        f"{schedule['title']}\n\n"
        f"{schedule['message']}\n\n"
        "予定時刻: "
        f"{schedule['scheduled_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"{notify_before_minutes}分前通知"
    )


def process_schedules(
    notify_before_minutes
):
    conn = get_connection()

    try:
        schedules = get_due_messages(
            conn,
            notify_before_minutes
        )

        for schedule in schedules:

            try:
                message = build_message(
                    schedule,
                    notify_before_minutes
                )

                notify_slack(
                    conn,
                    message
                )

                mark_message_used(
                    conn,
                    schedule["id"]
                )

                conn.commit()

                print(
                    "[Schedule] 通知完了 "
                    f"ID={schedule['id']} "
                    f"title={schedule['title']}",
                    flush=True
                )

            except Exception as e:
                conn.rollback()

                print(
                    "[Schedule] 通知失敗 "
                    f"ID={schedule['id']} "
                    f"{type(e).__name__}: {e}",
                    flush=True
                )

    finally:
        conn.close()


# =========================================================
# calendar_once
# =========================================================

def get_due_calendar_once(
    conn,
    notify_before_minutes
):
    """
    開始時刻のN分前を迎えた単発予定を取得する。

    notified = 0 の予定だけ対象。
    """

    cursor = conn.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                id,
                title,
                description,
                scheduled_date,
                start_time,
                end_time,
                notified

            FROM calendar_once

            WHERE
                completed = 0

                AND notified = 0

                AND start_time IS NOT NULL

                AND TIMESTAMP(
                    scheduled_date,
                    start_time
                ) <= DATE_ADD(
                    NOW(),
                    INTERVAL %s MINUTE
                )

            ORDER BY
                scheduled_date ASC,
                start_time ASC,
                id ASC
            """,
            (
                notify_before_minutes,
            )
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_calendar_once_notified(
    conn,
    schedule_id
):
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE calendar_once

            SET
                notified = 1

            WHERE
                id = %s
                AND notified = 0
            """,
            (
                schedule_id,
            )
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "単発予定の通知済み更新に失敗しました。"
                f" ID={schedule_id}"
            )

    finally:
        cursor.close()


def build_calendar_once_message(
    schedule,
    notify_before_minutes
):
    message = (
        "[アヴェリア 予定通知]\n"
        f"{schedule['title']}\n"
    )

    if schedule.get("description"):
        message += (
            "\n"
            f"{schedule['description']}\n"
        )

    message += (
        "\n日付: "
        f"{schedule['scheduled_date']}"
    )

    message += (
        "\n開始時刻: "
        f"{schedule['start_time']}"
    )

    if schedule.get("end_time"):
        message += (
            "\n終了時刻: "
            f"{schedule['end_time']}"
        )

    message += (
        f"\n{notify_before_minutes}分前通知"
    )

    return message


def process_calendar_once(
    notify_before_minutes
):
    conn = get_connection()

    try:
        schedules = get_due_calendar_once(
            conn,
            notify_before_minutes
        )

        for schedule in schedules:

            try:
                message = (
                    build_calendar_once_message(
                        schedule,
                        notify_before_minutes
                    )
                )

                notify_slack(
                    conn,
                    message
                )

                mark_calendar_once_notified(
                    conn,
                    schedule["id"]
                )

                conn.commit()

                print(
                    "[Calendar Once] 通知完了 "
                    f"ID={schedule['id']} "
                    f"title={schedule['title']}",
                    flush=True
                )

            except Exception as e:
                conn.rollback()

                print(
                    "[Calendar Once] 通知失敗 "
                    f"ID={schedule['id']} "
                    f"{type(e).__name__}: {e}",
                    flush=True
                )

    finally:
        conn.close()


# =========================================================
# calendar_weekly
# =========================================================

def get_due_calendar_weekly(
    conn,
    notify_before_minutes
):
    """
    今日の曜日に該当するweekly予定から、
    開始時刻のN分前を迎えた未通知予定を取得する。

    weekday:
        Monday    = 0
        Tuesday   = 1
        Wednesday = 2
        Thursday  = 3
        Friday    = 4
        Saturday  = 5
        Sunday    = 6
    """

    cursor = conn.cursor(
        dictionary=True
    )

    try:
        cursor.execute(
            """
            SELECT
                cw.id,
                cw.title,
                cw.description,
                cw.start_time,
                cw.end_time,
                cw.last_notified_date

            FROM calendar_weekly AS cw

            INNER JOIN calendar_weekly_days AS cwd
                ON cw.id = cwd.schedule_id

            WHERE
                cw.enabled = 1

                AND cwd.weekday =
                    WEEKDAY(CURDATE())

                AND cw.start_time IS NOT NULL

                AND TIMESTAMP(
                    CURDATE(),
                    cw.start_time
                ) <= DATE_ADD(
                    NOW(),
                    INTERVAL %s MINUTE
                )

                AND (
                    cw.last_notified_date IS NULL
                    OR
                    cw.last_notified_date <> CURDATE()
                )

            ORDER BY
                cw.start_time ASC,
                cw.id ASC
            """,
            (
                notify_before_minutes,
            )
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_calendar_weekly_notified(
    conn,
    schedule_id
):
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE calendar_weekly

            SET
                last_notified_date = CURDATE()

            WHERE
                id = %s

                AND (
                    last_notified_date IS NULL
                    OR
                    last_notified_date <> CURDATE()
                )
            """,
            (
                schedule_id,
            )
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "weekly予定の通知済み更新に失敗しました。"
                f" ID={schedule_id}"
            )

    finally:
        cursor.close()


def build_calendar_weekly_message(
    schedule,
    notify_before_minutes
):
    message = (
        "[アヴェリア 予定通知]\n"
        f"{schedule['title']}\n"
    )

    if schedule.get("description"):
        message += (
            "\n"
            f"{schedule['description']}\n"
        )

    message += (
        "\n開始時刻: "
        f"{schedule['start_time']}"
    )

    if schedule.get("end_time"):
        message += (
            "\n終了時刻: "
            f"{schedule['end_time']}"
        )

    message += (
        f"\n{notify_before_minutes}分前通知"
    )

    return message


def process_calendar_weekly(
    notify_before_minutes
):
    conn = get_connection()

    try:
        schedules = get_due_calendar_weekly(
            conn,
            notify_before_minutes
        )

        for schedule in schedules:

            try:
                message = (
                    build_calendar_weekly_message(
                        schedule,
                        notify_before_minutes
                    )
                )

                notify_slack(
                    conn,
                    message
                )

                mark_calendar_weekly_notified(
                    conn,
                    schedule["id"]
                )

                conn.commit()

                print(
                    "[Calendar Weekly] 通知完了 "
                    f"ID={schedule['id']} "
                    f"title={schedule['title']}",
                    flush=True
                )

            except Exception as e:
                conn.rollback()

                print(
                    "[Calendar Weekly] 通知失敗 "
                    f"ID={schedule['id']} "
                    f"{type(e).__name__}: {e}",
                    flush=True
                )

    finally:
        conn.close()


# =========================================================
# Main
# =========================================================

def run():
    """
    Aveliaスケジュール通知サービス。
    """

    # schedulesテーブル確認
    init_table()

    # SCHEDULER設定がなければ自動追加
    ensure_scheduler_settings()

    # 起動時に設定を読み込む
    check_interval = load_scheduler_interval()

    print(
        "[Avelia Schedule] "
        "通知サービス開始 "
        f"(interval={check_interval}s)",
        flush=True
    )

    while True:

        # 毎ループ読み直す。
        # DBを書き換えればサービス再起動なしで
        # 通知時間を変更できる。
        notify_before_minutes = (
            load_notify_before_minutes()
        )

        # -----------------------------
        # schedules
        # -----------------------------

        try:
            process_schedules(
                notify_before_minutes
            )

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "schedules処理エラー "
                f"{type(e).__name__}: {e}",
                flush=True
            )

        # -----------------------------
        # calendar_once
        # -----------------------------

        try:
            process_calendar_once(
                notify_before_minutes
            )

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "calendar_once処理エラー "
                f"{type(e).__name__}: {e}",
                flush=True
            )

        # -----------------------------
        # calendar_weekly
        # -----------------------------

        try:
            process_calendar_weekly(
                notify_before_minutes
            )

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "calendar_weekly処理エラー "
                f"{type(e).__name__}: {e}",
                flush=True
            )

        time.sleep(
            check_interval
        )


if __name__ == "__main__":
    run()