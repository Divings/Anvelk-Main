#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Avelia Google Calendar Schedule Worker

予定本体:
    Google Calendar

DBで保持するもの:
    settings のみ
      - SCHEDULER / check_interval
      - SCHEDULER / notify_before_minutes
      - SLACK / webhook_url

ローカルファイル:
    ~/.local/share/Avelia/google_calendar_token.json
        Google OAuthトークン

    ~/.local/share/Avelia/google_calendar_notify_state.json
        Google Calendarイベントの通知済み状態

このworkerは以下の旧予定テーブルを使用しない:
    schedules
    calendar_once
    calendar_weekly
    calendar_weekly_days
    calendar_weekly_status
"""

from __future__ import annotations

import configparser
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import mysql.connector
import requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================
# 基本設定
# ============================================================

DATABASE_CONF = "/opt/Anvelk-Mainframe/config/database.conf"

DATA_DIR = (
    Path.home()
    / ".local"
    / "share"
    / "Avelia"
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

GOOGLE_TOKEN_FILE = (
    DATA_DIR
    / "google_calendar_token.json"
)

NOTIFY_STATE_FILE = (
    DATA_DIR
    / "google_calendar_notify_state.json"
)

DEFAULT_CHECK_INTERVAL = 30
DEFAULT_NOTIFY_BEFORE_MINUTES = 5

GOOGLE_CALENDAR_ID = "primary"
GOOGLE_TIMEZONE = "Asia/Tokyo"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar"
]

STATE_RETENTION_DAYS = 60


# ============================================================
# Database
# ============================================================

def load_database_config():
    """
    /opt/Anvelk-Mainframe/config/database.conf から
    MySQL/MariaDB接続情報を取得する。

    workerは予定データにはDBを使用せず、
    settingsテーブルを読むためだけに接続する。
    """

    config = configparser.ConfigParser()

    read_files = config.read(
        DATABASE_CONF,
        encoding="utf-8",
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
        "127.0.0.1",
    ).strip()

    port = db.getint(
        "port",
        fallback=3306,
    )

    user = db.get(
        "user",
        "",
    ).strip()

    password = db.get(
        "password",
        "",
    )

    database = db.get(
        "database",
        "",
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


# ============================================================
# Settings
# ============================================================

def ensure_scheduler_settings():
    """
    必要なSCHEDULER設定が存在しなければ追加する。
    """

    conn = get_connection()
    cursor = conn.cursor()

    try:
        defaults = (
            (
                "SCHEDULER",
                "check_interval",
                str(DEFAULT_CHECK_INTERVAL),
            ),
            (
                "SCHEDULER",
                "notify_before_minutes",
                str(DEFAULT_NOTIFY_BEFORE_MINUTES),
            ),
        )

        for section_name, setting_key, setting_value in defaults:
            cursor.execute(
                """
                SELECT 1
                FROM settings
                WHERE section_name = %s
                  AND setting_key = %s
                LIMIT 1
                """,
                (
                    section_name,
                    setting_key,
                ),
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
                        section_name,
                        setting_key,
                        setting_value,
                    ),
                )

                print(
                    "[Avelia Schedule] "
                    "settingsへ "
                    f"{section_name}/{setting_key}="
                    f"{setting_value} を追加",
                    flush=True,
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
    minimum=0,
):
    """
    settingsテーブルから整数値を取得する。
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
            ),
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
        minimum=1,
    )


def load_notify_before_minutes():
    return load_integer_setting(
        "SCHEDULER",
        "notify_before_minutes",
        DEFAULT_NOTIFY_BEFORE_MINUTES,
        minimum=0,
    )


def load_slack_webhook_url():
    """
    settingsテーブルの
    SLACK / webhook_url
    からIncoming Webhook URLを取得する。

    環境変数は使用しない。
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
                "SLACK",
                "webhook_url",
            ),
        )

        row = cursor.fetchone()

    finally:
        cursor.close()
        conn.close()

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
            "settingsテーブルの "
            "SLACK / webhook_url が空です。"
        )

    return webhook_url


# ============================================================
# Slack
# ============================================================

def notify_slack(message):
    webhook_url = load_slack_webhook_url()

    try:
        response = requests.post(
            webhook_url,
            json={
                "text": message,
            },
            timeout=10,
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


# ============================================================
# Google OAuth / Calendar Service
# ============================================================

_google_credentials = None
_google_service = None


def _write_text_atomic(
    path: Path,
    text: str,
    mode: int | None = None,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(text)
            f.flush()
            os.fsync(
                f.fileno()
            )

        if mode is not None:
            try:
                os.chmod(
                    temp_path,
                    mode,
                )
            except OSError:
                pass

        os.replace(
            temp_path,
            path,
        )

    except Exception:
        try:
            os.unlink(
                temp_path
            )
        except OSError:
            pass
        raise


def save_google_token(
    credentials: Credentials,
):
    _write_text_atomic(
        GOOGLE_TOKEN_FILE,
        credentials.to_json(),
        mode=0o600,
    )


def load_google_credentials():
    """
    Avelia本体で取得済みのGoogle OAuth tokenを使用する。
    worker自身は対話OAuthを開始しない。
    """

    global _google_credentials

    if _google_credentials is not None:
        credentials = _google_credentials

    else:
        if not GOOGLE_TOKEN_FILE.exists():
            raise RuntimeError(
                "Google Calendar OAuth token がありません。 "
                f"{GOOGLE_TOKEN_FILE} "
                "Avelia本体でGoogle Calendar認証を完了してください。"
            )

        try:
            credentials = (
                Credentials
                .from_authorized_user_file(
                    str(
                        GOOGLE_TOKEN_FILE
                    ),
                    GOOGLE_SCOPES,
                )
            )

        except Exception as e:
            raise RuntimeError(
                "Google Calendar tokenを読み込めません: "
                f"{e}"
            ) from e

        _google_credentials = credentials

    if (
        credentials.expired
        and credentials.refresh_token
    ):
        try:
            credentials.refresh(
                Request()
            )

            save_google_token(
                credentials
            )

        except Exception as e:
            _google_credentials = None

            raise RuntimeError(
                "Google Calendar tokenの更新に失敗しました: "
                f"{e}"
            ) from e

    if not credentials.valid:
        _google_credentials = None

        raise RuntimeError(
            "Google Calendar OAuth token が無効です。"
            " Avelia本体から再認証してください。"
        )

    return credentials


def get_google_calendar_service(
    force_rebuild=False,
):
    global _google_service
    global _google_credentials

    if force_rebuild:
        _google_service = None
        _google_credentials = None

    credentials = (
        load_google_credentials()
    )

    if _google_service is None:
        _google_service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    return _google_service


# ============================================================
# Google Calendar Event Helpers
# ============================================================

def parse_google_datetime(
    value: str,
) -> datetime:
    text = str(
        value
    ).strip()

    if text.endswith("Z"):
        text = (
            text[:-1]
            + "+00:00"
        )

    dt = datetime.fromisoformat(
        text
    )

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=ZoneInfo(
                GOOGLE_TIMEZONE
            )
        )

    return dt


def get_event_start(
    event: dict[str, Any],
):
    start = event.get(
        "start",
        {},
    )

    value = start.get(
        "dateTime"
    )

    if not value:
        return None

    return parse_google_datetime(
        value
    )


def get_event_end(
    event: dict[str, Any],
):
    end = event.get(
        "end",
        {},
    )

    value = end.get(
        "dateTime"
    )

    if not value:
        return None

    return parse_google_datetime(
        value
    )


def get_event_notification_key(
    event: dict[str, Any],
    start_dt: datetime,
) -> str:
    event_id = str(
        event.get(
            "id",
            "",
        )
    ).strip()

    recurring_event_id = str(
        event.get(
            "recurringEventId",
            "",
        )
    ).strip()

    original_start = (
        event.get(
            "originalStartTime",
            {},
        ).get(
            "dateTime"
        )
    )

    if original_start:
        occurrence = (
            parse_google_datetime(
                original_start
            )
            .isoformat()
        )
    else:
        occurrence = (
            start_dt.isoformat()
        )

    identity = (
        recurring_event_id
        or event_id
    )

    return (
        f"{identity}|"
        f"{occurrence}"
    )


def fetch_upcoming_google_events(
    now: datetime,
    notify_before_minutes: int,
    check_interval: int,
):
    """
    Google Calendarから通知対象候補を取得する。

    singleEvents=Trueにより
    recurring eventを個々の発生回へ展開する。
    """

    service = (
        get_google_calendar_service()
    )

    late_grace_seconds = max(
        check_interval * 2,
        60,
    )

    earliest_start = (
        now
        - timedelta(
            seconds=late_grace_seconds
        )
    )

    latest_start = (
        now
        + timedelta(
            minutes=notify_before_minutes
        )
    )

    time_min = (
        earliest_start.isoformat()
    )

    time_max = (
        latest_start
        + timedelta(
            seconds=2
        )
    ).isoformat()

    def _request_events(calendar_service):
        events = []
        page_token = None

        while True:
            response = (
                calendar_service
                .events()
                .list(
                    calendarId=GOOGLE_CALENDAR_ID,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    showDeleted=False,
                    maxResults=2500,
                    pageToken=page_token,
                    timeZone=GOOGLE_TIMEZONE,
                )
                .execute()
            )

            events.extend(
                response.get(
                    "items",
                    [],
                )
            )

            page_token = (
                response.get(
                    "nextPageToken"
                )
            )

            if not page_token:
                break

        return events

    try:
        candidates = (
            _request_events(
                service
            )
        )

    except HttpError as e:
        status = getattr(
            e.resp,
            "status",
            None,
        )

        if status == 401:
            service = (
                get_google_calendar_service(
                    force_rebuild=True
                )
            )

            candidates = (
                _request_events(
                    service
                )
            )

        else:
            raise

    due = []

    for event in candidates:
        if event.get(
            "status"
        ) == "cancelled":
            continue

        start_dt = (
            get_event_start(
                event
            )
        )

        # 終日予定は通知対象外。
        if start_dt is None:
            continue

        start_local = (
            start_dt.astimezone(
                ZoneInfo(
                    GOOGLE_TIMEZONE
                )
            )
        )

        if (
            start_local
            < earliest_start
        ):
            continue

        if (
            start_local
            > latest_start
        ):
            continue

        due.append(
            event
        )

    due.sort(
        key=lambda event: (
            get_event_start(
                event
            )
            or now
        )
    )

    return due


# ============================================================
# Notification State
# ============================================================

def load_notify_state():
    if not NOTIFY_STATE_FILE.exists():
        return {
            "version": 1,
            "notified": {},
        }

    try:
        with NOTIFY_STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(
                f
            )

    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as e:
        raise RuntimeError(
            "通知状態ファイルを読み込めません: "
            f"{e}"
        ) from e

    if not isinstance(
        data,
        dict,
    ):
        data = {}

    notified = data.get(
        "notified",
        {},
    )

    if not isinstance(
        notified,
        dict,
    ):
        notified = {}

    return {
        "version": 1,
        "notified": notified,
    }


def save_notify_state(
    state,
):
    text = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

    _write_text_atomic(
        NOTIFY_STATE_FILE,
        text + "\n",
        mode=0o600,
    )


def prune_notify_state(
    state,
    now: datetime,
):
    notified = state.get(
        "notified",
        {},
    )

    threshold = (
        now
        - timedelta(
            days=STATE_RETENTION_DAYS
        )
    )

    cleaned = {}

    for key, value in notified.items():
        try:
            notified_at = (
                parse_google_datetime(
                    str(value)
                )
            )

            notified_at = (
                notified_at.astimezone(
                    ZoneInfo(
                        GOOGLE_TIMEZONE
                    )
                )
            )

        except Exception:
            continue

        if notified_at >= threshold:
            cleaned[
                key
            ] = value

    state[
        "notified"
    ] = cleaned


def already_notified(
    state,
    key,
):
    return (
        key
        in state.get(
            "notified",
            {},
        )
    )


def mark_notified(
    state,
    key,
    now: datetime,
):
    state.setdefault(
        "notified",
        {},
    )[
        key
    ] = now.isoformat()


# ============================================================
# Slack Message
# ============================================================

def build_google_calendar_message(
    event,
    notify_before_minutes,
):
    tz = ZoneInfo(
        GOOGLE_TIMEZONE
    )

    start_dt = (
        get_event_start(
            event
        )
    )

    end_dt = (
        get_event_end(
            event
        )
    )

    title = str(
        event.get(
            "summary",
            "(タイトルなし)",
        )
    ).strip()

    description = str(
        event.get(
            "description",
            "",
        )
        or ""
    ).strip()

    location = str(
        event.get(
            "location",
            "",
        )
        or ""
    ).strip()

    html_link = str(
        event.get(
            "htmlLink",
            "",
        )
        or ""
    ).strip()

    lines = [
        "[アヴェリア Google Calendar]",
        title,
    ]

    if description:
        lines.extend(
            [
                "",
                description,
            ]
        )

    if start_dt is not None:
        start_local = (
            start_dt.astimezone(
                tz
            )
        )

        lines.extend(
            [
                "",
                "日付: "
                f"{start_local:%Y-%m-%d}",
                "開始時刻: "
                f"{start_local:%H:%M}",
            ]
        )

    if end_dt is not None:
        end_local = (
            end_dt.astimezone(
                tz
            )
        )

        lines.append(
            "終了時刻: "
            f"{end_local:%H:%M}"
        )

    if location:
        lines.append(
            "場所: "
            f"{location}"
        )

    lines.append(
        f"{notify_before_minutes}分前通知"
    )

    if html_link:
        lines.extend(
            [
                "",
                html_link,
            ]
        )

    return "\n".join(
        lines
    )


# ============================================================
# Notification Processing
# ============================================================

def process_google_calendar(
    notify_before_minutes,
    check_interval,
):
    tz = ZoneInfo(
        GOOGLE_TIMEZONE
    )

    now = datetime.now(
        tz
    )

    state = (
        load_notify_state()
    )

    before_prune = dict(
        state.get(
            "notified",
            {},
        )
    )

    prune_notify_state(
        state,
        now,
    )

    state_changed = (
        before_prune
        != state.get(
            "notified",
            {},
        )
    )

    events = (
        fetch_upcoming_google_events(
            now=now,
            notify_before_minutes=(
                notify_before_minutes
            ),
            check_interval=(
                check_interval
            ),
        )
    )

    for event in events:
        start_dt = (
            get_event_start(
                event
            )
        )

        if start_dt is None:
            continue

        key = (
            get_event_notification_key(
                event,
                start_dt,
            )
        )

        if already_notified(
            state,
            key,
        ):
            continue

        title = str(
            event.get(
                "summary",
                "(タイトルなし)",
            )
        ).strip()

        try:
            message = (
                build_google_calendar_message(
                    event,
                    notify_before_minutes,
                )
            )

            notify_slack(
                message
            )

            mark_notified(
                state,
                key,
                now,
            )

            # Slack成功後すぐ保存。
            # workerが直後に落ちても二重通知を避ける。
            save_notify_state(
                state
            )

            state_changed = False

            print(
                "[Google Calendar] 通知完了 "
                f"event={event.get('id')} "
                f"title={title}",
                flush=True,
            )

        except Exception as e:
            print(
                "[Google Calendar] 通知失敗 "
                f"event={event.get('id')} "
                f"title={title} "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

    if state_changed:
        save_notify_state(
            state
        )


# ============================================================
# Startup Check
# ============================================================

def startup_check():
    ensure_scheduler_settings()

    if not GOOGLE_TOKEN_FILE.exists():
        raise RuntimeError(
            "Google Calendar OAuth token がありません。\n"
            f"必要ファイル: {GOOGLE_TOKEN_FILE}\n"
            "先にAvelia本体でGoogle Calendar認証を行ってください。"
        )

    # OAuth token確認。期限切れならrefreshして保存。
    get_google_calendar_service()

    # Slack設定確認。
    load_slack_webhook_url()


# ============================================================
# Main
# ============================================================

def run():
    """
    Avelia Google Calendar通知サービス。
    """

    startup_check()

    print(
        "[Avelia Schedule] "
        "Google Calendar通知サービス開始",
        flush=True,
    )

    print(
        "[Avelia Schedule] "
        f"calendar={GOOGLE_CALENDAR_ID} "
        f"timezone={GOOGLE_TIMEZONE}",
        flush=True,
    )

    while True:
        try:
            check_interval = (
                load_scheduler_interval()
            )

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "check_interval取得エラー "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

            check_interval = (
                DEFAULT_CHECK_INTERVAL
            )

        try:
            notify_before_minutes = (
                load_notify_before_minutes()
            )

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "notify_before_minutes取得エラー "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

            notify_before_minutes = (
                DEFAULT_NOTIFY_BEFORE_MINUTES
            )

        try:
            process_google_calendar(
                notify_before_minutes=(
                    notify_before_minutes
                ),
                check_interval=(
                    check_interval
                ),
            )

        except HttpError as e:
            print(
                "[Avelia Schedule] "
                "Google Calendar APIエラー "
                f"{e}",
                flush=True,
            )

        except Exception as e:
            print(
                "[Avelia Schedule] "
                "Google Calendar処理エラー "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

        time.sleep(
            check_interval
        )


if __name__ == "__main__":
    run()
