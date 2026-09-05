import json
import os

from calendar_manager import CalendarManager


calendar = CalendarManager(
    host=os.getenv(
        "AVELIA_DB_HOST",
        "localhost",
    ),

    user=os.getenv(
        "AVELIA_DB_USER",
        "avelia",
    ),

    password=os.getenv(
        "AVELIA_DB_PASSWORD",
        "",
    ),

    database=os.getenv(
        "AVELIA_DB_NAME",
        "avelia",
    ),

    port=int(
        os.getenv(
            "AVELIA_DB_PORT",
            "3306",
        )
    ),
)


# =========================================================
# Tool本体
# =========================================================


def tool_calendar_add_once(
    title,
    scheduled_date,
    start_time=None,
    end_time=None,
    description=None,
):
    return calendar.add_once(
        title=title,
        scheduled_date=scheduled_date,
        start_time=start_time,
        end_time=end_time,
        description=description,
        source="avelia",
    )


def tool_calendar_add_weekly(
    title,
    weekdays,
    start_time=None,
    end_time=None,
    description=None,
):
    return calendar.add_weekly(
        title=title,
        weekdays=weekdays,
        start_time=start_time,
        end_time=end_time,
        description=description,
        source="avelia",
    )


def tool_calendar_today():
    return calendar.get_today_schedules(
        unfinished_only=True
    )


def tool_calendar_date(
    scheduled_date,
):
    return calendar.get_date_schedules(
        target_date=scheduled_date,
        unfinished_only=True,
    )


def tool_calendar_complete_once(
    schedule_id,
):
    return calendar.complete_once(
        int(schedule_id)
    )


def tool_calendar_complete_weekly(
    schedule_id,
    scheduled_date=None,
):
    return calendar.complete_weekly(
        schedule_id=int(schedule_id),
        scheduled_date=scheduled_date,
    )


def tool_calendar_list_once():
    return calendar.list_once()


def tool_calendar_list_weekly():
    return calendar.list_weekly()


def tool_calendar_delete_once(
    schedule_id,
):
    return calendar.delete_once(
        int(schedule_id)
    )


def tool_calendar_delete_weekly(
    schedule_id,
):
    return calendar.delete_weekly(
        int(schedule_id)
    )


def tool_calendar_weekly_enable(
    schedule_id,
):
    return calendar.set_weekly_enabled(
        schedule_id=int(schedule_id),
        enabled=True,
    )


def tool_calendar_weekly_disable(
    schedule_id,
):
    return calendar.set_weekly_enabled(
        schedule_id=int(schedule_id),
        enabled=False,
    )


def tool_calendar_import_csv(
    path,
):
    return calendar.import_csv(
        csv_path=path
    )


# =========================================================
# Dispatcher
# =========================================================

TOOL_FUNCTIONS = {
    "calendar_add_once":
        tool_calendar_add_once,

    "calendar_add_weekly":
        tool_calendar_add_weekly,

    "calendar_today":
        tool_calendar_today,

    "calendar_date":
        tool_calendar_date,

    "calendar_complete_once":
        tool_calendar_complete_once,

    "calendar_complete_weekly":
        tool_calendar_complete_weekly,

    "calendar_list_once":
        tool_calendar_list_once,

    "calendar_list_weekly":
        tool_calendar_list_weekly,

    "calendar_delete_once":
        tool_calendar_delete_once,

    "calendar_delete_weekly":
        tool_calendar_delete_weekly,

    "calendar_weekly_enable":
        tool_calendar_weekly_enable,

    "calendar_weekly_disable":
        tool_calendar_weekly_disable,

    "calendar_import_csv":
        tool_calendar_import_csv,
}


def dispatch_calendar_tool(
    tool_name,
    arguments=None,
):
    """
    OpenAI側から来たtool_nameとargumentsを
    実際のPython関数へ振り分ける。
    """

    if arguments is None:
        arguments = {}

    function = TOOL_FUNCTIONS.get(
        tool_name
    )

    if function is None:
        return {
            "success": False,
            "error": (
                f"Unknown calendar tool: "
                f"{tool_name}"
            ),
        }

    try:
        result = function(
            **arguments
        )

        return result

    except TypeError as e:
        return {
            "success": False,
            "error": (
                "Tool arguments error: "
                + str(e)
            ),
        }

    except Exception as e:
        return {
            "success": False,
            "error": (
                f"{type(e).__name__}: "
                f"{e}"
            ),
        }


def dispatch_calendar_tool_json(
    tool_name,
    arguments=None,
):
    """
    Tool結果をOpenAIへ返しやすい
    JSON文字列にする。
    """

    result = dispatch_calendar_tool(
        tool_name,
        arguments,
    )

    return json.dumps(
        result,
        ensure_ascii=False,
        default=str,
    )