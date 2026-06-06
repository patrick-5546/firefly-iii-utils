import argparse
import json
import os

import requests
from dotenv import load_dotenv


class Args(argparse.Namespace):
    path: str = ""


def main():
    parser = argparse.ArgumentParser(description="Query the Firefly III API.")
    _ = parser.add_argument("path", help="API path after /api/v1/ (e.g. 'about')")
    args = parser.parse_args(namespace=Args())

    _ = load_dotenv()
    domain = os.environ["DOMAIN"]
    token = os.environ["FIRELY_III_PAT"]

    response = requests.get(
        f"{domain}/api/v1/{args.path}",
        headers={
            "accept": "application/vnd.api+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
