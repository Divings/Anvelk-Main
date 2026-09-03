#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import configparser
import os
import time

import mysql.connector
import requests


DATABASE_CONF = "/opt/config/database.conf"
CHECK_INTERVAL = 30


# =========================================================
# Database
# =========================================================

def load_database_config():
    """
    /opt/config/database.conf から
    MySQL/MariaDB 接続情報を取得する。

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
# Slack
# =========================================================

def load_slack_webhook_url(conn):
    """
    Slack Webhook URL を取得する。

    優先順位:
    1. 環境変数 SLACK_WEBHOOK_URL
    2. settings テーブル
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

    HTTP 2xx以外、通信エラーは例外にする。
    例外時は呼び出し側で message_use を更新しない。
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
# Schedule table
# =========================================================

def init_table():
    """
    schedules テーブルが存在しない場合は作成する。
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
    現在時刻までに通知予定時刻を迎えた
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
    通知済みのスケジュールを
    message_use = 1 に更新する。
    """

    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE schedules
            SET message_use = 1
            WHERE id = %s
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


# =========================================================
# Schedule notification
# =========================================================

def build_message(schedule):
    """
    Slackへ送る本文を生成する。
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
    通知対象を取得し、
    Slack送信成功後に message_use を1へ更新する。
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

                # Slack通知成功後だけ使用済みにする。
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
# Main loop
# =========================================================

def run():
    init_table()

    print(
        "[Avelia Schedule] "
        "通知サービス開始",
        flush=True
    )

    while True:

        try:
            process_schedules()

        except Exception as e:
            print(
                "[Avelia Schedule] "
                f"{type(e).__name__}: {e}",
                flush=True
            )

        time.sleep(
            CHECK_INTERVAL
        )


if __name__ == "__main__":
    run()
