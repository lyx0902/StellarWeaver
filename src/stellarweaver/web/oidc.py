"""OIDC authorization-code login with PKCE and opaque browser sessions."""

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlsplit
from uuid import UUID

import httpx
import jwt
from sqlalchemy import text
from sqlalchemy.engine import Engine

from stellarweaver.domain import JSONDict
from stellarweaver.web.db import scoped

SESSION_COOKIE = "stellarweaver_session"
FLOW_COOKIE = "stellarweaver_oidc"
CALLBACK = "/api/v1/auth/callback"
SESSION_SECONDS = 8 * 3600
FLOW_SECONDS = 300


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class LoginRejected(Exception):
    """Invalid or expired OIDC login."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    origin: str
    client_id: str
    client_secret: str = field(repr=False)
    allow_local_http: bool = False

    def endpoint(self, value: str) -> str:
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or (
                url.scheme != "https"
                and not (
                    self.allow_local_http
                    and url.scheme == "http"
                    and url.hostname in {"localhost", "127.0.0.1"}
                )
            )
        ):
            raise ValueError("OIDC endpoints require HTTPS; loopback HTTP is for local tests")
        return value

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("OIDC client ID and secret are required")
        public = urlsplit(self.endpoint(self.origin))
        issuer = urlsplit(self.endpoint(self.issuer))
        if public.path or public.query or issuer.query:
            raise ValueError("Set an origin without a path, and an issuer without a query")

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")


@dataclass(frozen=True)
class _Flow:
    nonce: str
    verifier: str
    expires: float


class OIDC:
    def __init__(self, config: OIDCConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport
        self._flows: dict[str, _Flow] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=8,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )

    async def _discovery(self, client: httpx.AsyncClient) -> JSONDict:
        response = await client.get(
            self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        response.raise_for_status()
        data: JSONDict = response.json()
        if data["issuer"] != self.config.issuer:
            raise LoginRejected("Issuer mismatch")
        return data

    async def begin(self) -> tuple[str, str]:
        async with self._client() as client:
            data = await self._discovery(client)
        endpoint = self.config.endpoint(data["authorization_endpoint"])
        now = time.monotonic()
        self._flows = {state: flow for state, flow in self._flows.items() if flow.expires > now}
        if len(self._flows) >= 256:
            raise LoginRejected("Too many pending sign-ins")
        state = secrets.token_urlsafe(32)
        flow = _Flow(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now + FLOW_SECONDS)
        self._flows[state] = flow
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        url = httpx.URL(endpoint).copy_merge_params(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.origin + CALLBACK,
                "response_type": "code",
                "scope": "openid",
                "state": state,
                "nonce": flow.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return str(url), state

    async def finish(self, state: str, cookie: str, code: str) -> str:
        if (
            not state
            or not state.isascii()
            or not cookie.isascii()
            or not secrets.compare_digest(state, cookie)
        ):
            raise LoginRejected("Invalid login state")
        flow = self._flows.pop(state, None)
        if flow is None or flow.expires <= time.monotonic() or not code:
            raise LoginRejected("Expired login")
        async with self._client() as client:
            data = await self._discovery(client)
            response = await client.post(
                self.config.endpoint(data["token_endpoint"]),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.origin + CALLBACK,
                    "code_verifier": flow.verifier,
                },
                auth=(quote_plus(self.config.client_id), quote_plus(self.config.client_secret)),
            )
            response.raise_for_status()
            token = response.json()["id_token"]
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise LoginRejected("Unsupported ID token signature")
            response = await client.get(self.config.endpoint(data["jwks_uri"]))
            response.raise_for_status()
            keys = [
                item
                for item in response.json()["keys"]
                if item.get("kid") == header["kid"]
                and item.get("kty") == "RSA"
                and item.get("use", "sig") == "sig"
                and item.get("alg", "RS256") == "RS256"
            ]
        if len(keys) != 1:
            raise LoginRejected("Unknown ID token signing key")
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0], algorithm="RS256"),
            algorithms=["RS256"],
            audience=self.config.client_id,
            issuer=self.config.issuer,
            leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
        aud, azp, nonce, subject = claims["aud"], claims.get("azp"), claims["nonce"], claims["sub"]
        if (
            (isinstance(aud, list) and len(aud) > 1) or azp is not None
        ) and azp != self.config.client_id:
            raise LoginRejected("Invalid authorized party")
        if (
            not isinstance(nonce, str)
            or not nonce.isascii()
            or not secrets.compare_digest(nonce, flow.nonce)
            or not isinstance(subject, str)
            or not subject
            or len(subject) > 255
        ):
            raise LoginRejected("Invalid ID token claims")
        return subject


def create_session(engine: Engine, tenant: str, member: str) -> str:
    value = secrets.token_urlsafe(32)
    with scoped(engine, tenant) as conn:
        conn.execute(text("DELETE FROM sw.browser_sessions WHERE expires_at<=now()"))
        conn.execute(
            text("""
            INSERT INTO sw.browser_sessions(tenant_id,digest,member_id,expires_at)
            VALUES(:tenant,:digest,:member,now()+make_interval(secs => :seconds))
        """),
            {
                "tenant": tenant,
                "member": member,
                "digest": _hash(value),
                "seconds": SESSION_SECONDS,
            },
        )
    return tenant + "." + value


def _cookie_parts(cookie: str) -> tuple[str, str] | None:
    tenant, _, token = cookie.partition(".")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    try:
        return str(UUID(tenant)), _hash(token)
    except ValueError:
        return None


def session_member(engine: Engine, cookie: str) -> tuple[str, str] | None:
    parts = _cookie_parts(cookie)
    if parts is None:
        return None
    tenant, digest = parts
    with scoped(engine, tenant) as conn:
        member = conn.execute(
            text("""
            SELECT member_id::text FROM sw.browser_sessions WHERE digest=:digest AND expires_at>now()
        """),
            {"digest": digest},
        ).scalar_one_or_none()
    return (tenant, member) if member else None


def logout(engine: Engine, cookie: str) -> None:
    parts = _cookie_parts(cookie)
    if parts is not None:
        tenant, digest = parts
        with scoped(engine, tenant) as conn:
            conn.execute(
                text("DELETE FROM sw.browser_sessions WHERE digest=:digest"), {"digest": digest}
            )
