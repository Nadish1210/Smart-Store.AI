"""authentication.py - login, logout, password reset/change and account pages.

Roles come ONLY from the database (``my_profile()`` RPC): a row in app_admins
makes a super admin, a row in shops makes a shop user. The email address and
user metadata are never used to decide privileges.
"""
from __future__ import annotations

import re

import streamlit as st

import supabase_client as sc

BRAND = "SmartStock AI"
TAGLINE = "Smart Thinking Leads to a Brighter Future."
SUSPENDED_MESSAGE = ("Your account is currently suspended. "
                     "Please contact the administrator.")
MIN_PASSWORD = 8
ADMIN_CONTACT_EMAIL = "nadishmanzoor64@gmail.com"
S = st.session_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _auth_error(exc: Exception) -> str:
    text = (getattr(exc, "message", None) or str(exc) or "").lower()
    if "invalid login" in text or "invalid credentials" in text:
        return "Incorrect email or password."
    if "not confirmed" in text:
        return "This account has not finished setup. Use the invitation email to set a password."
    if "rate limit" in text or "too many" in text or "over_" in text:
        return "Too many attempts. Please wait a few minutes and try again."
    if "same password" in text or "different from the old" in text:
        return "The new password must be different from the old one."
    if "weak" in text or "password should" in text:
        return "That password is too weak. Use at least 8 characters with letters and numbers."
    return "Something went wrong. Please try again."


def validate_new_password(password: str, confirm: str) -> str | None:
    """Return an error message, or None when the password is acceptable."""
    if len(password or "") < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one number."
    if password != confirm:
        return "The two passwords do not match."
    return None


def _sign_out_quietly(client) -> None:
    try:
        client.auth.sign_out({"scope": "local"})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------
def is_logged_in(state) -> bool:
    return state.get("sb_client") is not None and bool(state.get("auth"))


def login(state, email: str, password: str):
    """Return (ok, message). On success the client + profile go into session_state."""
    email = (email or "").strip().lower()
    if not email or not password:
        return False, "Enter your email address and password."
    client = sc.new_client()  # fresh client per login, never shared
    try:
        client.auth.sign_in_with_password({"email": email, "password": password})
        profile = client.rpc("my_profile").execute().data or {}
    except Exception as exc:
        _sign_out_quietly(client)
        return False, _auth_error(exc)
    role = profile.get("role")
    if role not in ("super_admin", "shop"):
        _sign_out_quietly(client)
        return False, "This account is not registered with SmartStock AI. Please contact the administrator."
    if role == "shop" and profile.get("status") != "active":
        _sign_out_quietly(client)
        return False, SUSPENDED_MESSAGE
    state["sb_client"] = client
    state["auth"] = {**profile, "email": profile.get("email") or email}
    return True, ""


def refresh_profile(state) -> None:
    """Re-check role/status on every run so suspension takes effect immediately."""
    client = sc.user_client(state)  # raises AuthRequired when expired
    try:
        profile = client.rpc("my_profile").execute().data or {}
    except Exception:
        raise sc.AuthRequired("Could not verify your account. Please log in again.") from None
    role = profile.get("role")
    if role not in ("super_admin", "shop"):
        raise sc.AuthRequired("This account is no longer registered.")
    if role == "shop" and profile.get("status") != "active":
        raise sc.AuthRequired(SUSPENDED_MESSAGE)
    state["auth"] = {**profile, "email": profile.get("email") or state["auth"].get("email", "")}


def logout(state, message: str | None = None) -> None:
    client = state.get("sb_client")
    if client is not None:
        _sign_out_quietly(client)
    state.clear()  # wipes cached data and the client: nothing survives logout
    if message:
        state["_login_notice"] = message


def change_password(state, current: str, new: str, confirm: str):
    problem = validate_new_password(new, confirm)
    if problem:
        return False, problem
    client = sc.user_client(state)
    try:
        email = client.auth.get_user().user.email
        check = sc.new_client()  # verify the current password on a throw-away client
        check.auth.sign_in_with_password({"email": email, "password": current})
        _sign_out_quietly(check)
    except Exception:
        return False, "Your current password is incorrect."
    try:
        client.auth.update_user({"password": new})
    except Exception as exc:
        return False, _auth_error(exc)
    return True, "Password changed."


# ---------------------------------------------------------------------------
# Email links (invitation / password recovery)
# ---------------------------------------------------------------------------
def process_email_link() -> None:
    """Handle ?token_hash=...&type=invite|recovery from the Supabase email templates."""
    params = st.query_params
    token_hash, kind = params.get("token_hash"), params.get("type")
    if not token_hash:
        return
    st.query_params.clear()  # one-time token: never process it twice
    if kind not in ("invite", "recovery"):
        S["_login_notice"] = "That link is not valid."
        return
    client = sc.new_client()
    try:
        result = client.auth.verify_otp({"token_hash": token_hash, "type": kind})
    except Exception:
        S["_login_notice"] = "This link is invalid or has expired. Ask your administrator or use Forgot password."
        return
    S["recovery_client"] = client
    S["recovery_kind"] = kind
    S["recovery_email"] = getattr(getattr(result, "user", None), "email", "") or ""


def render_set_password() -> None:
    kind = S.get("recovery_kind", "recovery")
    st.subheader("Welcome to SmartStock AI" if kind == "invite" else "Reset your password")
    st.caption(f"Choose a password for {S.get('recovery_email') or 'your account'}. "
               f"At least {MIN_PASSWORD} characters with letters and numbers.")
    with st.form("set_password_form"):
        new = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Save password", type="primary")
    if submitted:
        problem = validate_new_password(new, confirm)
        if problem:
            st.error(problem)
        else:
            client = S["recovery_client"]
            try:
                client.auth.update_user({"password": new})
            except Exception as exc:
                st.error(_auth_error(exc))
            else:
                _sign_out_quietly(client)
                for key in ("recovery_client", "recovery_kind", "recovery_email"):
                    S.pop(key, None)
                S["_login_notice"] = "Password saved. You can now log in."
                st.rerun()
    if st.button("Cancel"):
        _sign_out_quietly(S["recovery_client"])
        for key in ("recovery_client", "recovery_kind", "recovery_email"):
            S.pop(key, None)
        st.rerun()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def brand_header() -> None:
    st.title(f"📦 {BRAND}")
    st.caption(TAGLINE)


def render_login() -> None:
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        brand_header()
        notice = S.pop("_login_notice", None)
        if notice:
            (st.error if "suspended" in notice.lower() else st.info)(notice)
        if S.get("_view") == "forgot":
            st.subheader("Forgot password")
            st.info("For security, passwords are reset by the administrator. "
                    "Send the administrator an email with your shop name and the email address you "
                    "log in with. They will send you a new password.")
            st.markdown(f"**Administrator:** [{ADMIN_CONTACT_EMAIL}](mailto:{ADMIN_CONTACT_EMAIL}"
                        "?subject=SmartStock%20AI%20password%20reset)")
            st.caption("After you log in with the new password, change it in Account Settings.")
            if st.button("Back to login"):
                S["_view"] = "login"
                st.rerun()
            return
        st.subheader("Log in")
        with st.form("login_form"):
            email = st.text_input("Email address")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", type="primary")
        if submitted:
            ok, msg = login(S, email, password)
            if ok:
                st.rerun()
            st.error(msg)
        if st.button("Forgot password"):
            S["_view"] = "forgot"
            st.rerun()
        st.caption("Accounts are created by the administrator. If you need access, contact them : its.nadish@gmail.com")


def render_account_settings() -> None:
    st.header("Account Settings")
    profile = S["auth"]
    if profile["role"] == "super_admin":
        st.write(f"**Role:** Super Admin  \n**Email:** {profile.get('email', '')}")
    else:
        st.write(f"**Shop:** {profile['shop_name']}  \n**Owner:** {profile['owner_name']}  \n"
                 f"**Email:** {profile['email']}  \n**Status:** {profile['status'].title()}")
    st.subheader("Change password")
    with st.form("change_password_form", clear_on_submit=True):
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Change password", type="primary")
    if submitted:
        ok, msg = change_password(S, current, new, confirm)
        (st.success if ok else st.error)(msg)
    st.subheader("Log out")
    if st.button("Log out"):
        logout(S)
        st.rerun()
