
import streamlit as st
import sqlite3
import pandas as pd
import pyttsx3
from datetime import datetime, timedelta
from pathlib import Path

# ---------------- CONFIGURATION ----------------

st.set_page_config(
    page_title="QueueSmart",
    page_icon="🍽️",
    layout="wide"
)

DB = Path(__file__).parent / "queuesmart.db"
KITCHEN_CAPACITY = 45  # Maximum pending preparation minutes

# ---------------- DATABASE ----------------

def connect():
    return sqlite3.connect(DB, timeout=10)

def initialize_database():
    with connect() as con:
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

        con.execute("""
            CREATE TABLE IF NOT EXISTS preparation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT,
                actual_minutes REAL
            )
        """)

        count = con.execute("SELECT COUNT(*) FROM menu").fetchone()[0]

        if count == 0:
            starter_menu = [
                ("Masala Dosa", 50, 8, 20, 1),
                ("Idli", 30, 5, 30, 1),
                ("Poori", 45, 7, 20, 1),
                ("Veg Fried Rice", 70, 12, 15, 1),
                ("Sandwich", 40, 6, 20, 1),
                ("Coffee", 15, 2, 50, 1),
                ("Tea", 10, 2, 50, 1)
            ]

            con.executemany("""
                INSERT INTO menu
                (name, price, prep_time, stock, available)
                VALUES (?, ?, ?, ?, ?)
            """, starter_menu)

initialize_database()

# ---------------- DATA FUNCTIONS ----------------

def get_menu():
    with connect() as con:
        return pd.read_sql_query(
            "SELECT * FROM menu ORDER BY id", con
        )

def get_orders():
    with connect() as con:
        return pd.read_sql_query(
            "SELECT * FROM orders ORDER BY created_at, id", con
        )

def get_order_items(order_id):
    with connect() as con:
        return pd.read_sql_query(
            "SELECT * FROM order_items WHERE order_id=?",
            con, params=(order_id,)
        )

def estimate_prep_time(item_name, base_time):
    """Adapt estimates using actual completed preparation history."""
    with connect() as con:
        history = pd.read_sql_query("""
            SELECT actual_minutes
            FROM preparation_history
            WHERE item_name=?
            ORDER BY id DESC LIMIT 10
        """, con, params=(item_name,))

    if len(history) >= 3:
        average = history["actual_minutes"].mean()
        return max(1, round((base_time * 0.3) + (average * 0.7)))

    return int(base_time)

def pending_workload():
    with connect() as con:
        result = con.execute("""
            SELECT COALESCE(SUM(estimated_minutes), 0)
            FROM orders
            WHERE status IN ('Accepted', 'Preparing')
        """).fetchone()

    return int(result[0])

def speak_order(order_id, items, estimated):
    """Announce an accepted order through the kitchen computer speaker."""
    try:
        engine = pyttsx3.init()
        description = ", ".join(
            f"{row['quantity']} {row['item_name']}"
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

# ---------------- ORDER PROCESSING ----------------

def place_order(student, cart):
    menu = get_menu()
    total_prep = 0
    total_price = 0
    order_lines = []

    for item_name, quantity in cart.items():
        item = menu[menu["name"] == item_name]

        if item.empty:
            return False, "Menu item not found."

        row = item.iloc[0]

        if not row["available"] or quantity > row["stock"]:
            return False, f"Insufficient stock for {item_name}."

        estimate = estimate_prep_time(
            item_name, int(row["prep_time"])
        )

        total_prep += estimate
        total_price += float(row["price"]) * quantity

        order_lines.append({
            "name": item_name,
            "quantity": quantity,
            "price": float(row["price"]),
            "prep": estimate
        })

    workload = pending_workload()

    if workload + total_prep > KITCHEN_CAPACITY:
        return False, (
            "Kitchen is currently at capacity. "
            "Please try again in a few minutes."
        )

    now = datetime.now()
    pickup = now + timedelta(minutes=max(5, workload + total_prep))

    with connect() as con:
        cursor = con.execute("""
            INSERT INTO orders
            (student, created_at, pickup_at, estimated_minutes)
            VALUES (?, ?, ?, ?)
        """, (
            student,
            now.isoformat(timespec="microseconds"),
            pickup.isoformat(timespec="minutes"),
            total_prep
        ))

        order_id = cursor.lastrowid

        for line in order_lines:
            con.execute("""
                INSERT INTO order_items
                (order_id, item_name, quantity, price)
                VALUES (?, ?, ?, ?)
            """, (
                order_id,
                line["name"],
                line["quantity"],
                line["price"]
            ))

            con.execute("""
                UPDATE menu SET stock = stock - ?
                WHERE name = ?
            """, (line["quantity"], line["name"]))

    return True, {
        "order_id": order_id,
        "pickup": pickup,
        "estimate": total_prep,
        "total": total_price
    }

# ---------------- ORDER STATUS ----------------

def update_status(order_id, new_status):
    with connect() as con:
        old = con.execute("""
            SELECT status FROM orders WHERE id=?
        """, (order_id,)).fetchone()

        if not old:
            return

        if new_status == "Ready" and old[0] != "Ready":
            items = con.execute("""
                SELECT item_name FROM order_items WHERE order_id=?
            """, (order_id,)).fetchall()

            for item in items:
                con.execute("""
                    INSERT INTO preparation_history
                    (item_name, actual_minutes)
                    VALUES (?, ?)
                """, (item[0], 0))

        con.execute("""
            UPDATE orders SET status=? WHERE id=?
        """, (new_status, order_id))

def announce_unannounced_orders():
    orders = get_orders()
    pending = orders[
        (orders["status"] == "Accepted") &
        (orders["announced"] == 0)
    ]

    for _, order in pending.iterrows():
        items = get_order_items(int(order["id"]))
        success = speak_order(
            int(order["id"]),
            items,
            int(order["estimated_minutes"])
        )

        if success:
            with connect() as con:
                con.execute("""
                    UPDATE orders SET announced=1 WHERE id=?
                """, (int(order["id"]),))

# ---------------- SIDEBAR ----------------

st.sidebar.title("🍽️ QueueSmart")
st.sidebar.caption("AI-Powered Smart Canteen Management")

role = st.sidebar.selectbox(
    "Select your dashboard",
    ["Student", "Kitchen Staff", "Canteen Manager"]
)

st.sidebar.divider()
st.sidebar.caption("One canteen • One kitchen • One pickup counter")

# ---------------- HEADER ----------------

st.title("🍽️ QueueSmart")
st.caption("Smarter ordering. Faster service. Better kitchen management.")

# ---------------- STUDENT ----------------

if role == "Student":

    st.header("Student Food Ordering")

    student = st.text_input("Enter your name")

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
            st.subheader("Build your order")

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
            if not student.strip():
                st.error("Please enter your name.")
            elif not cart:
                st.error("Please select at least one food item.")
            else:
                success, result = place_order(student.strip(), cart)

                if success:
                    st.session_state.last_order = result
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
                    st.write(f"Total: ₹{result['total']:.2f}")

                    st.info(
                        "Your order is in the kitchen queue. "
                        "Please check its status before collecting."
                    )

                    items = get_order_items(result["order_id"])
                    if speak_order(
                        result["order_id"],
                        items,
                        result["estimate"]
                    ):
                        with connect() as con:
                            con.execute("""
                                UPDATE orders SET announced=1
                                WHERE id=?
                            """, (result["order_id"],))
                else:
                    st.error(result)

    st.divider()
    st.subheader("Track Your Orders")

    all_orders = get_orders()

    if not all_orders.empty:
        if student.strip():
            my_orders = all_orders[
                all_orders["student"].str.lower()
                == student.strip().lower()
            ]

            if not my_orders.empty:
                st.dataframe(
                    my_orders[
                        ["id", "pickup_at", "status"]
                    ],
                    use_container_width=True,
                    hide_index=True
                )
            else:
                st.caption("No orders found for this name.")
        else:
            st.caption("Enter your name to view your orders.")

# ---------------- KITCHEN STAFF ----------------

elif role == "Kitchen Staff":

    st.header("👨‍🍳 Kitchen Dashboard")

    orders = get_orders()

    if orders.empty:
        st.info("No orders received yet.")
    else:
        # FCFS: timestamp first; exact timestamp ties use faster prep.
        orders["created_at"] = pd.to_datetime(orders["created_at"])
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
                st.write(f"Status: **{order['status']}**")
                st.write(
                    f"Pickup: {order['pickup_at']}"
                )
                st.write(
                    f"Estimated preparation: "
                    f"{order['estimated_minutes']} minutes"
                )

                for _, item in items.iterrows():
                    st.write(
                        f"• {item['quantity']} × {item['item_name']}"
                    )

                if order["status"] not in ["Collected", "Cancelled"]:
                    new_status = st.selectbox(
                        "Update order status",
                        ["Accepted", "Preparing", "Ready", "Collected"],
                        index=[
                            "Accepted", "Preparing",
                            "Ready", "Collected"
                        ].index(order["status"]),
                        key=f"status_{order_id}"
                    )

                    if st.button(
                        "Save status",
                        key=f"save_{order_id}"
                    ):
                        update_status(order_id, new_status)
                        st.rerun()

    st.divider()
    st.subheader("Kitchen Capacity")

    workload = pending_workload()
    st.metric(
        "Pending preparation workload",
        f"{workload} / {KITCHEN_CAPACITY} minutes"
    )

    st.progress(
        min(workload / KITCHEN_CAPACITY, 1.0)
    )

    if st.button("Announce pending orders through speaker"):
        announce_unannounced_orders()
        st.success("Announcement attempt completed.")

# ---------------- MANAGER ----------------

elif role == "Canteen Manager":

    st.header("📊 Manager Dashboard")

    menu = get_menu()
    orders = get_orders()

    col1, col2, col3 = st.columns(3)

    col1.metric("Menu Items", len(menu))
    col2.metric("Total Orders", len(orders))
    col3.metric("Pending Workload", f"{pending_workload()} min")

    st.divider()
    st.subheader("Manage Menu and Inventory")

    st.dataframe(
        menu,
        use_container_width=True,
        hide_index=True
    )

    with st.expander("Add a new menu item"):
        with st.form("add_menu"):
            name = st.text_input("Food name")
            price = st.number_input("Price (₹)", min_value=1.0)
            prep = st.number_input(
                "Base preparation time (minutes)",
                min_value=1,
                max_value=60,
                value=5
            )
            stock = st.number_input(
                "Initial stock",
                min_value=0,
                value=10
            )

            add = st.form_submit_button("Add item")

        if add:
            try:
                with connect() as con:
                    con.execute("""
                        INSERT INTO menu
                        (name, price, prep_time, stock, available)
                        VALUES (?, ?, ?, ?, 1)
                    """, (name.strip(), price, prep, stock))

                st.success("Menu item added.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("This menu item already exists.")

    with st.expander("Update inventory"):
        if not menu.empty:
            selected = st.selectbox(
                "Select food",
                menu["name"].tolist()
            )

            new_stock = st.number_input(
                "New stock quantity",
                min_value=0,
                value=10
            )

            if st.button("Update stock"):
                with connect() as con:
                    con.execute("""
                        UPDATE menu SET stock=? WHERE name=?
                    """, (new_stock, selected))

                st.success("Inventory updated.")
                st.rerun()

    with st.expander("Enable or disable menu items"):
        if not menu.empty:
            selected = st.selectbox(
                "Select item to change",
                menu["name"].tolist(),
                key="availability_item"
            )

            current = menu[
                menu["name"] == selected
            ].iloc[0]["available"]

            enabled = st.checkbox(
                "Item available for ordering",
                value=bool(current)
            )

            if st.button("Save availability"):
                with connect() as con:
                    con.execute("""
                        UPDATE menu SET available=? WHERE name=?
                    """, (int(enabled), selected))

                st.success("Availability updated.")
                st.rerun()

    st.divider()
    st.subheader("Order Analytics")

    if not orders.empty:
        st.metric(
            "Orders accepted",
            len(orders)
        )

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
        st.info("Analytics will appear when orders arrive.")

# ---------------- FOOTER ----------------

st.divider()
st.caption(
    "QueueSmart | AI-assisted preparation estimates "
    "| FCFS scheduling | Smart kitchen operations"
)
