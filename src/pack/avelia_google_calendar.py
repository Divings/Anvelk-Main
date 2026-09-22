from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================
# Avelia Google Calendar
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/calendar"
]

DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_CALENDAR_ID = "primary"


# ============================================================
# Avelia DATA_DIR
# ============================================================

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


DEFAULT_CREDENTIALS_FILE = (
    DATA_DIR
    / "google_calendar_credentials.json"
)

DEFAULT_TOKEN_FILE = (
    DATA_DIR
    / "google_calendar_token.json"
)


# ============================================================
# Calendar Event
# ============================================================

@dataclass
class CalendarEvent:
    id: str
    title: str
    start: str
    end: str

    description: str = ""
    location: str = ""
    html_link: str = ""
    status: str = ""

    all_day: bool = False

    calendar_id: str = DEFAULT_CALENDAR_ID

    @classmethod
    def from_google(
        cls,
        event: dict[str, Any],
        calendar_id: str = DEFAULT_CALENDAR_ID,
    ) -> "CalendarEvent":

        start_data = event.get(
            "start",
            {},
        )

        end_data = event.get(
            "end",
            {},
        )

        all_day = (
            "date" in start_data
            and "dateTime" not in start_data
        )

        start = (
            start_data.get("dateTime")
            or start_data.get("date")
            or ""
        )

        end = (
            end_data.get("dateTime")
            or end_data.get("date")
            or ""
        )

        return cls(
            id=event.get(
                "id",
                "",
            ),

            title=event.get(
                "summary",
                "(タイトルなし)",
            ),

            start=start,
            end=end,

            description=event.get(
                "description",
                "",
            ),

            location=event.get(
                "location",
                "",
            ),

            html_link=event.get(
                "htmlLink",
                "",
            ),

            status=event.get(
                "status",
                "",
            ),

            all_day=all_day,

            calendar_id=calendar_id,
        )

    def to_dict(
        self,
    ) -> dict[str, Any]:

        return asdict(
            self
        )


# ============================================================
# Google Calendar Manager
# ============================================================

class GoogleCalendarManager:

    def __init__(
        self,
        credentials_file: str | Path = DEFAULT_CREDENTIALS_FILE,
        token_file: str | Path = DEFAULT_TOKEN_FILE,
        calendar_id: str = DEFAULT_CALENDAR_ID,
        timezone: str = DEFAULT_TIMEZONE,
    ):

        self.credentials_file = Path(
            credentials_file
        )

        self.token_file = Path(
            token_file
        )

        self.calendar_id = (
            calendar_id
        )

        self.timezone_name = (
            timezone
        )

        self.tz = ZoneInfo(
            timezone
        )

        self.credentials: Optional[
            Credentials
        ] = None

        self.service = None


    # ========================================================
    # OAuth Authentication
    # ========================================================

    def authenticate(
        self,
    ):

        creds = None

        # ----------------------------------------------------
        # token読み込み
        # ----------------------------------------------------

        if self.token_file.exists():

            try:

                creds = (
                    Credentials
                    .from_authorized_user_file(
                        str(
                            self.token_file
                        ),
                        SCOPES,
                    )
                )

            except Exception as e:

                print(
                    "[GoogleCalendar] "
                    "token読み込み失敗: "
                    f"{e}",
                    file=sys.stderr,
                )

                creds = None


        # ----------------------------------------------------
        # token更新
        # ----------------------------------------------------

        if (
            creds
            and creds.expired
            and creds.refresh_token
        ):

            try:

                creds.refresh(
                    Request()
                )

            except Exception as e:

                print(
                    "[GoogleCalendar] "
                    "token更新失敗: "
                    f"{e}",
                    file=sys.stderr,
                )

                creds = None


        # ----------------------------------------------------
        # 初回OAuth
        # ----------------------------------------------------

        if (
            not creds
            or not creds.valid
        ):

            if not self.credentials_file.exists():

                raise FileNotFoundError(
                    "\n"
                    "Google Calendar OAuth情報がありません。\n\n"
                    "以下にcredentials JSONを置いてください。\n"
                    f"{self.credentials_file}\n"
                )

            flow = (
                InstalledAppFlow
                .from_client_secrets_file(
                    str(
                        self.credentials_file
                    ),
                    SCOPES,
                )
            )

            creds = (
                flow.run_local_server(
                    port=0,
                    open_browser=True,
                )
            )


        # ----------------------------------------------------
        # token保存
        # ----------------------------------------------------

        self.token_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.token_file.write_text(
            creds.to_json(),
            encoding="utf-8",
        )

        self.credentials = creds


        # ----------------------------------------------------
        # API client
        # ----------------------------------------------------

        self.service = build(
            "calendar",
            "v3",
            credentials=creds,
            cache_discovery=False,
        )

        return self.service


    # ========================================================
    # Service Check
    # ========================================================

    def ensure_service(
        self,
    ):

        if self.service is None:

            self.authenticate()


    # ========================================================
    # Date / Time
    # ========================================================

    def normalize_datetime(
        self,
        value: datetime | str,
    ) -> datetime:

        if isinstance(
            value,
            datetime,
        ):

            dt = value

        elif isinstance(
            value,
            str,
        ):

            value = value.strip()

            if (
                " " in value
                and "T" not in value
            ):

                value = value.replace(
                    " ",
                    "T",
                    1,
                )

            dt = datetime.fromisoformat(
                value
            )

        else:

            raise TypeError(
                "日時は datetime または "
                "ISO形式文字列で指定してください"
            )


        if dt.tzinfo is None:

            dt = dt.replace(
                tzinfo=self.tz
            )

        return dt


    def normalize_date(
        self,
        value: date | datetime | str,
    ) -> date:

        if isinstance(
            value,
            datetime,
        ):

            return value.date()


        if isinstance(
            value,
            date,
        ):

            return value


        if isinstance(
            value,
            str,
        ):

            return date.fromisoformat(
                value
            )


        raise TypeError(
            "日付は date / datetime / "
            "YYYY-MM-DD形式で指定してください"
        )


    # ========================================================
    # List Events
    # ========================================================

    def list_events(
        self,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        max_results: int = 100,
        query: str | None = None,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:

        self.ensure_service()

        cal_id = (
            calendar_id
            or self.calendar_id
        )


        if start is None:

            start_dt = datetime.now(
                self.tz
            )

        else:

            start_dt = (
                self.normalize_datetime(
                    start
                )
            )


        if end is None:

            end_dt = (
                start_dt
                + timedelta(
                    days=30
                )
            )

        else:

            end_dt = (
                self.normalize_datetime(
                    end
                )
            )


        params = {
            "calendarId": cal_id,

            "timeMin": (
                start_dt.isoformat()
            ),

            "timeMax": (
                end_dt.isoformat()
            ),

            "singleEvents": True,

            "orderBy": "startTime",

            "maxResults": max_results,
        }


        if query:

            params["q"] = query


        try:

            result = (
                self.service
                .events()
                .list(
                    **params
                )
                .execute()
            )

            items = result.get(
                "items",
                [],
            )


            return [

                CalendarEvent.from_google(
                    event,
                    calendar_id=cal_id,
                )

                for event in items

                if event.get(
                    "status"
                ) != "cancelled"
            ]


        except HttpError as e:

            raise RuntimeError(
                "Google Calendarの予定取得失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # Today
    # ========================================================

    def today_events(
        self,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:

        now = datetime.now(
            self.tz
        )

        start = now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        end = (
            start
            + timedelta(
                days=1
            )
        )


        return self.list_events(
            start=start,
            end=end,
            calendar_id=calendar_id,
        )


    # ========================================================
    # Tomorrow
    # ========================================================

    def tomorrow_events(
        self,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:

        now = datetime.now(
            self.tz
        )

        start = (
            now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            + timedelta(
                days=1
            )
        )

        end = (
            start
            + timedelta(
                days=1
            )
        )


        return self.list_events(
            start=start,
            end=end,
            calendar_id=calendar_id,
        )


    # ========================================================
    # Events On Date
    # ========================================================

    def events_on_date(
        self,
        target_date: date | datetime | str,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:

        d = self.normalize_date(
            target_date
        )

        start = datetime(
            d.year,
            d.month,
            d.day,
            0,
            0,
            0,
            tzinfo=self.tz,
        )

        end = (
            start
            + timedelta(
                days=1
            )
        )


        return self.list_events(
            start=start,
            end=end,
            calendar_id=calendar_id,
        )


    # ========================================================
    # Get Event
    # ========================================================

    def get_event(
        self,
        event_id: str,
        calendar_id: str | None = None,
    ) -> CalendarEvent:

        self.ensure_service()

        cal_id = (
            calendar_id
            or self.calendar_id
        )


        try:

            event = (
                self.service
                .events()
                .get(
                    calendarId=cal_id,
                    eventId=event_id,
                )
                .execute()
            )


            return (
                CalendarEvent
                .from_google(
                    event,
                    calendar_id=cal_id,
                )
            )


        except HttpError as e:

            raise RuntimeError(
                "イベント取得失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # Search
    # ========================================================

    def search_events(
        self,
        keyword: str,
        days_before: int = 30,
        days_after: int = 365,
        max_results: int = 100,
        calendar_id: str | None = None,
    ) -> list[CalendarEvent]:

        now = datetime.now(
            self.tz
        )


        return self.list_events(

            start=(
                now
                - timedelta(
                    days=days_before
                )
            ),

            end=(
                now
                + timedelta(
                    days=days_after
                )
            ),

            max_results=max_results,

            query=keyword,

            calendar_id=calendar_id,
        )


    # ========================================================
    # Create Event
    # ========================================================

    def create_event(
        self,
        title: str,
        start: datetime | str,
        end: datetime | str | None = None,
        duration_minutes: int = 60,
        description: str = "",
        location: str = "",
        calendar_id: str | None = None,
    ) -> CalendarEvent:

        self.ensure_service()

        cal_id = (
            calendar_id
            or self.calendar_id
        )


        start_dt = (
            self.normalize_datetime(
                start
            )
        )


        if end is None:

            end_dt = (
                start_dt
                + timedelta(
                    minutes=duration_minutes
                )
            )

        else:

            end_dt = (
                self.normalize_datetime(
                    end
                )
            )


        if end_dt <= start_dt:

            raise ValueError(
                "終了時刻は開始時刻より後にしてください"
            )


        body = {

            "summary": title,

            "description": (
                description
            ),

            "location": (
                location
            ),

            "start": {

                "dateTime": (
                    start_dt.isoformat()
                ),

                "timeZone": (
                    self.timezone_name
                ),
            },

            "end": {

                "dateTime": (
                    end_dt.isoformat()
                ),

                "timeZone": (
                    self.timezone_name
                ),
            },
        }


        try:

            event = (
                self.service
                .events()
                .insert(
                    calendarId=cal_id,
                    body=body,
                )
                .execute()
            )


            return (
                CalendarEvent
                .from_google(
                    event,
                    calendar_id=cal_id,
                )
            )


        except HttpError as e:

            raise RuntimeError(
                "イベント作成失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # All Day Event
    # ========================================================

    def create_all_day_event(
        self,
        title: str,
        start_date: date | datetime | str,
        end_date: date | datetime | str | None = None,
        description: str = "",
        location: str = "",
        calendar_id: str | None = None,
    ) -> CalendarEvent:

        self.ensure_service()

        cal_id = (
            calendar_id
            or self.calendar_id
        )


        start_d = (
            self.normalize_date(
                start_date
            )
        )


        if end_date is None:

            end_d = (
                start_d
                + timedelta(
                    days=1
                )
            )

        else:

            end_d = (
                self.normalize_date(
                    end_date
                )
            )


        if end_d <= start_d:

            raise ValueError(
                "終了日は開始日より後にしてください"
            )


        body = {

            "summary": title,

            "description": (
                description
            ),

            "location": (
                location
            ),

            "start": {

                "date": (
                    start_d.isoformat()
                )
            },

            "end": {

                "date": (
                    end_d.isoformat()
                )
            },
        }


        try:

            event = (
                self.service
                .events()
                .insert(
                    calendarId=cal_id,
                    body=body,
                )
                .execute()
            )


            return (
                CalendarEvent
                .from_google(
                    event,
                    calendar_id=cal_id,
                )
            )


        except HttpError as e:

            raise RuntimeError(
                "終日予定作成失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # Update Event
    # ========================================================

    def update_event(
        self,
        event_id: str,
        title: str | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
    ) -> CalendarEvent:

        self.ensure_service()

        cal_id = (
            calendar_id
            or self.calendar_id
        )


        body: dict[str, Any] = {}


        if title is not None:

            body["summary"] = title


        if description is not None:

            body["description"] = (
                description
            )


        if location is not None:

            body["location"] = (
                location
            )


        if start is not None:

            start_dt = (
                self.normalize_datetime(
                    start
                )
            )

            body["start"] = {

                "dateTime": (
                    start_dt.isoformat()
                ),

                "timeZone": (
                    self.timezone_name
                ),
            }


        if end is not None:

            end_dt = (
                self.normalize_datetime(
                    end
                )
            )

            body["end"] = {

                "dateTime": (
                    end_dt.isoformat()
                ),

                "timeZone": (
                    self.timezone_name
                ),
            }


        if not body:

            raise ValueError(
                "変更内容がありません"
            )


        try:

            event = (
                self.service
                .events()
                .patch(
                    calendarId=cal_id,
                    eventId=event_id,
                    body=body,
                )
                .execute()
            )


            return (
                CalendarEvent
                .from_google(
                    event,
                    calendar_id=cal_id,
                )
            )


        except HttpError as e:

            raise RuntimeError(
                "イベント更新失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # Delete Event
    # ========================================================

    def delete_event(
        self,
        event_id: str,
        calendar_id: str | None = None,
    ) -> bool:

        self.ensure_service()

        cal_id = (
            calendar_id
            or self.calendar_id
        )


        try:

            (
                self.service
                .events()
                .delete(
                    calendarId=cal_id,
                    eventId=event_id,
                )
                .execute()
            )

            return True


        except HttpError as e:

            raise RuntimeError(
                "イベント削除失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # Delete By Exact Title
    # ========================================================

    def delete_event_by_title(
        self,
        title: str,
        calendar_id: str | None = None,
    ) -> list[str]:

        events = self.search_events(
            title,
            calendar_id=calendar_id,
        )

        deleted_ids = []


        for event in events:

            if (
                event.title.strip()
                == title.strip()
            ):

                self.delete_event(
                    event.id,
                    calendar_id=calendar_id,
                )

                deleted_ids.append(
                    event.id
                )


        return deleted_ids


    # ========================================================
    # Calendar List
    # ========================================================

    def list_calendars(
        self,
    ) -> list[dict[str, Any]]:

        self.ensure_service()

        calendars: list[
            dict[str, Any]
        ] = []

        page_token = None


        try:

            while True:

                result = (
                    self.service
                    .calendarList()
                    .list(
                        pageToken=page_token
                    )
                    .execute()
                )


                for item in result.get(
                    "items",
                    [],
                ):

                    calendars.append({

                        "id": (
                            item.get(
                                "id"
                            )
                        ),

                        "summary": (
                            item.get(
                                "summary"
                            )
                        ),

                        "primary": (
                            item.get(
                                "primary",
                                False,
                            )
                        ),

                        "access_role": (
                            item.get(
                                "accessRole"
                            )
                        ),

                        "timezone": (
                            item.get(
                                "timeZone"
                            )
                        ),
                    })


                page_token = (
                    result.get(
                        "nextPageToken"
                    )
                )


                if not page_token:
                    break


            return calendars


        except HttpError as e:

            raise RuntimeError(
                "カレンダー一覧取得失敗: "
                f"{e}"
            ) from e


    # ========================================================
    # JSON
    # ========================================================

    def events_as_json(
        self,
        events: list[CalendarEvent],
        indent: int = 2,
    ) -> str:

        return json.dumps(
            [
                event.to_dict()
                for event in events
            ],
            ensure_ascii=False,
            indent=indent,
        )


    # ========================================================
    # Human Readable Format
    # ========================================================

    def format_events(
        self,
        events: list[CalendarEvent],
    ) -> str:

        if not events:

            return "予定はありません。"


        lines = []


        for event in events:

            if event.all_day:

                lines.append(
                    f"・{event.start} "
                    f"{event.title}（終日）"
                )

            else:

                try:

                    start = (
                        datetime
                        .fromisoformat(
                            event.start
                        )
                        .astimezone(
                            self.tz
                        )
                    )

                    end = (
                        datetime
                        .fromisoformat(
                            event.end
                        )
                        .astimezone(
                            self.tz
                        )
                    )


                    lines.append(
                        "・"
                        f"{start:%Y/%m/%d %H:%M}"
                        "〜"
                        f"{end:%H:%M} "
                        f"{event.title}"
                    )


                except Exception:

                    lines.append(
                        f"・{event.start} "
                        f"{event.title}"
                    )


            if event.location:

                lines.append(
                    "  場所: "
                    f"{event.location}"
                )


            if event.description:

                lines.append(
                    "  詳細: "
                    f"{event.description}"
                )


        return "\n".join(
            lines
        )


# ============================================================
# 共通インスタンス
# ============================================================

calendar_manager = GoogleCalendarManager(
    credentials_file=(
        DEFAULT_CREDENTIALS_FILE
    ),
    token_file=(
        DEFAULT_TOKEN_FILE
    ),
    calendar_id=(
        DEFAULT_CALENDAR_ID
    ),
    timezone=(
        DEFAULT_TIMEZONE
    ),
)


# ============================================================
# Avelia Tool Interface
# ============================================================

def google_calendar_tool(
    action: str,
    **kwargs,
) -> dict[str, Any]:

    try:

        # ----------------------------------------------------
        # today
        # ----------------------------------------------------

        if action == "today":

            events = (
                calendar_manager
                .today_events()
            )

            return {

                "success": True,

                "events": [
                    event.to_dict()
                    for event in events
                ],
            }


        # ----------------------------------------------------
        # tomorrow
        # ----------------------------------------------------

        elif action == "tomorrow":

            events = (
                calendar_manager
                .tomorrow_events()
            )

            return {

                "success": True,

                "events": [
                    event.to_dict()
                    for event in events
                ],
            }


        # ----------------------------------------------------
        # date
        # ----------------------------------------------------

        elif action == "date":

            events = (
                calendar_manager
                .events_on_date(
                    kwargs[
                        "date"
                    ]
                )
            )

            return {

                "success": True,

                "events": [
                    event.to_dict()
                    for event in events
                ],
            }


        # ----------------------------------------------------
        # search
        # ----------------------------------------------------

        elif action == "search":

            events = (
                calendar_manager
                .search_events(
                    kwargs[
                        "keyword"
                    ],
                    days_before=(
                        kwargs.get(
                            "days_before",
                            30,
                        )
                    ),
                    days_after=(
                        kwargs.get(
                            "days_after",
                            365,
                        )
                    ),
                )
            )

            return {

                "success": True,

                "events": [
                    event.to_dict()
                    for event in events
                ],
            }


        # ----------------------------------------------------
        # get
        # ----------------------------------------------------

        elif action == "get":

            event = (
                calendar_manager
                .get_event(
                    kwargs[
                        "event_id"
                    ]
                )
            )

            return {

                "success": True,

                "event": (
                    event.to_dict()
                ),
            }


        # ----------------------------------------------------
        # create
        # ----------------------------------------------------

        elif action == "create":

            event = (
                calendar_manager
                .create_event(

                    title=kwargs[
                        "title"
                    ],

                    start=kwargs[
                        "start"
                    ],

                    end=kwargs.get(
                        "end"
                    ),

                    duration_minutes=(
                        kwargs.get(
                            "duration_minutes",
                            60,
                        )
                    ),

                    description=(
                        kwargs.get(
                            "description",
                            "",
                        )
                    ),

                    location=(
                        kwargs.get(
                            "location",
                            "",
                        )
                    ),
                )
            )


            return {

                "success": True,

                "event": (
                    event.to_dict()
                ),
            }


        # ----------------------------------------------------
        # create_all_day
        # ----------------------------------------------------

        elif action == "create_all_day":

            event = (
                calendar_manager
                .create_all_day_event(

                    title=kwargs[
                        "title"
                    ],

                    start_date=kwargs[
                        "date"
                    ],

                    end_date=kwargs.get(
                        "end_date"
                    ),

                    description=(
                        kwargs.get(
                            "description",
                            "",
                        )
                    ),

                    location=(
                        kwargs.get(
                            "location",
                            "",
                        )
                    ),
                )
            )


            return {

                "success": True,

                "event": (
                    event.to_dict()
                ),
            }


        # ----------------------------------------------------
        # update
        # ----------------------------------------------------

        elif action == "update":

            event = (
                calendar_manager
                .update_event(

                    event_id=kwargs[
                        "event_id"
                    ],

                    title=kwargs.get(
                        "title"
                    ),

                    start=kwargs.get(
                        "start"
                    ),

                    end=kwargs.get(
                        "end"
                    ),

                    description=(
                        kwargs.get(
                            "description"
                        )
                    ),

                    location=(
                        kwargs.get(
                            "location"
                        )
                    ),
                )
            )


            return {

                "success": True,

                "event": (
                    event.to_dict()
                ),
            }


        # ----------------------------------------------------
        # delete
        # ----------------------------------------------------

        elif action == "delete":

            event_id = kwargs[
                "event_id"
            ]

            calendar_manager.delete_event(
                event_id
            )


            return {

                "success": True,

                "deleted_event_id": (
                    event_id
                ),
            }


        # ----------------------------------------------------
        # delete_by_title
        # ----------------------------------------------------

        elif action == "delete_by_title":

            deleted_ids = (
                calendar_manager
                .delete_event_by_title(
                    kwargs[
                        "title"
                    ]
                )
            )


            return {

                "success": True,

                "deleted_count": (
                    len(
                        deleted_ids
                    )
                ),

                "deleted_event_ids": (
                    deleted_ids
                ),
            }


        # ----------------------------------------------------
        # calendars
        # ----------------------------------------------------

        elif action == "calendars":

            calendars = (
                calendar_manager
                .list_calendars()
            )


            return {

                "success": True,

                "calendars": (
                    calendars
                ),
            }


        else:

            return {

                "success": False,

                "error": (
                    "不明なaction: "
                    f"{action}"
                ),
            }


    except Exception as e:

        return {

            "success": False,

            "error": (
                str(
                    e
                )
            ),
        }


# ============================================================
# Avelia簡易呼び出し
# ============================================================

def get_today_schedule(
) -> str:

    events = (
        calendar_manager
        .today_events()
    )

    return (
        calendar_manager
        .format_events(
            events
        )
    )


def get_tomorrow_schedule(
) -> str:

    events = (
        calendar_manager
        .tomorrow_events()
    )

    return (
        calendar_manager
        .format_events(
            events
        )
    )


def get_date_schedule(
    target_date: str,
) -> str:

    events = (
        calendar_manager
        .events_on_date(
            target_date
        )
    )

    return (
        calendar_manager
        .format_events(
            events
        )
    )


# ============================================================
# CLI
# ============================================================

def cli():

    parser = argparse.ArgumentParser(
        description=(
            "Avelia Google Calendar"
        )
    )


    sub = parser.add_subparsers(
        dest="command"
    )


    # ========================================================
    # auth
    # ========================================================

    sub.add_parser(
        "auth",
        help="Google OAuth認証",
    )


    # ========================================================
    # today
    # ========================================================

    sub.add_parser(
        "today",
        help="今日の予定",
    )


    # ========================================================
    # tomorrow
    # ========================================================

    sub.add_parser(
        "tomorrow",
        help="明日の予定",
    )


    # ========================================================
    # date
    # ========================================================

    date_parser = (
        sub.add_parser(
            "date",
            help="指定日の予定",
        )
    )

    date_parser.add_argument(
        "date",
        help="YYYY-MM-DD",
    )


    # ========================================================
    # search
    # ========================================================

    search_parser = (
        sub.add_parser(
            "search",
            help="予定検索",
        )
    )

    search_parser.add_argument(
        "keyword"
    )


    # ========================================================
    # add
    # ========================================================

    add_parser = (
        sub.add_parser(
            "add",
            help="予定追加",
        )
    )

    add_parser.add_argument(
        "title"
    )

    add_parser.add_argument(
        "start"
    )

    add_parser.add_argument(
        "--end"
    )

    add_parser.add_argument(
        "--minutes",
        type=int,
        default=60,
    )

    add_parser.add_argument(
        "--description",
        default="",
    )

    add_parser.add_argument(
        "--location",
        default="",
    )


    # ========================================================
    # allday
    # ========================================================

    all_day_parser = (
        sub.add_parser(
            "allday",
            help="終日予定追加",
        )
    )

    all_day_parser.add_argument(
        "title"
    )

    all_day_parser.add_argument(
        "date"
    )


    # ========================================================
    # delete
    # ========================================================

    delete_parser = (
        sub.add_parser(
            "delete",
            help="予定削除",
        )
    )

    delete_parser.add_argument(
        "event_id"
    )


    # ========================================================
    # calendars
    # ========================================================

    sub.add_parser(
        "calendars",
        help="カレンダー一覧",
    )


    args = parser.parse_args()


    # ========================================================
    # Execute
    # ========================================================

    if args.command == "auth":

        calendar_manager.authenticate()

        print(
            "Google Calendar認証成功"
        )


    elif args.command == "today":

        print(
            get_today_schedule()
        )


    elif args.command == "tomorrow":

        print(
            get_tomorrow_schedule()
        )


    elif args.command == "date":

        print(
            get_date_schedule(
                args.date
            )
        )


    elif args.command == "search":

        result = (
            google_calendar_tool(
                "search",
                keyword=args.keyword,
            )
        )

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


    elif args.command == "add":

        result = (
            google_calendar_tool(

                "create",

                title=args.title,

                start=args.start,

                end=args.end,

                duration_minutes=(
                    args.minutes
                ),

                description=(
                    args.description
                ),

                location=(
                    args.location
                ),
            )
        )


        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


    elif args.command == "allday":

        result = (
            google_calendar_tool(

                "create_all_day",

                title=args.title,

                date=args.date,
            )
        )


        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


    elif args.command == "delete":

        result = (
            google_calendar_tool(

                "delete",

                event_id=(
                    args.event_id
                ),
            )
        )


        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


    elif args.command == "calendars":

        result = (
            google_calendar_tool(
                "calendars"
            )
        )


        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


    else:

        parser.print_help()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    cli()