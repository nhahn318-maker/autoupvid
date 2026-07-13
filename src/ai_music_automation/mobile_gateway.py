from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "web_static"
ACCESS_FILE = ROOT / "data" / "state" / "mobile-gateway.json"
SESSION_FILE = ROOT / "data" / "state" / "mobile-gateway-session.json"
BACKEND_URL = "http://127.0.0.1:8000"
ALLOWED_STATIC = {
    "mobile.html",
    "mobile.css",
    "mobile.js",
    "mobile.webmanifest",
    "mobile-sw.js",
    "mobile-icon-180.png",
    "mobile-icon-512.png",
}
PUBLIC_STATIC = {
    "mobile.webmanifest",
    "mobile-icon-180.png",
    "mobile-icon-512.png",
}

app = FastAPI(title="Phat Phap Auto Mobile Gateway", docs_url=None, redoc_url=None, openapi_url=None)
HOST = "0.0.0.0"
PORT = 8765


def access_credentials() -> dict[str, str]:
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ACCESS_FILE.exists():
        return json.loads(ACCESS_FILE.read_text(encoding="utf-8-sig"))
    credentials = {"username": "owner", "password": secrets.token_urlsafe(18)}
    ACCESS_FILE.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    return credentials


def session_secret() -> str:
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8-sig"))
        secret = str(data.get("secret") or "")
        if secret:
            return secret
    secret = secrets.token_urlsafe(32)
    SESSION_FILE.write_text(json.dumps({"secret": secret}, indent=2), encoding="utf-8")
    return secret


def sign_session(username: str) -> str:
    nonce = secrets.token_urlsafe(18)
    payload = f"{username}:{nonce}"
    signature = hmac.new(session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def valid_session(cookie_value: str) -> bool:
    try:
        username, nonce, signature = cookie_value.split(":", 2)
    except ValueError:
        return False
    credentials = access_credentials()
    if not hmac.compare_digest(username, credentials["username"]):
        return False
    payload = f"{username}:{nonce}"
    expected = hmac.new(session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def authorized(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    credentials = access_credentials()
    return hmac.compare_digest(username, credentials["username"]) and hmac.compare_digest(password, credentials["password"])


def login_page(error: str = "") -> HTMLResponse:
    username = access_credentials()["username"]
    error_html = f'<p class="error">{error}</p>' if error else ""
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="theme-color" content="#10273a" />
    <title>Sign in - Phat Phap Auto</title>
    <link rel="apple-touch-icon" sizes="180x180" href="/static/mobile-icon-180.png" />
    <style>
      :root {{ font-family: Arial, sans-serif; background: #edf3f6; color: #142330; }}
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 20px; }}
      form {{ width: min(420px, 100%); background: #fff; border: 1px solid #d7e1e7; border-radius: 8px; padding: 22px; display: grid; gap: 14px; box-shadow: 0 1px 2px #12243612; }}
      h1 {{ margin: 0; font-size: 24px; }}
      p {{ margin: 0; color: #526b7b; line-height: 1.4; }}
      label {{ display: grid; gap: 6px; font-size: 13px; font-weight: 700; color: #415c6d; }}
      input {{ border: 1px solid #b7c7d2; border-radius: 6px; font: inherit; min-height: 44px; padding: 0 12px; }}
      button {{ background: #102f43; border: 0; border-radius: 7px; color: #fff; font: inherit; font-weight: 700; min-height: 46px; }}
      .error {{ color: #a53830; font-weight: 700; }}
    </style>
  </head>
  <body>
    <form method="post" action="/login">
      <h1>Phat Phap Auto</h1>
      <p>Sign in once on this device to control the automation server.</p>
      {error_html}
      <label>Email<input name="username" autocomplete="username" value="{username}" /></label>
      <label>Password<input name="password" type="password" autocomplete="current-password" autofocus /></label>
      <button type="submit">Sign in</button>
    </form>
  </body>
</html>"""
    return HTMLResponse(html)


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.method == "GET" and request.url.path.startswith("/static/"):
        asset_name = request.url.path.rsplit("/", 1)[-1]
        if asset_name in PUBLIC_STATIC:
            return await call_next(request)
    if request.url.path == "/login":
        return await call_next(request)
    if valid_session(request.cookies.get("mobile_session", "")):
        return await call_next(request)
    if authorized(request.headers.get("authorization", "")):
        response = await call_next(request)
        response.set_cookie("mobile_session", sign_session(access_credentials()["username"]), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 60)
        return response
    if request.method == "GET" and request.url.path in {"/", "/mobile"}:
        return RedirectResponse("/login", status_code=303)
    return PlainTextResponse("Authentication required", status_code=401)


@app.get("/login")
def get_login() -> HTMLResponse:
    return login_page()


@app.post("/login")
async def post_login(request: Request) -> Response:
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    form = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
    username = str((form.get("username") or [""])[0])
    password = str((form.get("password") or [""])[0])
    credentials = access_credentials()
    if not (
        hmac.compare_digest(username, credentials["username"])
        and hmac.compare_digest(password, credentials["password"])
    ):
        return login_page("Incorrect email or password.")
    response = RedirectResponse("/mobile", status_code=303)
    response.set_cookie("mobile_session", sign_session(username), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 60)
    return response


def backend_json(method: str, path: str, payload: dict[str, Any] | None = None) -> Response:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BACKEND_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return JSONResponse(json.loads(response.read().decode("utf-8")), status_code=response.status)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return PlainTextResponse(body or error.reason, status_code=error.code)
    except urllib.error.URLError as error:
        return PlainTextResponse(f"Automation backend is unavailable: {error.reason}", status_code=503)


@app.get("/")
@app.get("/mobile")
def mobile_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "mobile.html")


@app.get("/static/{asset_name}")
def mobile_static(asset_name: str) -> FileResponse:
    if asset_name not in ALLOWED_STATIC:
        raise HTTPException(status_code=404, detail="Not found")
    media_type = "application/manifest+json" if asset_name.endswith(".webmanifest") else None
    return FileResponse(STATIC_DIR / asset_name, media_type=media_type)


@app.get("/api/status")
def status() -> Response:
    return backend_json("GET", "/api/status")


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> Response:
    return backend_json("GET", f"/api/jobs/{job_id}")


@app.post("/api/account/{account_id}")
async def account(account_id: str, request: Request) -> Response:
    return backend_json("POST", f"/api/account/{account_id}", await request.json())


@app.post("/api/fullauto-action")
async def fullauto_action(request: Request) -> Response:
    return backend_json("POST", "/api/fullauto-action", await request.json())


@app.post("/api/fullauto-bulk-action")
async def fullauto_bulk_action(request: Request) -> Response:
    return backend_json("POST", "/api/fullauto-bulk-action", await request.json())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.ai_music_automation.mobile_gateway:app", host=HOST, port=PORT)
