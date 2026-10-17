"""supabase_client.py - the ONLY module that creates Supabase clients.

Security rules enforced here
  * Clients are never cached globally (no st.cache_resource). Each login creates
    its own client, which is kept only in that visitor's st.session_state, so
    one user's auth session can never reach another visitor.
  * The secret / service-role key is read only inside ``service_client`` and is
    handed out only after the CURRENT user is verified as super admin by the
    database (``is_super_admin()``) and by Supabase Auth (``get_user()``).
  * Nothing in this module writes keys to widgets, logs or files.
"""
from __future__ import annotations

import streamlit as st
from supabase import Client, ClientOptions, create_client


class ConfigError(RuntimeError):
    """Missing or invalid Streamlit secrets."""


class AuthRequired(Exception):
    """No valid login session; the UI should show the login page."""


class PermissionDenied(Exception):
    """The current user is not allowed to perform this operation."""


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def _secret(name: str) -> str:
    try:
        value = st.secrets[name]
    except Exception:  # missing secrets file or missing key
        return ""
    return str(value).strip() if value else ""


def missing_config() -> list[str]:
    """Names of required secrets that are not set (empty list = OK)."""
    return [n for n in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY") if not _secret(n)]


def secret_key_configured() -> bool:
    return bool(_secret("SUPABASE_SECRET_KEY"))


def app_url() -> str:
    return _secret("APP_URL").rstrip("/")


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def new_client() -> Client:
    """A brand-new client using the PUBLISHABLE key (subject to RLS)."""
    if missing_config():
        raise ConfigError("Supabase is not configured. Add the secrets listed in the README.")
    options = ClientOptions(auto_refresh_token=False, persist_session=True)
    return create_client(_secret("SUPABASE_URL"), _secret("SUPABASE_PUBLISHABLE_KEY"), options=options)


def user_client(state) -> Client:
    """The logged-in visitor's own client (from THEIR session_state only)."""
    client = state.get("sb_client")
    if client is None:
        raise AuthRequired("Please log in.")
    try:
        session = client.auth.get_session()  # refreshes an expired access token
    except Exception:
        session = None
    if session is None:
        raise AuthRequired("Your session has expired. Please log in again.")
    return client


def verify_super_admin(state):
    """Confirm with Supabase Auth AND the database that the caller is super admin."""
    client = user_client(state)
    try:
        auth_user = client.auth.get_user()
        is_admin = client.rpc("is_super_admin").execute().data
    except Exception:
        raise PermissionDenied("Could not verify administrator access.") from None
    if not auth_user or not getattr(auth_user, "user", None) or is_admin is not True:
        raise PermissionDenied("Administrator access required.")
    return auth_user.user


def service_client(state) -> Client:
    """Privileged client (secret key). Only for verified super admins."""
    verify_super_admin(state)
    key = _secret("SUPABASE_SECRET_KEY")
    if not key:
        raise ConfigError("SUPABASE_SECRET_KEY is not set in Streamlit secrets; "
                          "it is required to invite, edit and delete shop accounts.")
    options = ClientOptions(auto_refresh_token=False, persist_session=False)
    return create_client(_secret("SUPABASE_URL"), key, options=options)
