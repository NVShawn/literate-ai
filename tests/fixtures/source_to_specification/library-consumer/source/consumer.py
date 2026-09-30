from library import normalize


def display_name(raw: str) -> str:
    return normalize(raw).title()
