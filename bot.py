import time
import logging
import requests
import psycopg2
from psycopg2 import extras

# =========================
# تنظیمات اصلی
# =========================

BOT_TOKEN = "160966979:s3cnOPW18kZcUJRSpIUp8r68jnuvjUK72wQ"

DB_CONNECTION_STRING = "postgresql://postgres:%5BFarhad35667900%5D@db.uvpwvhmwuklqqmhgdorx.supabase.co:5432/postgres"

BALE_API = f"https://tapi.bale.ai/bot{BOT_TOKEN}"

# =========================
# تنظیمات لاگ
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# وضعیت کاربران
# =========================

user_states = {}

# =========================
# دیتابیس
# =========================

def get_db_connection():
    try:
        conn = psycopg2.connect(
            DB_CONNECTION_STRING,
            connect_timeout=10
        )
        return conn

    except Exception as e:
        logging.error(f"DB Connection Error: {e}")
        return None


def verify_user(employee_number, password):

    conn = get_db_connection()

    if not conn:
        return None

    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:

            query = """
            SELECT
                u.id,
                u.employee_number,
                u.branch_id,
                u.role,
                b.name AS branch_name
            FROM users u
            LEFT JOIN branches b
                ON u.branch_id = b.id
            WHERE
                u.employee_number = %s
                AND u.password = %s
            """

            cur.execute(query, (employee_number, password))

            return cur.fetchone()

    except Exception as e:
        logging.error(f"Verify User Error: {e}")
        return None

    finally:
        conn.close()


def get_branch_employees(branch_id):

    conn = get_db_connection()

    if not conn:
        return []

    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:

            query = """
            SELECT id, name
            FROM employees
            WHERE branch_id = %s
            ORDER BY name
            """

            cur.execute(query, (branch_id,))

            return cur.fetchall()

    except Exception as e:
        logging.error(f"Get Employees Error: {e}")
        return []

    finally:
        conn.close()


def insert_collection(employee_id, amount, recorded_by):

    conn = get_db_connection()

    if not conn:
        return False

    try:
        with conn.cursor() as cur:

            query = """
            INSERT INTO collections
            (
                employee_id,
                amount,
                date,
                recorded_by
            )
            VALUES
            (
                %s,
                %s,
                CURRENT_DATE,
                %s
            )
            """

            cur.execute(
                query,
                (
                    employee_id,
                    amount,
                    recorded_by
                )
            )

            conn.commit()

            return True

    except Exception as e:
        logging.error(f"Insert Collection Error: {e}")
        return False

    finally:
        conn.close()

# =========================
# بله API
# =========================

def send_message(chat_id, text, reply_markup=None):

    url = f"{BALE_API}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:
            logging.error(
                f"Send Message Error: {response.text}"
            )

    except Exception as e:
        logging.error(f"Send Message Exception: {e}")


def make_keyboard(buttons):

    keyboard = []

    for btn in buttons:
        keyboard.append([{"text": btn}])

    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": True
    }

# =========================
# منطق ربات
# =========================

def reset_to_main(chat_id):

    user_states[chat_id]["state"] = "LOGGED_IN"

    keyboard = make_keyboard([
        "ثبت وصولی جدید",
        "خروج"
    ])

    send_message(
        chat_id,
        "منوی اصلی:",
        keyboard
    )


def handle_message(chat_id, text):

    state_data = user_states.get(chat_id)

    # =========================
    # شروع
    # =========================

    if text == "/start":

        user_states[chat_id] = {
            "state": "AWAITING_EMPLOYEE_NUMBER"
        }

        send_message(
            chat_id,
            "به سامانه ثبت وصول مطالبات خوش آمدید.\n\nشماره کارمندی را وارد کنید:"
        )

        return

    # =========================
    # اگر سشن نداشت
    # =========================

    if not state_data:

        send_message(
            chat_id,
            "لطفاً /start را ارسال کنید."
        )

        return

    current_state = state_data.get("state")

    # =========================
    # خروج
    # =========================

    if text == "خروج":

        user_states.pop(chat_id, None)

        send_message(
            chat_id,
            "از حساب کاربری خارج شدید.\n\n/start"
        )

        return

    # =========================
    # دریافت شماره کارمندی
    # =========================

    if current_state == "AWAITING_EMPLOYEE_NUMBER":

        user_states[chat_id]["employee_number"] = text

        user_states[chat_id]["state"] = "AWAITING_PASSWORD"

        send_message(
            chat_id,
            "رمز عبور را وارد کنید:"
        )

        return

    # =========================
    # دریافت رمز
    # =========================

    elif current_state == "AWAITING_PASSWORD":

        employee_number = state_data.get("employee_number")

        user_info = verify_user(
            employee_number,
            text
        )

        if not user_info:

            user_states[chat_id] = {
                "state": "AWAITING_EMPLOYEE_NUMBER"
            }

            send_message(
                chat_id,
                "اطلاعات ورود اشتباه است.\n\nشماره کارمندی:"
            )

            return

        user_states[chat_id].update({
            "state": "LOGGED_IN",
            "user_id": user_info["id"],
            "branch_id": user_info["branch_id"],
            "branch_name": user_info["branch_name"]
        })

        keyboard = make_keyboard([
            "ثبت وصولی جدید",
            "خروج"
        ])

        send_message(
            chat_id,
            f"ورود موفق.\nشعبه: {user_info['branch_name']}",
            keyboard
        )

        return

    # =========================
    # منوی اصلی
    # =========================

    elif current_state == "LOGGED_IN":

        if text == "ثبت وصولی جدید":

            employees = get_branch_employees(
                state_data["branch_id"]
            )

            if not employees:

                send_message(
                    chat_id,
                    "پرسنلی یافت نشد."
                )

                return

            user_states[chat_id]["employees"] = employees

            user_states[chat_id]["state"] = "SELECT_EMPLOYEE"

            buttons = []

            for emp in employees:
                buttons.append(emp["name"])

            buttons.append("خروج")

            keyboard = make_keyboard(buttons)

            send_message(
                chat_id,
                "پرسنل موردنظر را انتخاب کنید:",
                keyboard
            )

            return

    # =========================
    # انتخاب پرسنل
    # =========================

    elif current_state == "SELECT_EMPLOYEE":

        employees = state_data.get("employees", [])

        selected_employee = None

        for emp in employees:

            if emp["name"] == text:
                selected_employee = emp
                break

        if not selected_employee:

            send_message(
                chat_id,
                "انتخاب نامعتبر است."
            )

            return

        user_states[chat_id]["selected_employee"] = selected_employee

        user_states[chat_id]["state"] = "ENTER_AMOUNT"

        send_message(
            chat_id,
            f"مبلغ وصولی برای {selected_employee['name']} را وارد کنید:"
        )

        return

    # =========================
    # ثبت مبلغ
    # =========================

    elif current_state == "ENTER_AMOUNT":

        try:

            amount = int(
                text.replace(",", "").replace("،", "")
            )

            if amount <= 0:

                send_message(
                    chat_id,
                    "مبلغ نامعتبر است."
                )

                return

            employee = state_data["selected_employee"]

            success = insert_collection(
                employee["id"],
                amount,
                state_data["user_id"]
            )

            if success:

                send_message(
                    chat_id,
                    f"✅ مبلغ {amount:,} ریال ثبت شد."
                )

            else:

                send_message(
                    chat_id,
                    "❌ خطا در ثبت اطلاعات."
                )

            reset_to_main(chat_id)

        except ValueError:

            send_message(
                chat_id,
                "فقط عدد وارد کنید."
            )

# =========================
# دریافت آپدیت‌ها
# =========================

def get_updates(offset=None):

    url = f"{BALE_API}/getUpdates"

    params = {
        "timeout": 20,
        "offset": offset
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=25
        )

        if response.status_code == 200:
            return response.json()

        logging.error(
            f"GetUpdates Error: {response.text}"
        )

        return None

    except Exception as e:

        logging.error(
            f"GetUpdates Exception: {e}"
        )

        return None

# =========================
# حلقه اصلی
# =========================

def main():

    logging.info("Bot Started Successfully")

    offset = None

    while True:

        try:

            updates = get_updates(offset)

            if updates and updates.get("ok"):

                for result in updates.get("result", []):

                    offset = result["update_id"] + 1

                    if "message" not in result:
                        continue

                    message = result["message"]

                    chat_id = message["chat"]["id"]

                    text = message.get("text", "").strip()

                    if not text:
                        continue

                    logging.info(
                        f"Message From {chat_id}: {text}"
                    )

                    handle_message(chat_id, text)

            time.sleep(1)

        except Exception as e:

            logging.error(f"Main Loop Error: {e}")

            time.sleep(5)

# =========================
# اجرای ضد کرش
# =========================

if __name__ == "__main__":

    while True:

        try:

            main()

        except Exception as e:

            logging.error(
                f"Fatal Error Restarting Bot: {e}"
            )

            time.sleep(10)
