"""Validated API inputs. All money is integer satoshis."""

import re
from typing import Literal

from pydantic import BaseModel, Field, validator

MAPS = [
    {"value": "aggressor", "label": "Aggressor"},
    {"value": "oa_dm7", "label": "OA DM7"},
    {"value": "oa_minia", "label": "OA Minia"},
    {"value": "czest1dm", "label": "Czest1dm"},
    {"value": "oa_shine", "label": "OA Shine"},
    {"value": "kaos2", "label": "Kaos 2"},
]
MAX_PLAYERS = 8
MAX_TOTAL_HAIRCUT = 50


class PublicError(ValueError):
    """An intentional, fixed message safe to return to an anonymous player."""


def prize_per_kill(entry_amount: int, haircut: int) -> int:
    """One fifth of the entry, less the fee, rounded down once to whole sats."""
    return entry_amount * (100 - haircut) // 500


class SettingsInput(BaseModel):
    wallet_id: str = Field(alias="walletId", min_length=1, max_length=128)
    enabled: bool = False
    haircut: int = Field(default=5, ge=0, le=MAX_TOTAL_HAIRCUT)
    allow_public_creation: bool = Field(default=False, alias="allowPublicCreation")

    @validator("haircut", pre=True)
    def integer_fee(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("The service fee must be a whole-number percentage.")
        return value


class ArenaInput(BaseModel):
    name: str = Field(default="QUAKEJS public arena", min_length=1, max_length=80)
    join_amount: int = Field(alias="joinAmount", default=100, ge=100, le=1_000_000)
    map: Literal["aggressor", "oa_dm7", "oa_minia", "czest1dm", "oa_shine", "kaos2"] = (
        "aggressor"
    )

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


class PublicArenaInput(ArenaInput):
    creator_haircut: int = Field(
        default=0, alias="creatorHaircut", ge=0, le=MAX_TOTAL_HAIRCUT
    )
    ln_address: str = Field(default="", alias="lnAddress", max_length=254)
    nonce: str = Field(regex=r"^[a-f0-9]{48}$")

    @validator("creator_haircut", pre=True)
    def integer_creator_fee(cls, value):
        return SettingsInput.integer_fee(value)

    @validator("ln_address")
    def creator_address(cls, value):
        return EntryInput.address(value) if value.strip() else ""

    class Config:
        extra = "forbid"


class ServerSettingsInput(BaseModel):
    max_matches: int = Field(alias="maxMatches", ge=1, le=32)

    @validator("max_matches", pre=True)
    def integer_capacity(cls, value):
        if type(value) is not int:
            raise ValueError("Match capacity must be a whole number from 1 to 32.")
        return value

    class Config:
        extra = "forbid"
