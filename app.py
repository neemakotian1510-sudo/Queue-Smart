
import streamlit as st
import sqlite3
import pandas as pd
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from pathlib import Path

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

st.set_page_config(page_title="QueueSmart", page_icon="🍽️", layout="wide")

DB = Path(__file__).parent / "queuesmart.db"
KITCHEN_CAPACITY = 45
ROLES = ["Student", "Kitchen Staff", "Canteen Manager"]


# ---------------- DATABASE ----------------

def connect():
    return sqlite3.connect(DB, timeout=20)


def initialize_database():
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS menu (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                price REAL,
                prep_time INTEGER,
                stock INTEGER,
                available INTEGER DEFAULT 1
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student TEXT,
                created_at TEXT,
                pickup_at TEXT,
                estimated_minutes INTEGER,
                status TEXT DEFAULT 'Accepted',
                announced INTEGER DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                item_name TEXT,
                quantity INTEGER,
                price REAL
            )
        """)

        count = con.execute(
            "SELECT COUNT(*) FROM menu"
        ).fetchone()[0]

        if count == 0:
            con.executemany("""
                INSERT INTO menu
                (name, price, prep_time, stock, available)
                VALUES (?, ?, ?, ?, ?)
            """, [
                ("Masala Dosa", 50, 8, 20, 1),
                ("Idli", 30, 5, 30, 1),
                ("Poori", 45, 7, 20, 1),
                ("Veg Fried Rice", 70, 12, 15, 1),
                ("Sandwich", 40, 6, 20, 1),
                ("Coffee", 15, 2, 50, 1),
                ("Tea", 10, 2, 50, 1)
            ])


initialize_database()


# ---------------- AUTHENTICATION ----------------

def make_password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        200000
    )

    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)

        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            200000
        ).hex()

        return hmac.compare_digest(candidate, digest_hex)

    except (ValueError, TypeError):
        return False


def register_user(name, email, password, role):
    try:
        with connect() as con:
            con.execute("""
                INSERT INTO users
                (name, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                name.strip(),
                email.strip().lower(),
                make_password_hash(password),
                role,
                datetime.now().isoformat(timespec="seconds")
            ))

        return True, "Account created successfully! Please log in."

    except sqlite3.IntegrityError:
        return False, "This email is already registered. Please log in."


def authenticate(email, password):
    with connect() as con:
        user = con.execute("""
            SELECT id, name, email, password_hash, role
            FROM users
            WHERE email = ?
        """, (email.strip().lower(),)).fetchone()

    if user and verify_password(password, user[3]):
        return {
            "id": user[0],
            "name": user[1],
            "email": user[2],
            "role": user[4]
        }

    return None


# ---------------- DATA FUNCTIONS ----------------

def get_menu():
    with connect() as con:
        return pd.read_sql_query(
            "SELECT * FROM menu ORDER BY id",
            con
        )


def get_orders():
    with connect() as con:
        return pd.read_sql_query(
            "SELECT * FROM orders ORDER BY created_at, id",
            con
        )


def get_order_items(order_id):
    with connect() as con:
        return pd.read_sql_query(
            "SELECT * FROM order_items WHERE order_id = ?",
            con,
            params=(order_id,)
        )


def pending_workload():
    with connect() as con:
        row = con.execute("""
            SELECT COALESCE(SUM(estimated_minutes), 0)
            FROM orders
            WHERE status IN ('Accepted', 'Preparing')
        """).fetchone()

    return int(row[0])


# ---------------- SPEAKER ANNOUNCEMENT ----------------

def speak_order(order_id, items, estimated):
    if pyttsx3 is None:
        return False

    try:
        engine = pyttsx3.init()

        description = ", ".join(
            f"{int(row['quantity'])} {row['item_name']}"
            for _, row in items.iterrows()
        )

        engine.say(
            f"Attention kitchen. New order number {order_id}. "
            f"{description}. Estimated preparation time "
            f"{estimated} minutes."
        )

        engine.runAndWait()
        engine.stop()

        return True

    except Exception:
        return False


def announce_unannounced_orders():
    orders = get_orders()

    if orders.empty:
        return 0

    pending = orders[
        (orders["status"] == "Accepted") &
        (orders["announced"] == 0)
    ]

    announced_count = 0

    for _, order in pending.iterrows():
        order_id = int(order["id"])

        ok = speak_order(
            order_id,
            get_order_items(order_id),
            int(order["estimated_minutes"])
        )

        if ok:
            with connect() as con:
                con.execute(
                    "UPDATE orders SET announced = 1 WHERE id = ?",
                    (order_id,)
                )

            announced_count += 1

    return announced_count


# ---------------- ORDER PLACEMENT ----------------

def place_order(student, cart):
    menu = get_menu()

    total_prep = 0
    total_price = 0.0
    lines = []

    for item_name, quantity in cart.items():

        item = menu[menu["name"] == item_name]

        if item.empty:
            return False, "Menu item not found."

        row = item.iloc[0]

        if not row["available"] or quantity > int(row["stock"]):
            return False, f"Insufficient stock for {item_name}."

        prep = int(row["prep_time"])

        total_prep += prep
        total_price += float(row["price"]) * quantity

        lines.append((
            item_name,
            quantity,
            float(row["price"])
        ))

    workload = pending_workload()

    if workload + total_prep > KITCHEN_CAPACITY:
        return False, (
            "Kitchen is currently at capacity. "
            "Please try again in a few minutes."
        )

    now = datetime.now()

    pickup = now + timedelta(
        minutes=max(5, workload + total_prep)
    )

    with connect() as con:

        cur = con.execute("""
            INSERT INTO orders
            (student, created_at, pickup_at, estimated_minutes)
            VALUES (?, ?, ?, ?)
        """, (
            student,
            now.isoformat(timespec="microseconds"),
            pickup.isoformat(timespec="minutes"),
            total_prep
        ))

        order_id = cur.lastrowid

        for name, quantity, price in lines:

            con.execute("""
                INSERT INTO order_items
                (order_id, item_name, quantity, price)
                VALUES (?, ?, ?, ?)
            """, (
                order_id,
                name,
                quantity,
                price
            ))

            con.execute("""
                UPDATE menu
                SET stock = stock - ?
                WHERE name = ?
            """, (quantity, name))

    return True, {
        "order_id": order_id,
        "pickup": pickup,
        "estimate": total_prep,
        "total": total_price
    }


def update_status(order_id, new_status):
    with connect() as con:
        con.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (new_status, order_id)
        )


# ---------------- SIDEBAR ----------------

st.sidebar.title("🍽️ QueueSmart")
st.sidebar.caption("AI-Powered Smart Canteen Management")
st.sidebar.divider()

if "user" not in st.session_state:
    st.session_state.user = None


# ---------------- LOGIN AND REGISTRATION ----------------

if st.session_state.user is None:

    st.title("🍽️ QueueSmart")
    st.subheader("Welcome! Register or log in to continue")

    action = st.radio(
        "Choose an option",
        ["Login", "Register"],
        horizontal=True
    )

    if action == "Register":

        with st.form("register_form"):

            st.subheader("Create Your Account")

            reg_name = st.text_input("Full Name")
            reg_email = st.text_input("Email Address")

            reg_role = st.selectbox(
                "Register As",
                ROLES
            )

            reg_password = st.text_input(
                "Create Password",
                type="password"
            )

            reg_confirm = st.text_input(
                "Confirm Password",
                type="password"
            )

            reg_submit = st.form_submit_button(
                "Register",
                type="primary",
                use_container_width=True
            )

        if reg_submit:

            if (
                not reg_name.strip()
                or not reg_email.strip()
                or not reg_password
            ):
                st.error("Please fill in all fields.")

            elif "@" not in reg_email or "." not in reg_email:
                st.error("Please enter a valid email address.")

            elif len(reg_password) < 6:
                st.error(
                    "Password must contain at least 6 characters."
                )

            elif reg_password != reg_confirm:
                st.error("Passwords do not match.")

            else:

                ok, message = register_user(
                    reg_name,
                    reg_email,
                    reg_password,
                    reg_role
                )

                if ok:
                    st.success(message)
                else:
                    st.error(message)

    else:

        with st.form("login_form"):

            st.subheader("Log In to Your Account")

            login_email = st.text_input("Email Address")

            login_password = st.text_input(
                "Password",
                type="password"
            )

            login_submit = st.form_submit_button(
                "Login",
                type="primary",
                use_container_width=True
            )

        if login_submit:

            user = authenticate(
                login_email,
                login_password
            )

            if user:
                st.session_state.user = user
                st.rerun()

            else:
                st.error("Incorrect email or password.")

    st.info(
        "Create an account first, then log in using "
        "the same email and password."
    )

    st.stop()


# ---------------- LOGGED-IN USER ----------------

user = st.session_state.user
role = user["role"]

st.sidebar.success(f"Logged in: {user['name']}")
st.sidebar.write(f"Role: **{role}**")

if st.sidebar.button("Log out", use_container_width=True):
    st.session_state.user = None
    st.rerun()

st.sidebar.divider()
st.sidebar.caption(
    "One canteen • One kitchen • One pickup counter"
)

st.title("🍽️ QueueSmart")
st.caption(
    "Smarter ordering. Faster service. Better kitchen management."
)


# ---------------- STUDENT DASHBOARD ----------------

if role == "Student":

    st.header("Student Food Ordering")

    student = user["name"]

    menu = get_menu()

    available_menu = menu[
        (menu["available"] == 1) &
        (menu["stock"] > 0)
    ]

    if available_menu.empty:

        st.warning("No menu items are currently available.")

    else:

        st.subheader("Today's Menu")

        st.dataframe(
            available_menu[
                ["name", "price", "prep_time", "stock"]
            ].rename(columns={
                "name": "Food",
                "price": "Price (₹)",
                "prep_time": "Base preparation (min)",
                "stock": "Available quantity"
            }),
            use_container_width=True,
            hide_index=True
        )

        with st.form("order_form"):

            st.subheader("Build Your Order")

            cart = {}

            for _, item in available_menu.iterrows():

                quantity = st.number_input(
                    f"{item['name']} — ₹{item['price']}",
                    min_value=0,
                    max_value=int(item["stock"]),
                    value=0,
                    key=f"quantity_{item['id']}"
                )

                if quantity > 0:
                    cart[item["name"]] = int(quantity)

            submitted = st.form_submit_button(
                "Place Order",
                type="primary",
                use_container_width=True
            )

        if submitted:

            if not cart:

                st.error("Please select at least one food item.")

            else:

                success, result = place_order(
                    student,
                    cart
                )

                if success:

                    st.success("Your order has been accepted!")

                    st.subheader(
                        f"Order #{result['order_id']}"
                    )

                    st.write(
                        f"Estimated preparation: "
                        f"{result['estimate']} minutes"
                    )

                    st.write(
                        f"Pickup time: "
                        f"{result['pickup'].strftime('%I:%M %p')}"
                    )

                    st.write(
                        f"Total: ₹{result['total']:.2f}"
                    )

                    st.info(
                        "Your order is in the kitchen queue. "
                        "Check its status before collecting."
                    )

                else:
                    st.error(result)

    st.divider()
    st.subheader("Track Your Orders")

    all_orders = get_orders()

    if not all_orders.empty:

        my_orders = all_orders[
            all_orders["student"].str.lower() == student.lower()
        ]

        if not my_orders.empty:

            st.dataframe(
                my_orders[
                    ["id", "pickup_at", "status"]
                ].rename(columns={
                    "id": "Order number",
                    "pickup_at": "Pickup time",
                    "status": "Status"
                }),
                use_container_width=True,
                hide_index=True
            )

        else:
            st.caption("You have not placed any orders yet.")

    else:
        st.caption("You have not placed any orders yet.")


# ---------------- KITCHEN STAFF DASHBOARD ----------------

elif role == "Kitchen Staff":

    st.header("👨‍🍳 Kitchen Dashboard")

    orders = get_orders()

    if orders.empty:

        st.info("No orders received yet.")

    else:

        orders["created_at"] = pd.to_datetime(
            orders["created_at"]
        )

        orders = orders.sort_values(
            ["created_at", "estimated_minutes", "id"]
        )

        st.subheader("Live Order Queue")

        for _, order in orders.iterrows():

            order_id = int(order["id"])

            items = get_order_items(order_id)

            with st.container(border=True):

                st.subheader(f"Order #{order_id}")

                st.write(f"Student: {order['student']}")

                st.write(
                    f"Status: **{order['status']}**"
                )

                st.write(
                    f"Pickup: {order['pickup_at']}"
                )

                st.write(
                    f"Estimated preparation: "
                    f"{order['estimated_minutes']} minutes"
                )

                for _, item in items.iterrows():

                    st.write(
                        f"• {item['quantity']} × "
                        f"{item['item_name']}"
                    )

                if order["status"] not in [
                    "Collected",
                    "Cancelled"
                ]:

                    statuses = [
                        "Accepted",
                        "Preparing",
                        "Ready",
                        "Collected"
                    ]

                    current = (
                        order["status"]
                        if order["status"] in statuses
                        else "Accepted"
                    )

                    new_status = st.selectbox(
                        "Update Order Status",
                        statuses,
                        index=statuses.index(current),
                        key=f"status_{order_id}"
                    )

                    if st.button(
                        "Save Status",
                        key=f"save_{order_id}"
                    ):

                        update_status(
                            order_id,
                            new_status
                        )

                        st.rerun()

    st.divider()
    st.subheader("Kitchen Capacity")

    workload = pending_workload()

    st.metric(
        "Pending Preparation Workload",
        f"{workload} / {KITCHEN_CAPACITY} minutes"
    )

    st.progress(
        min(workload / KITCHEN_CAPACITY, 1.0)
    )

    if st.button("Announce Pending Orders Through Speaker"):

        count = announce_unannounced_orders()

        if count:

            st.success(
                f"Announced {count} order(s)."
            )

        else:

            st.warning(
                "No announcements were made. "
                "Speaker audio is available only when "
                "the app runs on a computer with a speaker "
                "and text-to-speech support."
            )


# ---------------- CANTEEN MANAGER DASHBOARD ----------------

elif role == "Canteen Manager":

    st.header("📊 Manager Dashboard")

    menu = get_menu()
    orders = get_orders()

    col1, col2, col3 = st.columns(3)

    col1.metric("Menu Items", len(menu))

    col2.metric("Total Orders", len(orders))

    col3.metric(
        "Pending Workload",
        f"{pending_workload()} min"
    )

    st.divider()
    st.subheader("Manage Menu and Inventory")

    st.dataframe(
        menu,
        use_container_width=True,
        hide_index=True
    )

    with st.expander("Add a New Menu Item"):

        with st.form("add_menu"):

            name = st.text_input("Food Name")

            price = st.number_input(
                "Price (₹)",
                min_value=1.0
            )

            prep = st.number_input(
                "Base Preparation Time (minutes)",
                min_value=1,
                max_value=60,
                value=5
            )

            stock = st.number_input(
                "Initial Stock",
                min_value=0,
                value=10
            )

            add = st.form_submit_button("Add Item")

        if add:

            if not name.strip():

                st.error("Enter a food name.")

            else:

                try:

                    with connect() as con:

                        con.execute("""
                            INSERT INTO menu
                            (name, price, prep_time, stock, available)
                            VALUES (?, ?, ?, ?, 1)
                        """, (
                            name.strip(),
                            price,
                            prep,
                            stock
                        ))

                    st.success("Menu item added.")

                    st.rerun()

                except sqlite3.IntegrityError:

                    st.error(
                        "This menu item already exists."
                    )

    with st.expander("Update Inventory"):

        if not menu.empty:

            selected = st.selectbox(
                "Select Food",
                menu["name"].tolist()
            )

            new_stock = st.number_input(
                "New Stock Quantity",
                min_value=0,
                value=10
            )

            if st.button("Update Stock"):

                with connect() as con:

                    con.execute("""
                        UPDATE menu
                        SET stock = ?
                        WHERE name = ?
                    """, (
                        new_stock,
                        selected
                    ))

                st.success("Inventory updated.")

                st.rerun()

    with st.expander("Enable or Disable Menu Items"):

        if not menu.empty:

            selected = st.selectbox(
                "Select Item to Change",
                menu["name"].tolist(),
                key="availability_item"
            )

            current = menu[
                menu["name"] == selected
            ].iloc[0]["available"]

            enabled = st.checkbox(
                "Item Available for Ordering",
                value=bool(current)
            )

            if st.button("Save Availability"):

                with connect() as con:

                    con.execute("""
                        UPDATE menu
                        SET available = ?
                        WHERE name = ?
                    """, (
                        int(enabled),
                        selected
                    ))

                st.success("Availability updated.")

                st.rerun()

    st.divider()
    st.subheader("Order Analytics")

    if not orders.empty:

        st.metric("Orders Accepted", len(orders))

        st.bar_chart(
            orders["status"].value_counts()
        )

        st.dataframe(
            orders[
                ["id", "student", "status", "estimated_minutes"]
            ],
            use_container_width=True,
            hide_index=True
        )

    else:

        st.info(
            "Analytics will appear when orders arrive."
        )


# ---------------- FOOTER ----------------

st.divider()

st.caption(
    "QueueSmart | AI-assisted preparation estimates | "
    "FCFS scheduling | Smart kitchen operations"
)
