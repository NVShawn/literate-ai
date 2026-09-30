import platform


def compute_backend() -> str:
    if platform.system() == "Linux":
        return "cuda"
    return "cpu"


def language_runtime() -> str:
    return "python"


def process(values: list[int]) -> list[int]:
    return [value * 2 for value in values]
