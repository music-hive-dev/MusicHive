"""Small text-formatting helpers shared by the interface."""


def format_time(milliseconds: int) -> str:
    """Format a time in milliseconds as m:ss (or h:mm:ss for long tracks)."""
    total_seconds = max(0, milliseconds) // 1000
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"