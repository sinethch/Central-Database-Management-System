from fastapi import Request
from app.auth import current_user

async def verify_api_key(request: Request):
    return await current_user(request)
