# -*- coding: utf-8 -*-

import configparser
import time
import mysql.connector

from slack_notify import notify_slack


DATABASE_CONF = "/opt/config/database.conf"
CHECK_INTERVAL = 30


def load_database_config():
    config = configparser.ConfigParser()
    config.read(DATABASE_CONF, encoding="utf-8")

    if "database" not in config:
        raise RuntimeError(
            "/opt/config/database.conf に "
            "[database] がありません"
        )

    db = config["database"]

    return {
        "host": db.get("host", "127.0.0.1"),
        "port": db.getint("port", 3306),
        "user": db.get("user"),
        "password": db.get("password", ""),
        "database": db.get("database"),
        "charset": "utf8mb4",
        "autocommit": False,
    }


def get_connection():
    return mysql.connector.connect(
        **load_database_config()
    )


def init_table():
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
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
        """)

        conn.commit()

    finally:
        cursor.close()
        conn.close()


def get_due_messages(conn):
    cursor = conn.cursor(
        dictionary=True
    )

    try:
        cursor.execute("""
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
        """)

        return cursor.fetchall()

    finally:
        cursor.close()


def mark_message_used(
    conn,
    message_id
):
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE schedules
            SET message_use = 1
            WHERE id = %s
              AND message_use = 0
        """, (
            message_id,
        ))

    finally:
        cursor.close()


def build_message(schedule):
    return (
        "[アヴェリア スケジュール]\n"
        f"{schedule['title']}\n\n"
        f"{schedule['message']}\n\n"
        f"予定時刻: "
        f"{schedule['scheduled_at'].strftime('%Y-%m-%d %H:%M')}"
    )


def process_schedules():
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
                    message,
                    mode="schedule"
                )

                mark_message_used(
                    conn,
                    schedule["id"]
                )

                conn.commit()

                print(
                    "[Schedule] 通知完了 "
                    f"ID={schedule['id']}"
                )

            except Exception as e:
                conn.rollback()

                print(
                    "[Schedule] 通知失敗 "
                    f"ID={schedule['id']} "
                    f"{e}"
                )

    finally:
        conn.close()


def run():
    init_table()

    print(
        "[Avelia Schedule] "
        "通知サービス開始"
    )

    while True:
        try:
            process_schedules()

        except Exception as e:
            print(
                "[Avelia Schedule] "
                f"Error: {e}"
            )

        time.sleep(
            CHECK_INTERVAL
        )


if __name__ == "__main__":
    run()