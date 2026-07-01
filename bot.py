به منظور اعمال تغییرات نهایی، کدهای پروژه را در دو بخش مجزا آماده کرده‌ام: **کد اسکریپت SQL** برای ساخت جداول و **کد پایتون (`bot.py`)** که شامل اصلاحات پولینگ بله، رفع مشکل تکرار پیام‌ها، مدیریت پایداری نشست‌ها، ذخیره‌سازی دائمی وصولی‌ها و حل مشکل منطقه زمانی ایران است.

### ۱. اسکریپت ساخت و به‌روزرسانی جداول دیتابیس (SQL)
این دستورات را در بخش **SQL Editor** در پنل Supabase خود اجرا کنید تا جداول با ساختار جدید و پیوند کلید خارجی مناسب ساخته شوند:

```sql
-- جدول کاربران (معاونین شعب و کاربران ارشد)
CREATE TABLE IF NOT EXISTS users (
    employee_id VARCHAR(50) PRIMARY KEY,
    telegram_id BIGINT UNIQUE,
    full_name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('admin', 'deputy')),
    branch_name VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- جدول ثبت دائمی وصولی‌ها
CREATE TABLE IF NOT EXISTS collections (
    id SERIAL PRIMARY KEY,
    employee_id VARCHAR(50) REFERENCES users(employee_id) ON DELETE CASCADE,
    branch_name VARCHAR(100) NOT NULL,
    amount_deputy NUMERIC(15, 2) DEFAULT 0,
    amount_colleagues NUMERIC(15, 2) DEFAULT 0,
    shamsi_date VARCHAR(10) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

-- نمونه داده برای تست (اختیاری - در صورت نیاز اطلاعات خود را جایگزین کنید)
-- INSERT INTO users (employee_id, full_name, role, branch_name) VALUES ('12345', 'حمید محمدی', 'deputy', 'شعبه مرکزی');
-- INSERT INTO users (employee_id, full_name, role, branch_name) VALUES ('99999', 'مدیر سیستم', 'admin', 'ستاد استان');
```

---

### ۲. کد کامل ربات (`bot.py`)
این فایل را به طور کامل جایگزین فایل قبلی خود در مخزن گیت (GitHub/GitLab) متصل به Render کنید. همچنین مطمئن شوید متغیر محیطی `TZ` با مقدار `Asia/Tehran` در پنل Render ست شده باشد.

```python
import os
import time
import logging
import requests
import psycopg2
from psycopg2 import pool
from datetime import datetime, timedelta, timezone

# تنظیمات پیشرفته لاگین برای عیب‌یابی دقیق‌تر
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# بارگذاری متغیرهای محیطی
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BALE_BOT_TOKEN")
DB_URL = os.getenv("DB_CONNECTION_STRING")
BASE_URL = f"https://api.bale.ai/bot{BOT_TOKEN}"

# راه‌اندازی Connection Pool برای مدیریت بهینه اتصال به دیتابیس Supabase
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
    logger.info("Database connection pool established successfully.")
except Exception as e:
    logger.error(f"Failed to create database connection pool: {e}")
    db_pool = None

# مدیریت وضعیت کاربران در حافظه موقت (State Machine)
# ساختار: {chat_id: {"state": "...", "amount_deputy": 0.0}}
user_states = {}

def get_db_connection():
    if db_pool:
        return db_pool.getconn()
    return psycopg2.connect(DB_URL)

def return_db_connection(conn):
    if db_pool:
        db_pool.putconn(conn)
    else:
        conn.close()

def get_iran_time():
    """محاسبه دقیق زمان رسمی ایران (با اختلاف ۳:۳۰+ نسبت به UTC)"""
    iran_timezone = timezone(timedelta(hours=3, minutes=30))
    return datetime.now(iran_timezone)

def get_shamsi_date():
    """تبدیل تاریخ جاری ایران به فرمت شمسی YYYY/MM/DD"""
    now_iran = get_iran_time()
    g_y, g_m, g_d = now_iran.year, now_iran.month, now_iran.day
    
    g_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    # سال کبیسه میلادی
    if (g_y % 4 == 0 and g_y % 100 != 0) or (g_y % 400 == 0):
        g_days_in_month[1] = 29

    gy = g_y - 1600
    gm = g_m - 1
    gd = g_d - 1

    g_day_no = 365 * gy + gy // 4 - gy // 100 + gy // 400
    for i in range(gm):
        g_day_no += g_days_in_month[i]
    g_day_no += gd

    jy = 979 + 33 * (g_day_no // 12053) + 4 * ((g_day_no % 12053) // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        jy += (g_day_no - 1) // 365
        g_day_no = (g_day_no - 1) % 365

    if g_day_no < 186:
        jm = 1 + g_day_no // 31
        jd = 1 + (g_day_no % 31)
    else:
        jm = 7 + (g_day_no - 186) // 30
        jd = 1 + ((g_day_no - 186) % 30)
        
    return f"{jy}/{jm:02d}/{jd:02d}"

def send_message(chat_id, text, reply_markup=None):
    """ارسال پیام به پیام‌رسان بله"""
    url = f"{BASE_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()
    except Exception as e:
        logger.error(f"Error sending message to {chat_id}: {e}")
        return None

def get_main_keyboard(role):
    """تولید کیبورد سفارشی بر اساس نقش کاربر"""
    if role == 'admin':
        return {
            "keyboard": [
                [{"text": "📊 گزارش شعب امروز"}, {"text": "📈 گزارش ۱۰ روز اخیر استان"}],
                [{"text": "🏆 ۵ شعب برتر استان"}]
            ],
            "resize_keyboard": True
        }
    else:  # deputy
        return {
            "keyboard": [
                [{"text": "💰 ثبت وصولی روزانه"}, {"text": "📅 گزارش ۱۰ روز اخیر شعبه"}]
            ],
            "resize_keyboard": True
        }

# --- بخش متدهای دیتابیس ---

def find_user_by_employee_id(emp_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT employee_id, full_name, role, branch_name FROM users WHERE employee_id = %s", (emp_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"Database error in find_user_by_employee_id: {e}")
        return None
    finally:
        return_db_connection(conn)

def update_user_telegram_id(emp_id, chat_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET telegram_id = %s WHERE employee_id = %s", (chat_id, emp_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Database error in update_user_telegram_id: {e}")
    finally:
        return_db_connection(conn)

def find_user_by_telegram_id(chat_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT employee_id, full_name, role, branch_name FROM users WHERE telegram_id = %s", (chat_id,))
            return cur.fetchone()
    except Exception as e:
        logger.error(f"Database error in find_user_by_telegram_id: {e}")
        return None
    finally:
        return_db_connection(conn)

def save_collection(emp_id, branch_name, amount_deputy, amount_colleagues):
    """ثبت دائمی یک تراکنش وصولی جدید با تاریخ و ساعت رسمی ایران"""
    conn = get_db_connection()
    shamsi_date = get_shamsi_date()
    created_at_iran = get_iran_time()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collections (employee_id, branch_name, amount_deputy, amount_colleagues, shamsi_date, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (emp_id, branch_name, amount_deputy, amount_colleagues, shamsi_date, created_at_iran)
            )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Database error in save_collection: {e}")
        return False
    finally:
        return_db_connection(conn)

def get_branch_report(branch_name):
    """دریافت لیست گزارشات ۱۰ روز گذشته شعبه"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT shamsi_date, SUM(amount_deputy), SUM(amount_colleagues)
                FROM collections
                WHERE branch_name = %s
                GROUP BY shamsi_date
                ORDER BY shamsi_date DESC
                LIMIT 10
                """,
                (branch_name,)
            )
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_branch_report: {e}")
        return []
    finally:
        return_db_connection(conn)

# --- موتور پردازش پیام (Handler) ---

def handle_message(message):
    chat_id = message['chat']['id']
    text = message.get('text', '').strip()
    
    # بررسی وجود کاربر در دیتابیس
    user = find_user_by_telegram_id(chat_id)
    
    # مرحله ورود و احراز هویت
    if not user:
        user_state = user_states.get(chat_id, {})
        if user_state.get("state") == "WAITING_FOR_EMP_ID":
            emp_user = find_user_by_employee_id(text)
            if emp_user:
                emp_id, name, role, branch = emp_user
                update_user_telegram_id(emp_id, chat_id)
                
                # بروزرسانی سریع وضعیت کاربر در حافظه برای جلوگیری از پردازش اشتباه
                user_states[chat_id] = {"state": "LOGGED_IN"}
                
                send_message(
                    chat_id, 
                    f"✅ هویت شما تایید شد.\nخوش آمدید جناب {name}\nسمت: {role == 'admin' and 'کاربر ارشد' or 'معاون شعبه'}\nشعبه: {branch}", 
                    get_main_keyboard(role)
                )
            else:
                # عدم تغییر وضعیت برای شانس مجدد ورود
                send_message(chat_id, "❌ شماره کارمندی یافت نشد. لطفا کد کارمندی صحیح خود را مجدداً ارسال کنید:")
            return
        else:
            # شروع فرآیند احراز هویت
            user_states[chat_id] = {"state": "WAITING_FOR_EMP_ID"}
            send_message(chat_id, "سلام. جهت دسترسی به ربات وصول مطالبات، لطفاً شماره کارمندی خود را ارسال کنید:")
            return

    # پردازش درخواست کاربران معتبر
    emp_id, name, role, branch = user
    user_state = user_states.get(chat_id, {})
    current_state = user_state.get("state")

    # دریافت مبلغ وصولی شخص معاون
    if current_state == "WAITING_FOR_DEPUTY_AMOUNT":
        try:
            amount = float(text.replace(',', ''))
            user_states[chat_id] = {
                "state": "WAITING_FOR_COLLEAGUES_AMOUNT",
                "amount_deputy": amount
            }
            send_message(chat_id, "مبلغ وصولی همکاران شعبه را به ریال وارد کنید:")
        except ValueError:
            send_message(chat_id, "❌ خطا: لطفاً مقدار مبلغ را فقط به صورت عددی وارد کنید (مثال: 4500000):")
        return

    # دریافت مبلغ وصولی همکاران و ذخیره‌سازی نهایی
    elif current_state == "WAITING_FOR_COLLEAGUES_AMOUNT":
        try:
            amount_col = float(text.replace(',', ''))
            amount_dep = user_state.get("amount_deputy", 0)
            
            # ثبت دائمی رکورد در دیتابیس
            success = save_collection(emp_id, branch, amount_dep, amount_col)
            user_states[chat_id] = {"state": "LOGGED_IN"}
            
            if success:
                send_message(
                    chat_id, 
                    f"✅ اطلاعات با موفقیت ثبت شد.\n\n👤 وصولی شما: {amount_dep:,.0f} ریال\n👥 وصولی همکاران: {amount_col:,.0f} ریال", 
                    get_main_keyboard(role)
                )
            else:
                send_message(chat_id, "❌ خطا در ذخیره‌سازی داده‌ها. لطفا مجدداً تلاش کنید.", get_main_keyboard(role))
        except ValueError:
            send_message(chat_id, "❌ خطا: لطفاً مقدار مبلغ را فقط به صورت عددی وارد کنید:")
        return

    # پردازش گزینه‌های منوی معاونین
    if text == "💰 ثبت وصولی روزانه" and role == 'deputy':
        user_states[chat_id] = {"state": "WAITING_FOR_DEPUTY_AMOUNT"}
        send_message(chat_id, "لطفاً مبلغ وصولی شخص خودتان (معاون) را به ریال وارد کنید:")
        
    elif text == "📅 گزارش ۱۰ روز اخیر شعبه" and role == 'deputy':
        report = get_branch_report(branch)
        if report:
            msg = f"📊 گزارش عملکرد ۱۰ روز اخیر شعبه {branch}:\n\n"
            for row in report:
                msg += f"📅 تاریخ: {row[0]}\n👤 وصولی معاون: {row[1]:,.0f} ریال\n👥 وصولی همکاران: {row[2]:,.0f} ریال\n------------------\n"
            send_message(chat_id, msg)
        else:
            send_message(chat_id, "سابقه‌ای برای شعبه شما در ۱۰ روز اخیر یافت نشد.")

    # پردازش گزینه‌های منوی ادمین
    elif text == "📊 گزارش شعب امروز" and role == 'admin':
        send_message(chat_id, "🛠 گزارش شعب امروز در حال آماده‌سازی است...")

    elif text == "📈 گزارش ۱۰ روز اخیر استان" and role == 'admin':
        send_message(chat_id, "🛠 گزارش ۱۰ روز اخیر استان در حال پردازش است...")

    elif text == "🏆 ۵ شعب برتر استان" and role == 'admin':
        send_message(chat_id, "🏆 استخراج ۵ شعبه برتر استان در حال محاسبه است...")

    else:
        # نمایش مجدد منوی اصلی برای تعاملات متفرقه
        send_message(chat_id, "گزینه مورد نظر را از منوی زیر انتخاب کنید:", get_main_keyboard(role))

# --- چرخه پولینگ اصلی (Main Polling Loop) ---

def main():
    offset = 0
    logger.info("Bot initiated using Polling mechanism...")
    
    while True:
        try:
            url = f"{BASE_URL}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            res = requests.get(url, params=params, timeout=25)
            
            if res.status_code == 200:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        update_id = update["update_id"]
                        
                        if "message" in update:
                            handle_message(update["message"])
                        
                        # بروزرسانی آفست بلافاصله پس از خواندن پیام
                        offset = update_id + 1
            elif res.status_code == 409:
                logger.warning("Conflict (409). Check if Webhook is active elsewhere.")
                time.sleep(5)
            else:
                logger.error(f"Failed to fetch updates. Status: {res.status_code}")
                time.sleep(5)
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error in polling loop: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Unhandled error in main loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
```
