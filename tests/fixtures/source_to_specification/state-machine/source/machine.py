"""Small deterministic state machine used as static source evidence."""


class Door:
    def __init__(self) -> None:
        self.state = "closed"

    def open(self) -> None:
        if self.state != "closed":
            raise ValueError("door is not closed")
        self.state = "open"

    def close(self) -> None:
        if self.state != "open":
            raise ValueError("door is not open")
        self.state = "closed"
