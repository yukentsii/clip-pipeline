"""Accounts: the places clips are posted to, each with its own login and daily posting slots."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from clip_pipeline.db import iso
from clip_pipeline.publish.accounts import AccountError, AccountInUse, Accounts


class AccountCreate(BaseModel):
    platform: Literal["youtube", "instagram", "tiktok"]
    name: str = Field(min_length=1, max_length=60)
    # youtube: client_id, client_secret, refresh_token   instagram: user_id, access_token
    # tiktok: storage_state (path of a saved login on the server)
    credentials: dict[str, str] = Field(default_factory=dict)
    slots: list[str] | None = None             # daily posting times, e.g. ["09:00", "15:00", "21:00"]


class AccountEdit(BaseModel):
    """Everything is optional; send only what changed. Credentials are merged, never echoed back."""
    name: str | None = Field(default=None, min_length=1, max_length=60)
    slots: list[str] | None = None
    enabled: bool | None = None
    credentials: dict[str, str] | None = None


def build_router(accounts: Accounts) -> APIRouter:
    router = APIRouter(prefix="/accounts", tags=["accounts"])

    def get_or_404(account_id: str) -> dict:
        try:
            return accounts.get(account_id)
        except KeyError:
            raise HTTPException(404, "account not found")

    @router.get("")
    def list_accounts():
        """Every account with whether its login works, its slots and the next free one. No credentials."""
        return {"accounts": [accounts.public(a) for a in accounts.list()]}

    @router.post("", status_code=201)
    def create(body: AccountCreate):
        try:
            return accounts.public(accounts.create(body.platform, body.name, body.credentials, body.slots))
        except AccountError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/{account_id}")
    def get(account_id: str):
        return accounts.public(get_or_404(account_id))

    @router.patch("/{account_id}")
    def edit(account_id: str, body: AccountEdit):
        get_or_404(account_id)
        try:
            return accounts.public(accounts.update(account_id, **body.model_dump(exclude_unset=True, exclude_none=True)))
        except AccountError as exc:
            raise HTTPException(422, str(exc))

    @router.delete("/{account_id}", status_code=204)
    def delete(account_id: str):
        get_or_404(account_id)
        try:
            accounts.delete(account_id)
        except AccountInUse as exc:
            raise HTTPException(409, str(exc))

    @router.get("/{account_id}/next-slots")
    def next_slots(account_id: str, count: int = 5):
        """The next free posting slots of this account (UTC), for showing "will post at …" before approving."""
        get_or_404(account_id)
        out: list[str] = []
        for _ in range(max(1, min(count, 30))):
            out.append(iso(accounts.next_slot(account_id, taken=set(out))))
        return {"slots": out}

    return router
