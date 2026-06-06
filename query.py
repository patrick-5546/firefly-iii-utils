import argparse
import json
import os

import requests
from dotenv import load_dotenv
from pydantic import BaseModel


class Args(BaseModel):
    path: str


def main():
    parser = argparse.ArgumentParser(description="Query the Firefly III API.")
    _ = parser.add_argument("path", help="API path after /api/v1/ (e.g. 'about')")
    args = Args.model_validate(vars(parser.parse_args()))

    _ = load_dotenv()
    domain = os.environ["FIREFLY_III_URL"]
    token = os.environ["FIREFLY_III_PAT"]

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
