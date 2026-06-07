import os

import requests
from pydantic import ValidationError

from .models import AccountResponse


def lookup_account_name(account_id: int) -> str | None:
    """Best-effort lookup of a Firefly III asset account's display name."""
    try:
        url = os.environ["FIREFLY_III_URL"].rstrip("/")
        token = os.environ["FIREFLY_III_PAT"]
    except KeyError:
        return None
    try:
        response = requests.get(
            f"{url}/api/v1/accounts/{account_id}",
            headers={
                "accept": "application/vnd.api+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
        response.raise_for_status()
        return AccountResponse.model_validate(response.json()).data.attributes.name
    except (requests.RequestException, ValidationError):
        return None
