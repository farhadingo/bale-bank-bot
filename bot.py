import time
import threading
import requests
import psycopg2
from psycopg2 import extras
from flask import Flask

# ==================== وب‌سرور برای پاس کردن تست پورت Render ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    # گوش دادن روی پورت 10000 که پورت پیش‌فرض Render است روی آدرس 0.0.0.0
    app.run(host='0.0.0.0', port=10000)

# ==================== تنظیمات پروژه ====================
BOT_TOKEN = "123456:160966979:s3cnOPW18kZcUJRSpIUp8r68jnuvjUK72wQ" 
DB_CONNECTION_STRING = "postgresql://postgres:[Farhad35667900]@db.uvpwvhmwuklqqmhgdorx.supabase.co:5432/postgres"

BALE_API = f"https://tapi.bale.ai/bot{BOT_TOKEN}"
user_states = {}

# ==================== توابع ارتباط با دیتابیس ====================
def get_db_connection():
    try:
        conn = psycopg2.connect(DB_CONNECTION_STRING)
        return conn
    except Exception as e:
        print("خطا در اتصال به دیتابیس Supabase:", e)
        return None

def verify_user(employee_number, password):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            query = """
                SELECT u.id, u.employee_number, u.branch_id, u.role, b.name as branch_name 
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.employee_number = %s AND u.password = %s
            """
            cur.execute(query, (employee_number, password))
            return cur.fetchone()
    except Exception as e:
        print("خطا در تایید هویت کاربر:", e)
        return None
    finally:
        conn.close()

def get_branch_employees(branch_id):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM employees WHERE branch_id = %s", (branch_id,))
            return cur.fetchall()
    except Exception as e:
        print("خطا در دریافت لیست پرسنل:", e)
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
                INSERT INTO collections (employee_id, amount, date, recorded_by) 
                VALUES (%s, %s, CURRENT_DATE, %s)
            """
            cur.execute(query, (employee_id, amount, recorded_by))
            conn.commit()
            return True
    except Exception as e:
        print("خطا در ثبت وصولی:", e)
        return False
    finally:
        conn.close()

# ==================== توابع ارتباط با بله ====================
def send_message(chat_id, text, reply_markup=None):
    url = f"{BALE_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print("خطا در ارسال پیام به بله:", e)

def make_keyboard(buttons, one_time=True):
    keyboard = [[{"text": btn}] for btn in buttons]
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": one_time
    }

# ==================== منطق و پردازش پیام‌ها ====================
def handle_message(chat_id, text):
    state_data = user_states.get(chat_id)

    if text == "/start" or text == "خروج و بازگشت به منوی اصلی":
        user_states[chat_id] = {"state": "AWAITING_EMP_NUM"}
        send_message(chat_id, "سلام. به سامانه ثبت وصول مطالبات شعب خوش آمدید.\n\nلطفاً **شماره کارمندی** خود را ارسال کنید:")
        return

    if not state_data:
        user_states[chat_id] = {"state": "AWAITING_EMP_NUM"}
        send_message(chat_id, "لطفاً ابتدا با ارسال دستور /start ربات را شروع کنید.")
        return

    current_state = state_data.get("state")

    if current_state == "AWAITING_EMP_NUM":
        user_states[chat_id]["employee_number"] = text
        user_states[chat_id]["state"] = "AWAITING_PASSWORD"
        send_message(chat_id, "لطفاً **رمز عبور** خود را وارد کنید:")
        return

    elif current_state == "AWAITING_PASSWORD":
        emp_num = state_data.get("employee_number")
        password = text
        user_info = verify_user(emp_num, password)
        
        if user_info:
            user_states[chat_id].update({
                "state": "LOGGED_IN",
                "user_id": user_info["id"],
                "branch_id": user_info["branch_id"],
                "branch_name": user_info["branch_name"],
                "role": user_info["role"]
            })
            welcome_msg = (
                f"ورود موفقیت‌آمیز بود!\n"
                f"کاربر گرامی با شماره کارمندی: {emp_num}\n"
                f"شعبه انتسابی شما: {user_info['branch_name']}\n\n"
                f"چه کاری می‌خواهید انجام دهید؟"
            )
            keyboard = make_keyboard(["ثبت وصولی جدید", "خروج و بازگشت به منوی اصلی"])
            send_message(chat_id, welcome_msg, keyboard)
        else:
            user_states[chat_id] = {"state": "AWAITING_EMP_NUM"}
            send_message(chat_id, "شماره کارمندی یا رمز عبور اشتباه است.\nمجدداً **شماره کارمندی** خود را ارسال کنید:")
        return

    elif current_state == "LOGGED_IN":
        if text == "ثبت وصولی جدید":
            branch_id = state_data.get("branch_id")
            employees = get_branch_employees(branch_id)
            if not employees:
                send_message(chat_id, "هیچ پرسنلی برای شعبه شما در دیتابیس ثبت نشده است.")
                return
            user_states[chat_id]["employees_list"] = employees
            user_states[chat_id]["state"] = "SELECTING_EMPLOYEE"
            buttons = [emp["name"] for emp in employees]
            buttons.append("خروج و بازگشت به منوی اصلی")
            keyboard = make_keyboard(buttons)
            send_message(chat_id, "لطفاً پرسنل مورد نظر جهت ثبت وصولی را انتخاب کنید:", keyboard)
        else:
            send_message(chat_id, "گزینه نامعتبر است. لطفاً از دکمه‌های زیر استفاده کنید.")
        return

    elif current_state == "SELECTING_EMPLOYEE":
        employees = state_data.get("employees_list", [])
        selected_emp = next((emp for emp in employees if emp["name"] == text), None)
        if selected_emp:
            user_states[chat_id].update({
                "state": "ENTERING_AMOUNT",
                "selected_employee_id": selected_emp["id"],
                "selected_employee_name": selected_emp["name"]
            })
            keyboard = make_keyboard(["خروج و بازگشت به منوی اصلی"])
            send_message(chat_id, f"در حال ثبت وصولی برای «{selected_emp['name']}»\n\nلطفاً **مبلغ وصولی** را به ریال وارد کنید (فقط عدد انگلیسی):", keyboard)
        else:
            send_message(chat_id, "پرسنل انتخاب شده معتبر نیست. مجدداً از لیست انتخاب کنید.")
        return

    elif current_state == "ENTERING_AMOUNT":
        try:
            clean_amount = text.replace(",", "").replace("،", "").strip()
            amount = int(clean_amount)
            emp_id = state_data.get("selected_employee_id")
            emp_name = state_data.get("selected_employee_name")
            recorded_by = state_data.get("user_id")
            success = insert_collection(emp_id, amount, recorded_by)
            if success:
                send_message(chat_id, f"✅ مبلغ {amount:,} ریال برای همکار گرامی «{emp_name}» با موفقیت در دیتابیس ثبت شد.")
            else:
                send_message(chat_id, "❌ خطایی در ذخیره اطلاعات رخ داد. لطفاً مجدداً تلاش کنید.")
            user_states[chat_id]["state"] = "LOGGED_IN"
            keyboard = make_keyboard(["ثبت وصولی جدید", "خروج و بازگشت به منوی اصلی"])
            send_message(chat_id, "چه کاری می‌خواهید انجام دهید؟", keyboard)
        except ValueError:
            send_message(chat_id, "فرمت مبلغ معتبر نیست. لطفاً مبلغ را فقط به صورت عدد (مثلاً: 50000000) ارسال کنید.")
        return

# ==================== چرخه Polling ربات بله ====================
def get_updates(offset=None):
    url = f"{BALE_API}/getUpdates"
    params = {"timeout": 20, "offset": offset}
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print("خطا در ارتباط با سرور بله:", e)
    return None

def bot_loop():
    print("ربات ثبت وصول مطالبات بله با موفقیت در پس‌زمینه اجرا شد...")
    offset = None
    while True:
        updates = get_updates(offset)
        if updates and updates.get("ok"):
            for result in updates.get("result", []):
                update_id = result["update_id"]
                offset = update_id + 1
                if "message" in result:
                    message = result["message"]
                    chat_id = message["chat"]["id"]
                    text = message.get("text", "").strip()
                    if text:
                        handle_message(chat_id, text)
        time.sleep(1)

if __name__ == "__main__":
    # ۱. اجرای وب‌سرور فلاسک در یک ترد جداگانه جهت عبور از بررسی پورت Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # ۲. اجرای چرخه اصلی ربات بله در ترد اصلی
    bot_loop()
