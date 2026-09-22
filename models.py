"""Validated API inputs. All money is integer satoshis."""

import re
from typing import Literal

from pydantic import BaseModel, Field, validator

MAPS = [
    {"value": "aggressor", "label": "Aggressor"},
    {"value": "oa_dm1", "label": "OA DM1"},
    {"value": "oa_dm2", "label": "OA DM2"},
    {"value": "kaos2", "label": "Kaos 2"},
]
MAX_PLAYERS = 8


def prize_per_kill(entry_amount: int, haircut: int) -> int:
    """One fifth of the entry, less the fee, rounded down once to whole sats."""
    return entry_amount * (100 - haircut) // 500


class SettingsInput(BaseModel):
    wallet_id: str = Field(alias="walletId", min_length=1, max_length=128)
    enabled: bool = False
    haircut: int = Field(default=5, ge=0, le=100)

    @validator("haircut", pre=True)
    def integer_fee(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("The service fee must be a whole-number percentage.")
        return value


class ArenaInput(BaseModel):
    name: str = Field(default="QUAKEJS public arena", min_length=1, max_length=80)
    join_amount: int = Field(alias="joinAmount", default=100, ge=50, le=1_000_000)
    map: Literal["aggressor", "oa_dm1", "oa_dm2", "kaos2"] = "aggressor"

    @validator("join_amount", pre=True)
    def integer_amount(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Entry amount must be a whole number of sats.")
        return value


class EntryInput(BaseModel):
    ln_address: str = Field(alias="lnAddress", min_length=3, max_length=254)
    nonce: str = Field(regex=r"^[a-f0-9]{48}$")
    name: str = Field(default="PLAYER", min_length=1, max_length=18)

    @validator("ln_address")
    def address(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", value):
            raise ValueError("Enter a Lightning address, such as you@example.com.")
        local, domain = value.split("@")
        if "." not in domain or any(not part for part in domain.split(".")):
            raise ValueError("Invalid Lightning address domain.")
        return local + "@" + domain.lower()

    @validator("name")
    def player_name(cls, value):
        return re.sub(r"[^A-Za-z0-9 _.-]", "", value).strip()[:18] or "PLAYER"
