"""Synthetic hello-shaped app with planted spec-map anchors."""

from __future__ import annotations

import json
import sys

import litai_debug

litai_debug.install()


# litai:spec samples/hello-component/component.md:77 entrypoint
def main(request: dict) -> dict:
    litai_debug.trace_entrypoint()
    name = str(request.get("name") or "")
    messages = request.get("messages") or []
    if not name:
        # litai:spec samples/hello-component/component.md:102 error
        raise ValueError("name is required")
    # litai:spec samples/hello-component/component.md:82 behavior
    word_count = sum(len(str(item).split()) for item in messages)
    return {
        "greeting": f"Hello, {name}!",
        "recipient_id": name.casefold().replace(" ", "-"),
        "message_count": len(messages),
        "word_count": word_count,
    }


if __name__ == "__main__":
    arguments = json.loads(sys.argv[1]) if len(sys.argv) > 1 else [{}]
    request = arguments[0] if arguments else {}
    print(json.dumps(main(request), separators=(",", ":")))
