"""One reusable fixture: JSON stdin -> delayed text artifact or controlled failure."""

import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    data = json.load(sys.stdin)
    time.sleep(float(data.get("delay_seconds", 0)))
    if data.get("fail"):
        print("Controlled program failure", file=sys.stderr)
        return 7
    path = Path(data["output_path"]).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = str(data.get("text", "")).encode("utf-8")
    # Exclusive artifact creation makes accidental duplicate execution observable.
    with path.open("xb") as stream:
        stream.write(content)
    print(json.dumps({"outcome": data.get("outcome", "passed"), "output": {
        "path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content), "text": content.decode("utf-8")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
