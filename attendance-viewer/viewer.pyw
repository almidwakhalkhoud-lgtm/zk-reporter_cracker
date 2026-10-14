# -*- coding: utf-8 -*-
"""
نمایشگر ورود و خروج — برنامهٔ دسکتاپ مستقل
با دکمهٔ «دریافت اطلاعات» آخرین ورود/خروج‌ها را از وب‌سایت می‌گیرد و جدول نشان می‌دهد.
(داده‌ها همان است که برنامهٔ مغازه هر ۱۵ دقیقه از دستگاه اثرانگشت به سایت می‌فرستد)

نیاز: فقط پایتون — هیچ کتابخانهٔ اضافه‌ای لازم نیست.
اجرا: دوبار کلیک روی همین فایل (viewer.pyw)
"""
import json
import os
import ssl
import threading
import http.cookiejar
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "site_url": "https://reporter-app-snowy.vercel.app",
    "access_code": "",
}

# روز کاری مغازه: ۷ صبح تا ۲ بامداد؛ ثبت‌های قبل از ۴ صبح جزو روز قبل
BUSINESS_CUTOFF_HOUR = 4
DEDUP_SECONDS = 120


# ---------------------------------------------------------------- تنظیمات
def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(dict(DEFAULT_CONFIG))
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- ارتباط با سایت
def ssl_context():
    ctx = ssl.create_default_context()
    try:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    except AttributeError:
        pass
    return ctx


class SiteClient:
    def __init__(self, base_url):
        self.base = base_url.strip().rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            self.base = "https://" + self.base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ssl_context()),
        )

    def _request(self, path, data=None, timeout=30):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with self.opener.open(req, timeout=timeout) as resp:
            return json.load(resp)

    def login(self, access_code):
        try:
            self._request("/api/auth/login", {"code": access_code})
            return True
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False
            raise

    def attendance(self):
        return self._request("/api/attendance")


# ---------------------------------------------------------------- پردازش داده
def business_date(ts):
    """ts مثل 2026-07-15T17:30:39 — ثبت قبل از ۴ صبح جزو روز قبل"""
    day = datetime.strptime(ts[:10], "%Y-%m-%d").date()
    if int(ts[11:13]) < BUSINESS_CUTOFF_HOUR:
        day -= timedelta(days=1)
    return day


def dedup(punches):
    """حذف ثبت‌های پشت سر هم با فاصلهٔ کمتر از ۲ دقیقه برای هر عضو"""
    out, last = [], {}
    for p in sorted(punches, key=lambda p: p["ts"]):
        prev = last.get(p["pin"])
        current = datetime.fromisoformat(p["ts"])
        if prev and (current - prev).total_seconds() < DEDUP_SECONDS:
            continue
        last[p["pin"]] = current
        out.append(p)
    return out


DOW_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def summarize(punches):
    """گروه‌بندی به ازای (روز کاری، عضو): ورود = اولین ثبت، خروج = آخرین ثبت"""
    groups = {}
    for p in punches:
        key = (business_date(p["ts"]), p["pin"])
        groups.setdefault(key, []).append(p)
    rows = []
    for (day, pin), items in groups.items():
        items.sort(key=lambda p: p["ts"])
        first, last = items[0], items[-1]
        if len(items) > 1:
            checkout = last["ts"][11:19]
            minutes = int((datetime.fromisoformat(last["ts"])
                           - datetime.fromisoformat(first["ts"])).total_seconds() // 60)
            presence = "%d:%02d" % (minutes // 60, minutes % 60)
        else:
            checkout, presence = "—", "—"
        rows.append({
            "day": day,
            "date": day.strftime("%Y-%m-%d"),
            "weekday": DOW_FA[day.weekday()],
            "name": first.get("name") or ("عضو " + str(pin)),
            "pin": str(pin),
            "checkin": first["ts"][11:19],
            "checkout": checkout,
            "presence": presence,
            "count": len(items),
        })
    rows.sort(key=lambda r: (r["day"], r["pin"]), reverse=True)
    return rows


# ---------------------------------------------------------------- رابط کاربری
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        root.title("گزارش ورود و خروج — دستگاه اثرانگشت مغازه")
        root.geometry("980x560")
        root.minsize(760, 400)

        top = ttk.Frame(root, padding=(12, 12, 12, 6))
        top.pack(fill="x")

        self.fetch_btn = ttk.Button(top, text="⟳  دریافت اطلاعات", command=self.fetch)
        self.fetch_btn.pack(side="right")

        self.status = tk.StringVar(value="برای شروع، دکمهٔ «دریافت اطلاعات» را بزنید")
        ttk.Label(top, textvariable=self.status, anchor="e").pack(
            side="right", padx=(0, 14), fill="x", expand=True)

        columns = ("date", "weekday", "name", "pin", "checkin", "checkout", "presence", "count")
        headers = {
            "date": "تاریخ", "weekday": "روز", "name": "نام", "pin": "شناسه",
            "checkin": "ساعت ورود", "checkout": "ساعت خروج",
            "presence": "مدت حضور", "count": "تعداد ثبت",
        }
        widths = {"date": 110, "weekday": 90, "name": 160, "pin": 70,
                  "checkin": 100, "checkout": 100, "presence": 100, "count": 80}

        wrap = ttk.Frame(root, padding=(12, 0, 12, 12))
        wrap.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(wrap, columns=columns, show="headings")
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(col, width=widths[col], anchor="center")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.tag_configure("zebra", background="#f2f3fa")
        self.tree.tag_configure("today", background="#e2f5ec")

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    # ---------------------------------------------------------- دریافت
    def ensure_access_code(self):
        if self.cfg.get("access_code"):
            return True
        code = simpledialog.askstring(
            "کد ورود",
            "کد دسترسی ادمین سایت را وارد کنید\n(همان کدی که با آن وارد سایت می‌شوید):",
            show="*", parent=self.root)
        if not code:
            return False
        self.cfg["access_code"] = code.strip()
        save_config(self.cfg)
        return True

    def fetch(self):
        if not self.ensure_access_code():
            return
        self.fetch_btn.state(["disabled"])
        self.status.set("در حال دریافت اطلاعات از سایت…")
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            client = SiteClient(self.cfg["site_url"])
            if not client.login(self.cfg["access_code"]):
                # کد اشتباه — پاکش کن تا دفعهٔ بعد دوباره بپرسد
                self.cfg["access_code"] = ""
                save_config(self.cfg)
                self._done("کد دسترسی اشتباه است — دوباره «دریافت اطلاعات» را بزنید", error=True)
                return
            data = client.attendance()
            punches = dedup(data.get("punches", []))
            rows = summarize(punches)
            synced = data.get("syncedAt")
            self.root.after(0, lambda: self._show(rows, synced))
        except Exception as e:
            self._done("خطا: %s" % e, error=True)

    def _done(self, message, error=False):
        def apply():
            self.status.set(message)
            self.fetch_btn.state(["!disabled"])
            if error:
                messagebox.showerror("خطا", message, parent=self.root)
        self.root.after(0, apply)

    def _show(self, rows, synced):
        self.tree.delete(*self.tree.get_children())
        now = datetime.now()
        today = (now - timedelta(days=1)).date() if now.hour < BUSINESS_CUTOFF_HOUR else now.date()
        for i, r in enumerate(rows):
            tags = []
            if r["day"] == today:
                tags.append("today")
            elif i % 2:
                tags.append("zebra")
            self.tree.insert("", "end", values=(
                r["date"], r["weekday"], r["name"], r["pin"],
                r["checkin"], r["checkout"], r["presence"], r["count"],
            ), tags=tags)
        when = ""
        if synced:
            try:
                stamp = datetime.fromisoformat(synced.replace("Z", "+00:00"))
                stamp = stamp + timedelta(hours=4)  # ساعت عمان
                when = " | آخرین به‌روزرسانی از دستگاه: " + stamp.strftime("%H:%M")
            except ValueError:
                pass
        self.status.set("%d ردیف دریافت شد%s" % (len(rows), when))
        self.fetch_btn.state(["!disabled"])


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
