"""Small text-formatting helpers shared by the interface."""


def format_time(milliseconds: int) -> str:
    """Format a time in milliseconds as m:ss (or h:mm:ss for long tracks)."""
    total_seconds = max(0, milliseconds) // 1000
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_size(size_bytes: int) -> str:
    """Format a byte count in decimal units, for example "66.6 GB"."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(0, size_bytes))
    unit = 0
    while round(value, 1) >= 1000 and unit < len(units) - 1:
        value /= 1000
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    text = f"{value:.1f}".removesuffix(".0")
    return f"{text} {units[unit]}"


def format_long_duration(milliseconds: int, seconds: bool = False) -> str:
    """Format a long time as days, hours and minutes, for example "7d 1:23".

    Under a day it is just "1:23". With `seconds`, it is "7d 1:23:45".
    """
    total = max(0, milliseconds)
    if seconds:
        total_seconds = round(total / 1000)
        days, rest = divmod(total_seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, secs = divmod(rest, 60)
        clock = f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        total_minutes = round(total / 60000)
        days, rest = divmod(total_minutes, 1440)
        hours, minutes = divmod(rest, 60)
        clock = f"{hours}:{minutes:02d}"
    return f"{days}d {clock}" if days else clock