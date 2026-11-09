from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Optional
from pydantic import BaseModel, Field
from app.auth import authenticate, begin_mfa_challenge, complete_mfa_challenge, current_user, require_admin, create_user, list_users, delete_user, revoke_session, change_password
from app.activity_log import record_activity, list_activity, clear_activity as clear_activity_log

router = APIRouter(prefix="/auth", tags=["Authentication"])

class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

class NewUser(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: str = Field(default="user", pattern=r"^(user|admin)$")
    enable_mfa: bool = False

class MfaVerification(BaseModel):
    challenge: str = Field(min_length=20, max_length=128)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")

class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)

class ActivityEvent(BaseModel):
    client_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    db_id: Optional[str] = Field(default=None, max_length=128)
    ts: int = Field(gt=0)

@router.post("/login")
def login(payload: Credentials, request: Request):
    user = authenticate(payload.username.strip(), payload.password)
    if not user:
        raise HTTPException(401, "Invalid username or password.")
    return begin_mfa_challenge(user)

@router.post("/login/verify-mfa")
def verify_mfa(payload: MfaVerification, request: Request):
    token, user = complete_mfa_challenge(payload.challenge, payload.code)
    return {"token": token, "user": user}

@router.get("/me")
async def me(user=Depends(current_user)):
    return user

@router.post("/change-password")
async def update_password(payload: PasswordChange, user=Depends(current_user)):
    change_password(user["username"], payload.current_password, payload.new_password)
    return {"password_changed": True, "user": {"username": user["username"], "role": user["role"], "password_change_required": False}}

@router.post("/logout")
async def logout(request: Request, user=Depends(current_user)):
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        revoke_session(authorization[7:].strip())
    return {"logged_out": True}

@router.get("/users")
async def users(user=Depends(current_user)):
    await require_admin(user)
    return {"users": list_users()}

@router.post("/users", status_code=201)
async def add_user(payload: NewUser, user=Depends(current_user)):
    await require_admin(user)
    result = create_user(payload.username.strip(), payload.password, payload.enable_mfa, payload.role)
    return {"username": payload.username.strip(), "role": payload.role, **result}

@router.delete("/users/{username}")
async def remove_user(username: str, user=Depends(current_user)):
    await require_admin(user)
    delete_user(username)
    return {"deleted": username}

@router.get("/activity")
async def activity(user=Depends(current_user), db_id: Optional[str] = None):
    events, cleared_at = list_activity(db_id=db_id)
    return {"events": events, "cleared_at": cleared_at}

@router.post("/activity", status_code=201)
async def add_activity(payload: ActivityEvent, user=Depends(current_user)):
    return record_activity(
        payload.client_id,
        user["username"],
        user["role"],
        payload.kind,
        payload.title,
        payload.detail,
        payload.ts,
        db_id=payload.db_id,
    )

@router.post("/activity/clear")
async def clear_activity(user=Depends(current_user)):
    await require_admin(user)
    return {"cleared": True, "cleared_at": clear_activity_log()}
