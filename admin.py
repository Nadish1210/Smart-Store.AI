"""admin.py - Super Admin dashboard and shop management.

Database rules (admin role, 10-shop limit, duplicate emails) are enforced by
SQL functions. The privileged service client is only obtained through
``supabase_client.service_client``, which re-verifies the caller as super admin.
"""
from __future__ import annotations

import re
import secrets
import string
from datetime import date

import httpx
import pandas as pd
import streamlit as st
from postgrest.exceptions import APIError

import authentication as auth
import supabase_client as sc

S = st.session_state
MAX_SHOPS = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HANDLED = (ValueError, sc.PermissionDenied, sc.ConfigError)


class AdminError(ValueError):
    """Friendly error from a database or Auth call."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _db(call):
    try:
        return call()
    except APIError as exc:
        raise AdminError((getattr(exc, "message", None) or str(exc) or "Database error.").strip()) from None
    except httpx.TransportError:
        raise AdminError("Could not reach the database. Please try again.") from None


def _rpc(state, name, params=None):
    client = sc.user_client(state)
    return _db(lambda: client.rpc(name, params or {}).execute().data)


def _auth_call(call):
    try:
        return call()
    except httpx.TransportError:
        raise AdminError("Could not reach Supabase Auth. Please try again.") from None
    except Exception as exc:
        text = (getattr(exc, "message", None) or str(exc) or "").strip()
        if "already" in text.lower() and "regist" in text.lower():
            text = "An authentication account with this email already exists."
        raise AdminError(text or "Authentication service error.") from None


def _clean(shop_name, owner_name, email, phone, status="active") -> dict:
    shop_name, owner_name = (shop_name or "").strip(), (owner_name or "").strip()
    email, phone = (email or "").strip().lower(), (phone or "").strip()
    if not shop_name:
        raise ValueError("Shop name is required.")
    if not owner_name:
        raise ValueError("Owner name is required.")
    if len(shop_name) > 120 or len(owner_name) > 120:
        raise ValueError("Shop and owner names must be at most 120 characters.")
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if len(phone) > 40:
        raise ValueError("Phone number must be at most 40 characters.")
    if status not in ("active", "suspended"):
        raise ValueError("Status must be active or suspended.")
    return {"shop_name": shop_name, "owner_name": owner_name, "email": email,
            "phone": phone, "status": status}


# ---------------------------------------------------------------------------
# Operations (each returns (ok, message, ...))
# ---------------------------------------------------------------------------
def overview(state) -> dict:
    return _rpc(state, "admin_overview")


def list_shops(state) -> list[dict]:
    client = sc.user_client(state)
    return _db(lambda: client.table("shops").select("*").order("shop_number").execute().data)


def generate_password(length: int = 12) -> str:
    """Random password with letters and digits (always at least one of each)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def create_shop(state, shop_name, owner_name, email, phone="", status="active", password=""):
    """Create a shop. The admin chooses the password and shares it; NO email is ever sent."""
    try:
        f = _clean(shop_name, owner_name, email, phone, status)
        problem = auth.validate_new_password(password, password)
        if problem:
            raise ValueError(problem)
        _rpc(state, "admin_can_create_shop", {"p_email": f["email"]})  # limit + duplicate pre-check
        svc = sc.service_client(state)
        result = _auth_call(lambda: svc.auth.admin.create_user({
            "email": f["email"], "password": password, "email_confirm": True,
            "user_metadata": {"shop_name": f["shop_name"]}}))
        user = getattr(result, "user", None)
        if user is None:
            raise AdminError("The login account could not be created.")
        try:
            shop = _rpc(state, "admin_register_shop", {
                "p_user_id": user.id, "p_shop_name": f["shop_name"], "p_owner_name": f["owner_name"],
                "p_email": f["email"], "p_phone": f["phone"], "p_status": f["status"]})
        except Exception:
            try:  # roll back the auth user so no orphan account remains
                svc.auth.admin.delete_user(user.id)
            except Exception:
                pass
            raise
        return True, f"Shop '{f['shop_name']}' created. Share the login details below with the owner.", shop
    except HANDLED as exc:
        return False, str(exc), None


def update_shop(state, shop: dict, shop_name, owner_name, email, phone=""):
    try:
        f = _clean(shop_name, owner_name, email, phone, shop["status"])
        changed_email = f["email"] != shop["email"].lower()
        params = {"p_shop_id": shop["id"], "p_shop_name": f["shop_name"], "p_owner_name": f["owner_name"],
                  "p_email": f["email"], "p_phone": f["phone"]}
        if not changed_email:
            _rpc(state, "admin_update_shop", params)
            return True, "Shop details saved."
        if not _rpc(state, "admin_email_available", {"p_email": f["email"], "p_exclude": shop["id"]}):
            raise AdminError("A shop with this email address already exists.")
        svc = sc.service_client(state)
        _auth_call(lambda: svc.auth.admin.update_user_by_id(
            shop["owner_user_id"], {"email": f["email"], "email_confirm": True}))
        try:
            _rpc(state, "admin_update_shop", params)
        except Exception:
            try:  # revert the login email so Auth and the shop record stay in sync
                svc.auth.admin.update_user_by_id(shop["owner_user_id"], {"email": shop["email"], "email_confirm": True})
            except Exception:
                pass
            raise
        return True, "Shop details saved. The login email was updated too."
    except HANDLED as exc:
        return False, str(exc)


def set_status(state, shop_id: str, status: str):
    try:
        _rpc(state, "admin_set_shop_status", {"p_shop_id": shop_id, "p_status": status})
        return True, ("Shop activated." if status == "active"
                      else "Shop suspended. Its data is kept and it is blocked immediately.")
    except HANDLED as exc:
        return False, str(exc)


def set_password(state, shop: dict, password: str):
    """Admin sets a new password for a shop owner (to be shared with them)."""
    try:
        problem = auth.validate_new_password(password, password)
        if problem:
            raise ValueError(problem)
        svc = sc.service_client(state)
        _auth_call(lambda: svc.auth.admin.update_user_by_id(shop["owner_user_id"], {"password": password}))
        return True, "New password saved. Share it with the shop owner."
    except HANDLED as exc:
        return False, str(exc)


def export_shop_json(state, shop_id: str) -> str:
    import json
    return json.dumps(_rpc(state, "admin_export_shop", {"p_shop_id": shop_id}), indent=2, ensure_ascii=False)


def delete_shop(state, shop: dict):
    """Delete the shop's login; the database cascades to ALL of its data."""
    try:
        svc = sc.service_client(state)
        _auth_call(lambda: svc.auth.admin.delete_user(shop["owner_user_id"]))
        return True, f"Shop '{shop['shop_name']}' and all of its data were permanently deleted."
    except HANDLED as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _flash(level: str, message: str) -> None:
    S["_flash"] = (level, message)


def _show_flash() -> None:
    item = S.pop("_flash", None)
    if item:
        getattr(st, item[0])(item[1])


def _shops_df(shops: list[dict]) -> pd.DataFrame:
    rows = [{
        "Shop ID": f"SHOP-{s['shop_number']:03d}", "Shop Name": s["shop_name"],
        "Owner Name": s["owner_name"], "Email": s["email"], "Phone Number": s["phone"] or "-",
        "Account Status": s["status"].title(), "Registration Date": str(s["created_at"])[:10],
        "Actions": "Select below",
    } for s in shops]
    return pd.DataFrame(rows, columns=["Shop ID", "Shop Name", "Owner Name", "Email", "Phone Number",
                                       "Account Status", "Registration Date", "Actions"])


def _table(df: pd.DataFrame) -> None:
    try:
        st.dataframe(df, hide_index=True, width="stretch")
    except Exception:
        st.dataframe(df, hide_index=True, use_container_width=True)


def page_admin_dashboard(state) -> None:
    st.header("Admin Dashboard")
    _show_flash()
    ov = overview(state)
    c = st.columns(4)
    c[0].metric("Total Shops", ov["total"], border=True)
    c[1].metric("Active Shops", ov["active"], border=True)
    c[2].metric("Suspended Shops", ov["suspended"], border=True)
    c[3].metric("Available Shop Slots", f"{ov['slots']} of {ov['max_shops']}", border=True)
    if ov["slots"] == 0:
        st.warning("The maximum of 10 shops has been reached. Delete a shop to free a slot.")
    st.progress(min(1.0, ov["total"] / ov["max_shops"]), text=f"{ov['total']} of {ov['max_shops']} shop slots used")
    st.subheader("Shops")
    shops = list_shops(state)
    if not shops:
        st.info("No shops yet. Open Shop Management to create the first one.")
    else:
        _table(_shops_df(shops).drop(columns=["Actions"]))


def _show_new_credentials() -> None:
    creds = S.pop("new_creds", None)
    if creds:
        st.warning("Copy these login details now and send them to the shop owner. "
                   "The password is shown only this once.")
        st.code(f"Shop: {creds['shop']}\nEmail: {creds['email']}\nPassword: {creds['password']}\n"
                f"Login at: {sc.app_url() or 'your SmartStock AI app link'}", language=None)
        st.caption("Tell the owner to change the password in Account Settings after the first login.")


def page_shop_management(state) -> None:
    st.header("Shop Management")
    _show_flash()
    _show_new_credentials()
    ov = overview(state)
    shops = list_shops(state)

    with st.expander("Create Shop", expanded=not shops):
        if ov["slots"] == 0:
            st.error("Shop limit reached (10). Delete a shop before creating another.")
        else:
            st.caption(f"{ov['slots']} slot(s) available.")
            st.caption("You choose the owner's password and share it with them. No email is sent.")
            if "gen_pw" not in S:
                S["gen_pw"] = generate_password()
            nonce = S.setdefault("create_nonce", 0)  # new form key after success = cleared fields
            with st.form(f"create_shop_{nonce}"):
                name = st.text_input("Shop Name *")
                owner = st.text_input("Owner Name *")
                email = st.text_input("Email Address (used to log in) *")
                phone = st.text_input("Phone Number (optional)")
                status = st.selectbox("Account Status", ["active", "suspended"],
                                      format_func=str.title)
                password = st.text_input("Password for the owner", value=S["gen_pw"],
                                         help="A random password is suggested. You can type your own "
                                              "(8+ characters, letters and numbers).")
                if st.form_submit_button("Create Shop", type="primary"):
                    ok, msg, shop = create_shop(state, name, owner, email, phone, status, password)
                    if ok:
                        S["create_nonce"] = nonce + 1
                        S["new_creds"] = {"shop": shop["shop_name"], "email": shop["email"],
                                          "password": password}
                        S["gen_pw"] = generate_password()
                        _flash("success", msg)
                        st.rerun()
                    st.error(msg)

    st.subheader("All shops")
    if not shops:
        st.info("No shops registered yet.")
        return
    _table(_shops_df(shops))

    st.subheader("Manage a shop")
    by_id = {s["id"]: s for s in shops}
    if S.get("manage_shop") not in by_id:
        S.pop("manage_shop", None)
    shop_id = st.selectbox("Select shop", list(by_id), key="manage_shop",
                           format_func=lambda i: f"SHOP-{by_id[i]['shop_number']:03d} - {by_id[i]['shop_name']}")
    shop = by_id[shop_id]
    st.write(f"**Status:** {shop['status'].title()}  |  **Registered:** {str(shop['created_at'])[:10]}")

    if shop["status"] == "active":
        if st.button("Suspend Shop", key=f"sus_{shop_id}"):
            ok, msg = set_status(state, shop_id, "suspended")
            _flash("success" if ok else "error", msg)
            st.rerun()
    else:
        if st.button("Activate Shop", key=f"act_{shop_id}", type="primary"):
            ok, msg = set_status(state, shop_id, "active")
            _flash("success" if ok else "error", msg)
            st.rerun()

    with st.expander("Set a new password for this shop"):
        st.caption("Use this when the owner forgot the password. Then share the new password with them.")
        new_pw = st.text_input("New password", value=generate_password(), key=f"setpw_{shop_id}")
        if st.button("Save new password", key=f"savepw_{shop_id}"):
            ok, msg = set_password(state, shop, new_pw)
            if ok:
                S["new_creds"] = {"shop": shop["shop_name"], "email": shop["email"], "password": new_pw}
            _flash("success" if ok else "error", msg)
            st.rerun()

    with st.expander("Edit Shop"):
        with st.form(f"edit_shop_{shop_id}"):
            name = st.text_input("Shop Name *", shop["shop_name"])
            owner = st.text_input("Owner Name *", shop["owner_name"])
            email = st.text_input("Email Address *", shop["email"])
            phone = st.text_input("Phone Number", shop["phone"])
            if st.form_submit_button("Save changes", type="primary"):
                ok, msg = update_shop(state, shop, name, owner, email, phone)
                if ok:
                    _flash("success", msg)
                    st.rerun()
                st.error(msg)

    with st.expander("Delete Shop"):
        st.error("Permanent deletion removes this shop's login AND all of its products, suppliers, "
                 "sales and stock history. This cannot be undone. To keep the data without "
                 "deleting it, use Suspend Shop instead.")
        key = f"export_{shop_id}"
        if st.button("Prepare data export (JSON)", key=f"prep_{shop_id}"):
            try:
                S[key] = export_shop_json(state, shop_id)
            except HANDLED as exc:
                st.error(str(exc))
        if S.get(key):
            st.download_button("Download shop data before deleting", S[key],
                               f"{shop['shop_name'].replace(' ', '_')}_backup_{date.today()}.json",
                               "application/json", key=f"dl_{shop_id}")
        typed = st.text_input(f"Type the shop name ({shop['shop_name']}) to confirm", key=f"typed_{shop_id}")
        sure = st.checkbox("I understand this permanently deletes the shop and its data", key=f"sure_{shop_id}")
        if st.button("Delete Shop permanently", disabled=not (sure and typed.strip() == shop["shop_name"]),
                     key=f"del_{shop_id}"):
            ok, msg = delete_shop(state, shop)
            S.pop(key, None)
            _flash("success" if ok else "error", msg)
            st.rerun()
        
