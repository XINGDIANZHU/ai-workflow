"""Register the endpoint in this installation's Restate, without starting a task."""
import argparse
import json
from urllib.request import Request, build_opener, ProxyHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin", default="http://127.0.0.1:29070")
    parser.add_argument("--endpoint", default="http://127.0.0.1:29080")
    args = parser.parse_args()
    request = Request(args.admin.rstrip("/") + "/deployments",
                      data=json.dumps({"uri": args.endpoint}).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
