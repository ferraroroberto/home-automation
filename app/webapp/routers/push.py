"""Browser Web Push subscription API."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.push_notifications import load_push_config, save_subscription

router = APIRouter()


@router.get("/api/push/config")
async def get_push_config() -> Dict[str, Any]:
    cfg = load_push_config()
    return {"available": bool(cfg["public_key"] and cfg["private_key"]), "public_key": cfg["public_key"]}


@router.post("/api/push/subscriptions")
async def post_push_subscription(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected a JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    try:
        count = save_subscription(body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "count": count}
