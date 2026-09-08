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


def load_scheduler_interval():
    """
    settingsテーブルから
    スケジュール確認間隔を取得する。

    SCHEDULER / check_interval

    未設定・不正値の場合は30秒。
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
                "SCHEDULER",
                "check_interval",
            )
        )

        row = cursor.fetchone()

        if row is None:
            return 30

        try:
            value = int(row[0])

        except (TypeError, ValueError):
            return 30

        if value < 1:
            return 30

        return value

    finally:
        cursor.close()
        conn.close()


CHECK_INTERVAL = load_scheduler_interval()


# =========================================================
# Slack
# =========================================================

def load_slack_webhook_url(conn):
    """
    Slack Webhook URLを取得する。

    優先順位:
        1. 環境変数 SLACK_WEBHOOK_URL
        2. settingsテーブル
           section_name = SLACK
           setting_key  = webhook_url
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

    HTTP 2xx以外または通信エラーの場合は例外にする。
    """

    webhook_url = load_slack_webhook_url(
        conn
    )

    payload = {
        "text": message
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
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


def get_due_messages(conn):
    """
    現在時刻までに通知時刻を迎えた
    未通知メッセージを取得する。
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
                AND scheduled_at <= NOW()

            ORDER BY
                scheduled_at ASC,
                id ASC
            """
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_message_used(
    conn,
    message_id
):
    """
    schedulesの通知を使用済みにする。
    """

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


def build_message(schedule):
    """
    schedules用Slack通知本文。
    """

    return (
        "[アヴェリア スケジュール]\n"
        f"{schedule['title']}\n\n"
        f"{schedule['message']}\n\n"
        "予定時刻: "
        f"{schedule['scheduled_at'].strftime('%Y-%m-%d %H:%M')}"
    )


def process_schedules():
    """
    schedulesの通知処理。

    Slack送信成功後だけ
    message_use = 1 にする。
    """

    conn = get_connection()

    try:
        schedules = get_due_messages(
            conn
        )

        for schedule in schedules:

            try:
                message = build_message(
                    schedule
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

def get_due_calendar_once(conn):
    """
    開始時刻を迎えた単発予定のうち、
    まだ通知していない予定を取得する。

    サービス停止中に予定時刻を過ぎても、
    復旧後の最初のチェックで取得される。
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
                ) <= NOW()

            ORDER BY
                scheduled_date ASC,
                start_time ASC,
                id ASC
            """
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_calendar_once_notified(
    conn,
    schedule_id
):
    """
    単発予定を通知済みにする。

    Slack通知成功後に呼び出す。
    """

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


def build_calendar_once_message(schedule):
    """
    単発予定のSlack通知本文。
    """

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

    return message


def process_calendar_once():
    """
    単発予定を通知する。

    Slack送信成功後だけ notified = 1 にする。

    Slack送信に失敗した場合は
    notified = 0 のままなので、
    次回チェック時に再試行される。
    """

    conn = get_connection()

    try:
        schedules = get_due_calendar_once(
            conn
        )

        for schedule in schedules:

            try:
                message = (
                    build_calendar_once_message(
                        schedule
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

def get_due_calendar_weekly(conn):
    """
    今日の曜日に該当するweekly予定から、
    開始時刻を迎えた未通知予定を取得する。

    weekday:
        Monday    = 0
        Tuesday   = 1
        Wednesday = 2
        Thursday  = 3
        Friday    = 4
        Saturday  = 5
        Sunday    = 6

    MySQL WEEKDAY()も同じ形式。
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

                AND cw.start_time <= CURTIME()

                AND (
                    cw.last_notified_date IS NULL
                    OR
                    cw.last_notified_date <> CURDATE()
                )

            ORDER BY
                cw.start_time ASC,
                cw.id ASC
            """
        )

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_calendar_weekly_notified(
    conn,
    schedule_id
):
    """
    weekly予定を今日通知済みにする。

    Slack通知成功後に呼び出す。
    """

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


def build_calendar_weekly_message(schedule):
    """
    weekly予定のSlack通知本文。
    """

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

    return message


def process_calendar_weekly():
    """
    今日のweekly予定を通知する。

    Slack送信成功後だけ
    last_notified_dateを今日の日付へ更新する。

    Slack送信に失敗した場合は
    last_notified_dateを更新しないため、
    次回チェック時に再試行される。
    """

    conn = get_connection()

    try:
        schedules = get_due_calendar_weekly(
            conn
        )

        for schedule in schedules:

            try:
                message = (
                    build_calendar_weekly_message(
                        schedule
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
# Main loop
# =========================================================

def run():
    """
    Aveliaスケジュール通知サービス。
    """

    init_table()

    print(
        "[Avelia Schedule] "
        "通知サービス開始 "
        f"(interval={CHECK_INTERVAL}s)",
        flush=True
    )

    while True:

        # -----------------------------
        # schedules
        # -----------------------------

        try:
            process_schedules()

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
            process_calendar_once()

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
            process_calendar_weekly()

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "calendar_weekly処理エラー "
                f"{type(e).__name__}: {e}",
                flush=True
            )

        time.sleep(
            CHECK_INTERVAL
        )


if __name__ == "__main__":
    run()