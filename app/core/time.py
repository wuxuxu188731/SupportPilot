from datetime import datetime, timezone


def utc_now() -> datetime:
    """返回带时区信息的当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def format_utc_timestamp(value: datetime) -> str:
    """把带时区时间统一格式化为秒精度的 UTC ISO 8601 文本。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("value must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
