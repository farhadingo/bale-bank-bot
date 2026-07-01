import time
import logging
import requests
import psycopg2
from psycopg2 import extras
from datetime import datetime

# ==========================================
# تنظیمات اصلی
# ==========================================
BOT_TOKEN = "160966979:s3cnOPW18kZcUJRSpIUp8r68jnuvjUK72wQ"
DB_CONNECTION_STRING = "postgresql://postgres.uvpwvhmwuklqqmhgdorx:[Farhad35667900]@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"

BALE_API = f"https://tapi.bale.ai/bot{BOT_TOKEN}"

# تنظیمات لاگ‌گیری
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# دیکشنری نگهداری نشست‌های کاربران
user_states = {}

# ==========================================
# توابع کمکی تاریخ شمسی (فرمول ریاضی ساده بدون نیاز به کتابخانه اضافی)
# ==========================================
def get_shamsi_date():
    """محاسبه ساده و دقیق تاریخ شمسی امروز جهت استفاده در گزارشات"""
    today = datetime.now()
    g_y, g_m, g_d = today.year, today.month, today.day
    g_days_in_month = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    
    # بررسی سال کبیسه میلادی
    if (g_y % 4 == 0 and g_y % 100 != 0) or (g_y % 400 == 0):
        g_days_in_month[2] = 29

    gy = g_y - 1600
    gm = g_m - 1
    gd = g_d - 1

    g_day_no = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    for i in range(gm):
        g_day_no += g_days_in_month[i + 1]
    g_day_no += gd

    jy = 979 + 33 * (g_day_no // 12053) + 4 * ((g_day_no % 12053) // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        jy += (g_day_no - 1) // 365
        g_day_no = (g_day_no - 1) % 365

    for i in range(11):
        # روزهای ماه‌های شمسی
        jy_days = 31 if i < 6 else 30
        if g_day_no < jy_days:
            jm = i + 1
            jd = g_day_no + 1
            return f"{jy}/{jm:02d}/{jd:02d}"
        g_day_no -= jy_days
    
    return f"{jy}/12/{g_day_no + 1:02d}"

def get_current_time():
    return datetime.now().strftime("%H:%M:%S")

# ==========================================
# توابع ارتباط با دیتابیس Supabase
# ==========================================
def get_db_connection():
    try:
        conn = psycopg2.connect(DB_CONNECTION_STRING, connect_timeout=5)
        return conn
    except Exception as e:
        logging.error(f"خطا در اتصال به دیتابیس Supabase: {e}")
        return None

def verify_user_by_emp_number(employee_number):
    """بررسی وجود شماره کارمندی در دیتابیس و برگرداندن اطلاعات کاربر"""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            query = """
                SELECT u.id, u.employee_number, u.full_name, u.role, u.title, u.branch_id, b.name as branch_name 
                FROM users u
                LEFT JOIN branches b ON u.branch_id = b.id
                WHERE u.employee_number = %s
            """
            cur.execute(query, (employee_number,))
            return cur.fetchone()
    except Exception as e:
        logging.error(f"Error in verify_user: {e}")
        return None
    finally:
        conn.close()

def save_collection(branch_id, deputy_amount, others_amount, recorded_by):
    """ذخیره یا آپدیت وصولی روز جاری برای یک شعبه مشخص"""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            # اگر برای امروز رکوردی ثبت شده بود آن را آپدیت کند (Upsert)
            query = """
                INSERT INTO collections (branch_id, deputy_amount, others_amount, date, recorded_by)
                VALUES (%s, %s, %s, CURRENT_DATE, %s)
                ON CONFLICT (branch_id, date) 
                DO UPDATE SET 
                    deputy_amount = EXCLUDED.deputy_amount,
                    others_amount = EXCLUDED.others_amount,
                    recorded_by = EXCLUDED.recorded_by;
            """
            cur.execute(query, (branch_id, deputy_amount, others_amount, recorded_by))
            conn.commit()
            return True
    except Exception as e:
        logging.error(f"Error in save_collection: {e}")
        return False
    finally:
        conn.close()

def get_branch_ten_days_report(branch_id):
    """گزارش ۱۰ روز اخیر برای یک شعبه خاص"""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            query = """
                SELECT date, deputy_amount, others_amount, total_amount 
                FROM collections 
                WHERE branch_id = %s AND date >= CURRENT_DATE - INTERVAL '10 days'
                ORDER BY date DESC
            """
            cur.execute(query, (branch_id,))
            return cur.fetchall()
    except Exception as e:
        logging.error(f"Error in get_branch_ten_days_report: {e}")
        return []
    finally:
        conn.close()

def get_admin_today_report():
    """گزارش کامل وصولی‌های امروز به تفکیک تمام شعب برای مدیران ارشد"""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            query = """
                SELECT b.name as branch_name, 
                       COALESCE(c.deputy_amount, 0) as deputy_amount, 
                       COALESCE(c.others_amount, 0) as others_amount, 
                       COALESCE(c.total_amount, 0) as total_amount
                FROM branches b
                LEFT JOIN collections c ON b.id = c.branch_id AND c.date = CURRENT_DATE
                WHERE b.name != 'ستاد استان'
                ORDER BY total_amount DESC, b.name ASC
            """
            cur.execute(query)
            return cur.fetchall()
    except Exception as e:
        logging.error(f"Error in get_admin_today_report: {e}")
        return []
    finally:
        conn.close()

def get_admin_ten_days_report():
    """گزارش جمع‌بندی وصولی کل شعب در ۱۰ روز اخیر"""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            query = """
                SELECT c.date, 
                       SUM(c.deputy_amount) as total_deputy, 
                       SUM(c.others_amount) as total_others, 
                       SUM(c.total_amount) as grand_total
                FROM collections c
                GROUP BY c.date
                ORDER BY c.date DESC
                LIMIT 10
            """
            cur.execute(query)
            return cur.fetchall()
    except Exception as e:
        logging.error(f"Error in get_admin_ten_days_report: {e}")
        return []
    finally:
        conn.close()

def get_top_performing_branches():
    """گزارش طلایی: شعب برتر استان بر اساس مجموع وصولی کل دوران"""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            query = """
                SELECT b.name as branch_name, SUM(c.total_amount) as total_collected
                FROM collections c
                JOIN branches b ON c.branch_id = b.id
                GROUP BY b.name
                ORDER BY total_collected DESC
                LIMIT 5
            """
            cur.execute(query)
            return cur.fetchall()
    except Exception as e:
        logging.error(f"Error in get_top_performing_branches: {e}")
        return []
    finally:
        conn.close()

# ==========================================
# توابع ارتباطی ربات پیام رسان بله
# ==========================================
def send_message(chat_id, text, reply_markup=None):
    url = f"{BALE_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            logging.error(f"Bale API Error: {res.text}")
    except Exception as e:
        logging.error(f"Exception sending message: {e}")

def make_keyboard(buttons):
    """ساخت سریع کیبورد پاسخ متنی بله"""
    keyboard = []
    # چینش دکمه‌ها در ردیف‌های یک یا دو عددی
    for i in range(0, len(buttons), 2):
        row = [{"text": btn} for btn in buttons[i:i+2]]
        keyboard.append(row)
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

# ==========================================
# هسته منطق مدیریت گفتگوها (State Machine)
# ==========================================
def handle_message(chat_id, text):
    state_data = user_states.get(chat_id)

    # فرمان خروج در هر لحظه فعال باشد
    if text == "خروج از حساب" or text == "/start":
        user_states[chat_id] = {"state": "AWAITING_EMP_NUMBER"}
        send_message(chat_id, "🔐 لطفاً شماره کارمندی خود را جهت ورود وارد کنید:")
        return

    if not state_data:
        user_states[chat_id] = {"state": "AWAITING_EMP_NUMBER"}
        send_message(chat_id, "🔐 لطفاً شماره کارمندی خود را جهت ورود وارد کنید:")
        return

    current_state = state_data.get("state")

    # 1. ورود با شماره کارمندی
    if current_state == "AWAITING_EMP_NUMBER":
        user_info = verify_user_by_emp_number(text.strip())
        
        if user_info:
            role = user_info["role"]
            user_states[chat_id] = {
                "state": "LOGGED_IN",
                "user_id": user_info["id"],
                "employee_number": user_info["employee_number"],
                "full_name": user_info["full_name"],
                "role": role,
                "title": user_info["title"],
                "branch_id": user_info["branch_id"],
                "branch_name": user_info["branch_name"]
            }

            welcome_text = (
                f"👤 ورود موفقیت‌آمیز!\n\n"
                f"خوش آمدید {user_info['title']} جناب/سرکار {user_info['full_name']}\n"
                f"🆔 شماره کارمندی: {user_info['employee_number']}\n"
                f"📅 تاریخ امروز: {get_shamsi_date()} | 🕒 ساعت: {get_current_time()}\n"
            )
            
            if role == "deputy":
                buttons = ["ثبت میزان وصول مطالبات امروز", "تهیه گزارش ۱۰ روز اخیر شعبه", "خروج از حساب"]
            else: # admin
                buttons = [
                    "آمار وصولی امروز شعب", 
                    "گزارش کل استان (۱۰ روز اخیر)", 
                    "۵ شعبه برتر استان",
                    "خروج از حساب"
                ]
            
            send_message(chat_id, welcome_text, make_keyboard(buttons))
        else:
            send_message(chat_id, "❌ شماره کارمندی یافت نشد یا معتبر نیست.\nمجدداً شماره کارمندی را ارسال نمایید:")
        return

    # 2. مدیریت فرآیندهای منوی اصلی بعد از ورود
    elif current_state == "LOGGED_IN":
        role = state_data.get("role")

        # ------------------ پنل معاونین شعب ------------------
        if role == "deputy":
            if text == "ثبت میزان وصول مطالبات امروز":
                user_states[chat_id]["state"] = "GETTING_DEPUTY_AMOUNT"
                send_message(
                    chat_id, 
                    "🔹 میزان وصول مطالبات شخص معاون شعبه را به ریال وارد کنید (مثال: 50000000):\n\n(از کیبورد لاتین استفاده کنید)",
                    make_keyboard(["انصراف و بازگشت"])
                )
            
            elif text == "تهیه گزارش ۱۰ روز اخیر شعبه":
                branch_id = state_data.get("branch_id")
                records = get_branch_ten_days_report(branch_id)
                if not records:
                    send_message(chat_id, "ℹ️ هیچ وصولی ثبت‌شده‌ای در ۱۰ روز گذشته برای این شعبه یافت نشد.")
                else:
                    report = f"📊 گزارش وصول مطالبات شعبه {state_data['branch_name']} (۱۰ روز اخیر):\n\n"
                    for r in records:
                        # فرمت دهی تاریخ میلادی به ظاهر مناسب
                        report += (
                            f"📅 تاریخ: {r['date']}\n"
                            f"💰 وصولی معاون: {r['deputy_amount']:,} ریال\n"
                            f"👥 وصولی همکاران: {r['others_amount']:,} ریال\n"
                            f"🔺 جمع کل: {r['total_amount']:,} ریال\n"
                            f"---------------------------\n"
                        )
                    send_message(chat_id, report)
            else:
                send_message(chat_id, "⚠️ گزینه انتخاب شده معتبر نیست. لطفاً از دکمه‌های کیبورد استفاده کنید.")
        
        # ------------------ پنل مدیران ارشد ------------------
        elif role == "admin":
            if text == "آمار وصولی امروز شعب":
                records = get_admin_today_report()
                if not records:
                    send_message(chat_id, "ℹ️ تا این لحظه اطلاعات وصولی برای امروز ثبت نشده است.")
                else:
                    report = f"📊 آمار وصولی شعب استان زنجان در تاریخ {get_shamsi_date()}:\n\n"
                    grand_total = 0
                    for r in records:
                        total = r['total_amount']
                        grand_total += total
                        status = "✅ ثبت شده" if total > 0 else "❌ ثبت نشده"
                        report += (
                            f"🏢 شعبه: {r['branch_name']} ({status})\n"
                            f"   • وصولی معاون: {r['deputy_amount']:,} ریال\n"
                            f"   • وصولی همکاران: {r['others_amount']:,} ریال\n"
                            f"   • جمع وصولی: {total:,} ریال\n"
                            f"---------------------------\n"
                        )
                    report += f"\n📈 مجموع کل وصولی استان امروز: {grand_total:,} ریال"
                    send_message(chat_id, report)

            elif text == "گزارش کل استان (۱۰ روز اخیر)":
                records = get_admin_ten_days_report()
                if not records:
                    send_message(chat_id, "ℹ️ داده‌ای جهت نمایش یافت نشد.")
                else:
                    report = f"📉 گزارش مجموع وصولی استان زنجان (۱۰ روز اخیر):\n\n"
                    for r in records:
                        report += (
                            f"📅 تاریخ: {r['date']}\n"
                            f"   • مجموع معاونین: {int(r['total_deputy']):,} ریال\n"
                            f"   • مجموع همکاران: {int(r['total_others']):,} ریال\n"
                            f"   • جمع کل روز: {int(r['grand_total']):,} ریال\n"
                            f"---------------------------\n"
                        )
                    send_message(chat_id, report)

            elif text == "۵ شعبه برتر استان":
                records = get_top_performing_branches()
                if not records:
                    send_message(chat_id, "ℹ️ داده‌ای در دیتابیس یافت نشد.")
                else:
                    report = "🏆 ۵ شعبه برتر استان زنجان بر اساس مجموع کارکرد کل:\n\n"
                    for idx, r in enumerate(records, 1):
                        report += f"{idx}. {r['branch_name']} 👈 {int(r['total_collected']):,} ریال\n"
                    send_message(chat_id, report)
            else:
                send_message(chat_id, "⚠️ گزینه انتخاب شده معتبر نیست.")

    # 3. دریافت مبلغ وصولی معاون
    elif current_state == "GETTING_DEPUTY_AMOUNT":
        if text == "انصراف و بازگشت":
            user_states[chat_id]["state"] = "LOGGED_IN"
            buttons = ["ثبت میزان وصول مطالبات امروز", "تهیه گزارش ۱۰ روز اخیر شعبه", "خروج از حساب"]
            send_message(chat_id, "عملیات لغو شد. به منوی اصلی بازگشتید.", make_keyboard(buttons))
            return
        
        try:
            amount = int(text.replace(",", "").replace("،", "").strip())
            if amount < 0:
                raise ValueError
            
            user_states[chat_id]["temp_deputy_amount"] = amount
            user_states[chat_id]["state"] = "GETTING_OTHERS_AMOUNT"
            send_message(
                chat_id, 
                "👥 بسیار خوب. حالا میزان وصول مطالبات سایر همکاران شعبه خود را به ریال وارد کنید:\n\n(مثال: 120000000)",
                make_keyboard(["انصراف و بازگشت"])
            )
        except ValueError:
            send_message(chat_id, "❌ لطفاً مبلغ معتبر را فقط به صورت عدد انگلیسی وارد کنید:")

    # 4. دریافت مبلغ سایر پرسنل و ذخیره نهایی
    elif current_state == "GETTING_OTHERS_AMOUNT":
        if text == "انصراف و بازگشت":
            user_states[chat_id]["state"] = "LOGGED_IN"
            buttons = ["ثبت میزان وصول مطالبات امروز", "تهیه گزارش ۱۰ روز اخیر شعبه", "خروج از حساب"]
            send_message(chat_id, "عملیات لغو شد. به منوی اصلی بازگشتید.", make_keyboard(buttons))
            return

        try:
            others_amount = int(text.replace(",", "").replace("،", "").strip())
            if others_amount < 0:
                raise ValueError
            
            deputy_amount = state_data.get("temp_deputy_amount")
            branch_id = state_data.get("branch_id")
            recorded_by = state_data.get("user_id")

            # ثبت در دیتابیس
            success = save_collection(branch_id, deputy_amount, others_amount, recorded_by)
            
            user_states[chat_id]["state"] = "LOGGED_IN"
            buttons = ["ثبت میزان وصول مطالبات امروز", "تهیه گزارش ۱۰ روز اخیر شعبه", "خروج از حساب"]
            
            if success:
                success_msg = (
                    f"✅ ثبت اطلاعات وصولی امروز با موفقیت انجام شد:\n\n"
                    f"🏢 شعبه: {state_data['branch_name']}\n"
                    f"💰 وصولی معاون: {deputy_amount:,} ریال\n"
                    f"👥 وصولی همکاران: {others_amount:,} ریال\n"
                    f"📈 جمع کل: {deputy_amount + others_amount:,} ریال\n"
                )
                send_message(chat_id, success_msg, make_keyboard(buttons))
            else:
                send_message(chat_id, "❌ متأسفانه در حین ذخیره خطایی رخ داد. لطفاً مجدداً تلاش کنید.", make_keyboard(buttons))

        except ValueError:
            send_message(chat_id, "❌ لطفاً مبلغ معتبر را فقط به صورت عدد انگلیسی وارد کنید:")

# ==========================================
# دریافت آپدیت‌ها (Polling) با مدیریت خطا
# ==========================================
def get_updates(offset=None):
    url = f"{BALE_API}/getUpdates"
    params = {"timeout": 20, "offset": offset}
    try:
        response = requests.get(url, params=params, timeout=25)
        if response.status_code == 200:
            return response.json()
        logging.error(f"GetUpdates API error: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Network error in get_updates: {e}")
    return None

def main():
    logging.info("ربات پایش وصول مطالبات با موفقیت در حالت Polling فعال شد...")
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            if updates and updates.get("ok"):
                for result in updates.get("result", []):
                    offset = result["update_id"] + 1
                    if "message" in result:
                        msg = result["message"]
                        chat_id = msg["chat"]["id"]
                        text = msg.get("text", "").strip()
                        if text:
                            handle_message(chat_id, text)
            time.sleep(1)
        except Exception as e:
            logging.error(f"Error in main polling cycle: {e}")
            time.sleep(5)

if __name__ == "__main__":
    # حلقه نگهدارنده بیرونی جهت تضمین عدم کرش و پایداری ۲۴ ساعته در Render
    while True:
        try:
            main()
        except Exception as e:
            logging.error(f"Fatal crash restart trigger: {e}")
            time.sleep(10)
