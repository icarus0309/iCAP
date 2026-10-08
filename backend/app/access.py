"""Ownership and visibility rules for records in the shared workspace."""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException

Visibility = Literal["private", "public"]


def can_view(record: dict, principal: dict) -> bool:
    if principal["role"] == "admin":
        return True
    return (record.get("visibility", "public") == "public"
            or record.get("owner_user_id") == principal["id"])


def can_edit(record: dict, principal: dict) -> bool:
    if principal["role"] == "admin":
        return True
    return bool(record.get("owner_user_id")) and record["owner_user_id"] == principal["id"]


def require_view(record: dict, principal: dict) -> dict:
    if not can_view(record, principal):
        raise HTTPException(404, "记录不存在")
    return record


def require_edit(record: dict, principal: dict) -> dict:
    if not can_edit(record, principal):
        raise HTTPException(403, "无权修改此记录")
    return record


def ownership(principal: dict, visibility: Visibility) -> dict:
    return {"owner_user_id": principal["id"], "visibility": visibility}


def visible(records: list[dict], principal: dict) -> list[dict]:
    return [record for record in records if can_view(record, principal)]
