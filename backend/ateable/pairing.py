"""Encoding helpers for portable, single-use Ateable pairing keys."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

PAIR_PREFIX = "ATEABLE1."


class PairKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PairKey:
    controller_url: str
    key_id: str
    secret: str


def encode_pair_key(controller_url: str, key_id: str, secret: str) -> str:
    payload = json.dumps(
        {"controller": controller_url.rstrip("/"), "id": key_id, "secret": secret},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return PAIR_PREFIX + encoded


def decode_pair_key(value: str) -> PairKey:
    value = value.strip()
    if not value.upper().startswith(PAIR_PREFIX):
        raise PairKeyError("Pair key has an unsupported format")
    encoded = value[len(PAIR_PREFIX) :]
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        controller = str(payload["controller"]).rstrip("/")
        key_id = str(payload["id"])
        secret = str(payload["secret"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise PairKeyError("Pair key is malformed") from exc
    if (
        not controller.startswith(("http://", "https://"))
        or not key_id
        or len(secret) < 32
    ):
        raise PairKeyError("Pair key is malformed")
    return PairKey(controller, key_id, secret)


def hash_pair_secret(controller_url: str, key_id: str, secret: str) -> str:
    bound = f"{controller_url.rstrip('/')}:{key_id}:{secret}"
    return hashlib.sha256(bound.encode()).hexdigest()
