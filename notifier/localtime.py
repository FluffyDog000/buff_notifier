from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "МСК")


def moscow(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(MSK)
