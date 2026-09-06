#!/usr/bin/env python3

import configparser
import datetime
import logging
import os
import shlex
import subprocess
import sys
import time
import traceback

import mysql.connector
from mysql.connector import Error


# =========================================================
# 設定
# =========================================================

CONFIG_FILE = "/opt/Anvelk-Mainframe/config/database.conf"


# =========================================================
# STATUS
# =========================================================

STATUS_PENDING = 0
STATUS_RUNNING = 1
STATUS_SUCCESS = 2
STATUS_FAILED = 3
STATUS_DISABLED = 4


# =========================================================
# ログ
#
# 専用ログファイルは作成しない。
# stdout / stderr は systemd journal に送る。
# =========================================================

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(message)s"
        ),
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


# =========================================================
# 設定ファイル読み込み
# =========================================================

def load_config():

    config = configparser.ConfigParser()

    if not os.path.isfile(CONFIG_FILE):
        raise FileNotFoundError(
            f"設定ファイルがありません: {CONFIG_FILE}"
        )

    config.read(
        CONFIG_FILE,
        encoding="utf-8"
    )

    if "DATABASE" not in config:
        raise ValueError(
            "[DATABASE] セクションがありません"
        )

    return config


# =========================================================
# DB接続
# =========================================================

def connect_db(config):

    return mysql.connector.connect(
        host=config["DATABASE"].get(
            "host",
            "127.0.0.1"
        ),

        port=config["DATABASE"].getint(
            "port",
            3306
        ),

        user=config["DATABASE"]["user"],

        password=config["DATABASE"]["password"],

        database=config["DATABASE"]["database"],

        charset="utf8mb4",

        autocommit=False,
    )


# =========================================================
# MySQL TIME → 秒
#
# mysql-connector-pythonではTIMEが
# datetime.timedeltaになる場合がある。
# =========================================================

def time_to_seconds(value):

    if isinstance(
        value,
        datetime.timedelta
    ):
        return int(
            value.total_seconds()
        )

    if isinstance(
        value,
        datetime.time
    ):
        return (
            value.hour * 3600
            + value.minute * 60
            + value.second
        )

    raise TypeError(
        f"未対応のrun_time型です: {type(value)}"
    )


def now_to_seconds(now):

    return (
        now.hour * 3600
        + now.minute * 60
        + now.second
    )


# =========================================================
# pendingタスク取得
# =========================================================

def get_pending_tasks(conn):

    sql = """
        SELECT
            id,
            task_name,

            program_path,
            arguments,
            working_directory,

            run_date,
            run_time,

            is_one_shot,
            is_weekly,
            weekday_code,

            status,

            last_run_at,
            last_exit_code,
            last_error

        FROM scheduled_tasks

        WHERE status = %s

        ORDER BY
            run_time ASC,
            id ASC
    """

    cursor = conn.cursor(
        dictionary=True
    )

    try:

        cursor.execute(
            sql,
            (
                STATUS_PENDING,
            )
        )

        return cursor.fetchall()

    finally:

        cursor.close()


# =========================================================
# 今日すでに実行済みか
# =========================================================

def already_run_today(
    task,
    today
):

    last_run_at = task.get(
        "last_run_at"
    )

    if last_run_at is None:
        return False

    if not isinstance(
        last_run_at,
        datetime.datetime
    ):
        return False

    return (
        last_run_at.date()
        == today
    )


# =========================================================
# 実行対象判定
# =========================================================

def should_run(
    task,
    now
):

    if (
        task["status"]
        != STATUS_PENDING
    ):
        return False

    today = now.date()

    current_seconds = (
        now_to_seconds(now)
    )

    run_seconds = (
        time_to_seconds(
            task["run_time"]
        )
    )

    # =====================================================
    # 単発タスク
    # =====================================================

    if task["is_one_shot"]:

        run_date = task[
            "run_date"
        ]

        if run_date is None:

            logging.warning(
                "task[%s] "
                "単発タスクですが"
                "run_dateがNULLです",
                task["id"],
            )

            return False

        # 指定日前
        if today < run_date:
            return False

        # 指定日だが時刻前
        if (
            today == run_date
            and
            current_seconds < run_seconds
        ):
            return False

        return True

    # =====================================================
    # 曜日指定繰り返し
    # =====================================================

    if task["is_weekly"]:

        weekday_code = task[
            "weekday_code"
        ]

        if weekday_code is None:

            logging.warning(
                "task[%s] "
                "曜日指定タスクですが"
                "weekday_codeがNULLです",
                task["id"],
            )

            return False

        if (
            now.weekday()
            != weekday_code
        ):
            return False

        if (
            current_seconds
            < run_seconds
        ):
            return False

        if already_run_today(
            task,
            today
        ):
            return False

        return True

    return False


# =========================================================
# タスク確保
#
# pending → running
#
# 条件付きUPDATEで二重取得を防ぐ。
# =========================================================

def claim_task(
    conn,
    task_id
):

    sql = """
        UPDATE scheduled_tasks

        SET
            status = %s,
            last_error = NULL

        WHERE
            id = %s
            AND status = %s
    """

    cursor = conn.cursor()

    try:

        cursor.execute(
            sql,
            (
                STATUS_RUNNING,
                task_id,
                STATUS_PENDING,
            )
        )

        if cursor.rowcount != 1:

            conn.rollback()

            return False

        conn.commit()

        return True

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()


# =========================================================
# コマンド生成
#
# program_pathは非固定。
# shell=Trueは使用しない。
# =========================================================

def build_command(task):

    program_path = task[
        "program_path"
    ]

    if not program_path:
        raise ValueError(
            "program_path が空です"
        )

    if not os.path.isfile(
        program_path
    ):
        raise FileNotFoundError(
            f"プログラムが存在しません: "
            f"{program_path}"
        )

    if not os.access(
        program_path,
        os.X_OK
    ):
        raise PermissionError(
            f"実行権限がありません: "
            f"{program_path}"
        )

    command = [
        program_path
    ]

    arguments = task.get(
        "arguments"
    )

    if arguments:

        command.extend(
            shlex.split(
                arguments
            )
        )

    return command


# =========================================================
# working_directory
# =========================================================

def get_working_directory(task):

    directory = task.get(
        "working_directory"
    )

    if not directory:
        return None

    if not os.path.isdir(
        directory
    ):
        raise FileNotFoundError(
            f"working_directoryが存在しません: "
            f"{directory}"
        )

    return directory


# =========================================================
# タスク実行
# =========================================================

def execute_task(
    task,
    timeout
):

    command = build_command(
        task
    )

    working_directory = (
        get_working_directory(
            task
        )
    )

    logging.info(
        "タスク起動 "
        "ID=%s "
        "name=%s "
        "command=%s",
        task["id"],
        task["task_name"],
        command,
    )

    result = subprocess.run(
        command,

        cwd=working_directory,

        shell=False,

        capture_output=True,

        text=True,

        timeout=timeout,
    )

    if result.stdout:

        logging.info(
            "task[%s] stdout:\n%s",
            task["id"],
            result.stdout.rstrip(),
        )

    if result.stderr:

        logging.warning(
            "task[%s] stderr:\n%s",
            task["id"],
            result.stderr.rstrip(),
        )

    return result.returncode


# =========================================================
# 成功
# =========================================================

def mark_success(
    conn,
    task,
    exit_code
):

    # 単発はsuccess
    if task["is_one_shot"]:

        next_status = (
            STATUS_SUCCESS
        )

    # 曜日繰り返しはpendingへ戻す
    else:

        next_status = (
            STATUS_PENDING
        )

    sql = """
        UPDATE scheduled_tasks

        SET
            status = %s,
            last_run_at = NOW(),
            last_exit_code = %s,
            last_error = NULL

        WHERE id = %s
    """

    cursor = conn.cursor()

    try:

        cursor.execute(
            sql,
            (
                next_status,
                exit_code,
                task["id"],
            )
        )

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()


# =========================================================
# 失敗
# =========================================================

def mark_failure(
    conn,
    task,
    error_message,
    exit_code=None
):

    sql = """
        UPDATE scheduled_tasks

        SET
            status = %s,
            last_run_at = NOW(),
            last_exit_code = %s,
            last_error = %s

        WHERE id = %s
    """

    cursor = conn.cursor()

    try:

        cursor.execute(
            sql,
            (
                STATUS_FAILED,
                exit_code,
                error_message,
                task["id"],
            )
        )

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        cursor.close()


# =========================================================
# 1タスク処理
# =========================================================

def process_task(
    conn,
    task,
    now,
    timeout
):

    if not should_run(
        task,
        now
    ):
        return

    if not claim_task(
        conn,
        task["id"]
    ):

        logging.info(
            "task[%s] は"
            "別プロセスが取得済みです",
            task["id"],
        )

        return

    try:

        exit_code = execute_task(
            task,
            timeout
        )

        # =================================================
        # 成功
        # =================================================

        if exit_code == 0:

            mark_success(
                conn,
                task,
                exit_code
            )

            logging.info(
                "タスク正常終了 "
                "ID=%s "
                "name=%s",
                task["id"],
                task["task_name"],
            )

        # =================================================
        # exit code != 0
        # =================================================

        else:

            message = (
                f"exit code "
                f"{exit_code} "
                "で終了しました"
            )

            mark_failure(
                conn,
                task,
                message,
                exit_code
            )

            logging.error(
                "タスク異常終了 "
                "ID=%s "
                "name=%s "
                "exit=%s",
                task["id"],
                task["task_name"],
                exit_code,
            )

    # =====================================================
    # タイムアウト
    # =====================================================

    except subprocess.TimeoutExpired:

        message = (
            "タスク実行が"
            "タイムアウトしました"
        )

        logging.error(
            "task[%s] %s",
            task["id"],
            message,
        )

        try:

            mark_failure(
                conn,
                task,
                message
            )

        except Exception:

            logging.exception(
                "タイムアウト状態の"
                "DB記録に失敗"
            )

    # =====================================================
    # その他
    # =====================================================

    except Exception as exc:

        error_message = (
            f"{type(exc).__name__}: "
            f"{exc}\n"
            f"{traceback.format_exc()}"
        )

        logging.error(
            "タスク実行エラー "
            "ID=%s "
            "name=%s\n%s",
            task["id"],
            task["task_name"],
            error_message,
        )

        try:

            mark_failure(
                conn,
                task,
                error_message
            )

        except Exception:

            logging.exception(
                "DBへのエラー記録にも失敗"
            )


# =========================================================
# 全タスク処理
# =========================================================

def process_tasks(
    conn,
    timeout
):

    now = (
        datetime.datetime.now()
    )

    tasks = (
        get_pending_tasks(
            conn
        )
    )

    for task in tasks:

        process_task(
            conn,
            task,
            now,
            timeout
        )


# =========================================================
# main
# =========================================================

def main():

    setup_logging()

    logging.info(
        "Avelia Task Runner 起動"
    )

    config = load_config()

    # =====================================================
    # RUNNER設定
    # =====================================================

    if "RUNNER" in config:

        poll_interval = (
            config[
                "RUNNER"
            ].getint(
                "poll_interval",
                20
            )
        )

        task_timeout = (
            config[
                "RUNNER"
            ].getint(
                "task_timeout",
                3600
            )
        )

    else:

        poll_interval = 20
        task_timeout = 3600

    # =====================================================
    # メインループ
    # =====================================================

    while True:

        conn = None

        try:

            conn = connect_db(
                config
            )

            if not conn.is_connected():

                raise RuntimeError(
                    "DBへ接続できませんでした"
                )

            process_tasks(
                conn,
                task_timeout
            )

        except KeyboardInterrupt:

            logging.info(
                "Avelia Task Runner 停止"
            )

            break

        except Error:

            logging.exception(
                "MySQLエラー"
            )

        except Exception:

            logging.exception(
                "Task Runner "
                "メインループエラー"
            )

        finally:

            if conn is not None:

                try:

                    if conn.is_connected():
                        conn.close()

                except Exception:
                    pass

        time.sleep(
            poll_interval
        )


if __name__ == "__main__":
    main()