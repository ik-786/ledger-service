"""Request bodies. Strict: unknown fields are rejected and numbers must be JSON integers."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Amounts are integers in the currency's minor unit (cents for USD, yen for JPY).
MAX_AMOUNT = 10**15
# Kept well inside SQLite's signed 64-bit integers, so sums can never overflow.
MAX_BALANCE = 10**18
MAX_LEGS = 50
MAX_HOLD_SECONDS = 30 * 24 * 3600

CURRENCIES: dict[str, int] = {
    # ISO 4217 code -> number of minor-unit digits
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "INR": 2,
    "JPY": 0,
    "KWD": 3,
}

AccountId = Annotated[str, Field(min_length=1, max_length=64)]
Amount = Annotated[int, Field(gt=0, le=MAX_AMOUNT)]
Description = Annotated[str, Field(max_length=500)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateAccount(Body):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    currency: str
    allow_negative: bool = False

    @field_validator("currency")
    @classmethod
    def known_currency(cls, value: str) -> str:
        if value not in CURRENCIES:
            raise ValueError(f"unsupported currency; use one of {', '.join(sorted(CURRENCIES))}")
        return value


class Transfer(Body):
    from_account_id: AccountId
    to_account_id: AccountId
    amount: Amount
    description: Description | None = None


class Leg(Body):
    account_id: AccountId
    amount: Annotated[int, Field(ge=-MAX_AMOUNT, le=MAX_AMOUNT)]

    @field_validator("amount")
    @classmethod
    def nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("a leg must move money; amount cannot be 0")
        return value


class Journal(Body):
    legs: Annotated[list[Leg], Field(min_length=2, max_length=MAX_LEGS)]
    description: Description | None = None


class CreateHold(Body):
    account_id: AccountId
    destination_account_id: AccountId
    amount: Amount
    expires_in_seconds: Annotated[int, Field(gt=0, le=MAX_HOLD_SECONDS)] = 7 * 24 * 3600


class Capture(Body):
    amount: Amount | None = None


class Reverse(Body):
    description: Description | None = None


class Empty(Body):
    pass
