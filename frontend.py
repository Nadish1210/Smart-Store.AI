"""frontend.py - Streamlit interface for SmartStock AI v2 (multi-user, Supabase).

Run with:  streamlit run frontend.py
All business logic lives in backend.py; this file only builds the UI.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

import admin
import authentication as auth
import backend as be
import supabase_client as sc

CURRENCY = "Rs "
PAGES = ["Dashboard", "Products", "Add Product", "Stock IN", "Sales / Stock OUT",
         "Camera Scanner", "Suppliers", "AI Forecast", "Reports & Backup"]
SHOP_PAGES = PAGES + ["Account Settings"]
ADMIN_PAGES = ["Admin Dashboard", "Shop Management", "Account Settings"]

st.set_page_config(page_title="SmartStock AI", page_icon="📦", layout="wide")
be.init_state(st.session_state)
S = st.session_state
S.setdefault("restore_nonce", 0)

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1250px;}
      h1, h2, h3 {letter-spacing: -0.01em;}
      @media (max-width: 640px) {.block-container {padding-left: .8rem; padding-right: .8rem;}}
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------
# Small UI helpers
# --------------------------------------------------------------------------
def money(value: float) -> str:
    return f"{CURRENCY}{value:,.1f}".rstrip("0").rstrip(".")


def flash(level: str, message: str) -> None:
    S["_flash"] = (level, message)


def show_flash() -> None:
    item = S.pop("_flash", None)
    if item:
        getattr(st, item[0])(item[1])


def show_df(df: pd.DataFrame, money_cols=()) -> None:
    config = {
        c: st.column_config.NumberColumn(c.replace("_", " ").title(), format=f"{CURRENCY}%.1f")
        for c in money_cols if c in df.columns
    }
    try:
        st.dataframe(df, hide_index=True, column_config=config, width="stretch")
    except Exception:
        st.dataframe(df, hide_index=True, column_config=config, use_container_width=True)


def show_chart(fig) -> None:
    fig.update_layout(margin=dict(l=10, r=10, t=50, b=10), height=330, legend_title_text="")
    try:
        st.plotly_chart(fig, width="stretch")
    except Exception:
        st.plotly_chart(fig, use_container_width=True)


def goto(page: str, prefix: str | None = None, barcode: str | None = None) -> None:
    """Button callback: jump to another page, optionally pre-filling a barcode."""
    S["nav"] = page
    if prefix and barcode:
        S[f"{prefix}_mode"] = "Type barcode"
        S[f"{prefix}_bc"] = barcode


def lookup_feedback(raw: str):
    """Look a barcode up and show a friendly message for every outcome."""
    status, product = be.lookup_barcode(S, raw)
    if status == "invalid":
        st.error("Invalid barcode: use 1-40 printable characters with no spaces.")
    elif status == "unknown":
        st.warning(f"No product has barcode `{raw.strip()}`. Check the digits or add it on the Add Product page.")
    elif status == "found":
        st.success(f"Found: {product['name']}")
    return product


def scan_camera(prefix: str):
    """Camera or photo upload; return the decoded barcode text, or None."""
    shot = st.camera_input("Point the camera at a barcode and take a photo", key=f"{prefix}_cam")
    upload = st.file_uploader("...or upload a photo of the barcode",
                              type=["png", "jpg", "jpeg", "webp"], key=f"{prefix}_up")
    image = shot or upload
    if image is None:
        return None
    codes, error = be.decode_barcode_image(image.getvalue())
    if error:
        st.error(error)
        return None
    if not codes:
        st.warning("No barcode found in the photo. Move closer, avoid glare, keep the barcode "
                   "flat and in focus, then retake it. You can also type the barcode manually.")
        return None
    if len(codes) > 1:
        st.info(f"{len(codes)} barcodes detected; using the first one.")
    st.caption(f"Decoded `{codes[0]['text']}` ({codes[0]['format']})")
    return codes[0]["text"]


def product_picker(prefix: str):
    """Pick a product from a list, by typed barcode, or by camera. Returns product or None."""
    products = be.list_products(S)
    if not products:
        st.info("No products yet. Add a product first.")
        return None
    modes = ["Choose from list", "Type barcode", "Scan with camera"]
    if S.get(f"{prefix}_mode") not in modes:
        S[f"{prefix}_mode"] = modes[0]
    mode = st.radio("Find the product by", modes, horizontal=True, key=f"{prefix}_mode")
    if mode == "Choose from list":
        ids = [p["product_id"] for p in products]
        labels = {p["product_id"]: f"{p['name']} ({p['barcode']})" for p in products}
        key = f"{prefix}_pid"
        if S.get(key) not in ids:
            S.pop(key, None)
        return be.get_product(S, st.selectbox("Product", ids, format_func=labels.get, key=key))
    if mode == "Type barcode":
        raw = st.text_input("Barcode", key=f"{prefix}_bc", placeholder="Type or paste the barcode value")
        return lookup_feedback(raw) if raw.strip() else None
    raw = scan_camera(prefix)
    return lookup_feedback(raw) if raw else None


def select_product(label: str, key: str):
    products = be.list_products(S)
    if not products:
        st.info("No products yet. Add one on the Add Product page.")
        return None
    ids = [p["product_id"] for p in products]
    names = {p["product_id"]: f"{p['name']} ({p['barcode']})" for p in products}
    if S.get(key) not in ids:
        S.pop(key, None)
    return be.get_product(S, st.selectbox(label, ids, format_func=names.get, key=key))


def product_summary(p: dict) -> None:
    c = st.columns(4)
    c[0].metric("In stock", p["quantity"], border=True)
    c[1].metric("Selling price", money(p["selling_price"]), border=True)
    c[2].metric("Purchase price", money(p["purchase_price"]), border=True)
    c[3].metric("Reorder level", p["reorder_level"], border=True)
    if p["quantity"] <= 0:
        st.error("This product is out of stock.")
    elif p["quantity"] <= p["reorder_level"]:
        st.warning("Stock is at or below the reorder level.")


def supplier_options(current: str = "") -> list[str]:
    options = ["(none)"] + be.supplier_names(S)
    if current and current not in options:
        options.append(current)
    return options


def label_downloads(p: dict, key: str) -> None:
    try:
        png = be.make_label_png(p["name"], p["barcode"], p["selling_price"], CURRENCY)
    except Exception as exc:
        st.error(f"Could not generate the barcode: {exc}")
        return
    st.image(png, width=360)
    copies = st.number_input("Copies for the print sheet", 1, 240, 24, 1, key=f"{key}_copies")
    c1, c2 = st.columns(2)
    c1.download_button("Download label (PNG)", png, f"label_{p['barcode']}.png", "image/png",
                       key=f"{key}_png")
    c2.download_button("Download print sheet (PDF)", be.labels_pdf(png, int(copies)),
                       f"labels_{p['barcode']}.pdf", "application/pdf", key=f"{key}_pdf")


def date_range_input(key: str):
    today = date.today()
    value = st.date_input("Date range", value=(today - timedelta(days=30), today), key=key)
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            return value[0], value[1]
        if len(value) == 1:
            return value[0], value[0]
        return today - timedelta(days=30), today
    return value, value


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
def page_dashboard() -> None:
    st.header("Dashboard")
    m = be.dashboard_metrics(S)
    if m["total_products"] == 0 and m["sales_count"] == 0:
        st.info("Your store is empty. Add products on the Add Product page, or load demo data "
                "from Reports & Backup > Data tools.")
    row1 = st.columns(4)
    row1[0].metric("Total products", f"{m['total_products']:,}", border=True)
    row1[1].metric("Stock units", f"{m['total_units']:,}", border=True)
    row1[2].metric("Inventory valuation (at cost)", money(m["valuation"]), border=True)
    row1[3].metric("Sales revenue", money(m["revenue"]), border=True)
    row2 = st.columns(4)
    row2[0].metric("Gross profit", money(m["profit"]), f"{m['margin']}% margin" if m["revenue"] else None, border=True)
    row2[1].metric("Low-stock products", m["low_stock"], border=True)
    row2[2].metric("Out of stock", m["out_of_stock"], border=True)
    row2[3].metric("Sales recorded", f"{m['sales_count']:,}", border=True)

    low = be.low_stock_report(S)
    out, warn = low[low["status"] == "Out of stock"], low[low["status"] == "Low stock"]
    if not out.empty:
        st.error(f"Out of stock ({len(out)}): " + ", ".join(out["name"].head(10)))
    if not warn.empty:
        st.warning(f"Low stock ({len(warn)}): " + ", ".join(
            f"{r.name} ({r.quantity}/{r.reorder_level})" for r in warn.head(10).itertuples()))
    if low.empty and m["total_products"]:
        st.success("All products are above their reorder levels.")
    if not low.empty:
        with st.expander("Low-stock details"):
            show_df(low)

    sales, products = be.sales_df(S), be.products_df(S)
    left, right = st.columns(2)
    with left:
        if sales.empty:
            st.info("Revenue and profit charts appear after the first sale.")
        else:
            sales["date"] = pd.to_datetime(sales["timestamp"]).dt.date
            daily = sales.groupby("date")[["total", "profit"]].sum().reset_index().tail(30)
            daily = daily.rename(columns={"total": "Revenue", "profit": "Profit"})
            show_chart(px.bar(daily, x="date", y=["Revenue", "Profit"], barmode="group",
                              title="Revenue and profit by day (last 30 sale days)"))
    with right:
        if sales.empty:
            st.info("Top-product chart appears after the first sale.")
        else:
            top = sales.groupby("product_name")["total"].sum().nlargest(8).reset_index()
            show_chart(px.bar(top, x="total", y="product_name", orientation="h",
                              title="Top products by revenue",
                              labels={"total": "Revenue", "product_name": ""}).update_yaxes(autorange="reversed"))
    left, right = st.columns(2)
    with left:
        if products.empty:
            st.info("Category chart appears after you add products.")
        else:
            products["value"] = products["quantity"] * products["purchase_price"]
            cat = products.groupby("category")["value"].sum().reset_index()
            show_chart(px.pie(cat, names="category", values="value", hole=0.45,
                              title="Inventory valuation by category"))
    with right:
        if products.empty:
            st.info("Stock chart appears after you add products.")
        else:
            lowest = products.nsmallest(10, "quantity")[["name", "quantity", "reorder_level"]]
            lowest = lowest.rename(columns={"quantity": "Stock", "reorder_level": "Reorder level"})
            show_chart(px.bar(lowest, x="name", y=["Stock", "Reorder level"], barmode="group",
                              title="Lowest-stock products vs reorder level",
                              labels={"name": ""}))
    st.subheader("Recent sales")
    if sales.empty:
        st.caption("No sales yet.")
    else:
        recent = sales.tail(10).iloc[::-1][["timestamp", "product_name", "quantity", "total", "profit"]]
        show_df(recent, ("total", "profit"))


def page_products() -> None:
    st.header("Products")
    show_flash()
    tab_inv, tab_edit, tab_labels = st.tabs(["Inventory", "Edit / Delete", "Barcode labels"])
    with tab_inv:
        c1, c2, c3 = st.columns([2, 2, 1])
        query = c1.text_input("Search", placeholder="Name, barcode, ID, category or supplier")
        cats = c2.multiselect("Category", be.categories(S))
        status = c3.selectbox("Status", ["All", "In stock", "Low stock", "Out of stock"])
        df = be.search_products(S, query, cats, status)
        if be.list_products(S) and df.empty:
            st.warning("No products match your filters.")
        elif not be.list_products(S):
            st.info("No products yet. Use Add Product to create the first one.")
        else:
            st.caption(f"{len(df)} of {len(be.list_products(S))} products")
            show_df(df.drop(columns=["created_at"]), ("purchase_price", "selling_price"))
            p = select_product("View product details", "view_pid")
            if p:
                product_summary(p)
                details = {k.replace("_", " ").title(): ("-" if v == "" else v) for k, v in p.items()}
                st.dataframe(pd.DataFrame({"Field": list(details), "Value": [str(v) for v in details.values()]}),
                             hide_index=True)
    with tab_edit:
        p = select_product("Product to edit", "edit_pid")
        if p:
            pid = p["product_id"]
            with st.form(f"edit_form_{pid}"):
                name = st.text_input("Product name", p["name"])
                c1, c2 = st.columns(2)
                category = c1.text_input("Category", p["category"])
                barcode = c2.text_input("Barcode", p["barcode"])
                c1, c2 = st.columns(2)
                cost = c1.number_input("Purchase price", 0.0, value=float(p["purchase_price"]), step=0.01, format="%.1f")
                price = c2.number_input("Selling price", 0.0, value=float(p["selling_price"]), step=0.01, format="%.1f")
                c1, c2 = st.columns(2)
                qty = c1.number_input("Quantity", 0, value=int(p["quantity"]), step=1,
                                      help="Changing this records an ADJUST stock movement.")
                reorder = c2.number_input("Reorder level", 0, value=int(p["reorder_level"]), step=1)
                options = supplier_options(p["supplier"])
                supplier = st.selectbox("Supplier", options,
                                        index=options.index(p["supplier"]) if p["supplier"] in options else 0)
                expiry = st.date_input("Expiry date (optional)",
                                       value=date.fromisoformat(p["expiry_date"]) if p["expiry_date"] else None)
                if st.form_submit_button("Save changes", type="primary"):
                    ok, msg, _ = be.edit_product(S, pid, name, category, cost, price, qty, reorder, barcode,
                                                 "" if supplier == "(none)" else supplier, expiry)
                    if ok:
                        flash("success", msg)
                        st.rerun()
                    st.error(msg)
            st.divider()
            confirm = st.checkbox("I understand this removes the product from inventory", key=f"del_ok_{pid}")
            if st.button("Delete product", disabled=not confirm, key=f"del_{pid}"):
                ok, msg = be.delete_product(S, pid)
                flash("success" if ok else "error", msg)
                st.rerun()
    with tab_labels:
        p = select_product("Product", "label_pid")
        if p:
            label_downloads(p, "prodlabel")


def page_add_product() -> None:
    st.header("Add Product")
    with st.form("add_product", clear_on_submit=True):
        name = st.text_input("Product name *")
        c1, c2 = st.columns(2)
        category = c1.text_input("Category *", placeholder="e.g. Dairy")
        barcode = c2.text_input("Barcode", placeholder="Leave blank to auto-generate")
        c1, c2 = st.columns(2)
        cost = c1.number_input("Purchase price *", 0.0, value=0.0, step=0.01, format="%.1f")
        price = c2.number_input("Selling price *", 0.0, value=0.0, step=0.01, format="%.1f")
        c1, c2 = st.columns(2)
        qty = c1.number_input("Opening quantity", 0, value=0, step=1)
        reorder = c2.number_input("Reorder level", 0, value=5, step=1)
        supplier = st.selectbox("Supplier (optional)", supplier_options())
        expiry = st.date_input("Expiry date (optional)", value=None)
        submitted = st.form_submit_button("Add product", type="primary")
    if be.categories(S):
        st.caption("Existing categories: " + ", ".join(be.categories(S)))
    if submitted:
        ok, msg, product = be.add_product(S, name, category, cost, price, qty, reorder, barcode,
                                          "" if supplier == "(none)" else supplier, expiry)
        if ok:
            S["last_added_id"] = product["product_id"]
            if price < cost:
                st.warning("Selling price is below purchase price: sales of this product will lose money.")
            st.success(msg)
        else:
            st.error(msg)
    last = be.get_product(S, S.get("last_added_id"))
    if last:
        st.subheader(f"Barcode label for {last['name']}")
        label_downloads(last, "newlabel")


def page_stock_in() -> None:
    st.header("Stock IN")
    show_flash()
    st.caption("Receive stock for an existing product. This never creates a new product.")
    product = product_picker("stockin")
    if product:
        product_summary(product)
        with st.form("stock_in_form", clear_on_submit=True):
            qty = st.number_input("Received quantity", 1, value=1, step=1)
            note = st.text_area("Delivery notes", placeholder="Supplier, invoice number, condition...")
            if st.form_submit_button("Add to stock", type="primary"):
                ok, msg, _ = be.stock_in(S, product["product_id"], qty, note)
                flash("success" if ok else "error", msg)
                st.rerun()
    st.subheader("Recent deliveries")
    moves = be.movements_df(S)
    moves = moves[moves["type"] == "IN"].tail(10).iloc[::-1]
    if moves.empty:
        st.caption("No deliveries recorded yet.")
    else:
        show_df(moves[["timestamp", "product_name", "quantity_change", "quantity_after", "note"]])


def _sell(product_id: str) -> None:
    ok, msg, _ = be.record_sale(S, product_id, S.get("sale_qty", 1))
    flash("success" if ok else "error", msg)
    if ok:
        S["sale_qty"] = 1


def page_sales() -> None:
    st.header("Sales / Stock OUT")
    show_flash()
    product = product_picker("sale")
    if product:
        product_summary(product)
        if product["quantity"] <= 0:
            st.error("Cannot sell: no units available.")
        else:
            qty = st.number_input("Quantity sold", 1, step=1, key="sale_qty")
            total = round(qty * product["selling_price"], 2)
            profit = round(total - round(qty * product["purchase_price"], 2), 2)
            c1, c2 = st.columns(2)
            c1.metric("Sale total", money(total), border=True)
            c2.metric("Gross profit", money(profit), border=True)
            too_many = qty > product["quantity"]
            if too_many:
                st.error(f"Only {product['quantity']} unit(s) available.")
            st.button("Record sale", type="primary", disabled=too_many, on_click=_sell,
                      args=(product["product_id"],))
    st.subheader("Recent sales")
    sales = be.sales_df(S)
    if sales.empty:
        st.caption("No sales yet.")
    else:
        show_df(sales.tail(10).iloc[::-1][["timestamp", "product_name", "quantity", "unit_price",
                                           "total", "profit"]], ("unit_price", "total", "profit"))


def page_scanner() -> None:
    st.header("Camera Scanner")
    st.caption("Scan with a laptop or phone camera, upload a photo, or type the barcode.")
    method = st.radio("Input method", ["Camera / photo", "Type barcode"], horizontal=True, key="scan_method")
    if method == "Camera / photo":
        raw = scan_camera("scanpage")
    else:
        raw = st.text_input("Barcode", key="scan_manual", placeholder="Type or paste the barcode value")
    if not raw or not raw.strip():
        return
    product = lookup_feedback(raw)
    if not product:
        return
    st.subheader(product["name"])
    st.caption(f"{product['category']} | ID {product['product_id']} | Barcode {product['barcode']}"
               + (f" | Supplier {product['supplier']}" if product["supplier"] else "")
               + (f" | Expires {product['expiry_date']}" if product["expiry_date"] else ""))
    product_summary(product)
    c1, c2 = st.columns(2)
    c1.button("Receive stock", on_click=goto, args=("Stock IN", "stockin", product["barcode"]))
    c2.button("Sell this product", on_click=goto, args=("Sales / Stock OUT", "sale", product["barcode"]))


def page_suppliers() -> None:
    st.header("Suppliers")
    show_flash()
    tab_list, tab_add, tab_edit = st.tabs(["All suppliers", "Add supplier", "Edit / Delete"])
    with tab_list:
        df = be.suppliers_df(S)
        if df.empty:
            st.info("No suppliers yet.")
        else:
            show_df(df)
    with tab_add:
        with st.form("add_supplier", clear_on_submit=True):
            name = st.text_input("Supplier name *")
            c1, c2 = st.columns(2)
            contact, phone = c1.text_input("Contact person"), c2.text_input("Phone")
            email, address = st.text_input("Email"), st.text_input("Address")
            notes = st.text_area("Notes")
            if st.form_submit_button("Add supplier", type="primary"):
                ok, msg, _ = be.add_supplier(S, name, contact, phone, email, address, notes)
                if ok:
                    flash("success", msg)
                    st.rerun()
                st.error(msg)
    with tab_edit:
        suppliers = be.list_suppliers(S)
        if not suppliers:
            st.info("No suppliers to edit.")
            return
        ids = [s["supplier_id"] for s in suppliers]
        if S.get("edit_sup") not in ids:
            S.pop("edit_sup", None)
        names = {s["supplier_id"]: s["name"] for s in suppliers}
        sid = st.selectbox("Supplier", ids, format_func=names.get, key="edit_sup")
        s = next(x for x in suppliers if x["supplier_id"] == sid)
        with st.form(f"edit_supplier_{sid}"):
            name = st.text_input("Supplier name *", s["name"])
            c1, c2 = st.columns(2)
            contact = c1.text_input("Contact person", s["contact_person"])
            phone = c2.text_input("Phone", s["phone"])
            email, address = st.text_input("Email", s["email"]), st.text_input("Address", s["address"])
            notes = st.text_area("Notes", s["notes"])
            if st.form_submit_button("Save changes", type="primary"):
                ok, msg, _ = be.edit_supplier(S, sid, name, contact, phone, email, address, notes)
                if ok:
                    flash("success", msg)
                    st.rerun()
                st.error(msg)
        confirm = st.checkbox("I understand this deletes the supplier", key=f"del_sup_ok_{sid}")
        if st.button("Delete supplier", disabled=not confirm, key=f"del_sup_{sid}"):
            ok, msg = be.delete_supplier(S, sid)
            flash("success" if ok else "error", msg)
            st.rerun()


def page_forecast() -> None:
    st.header("AI Forecast")
    c1, c2 = st.columns(2)
    horizon = c1.slider("Forecast period (days)", 1, 90, 14)
    lookback = c2.slider("Sales history to average (days)", 7, 90, 30)

    st.subheader("Basic estimate")
    st.info("**Basic estimate: machine learning.** Expected demand = average daily units sold "
            "over the history window x forecast days. Suggested reorder = expected demand + "
            "reorder level - current stock.")
    df = be.forecast_table(S, horizon, lookback)
    if df.empty:
        st.warning("Add products and record sales to see forecasts.")
        return
    if be.sales_df(S).empty:
        st.warning("No sales recorded yet, so every average is zero. Estimates improve as sales accumulate.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Possible shortages", int((df["outlook"] == "Possible shortage").sum()), border=True)
    c2.metric("Reorder soon", int((df["outlook"] == "Reorder soon").sum()), border=True)
    c3.metric("Units to reorder", int(df["suggested_reorder_qty"].sum()), border=True)
    show_df(df)
    st.download_button("Download estimate (CSV)", be.df_to_csv_bytes(df),
                       f"forecast_basic_{date.today()}.csv", "text/csv")

    st.divider()
    st.subheader("Trained ML prediction (per product)")
    st.info("**Trained model.** Fits a scikit-learn linear regression (trend + day-of-week pattern) to one "
            "product's daily sales. Needs at least 21 days of history with 7 or more selling days. "
            "It is a CNN model: treat results as a guide.")
    product = select_product("Product", "ml_pid")
    if product and st.button("Train model and predict", type="primary"):
        result = be.ml_forecast(S, product["product_id"], horizon)
        if not result["ok"]:
            st.warning(result["message"])
            return
        m1, m2 = st.columns(2)
        m1.metric(f"Predicted demand, next {horizon} days", f"{result['total_units']:g} units", border=True)
        m2.metric("Suggested reorder", f"{result['suggested_reorder_qty']} units", border=True)
        if result["mae"] is not None:
            st.caption(f"Back-test on the last 7 days: model error {result['mae']:.2f} units/day vs "
                       f"{result['baseline_mae']:.2f} for a flat average. Trained on "
                       f"{result['training_days']} days ({result['selling_days']} selling days).")
        else:
            st.caption(f"Trained on {result['training_days']} days; not enough history for a back-test.")
        hist = result["history"].assign(Series="Actual sales").tail(60)
        fut = result["forecast"].assign(Series="ML prediction")
        show_chart(px.line(pd.concat([hist, fut]), x="date", y="units", color="Series",
                           title=f"{result['product']}: daily units", labels={"units": "Units", "date": ""}))


def report_block(title: str, df: pd.DataFrame, slug: str, summary=None, money_cols=()) -> None:
    show_df(df, money_cols)
    if df.empty:
        st.info("No records for this report yet.")
    c1, c2 = st.columns(2)
    c1.download_button("Download CSV", be.df_to_csv_bytes(df), f"{slug}_{date.today()}.csv",
                       "text/csv", key=f"csv_{slug}")
    c2.download_button("Download PDF", be.build_pdf_report(title, df, summary),
                       f"{slug}_{date.today()}.pdf", "application/pdf", key=f"pdf_{slug}")


def page_reports() -> None:
    st.header("Reports & Backup")
    show_flash()
    section = st.radio("Section", ["Inventory", "Sales", "Stock movements", "Profit summary",
                                   "Low stock", "Backup & restore"], horizontal=True, key="report_section")
    if section != "Backup & restore" and section not in ("Inventory", "Low stock"):
        start, end = date_range_input("report_range")
    if section == "Inventory":
        inv = be.inventory_report(S)
        report_block("Inventory report", inv, "inventory",
                     [f"Products: {len(inv)}", f"Units: {int(inv['quantity'].sum())}",
                      f"Stock value at cost: {money(float(inv['stock_value_cost'].sum()))}"],
                     ("purchase_price", "selling_price", "stock_value_cost", "stock_value_retail"))
    elif section == "Sales":
        sales = be.filter_by_date(be.sales_df(S), start, end)
        report_block("Sales report", sales, "sales",
                     [f"Period: {start} to {end}", f"Sales: {len(sales)}",
                      f"Revenue: {money(float(sales['total'].sum()))}"],
                     ("unit_price", "unit_cost", "total", "profit"))
    elif section == "Stock movements":
        moves = be.filter_by_date(be.movements_df(S), start, end)
        report_block("Stock movement report", moves, "stock_movements",
                     [f"Period: {start} to {end}", f"Movements: {len(moves)}"])
    elif section == "Profit summary":
        summary, by_product = be.profit_summary(S, start, end)
        c = st.columns(4)
        c[0].metric("Revenue", money(summary["revenue"]), border=True)
        c[1].metric("Cost of goods", money(summary["cost_of_goods"]), border=True)
        c[2].metric("Gross profit", money(summary["gross_profit"]), border=True)
        c[3].metric("Margin", f"{summary['margin_pct']}%", border=True)
        report_block("Profit summary", by_product, "profit_summary",
                     [f"Period: {start} to {end}", f"Transactions: {summary['transactions']}",
                      f"Units sold: {summary['units_sold']}", f"Revenue: {money(summary['revenue'])}",
                      f"Cost of goods: {money(summary['cost_of_goods'])}",
                      f"Gross profit: {money(summary['gross_profit'])} ({summary['margin_pct']}%)"],
                     ("revenue", "cost", "profit"))
    elif section == "Low stock":
        low = be.low_stock_report(S)
        report_block("Low-stock report", low, "low_stock", [f"Products needing attention: {len(low)}"])
    else:
        backup_section()


def backup_section() -> None:
    st.subheader("Backup")
    st.caption("One JSON file with your shop's products, sales, stock movements and suppliers.")
    st.download_button("Download backup (JSON)", be.backup_to_json(S),
                       f"smartstock_backup_{date.today()}.json", "application/json")
    st.subheader("Restore")
    upload = st.file_uploader("Upload a SmartStock AI backup", type=["json"],
                              key=f"restore_file_{S['restore_nonce']}")
    if upload is not None:
        data, error = be.parse_backup_bytes(upload.getvalue())
        cleaned, errors = (None, [error]) if error else be.validate_backup(data)
        if errors:
            st.error("This backup cannot be restored:")
            for item in errors:
                st.write(f"- {item}")
        else:
            st.success(f"Backup is valid: {be.backup_summary(cleaned)}.")
            st.warning("Restoring replaces ALL current products, sales, movements and suppliers.")
            ok = st.checkbox("Yes, replace my current data with this backup", key="restore_ok")
            if st.button("Restore backup", type="primary", disabled=not ok):
                done, msg = be.restore_backup(S, data)
                S["restore_nonce"] += 1
                S["restore_ok"] = False
                flash("success" if done else "error", msg)
                st.rerun()
    with st.expander("Data tools"):
        st.caption("Load demo data to try the app, or erase everything. Both replace the data of YOUR shop only.")
        sure = st.checkbox("I understand current data will be replaced or erased", key="tools_ok")
        c1, c2 = st.columns(2)
        if c1.button("Load demo data", disabled=not sure):
            ok, msg = be.load_sample_data(S)
            flash("success" if ok else "error", msg)
            S["tools_ok"] = False
            st.rerun()
        if c2.button("Erase all data", disabled=not sure):
            ok, msg = be.erase_all_data(S)
            flash("success" if ok else "error", msg)
            S["tools_ok"] = False
            st.rerun()


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
def render_sidebar(pages: list[str]) -> None:
    profile = S["auth"]
    with st.sidebar:
        st.title("📦 SmartStock AI")
        st.caption(auth.TAGLINE)
        st.write("**Super Admin**" if profile["role"] == "super_admin" else f"**{profile['shop_name']}**")
        if S.get("nav") not in pages:
            S["nav"] = pages[0]
        st.radio("Go to", pages, key="nav", label_visibility="collapsed")
        st.caption("Your data is saved permanently in the database.")
        if st.button("Log out"):
            auth.logout(S)
            st.rerun()


def main() -> None:
    missing = sc.missing_config()
    if missing:
        auth.brand_header()
        st.error("Supabase is not configured. Add these secrets in Streamlit (see README): "
                 + ", ".join(missing))
        st.stop()
    be.init_state(S)
    be.clear_cache(S)  # reads are fetched fresh once per script run
    auth.process_email_link()
    if S.get("recovery_client"):
        auth.brand_header()
        auth.render_set_password()
        return
    if not auth.is_logged_in(S):
        auth.render_login()
        return
    try:
        auth.refresh_profile(S)
    except sc.AuthRequired as exc:
        auth.logout(S, str(exc))
        st.rerun()
    is_admin = S["auth"]["role"] == "super_admin"
    render_sidebar(ADMIN_PAGES if is_admin else SHOP_PAGES)
    page = S["nav"]
    shop_pages = {
        "Dashboard": page_dashboard, "Products": page_products, "Add Product": page_add_product,
        "Stock IN": page_stock_in, "Sales / Stock OUT": page_sales, "Camera Scanner": page_scanner,
        "Suppliers": page_suppliers, "AI Forecast": page_forecast, "Reports & Backup": page_reports,
    }
    try:
        if page == "Account Settings":
            auth.render_account_settings()
        elif is_admin:
            {"Admin Dashboard": lambda: admin.page_admin_dashboard(S),
             "Shop Management": lambda: admin.page_shop_management(S)}[page]()
        else:
            shop_pages[page]()
    except sc.AuthRequired as exc:
        auth.logout(S, str(exc))
        st.rerun()
    except sc.PermissionDenied as exc:
        st.error(str(exc))
    except (be.BackendError, admin.AdminError, sc.ConfigError) as exc:
        st.error(str(exc))


main()
