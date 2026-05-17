"""Login sessions over HTTP: cookies, request dependencies and /api/auth/*.

A login sets the `wishmap_session` cookie (HttpOnly, SameSite=Lax,
`Secure` when `secure_cookies` is on) holding a random session token; the
DB keeps only its sha256 (see `users`). Handlers that act for a user
depend on `current_user` (401 without a valid session), and every
mutating endpoint — including login itself — depends on
`require_same_origin`, which stops another site from driving a request
through the visitor's browser (CSRF).

Login and registration also set `wishmap_vault` (HttpOnly,
SameSite=Strict): the account's vault key (see `vault`), which the server
never stores in the clear. Handlers that seal or open provider secrets
depend on `vault_key` — 409 `vault_locked` when the cookie is missing or
stale (e.g. a session from before vaults existed), so the web UI asks the
user to log in again.

Failed password checks are throttled per username (`LoginThrottle` on the
app context): after a few misses that name is locked out for a minute.
"""

import logging
from datetime import timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from wishmap import users, vault
from wishmap.models import AccountResponse, Credentials, PasswordChange
from wishmap.state import AppContext
from wishmap.users import Login, User

logger = logging.getLogger(__name__)

SESSION_COOKIE = "wishmap_session"
VAULT_COOKIE = "wishmap_vault"


# Dependencies are `async def` where they only touch memory, so FastAPI
# resolves them on the event loop; DB-backed ones are plain `def`
# (threadpool) so a slow sqlite call never stalls the loop.
async def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def require_same_origin(request: Request) -> None:
    """Reject a cross-origin request to a mutating endpoint (CSRF), 403.

    Browsers label every request with `Sec-Fetch-Site`; only `same-origin`
    (and `none`, a user-initiated navigation) pass. Browsers too old to
    send it still send `Origin` on a cross-origin POST, so that must name
    this host. A request with neither header (curl, scripts) carries no
    ambient browser credentials to abuse, so it is allowed.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site not in ("same-origin", "none"):
            raise HTTPException(status_code=403, detail="cross_origin_blocked")
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    # Host only (not scheme): behind a TLS-terminating proxy the app may
    # see http while the browser's Origin says https.
    if origin == "null" or urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="cross_origin_blocked")


SameOrigin = Depends(require_same_origin)


def optional_user(request: Request, ctx: Ctx) -> User | None:
    """The user the request's session cookie belongs to, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return users.resolve_session(ctx.db_path, token)


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    """The signed-in user; 401 `not_authenticated` without a valid session."""
    if user is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def request_vault_key(request: Request, ctx: AppContext, user: User) -> bytes | None:
    """The user's vault key from the request's cookie; None if absent or stale.

    Stale: not the account's current key — e.g. a browser still holding
    the key of a vault that has since been replaced.
    """
    value = request.cookies.get(VAULT_COOKIE)
    key = None if value is None else vault.decode_key(value)
    if key is None or not users.vault_key_matches(ctx.db_path, user.id, key):
        return None
    return key


def vault_key(request: Request, ctx: Ctx, user: CurrentUser) -> bytes:
    """The signed-in user's vault key; 409 `vault_locked` without a valid one."""
    key = request_vault_key(request, ctx, user)
    if key is None:
        raise HTTPException(status_code=409, detail="vault_locked")
    return key


VaultKey = Annotated[bytes, Depends(vault_key)]


def _cookie_max_age(ctx: AppContext) -> int:
    return int(timedelta(days=ctx.settings.session_days).total_seconds())


def _set_session_cookie(response: Response, ctx: AppContext, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=_cookie_max_age(ctx),
        path="/",
        secure=ctx.settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def set_vault_cookie(response: Response, ctx: AppContext, key: bytes) -> None:
    # Strict: never sent along with a request another site started, not
    # even a top-level navigation — only this app's own fetches need it.
    response.set_cookie(
        VAULT_COOKIE,
        vault.encode_key(key),
        max_age=_cookie_max_age(ctx),
        path="/",
        secure=ctx.settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )


def _clear_auth_cookies(response: Response, ctx: AppContext) -> None:
    cookies: tuple[tuple[str, Literal["lax", "strict"]], ...] = (
        (SESSION_COOKIE, "lax"),
        (VAULT_COOKIE, "strict"),
    )
    for name, samesite in cookies:
        response.delete_cookie(
            name,
            path="/",
            secure=ctx.settings.secure_cookies,
            httponly=True,
            samesite=samesite,
        )


def _start_session(
    request: Request, response: Response, ctx: AppContext, login: Login
) -> None:
    """Sign the account in on this browser, replacing any session it had.

    A fresh token on every login (never reusing the request's) means a
    token planted before login is worthless afterwards (session fixation).
    The vault cookie is (re)set alongside the session cookie.
    """
    old_token = request.cookies.get(SESSION_COOKIE)
    if old_token:
        users.delete_session(ctx.db_path, old_token)
    token = users.create_session(
        ctx.db_path, login.user.id, timedelta(days=ctx.settings.session_days)
    )
    _set_session_cookie(response, ctx, token)
    set_vault_cookie(response, ctx, login.vault_key)


def _throttle_key(username: str) -> str:
    # Usernames are case-insensitive, so are their lockouts.
    return username.casefold()


def _check_password(ctx: AppContext, username: str, password: str) -> Login | None:
    """`users.login` behind the throttle: the login, or None if wrong.

    Raises 429 `too_many_attempts`, without checking anything, while the
    name is locked out. The attempt is reserved before the (slow) hash so
    parallel requests can't all slip past the limit.
    """
    key = _throttle_key(username)
    retry_after = ctx.login_throttle.begin(key)
    if retry_after > 0:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )
    try:
        login = users.login(ctx.db_path, username, password)
    except BaseException:
        ctx.login_throttle.cancel(key)
        raise
    if login is None:
        ctx.login_throttle.record_failure(key)
        logger.info("Failed password check for %r", username)
    else:
        ctx.login_throttle.record_success(key)
    return login


router = APIRouter(prefix="/api/auth", dependencies=[SameOrigin])

# Handlers are `def` (threadpool): scrypt deliberately takes tens of ms.


@router.post("/register", status_code=201, response_model=AccountResponse)
def register(
    body: Credentials, request: Request, response: Response, ctx: Ctx
) -> AccountResponse | JSONResponse:
    """Create an account and sign it in (403 when registration is closed)."""
    if not ctx.settings.allow_registration:
        return JSONResponse(status_code=403, content={"detail": "registration_closed"})
    try:
        login = users.register(ctx.db_path, body.username, body.password)
    except users.UsernameTakenError:
        return JSONResponse(status_code=409, content={"detail": "username_taken"})
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    logger.info("Registered user %r (id %d)", login.user.username, login.user.id)
    _start_session(request, response, ctx, login)
    return AccountResponse(username=login.user.username)


@router.post("/login", response_model=AccountResponse)
def login(
    body: Credentials, request: Request, response: Response, ctx: Ctx
) -> AccountResponse | JSONResponse:
    """Sign in: 401 `invalid_credentials`, 429 while the name is locked out."""
    login = _check_password(ctx, body.username, body.password)
    if login is None:
        return JSONResponse(status_code=401, content={"detail": "invalid_credentials"})
    _start_session(request, response, ctx, login)
    return AccountResponse(username=login.user.username)


@router.post("/logout", status_code=204)
def logout(request: Request, ctx: Ctx) -> Response:
    """End this browser's session (and drop its vault key). Idempotent."""
    response = Response(status_code=204)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        users.delete_session(ctx.db_path, token)
    _clear_auth_cookies(response, ctx)
    return response


@router.post("/password", status_code=204)
def change_password(
    body: PasswordChange, request: Request, ctx: Ctx, user: CurrentUser
) -> Response:
    """Change the password; signs out every *other* session of the account.

    The vault key is re-wrapped under the new password (unwrapped with the
    current one, so this works even from a browser without the vault
    cookie, and re-sets that cookie): sealed provider logins survive.
    403 `wrong_password` for a wrong current password (throttled like a
    login; not 401, which means "no session"), 422 for an unacceptable
    new one.
    """
    try:
        users.validate_new_password(body.new_password)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    login = _check_password(ctx, user.username, body.current_password)
    if login is None or login.user.id != user.id:
        return JSONResponse(status_code=403, content={"detail": "wrong_password"})
    users.set_password(
        ctx.db_path, user.id, body.new_password, vault_key=login.vault_key
    )
    users.delete_sessions(
        ctx.db_path, user.id, keep_token=request.cookies.get(SESSION_COOKIE)
    )
    logger.info("User %r changed their password", user.username)
    response = Response(status_code=204)
    set_vault_cookie(response, ctx, login.vault_key)
    return response
