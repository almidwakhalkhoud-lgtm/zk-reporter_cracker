# -*- coding: utf-8 -*-
"""
ربات تلگرام گزارش تردد — مخصوص اجرا روی سیستم مغازه
به دستگاه اثرانگشت ZKTeco F18 (پورت 4370) وصل می‌شود و در پاسخ به درخواست ادمین،
گزارش و فایل اکسل را در تلگرام می‌فرستد. فایل‌های بکاپ .dat ارسالی را هم پارس می‌کند.

پیش‌نیاز:  pip install pyzk
تنظیمات:   config.json کنار همین فایل
"""
import json, os, sys, time, gzip, tarfile, sqlite3, tempfile, io, re
import zipfile
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
LOG_PATH = os.path.join(BASE, 'bot.log')

# ---------------------------------------------------------------- تنظیمات
def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    missing = [k for k in ('bot_token', 'admin_ids', 'device_ip') if not cfg.get(k)]
    if missing or 'YOUR' in str(cfg.get('bot_token', '')):
        print('لطفاً ابتدا config.json را کامل کنید: ' + ', '.join(missing or ['bot_token']))
        sys.exit(1)
    cfg.setdefault('device_port', 4370)
    cfg.setdefault('comm_key', 0)
    cfg.setdefault('dedup_seconds', 120)
    cfg.setdefault('site_url', '')       # مثل https://example.vercel.app
    cfg.setdefault('site_key', '')       # همان ATTENDANCE_UPLOAD_KEY در Vercel
    cfg.setdefault('site_sync_minutes', 15)
    return cfg

def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass

# ---------------------------------------------------------------- تاریخ شمسی
def to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd

FA_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
DOW_FA = ['دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه', 'شنبه', 'یکشنبه']  # weekday() 0=Mon

def fa(s):
    return str(s).translate(FA_DIGITS)

def jdate(dt):
    jy, jm, jd = to_jalali(dt.year, dt.month, dt.day)
    return fa('%04d/%02d/%02d' % (jy, jm, jd))

def gdate(dt):
    return dt.strftime('%Y-%m-%d')

def ftime(dt):
    return fa(dt.strftime('%H:%M:%S'))

def dow(dt):
    return DOW_FA[dt.weekday()]

STATUS_FA = {0: 'ورود', 1: 'خروج', 2: 'خروج استراحت', 3: 'ورود استراحت', 4: 'ورود اضافه‌کار', 5: 'خروج اضافه‌کار'}
VERIFY_FA = {0: 'رمز', 1: 'اثر انگشت', 2: 'شناسه', 3: 'رمز', 4: 'کارت', 15: 'چهره'}

# ---------------------------------------------------------------- مدل رکورد
class Rec:
    __slots__ = ('pin', 'name', 'dt', 'status', 'verify')
    def __init__(self, pin, name, dt, status, verify):
        self.pin, self.name, self.dt, self.status, self.verify = str(pin), name, dt, status, verify

# روز کاری مغازه ۷ صبح تا ۲ بامداد است؛ ثبت‌های قبل از ۴ صبح جزو روز کاری قبل حساب می‌شوند.
BUSINESS_CUTOFF_HOUR = 4

def business_date(dt):
    d = dt.date()
    if dt.hour < BUSINESS_CUTOFF_HOUR:
        d -= timedelta(days=1)
    return d

def bdate_dt(d):
    """date → datetime برای توابع نمایش تاریخ"""
    return datetime(d.year, d.month, d.day)

def dedup(rows, seconds):
    out, last = [], {}
    for r in sorted(rows, key=lambda r: r.dt):
        prev = last.get(r.pin)
        if prev and (r.dt - prev).total_seconds() < seconds:
            continue
        last[r.pin] = r.dt
        out.append(r)
    return out

def daily_summary(rows):
    groups = {}
    for r in rows:
        groups.setdefault((r.pin, r.name, business_date(r.dt)), []).append(r)
    out = []
    for (pin, name, bd), g in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        g.sort(key=lambda r: r.dt)
        first, lastr = g[0], g[-1]
        out.append({'pin': pin, 'name': name, 'dt': bdate_dt(bd),
                    'first': first.dt, 'last': lastr.dt if len(g) > 1 else None, 'count': len(g)})
    return out

def fmt_dur(td):
    m = int(td.total_seconds() // 60)
    return fa(m // 60) + ' ساعت و ' + fa(m % 60) + ' دقیقه'

# ---------------------------------------------------------------- اتصال به دستگاه
def fetch_from_device(cfg):
    """اتصال به F18 و برگرداندن (rows, users_list)"""
    from zk import ZK
    zk = ZK(cfg['device_ip'], port=cfg['device_port'], timeout=15,
            password=cfg['comm_key'], force_udp=False, ommit_ping=True)
    conn = zk.connect()
    try:
        conn.disable_device()
        users = conn.get_users() or []
        atts = conn.get_attendance() or []
    finally:
        try:
            conn.enable_device()
        finally:
            conn.disconnect()
    names = {str(u.user_id): (u.name or ('عضو ' + str(u.user_id))) for u in users}
    rows = [Rec(a.user_id, names.get(str(a.user_id), 'عضو ' + str(a.user_id)),
                a.timestamp, getattr(a, 'punch', 0), getattr(a, 'status', 1)) for a in atts]
    rows.sort(key=lambda r: r.dt)
    ulist = [{'pin': str(u.user_id), 'name': u.name or '—', 'admin': bool(u.privilege)} for u in users]
    return rows, ulist

# ---------------------------------------------------------------- پارس فایل .dat
def parse_dat(data):
    """پارس بکاپ ZK format → (rows, users, serial, exported_at)"""
    head = data[:32].split(b'\0')[0].decode('latin-1', 'replace')
    if not head.startswith('ZK format'):
        raise ValueError('این فایل بکاپ ZKTeco نیست')
    serial = data[0x20:0x60].split(b'\0')[0].decode('latin-1', 'replace')
    exported = data[0xa0:0xc0].split(b'\0')[0].decode('latin-1', 'replace')
    i = data.find(b'\x1f\x8b\x08')
    if i < 0:
        raise ValueError('بخش فشردهٔ داده پیدا نشد')
    tar_bytes = gzip.decompress(data[i:])
    tf = tarfile.open(fileobj=io.BytesIO(tar_bytes))
    db_bytes = None
    for m in tf.getmembers():
        if m.name.lower().endswith('.db'):
            db_bytes = tf.extractfile(m).read()
    if not db_bytes:
        raise ValueError('دیتابیس ZKDB.db داخل فایل نیست')
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as t:
        t.write(db_bytes)
        tmp = t.name
    try:
        con = sqlite3.connect(tmp)
        users = {}
        ulist = []
        for pin, name, priv in con.execute('SELECT User_PIN, Name, Privilege FROM USER_INFO'):
            users[str(pin)] = name or ('عضو ' + str(pin))
            ulist.append({'pin': str(pin), 'name': name or '—', 'admin': bool(priv)})
        rows = []
        for pin, vt, ts, st in con.execute('SELECT User_PIN, Verify_Type, Verify_Time, Status FROM ATT_LOG'):
            dt = datetime.fromisoformat(ts)
            rows.append(Rec(pin, users.get(str(pin), 'عضو ' + str(pin)), dt, st, vt))
        con.close()
    finally:
        os.unlink(tmp)
    rows.sort(key=lambda r: r.dt)
    return rows, ulist, serial, exported

# ---------------------------------------------------------------- ساخت اکسل (xlsx)
XLSX_STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="4"><font><sz val="10"/><name val="Tahoma"/><color rgb="FF1C2333"/></font><font><b/><sz val="10"/><name val="Tahoma"/><color rgb="FFFFFFFF"/></font><font><b/><sz val="13"/><name val="Tahoma"/><color rgb="FF3730A3"/></font><font><sz val="9"/><name val="Tahoma"/><color rgb="FF68718A"/></font></fonts><fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF4F46E5"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF3F4FB"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEEF0FF"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFD5DAE8"/></left><right style="thin"><color rgb="FFD5DAE8"/></right><top style="thin"><color rgb="FFD5DAE8"/></top><bottom style="thin"><color rgb="FFD5DAE8"/></bottom><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="2" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="3" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf></cellXfs></styleSheet>'''

def _esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;'))

def _colref(i):
    s = ''
    i += 1
    while i:
        i -= 1
        s = chr(65 + i % 26) + s
        i //= 26
    return s

def _sheet_xml(title, subtitle, headers, rows, widths):
    n = len(headers)
    def cell(r, c, v, s):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s%d" s="%d" t="n"><v>%s</v></c>' % (_colref(c), r, s, v)
        return ('<c r="%s%d" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                % (_colref(c), r, s, _esc(v if v is not None else '')))
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView rightToLeft="1" workbookViewId="0" showGridLines="0"/></sheetViews>'
             '<sheetFormatPr defaultRowHeight="20"/><cols>']
    for i, w in enumerate(widths):
        parts.append('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w))
    parts.append('</cols><sheetData>')
    parts.append('<row r="1" ht="30" customHeight="1">' + cell(1, 0, title, 1) +
                 ''.join('<c r="%s1" s="1"/>' % _colref(i + 1) for i in range(n - 1)) + '</row>')
    parts.append('<row r="2" ht="18" customHeight="1">' + cell(2, 0, subtitle, 2) +
                 ''.join('<c r="%s2" s="2"/>' % _colref(i + 1) for i in range(n - 1)) + '</row>')
    parts.append('<row r="3" ht="24" customHeight="1">' +
                 ''.join(cell(3, c, h, 3) for c, h in enumerate(headers)) + '</row>')
    for ri, row in enumerate(rows):
        s = 5 if ri % 2 else 4
        parts.append('<row r="%d" ht="19" customHeight="1">' % (ri + 4) +
                     ''.join(cell(ri + 4, c, v, s) for c, v in enumerate(row)) + '</row>')
    parts.append('</sheetData><mergeCells count="2"><mergeCell ref="A1:%s1"/><mergeCell ref="A2:%s2"/></mergeCells></worksheet>'
                 % (_colref(n - 1), _colref(n - 1)))
    return ''.join(parts)

def build_xlsx(sheets):
    """sheets: list of dicts {name,title,subtitle,headers,rows,widths} → bytes"""
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    wb_sheets, wb_rels = [], []
    for i, sh in enumerate(sheets, 1):
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i)
        wb_sheets.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(sh['name']), i, i))
        wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i))
    ct.append('</Types>')
    style_rid = len(sheets) + 1
    wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % style_rid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ''.join(ct))
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets>%s</sheets></workbook>' % ''.join(wb_sheets))
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>' % ''.join(wb_rels))
        z.writestr('xl/styles.xml', XLSX_STYLES)
        for i, sh in enumerate(sheets, 1):
            z.writestr('xl/worksheets/sheet%d.xml' % i,
                       _sheet_xml(sh['title'], sh['subtitle'], sh['headers'], sh['rows'], sh['widths']))
    return buf.getvalue()

def report_xlsx(rows, users, source_label):
    now = datetime.now()
    sub = '%s | تهیهٔ گزارش: %s %s' % (source_label, jdate(now), fa(now.strftime('%H:%M')))
    log_rows = [[i + 1, r.name, fa(r.pin), jdate(r.dt), gdate(r.dt), dow(r.dt), ftime(r.dt),
                 STATUS_FA.get(r.status, 'کد %s' % r.status), VERIFY_FA.get(r.verify, 'کد %s' % r.verify)]
                for i, r in enumerate(rows)]
    daily = daily_summary(rows)
    daily_rows = [[d['name'], fa(d['pin']), jdate(d['dt']), gdate(d['dt']), dow(d['dt']),
                   ftime(d['first']), ftime(d['last']) if d['last'] else '—',
                   fmt_dur(d['last'] - d['first']) if d['last'] else '—', d['count']] for d in daily]
    user_rows = [[fa(u['pin']), u['name'], 'مدیر' if u['admin'] else 'کاربر عادی',
                  sum(1 for r in rows if r.pin == u['pin'])] for u in users]
    return build_xlsx([
        {'name': 'ریز ترددها', 'title': 'گزارش ریز ترددها', 'subtitle': sub,
         'headers': ['ردیف', 'نام', 'شناسه', 'تاریخ (شمسی)', 'تاریخ میلادی', 'روز', 'ساعت', 'نوع ثبت', 'روش تأیید'],
         'rows': log_rows, 'widths': [7, 16, 9, 13, 13, 11, 11, 15, 12]},
        {'name': 'خلاصه روزانه', 'title': 'گزارش خلاصهٔ روزانه', 'subtitle': sub,
         'headers': ['نام', 'شناسه', 'تاریخ (شمسی)', 'تاریخ میلادی', 'روز', 'اولین ثبت', 'آخرین ثبت', 'مدت حضور', 'تعداد ثبت'],
         'rows': daily_rows, 'widths': [16, 9, 13, 13, 11, 11, 11, 20, 10]},
        {'name': 'اعضا', 'title': 'فهرست اعضا', 'subtitle': sub,
         'headers': ['شناسه', 'نام', 'سطح دسترسی', 'تعداد تردد'],
         'rows': user_rows, 'widths': [10, 20, 14, 12]},
    ])

# ---------------------------------------------------------------- ارسال به وب‌سایت
def push_to_site(cfg, rows, users):
    """ارسال داده‌ها به سایت (Next.js/Vercel). True یعنی موفق."""
    if not cfg.get('site_url') or not cfg.get('site_key'):
        return False
    payload = json.dumps({
        'users': [{'pin': u['pin'], 'name': u['name'], 'admin': u['admin']} for u in users],
        'punches': [{'pin': r.pin, 'ts': r.dt.strftime('%Y-%m-%dT%H:%M:%S'),
                     'status': r.status, 'verify': r.verify} for r in rows],
    }).encode('utf-8')
    base = cfg['site_url'].strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = ('http://' if base.split(':')[0] in ('localhost', '127.0.0.1') else 'https://') + base
    url = base + '/api/attendance'
    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json',
        'x-upload-key': cfg['site_key'],
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.load(resp)
    log('sync به سایت: %s رکورد جدید' % result.get('saved', 0))
    return True

def try_push_to_site(cfg, rows, users):
    try:
        return push_to_site(cfg, rows, users)
    except Exception as e:
        log('site sync error: %s' % e)
        return False

# ---------------------------------------------------------------- تلگرام
class Tg:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token
        self.file_base = 'https://api.telegram.org/file/bot' + token
        self.offset = 0

    def call(self, method, params=None, timeout=65):
        url = self.base + '/' + method
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def get_updates(self):
        try:
            r = self.call('getUpdates', {'timeout': 50, 'offset': self.offset,
                                         'allowed_updates': '["message"]'}, timeout=60)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(5)
            return []
        ups = r.get('result', [])
        if ups:
            self.offset = ups[-1]['update_id'] + 1
        return ups

    def send_text(self, chat_id, text, keyboard=None):
        p = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
        if keyboard:
            p['reply_markup'] = json.dumps(keyboard, ensure_ascii=False)
        try:
            self.call('sendMessage', p, timeout=30)
        except Exception as e:
            log('sendMessage error: %s' % e)

    def send_document(self, chat_id, filename, blob, caption=''):
        boundary = '----zkbot%d' % int(time.time() * 1000)
        parts = []
        for k, v in (('chat_id', str(chat_id)), ('caption', caption)):
            parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                          % (boundary, k, v)).encode())
        parts.append(('--%s\r\nContent-Disposition: form-data; name="document"; filename="%s"\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n' % (boundary, filename)).encode())
        parts.append(blob)
        parts.append(('\r\n--%s--\r\n' % boundary).encode())
        body = b''.join(parts)
        req = urllib.request.Request(self.base + '/sendDocument', data=body, headers={
            'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)

    def download_file(self, file_id):
        r = self.call('getFile', {'file_id': file_id}, timeout=30)
        path = r['result']['file_path']
        with urllib.request.urlopen(self.file_base + '/' + path, timeout=120) as resp:
            return resp.read()

# ---------------------------------------------------------------- منطق ربات
KEYBOARD = {'keyboard': [
    [{'text': '🌙 گزارش دیروز'}, {'text': '📊 گزارش امروز'}],
    [{'text': '📅 گزارش کامل'}],
    [{'text': '🔄 ارسال گزارشات به سایت'}],
    [{'text': '👥 اعضا'}, {'text': 'ℹ️ وضعیت دستگاه'}],
], 'resize_keyboard': True}

HELP = ('سلام! ربات گزارش تردد آماده است 🙌\n\n'
        '🌙 <b>گزارش دیروز</b> — ورود و خروج روز کاری قبل\n'
        '📊 <b>گزارش امروز</b> — ورود و خروج امروز\n'
        '📅 <b>گزارش کامل</b> — کل سوابق دستگاه + فایل اکسل\n'
        '🔄 <b>ارسال گزارشات به سایت</b> — ارسال فوری اطلاعات دستگاه به سایت\n'
        '👥 <b>اعضا</b> — فهرست اعضای تعریف‌شده\n'
        'ℹ️ <b>وضعیت دستگاه</b> — تست اتصال به دستگاه\n\n'
        'روز کاری از ۷ صبح تا ۲ بامداد حساب می‌شود.\n'
        '📎 همچنین اگر فایل بکاپ <code>.dat</code> بفرستید، همان‌جا برایتان اکسل می‌کنم.')

def day_report_text(rows, target_bd, title):
    """گزارش ورود/خروج یک روز کاری مشخص (اولین ثبت = ورود، آخرین = خروج)"""
    days = [r for r in rows if business_date(r.dt) == target_bd]
    show = bdate_dt(target_bd)
    head = '%s — %s (%s)\n' % (title, jdate(show), gdate(show))
    if not days:
        return head + '\nدر این روز ترددی ثبت نشده است.'
    lines = [head]
    by_user = {}
    for r in days:
        by_user.setdefault(r.name, []).append(r)
    for name, g in by_user.items():
        g.sort(key=lambda r: r.dt)
        lines.append('\n👤 <b>%s</b>' % name)
        lines.append('  🟢 ورود: %s' % ftime(g[0].dt))
        if len(g) > 1:
            lines.append('  🔴 خروج: %s' % ftime(g[-1].dt))
            lines.append('  ⏱ حضور: %s' % fmt_dur(g[-1].dt - g[0].dt))
        else:
            lines.append('  🔴 خروج: ثبت نشده')
    return '\n'.join(lines)

def today_text(rows):
    return day_report_text(rows, business_date(datetime.now()), '📊 <b>گزارش امروز</b>')

def yesterday_text(rows):
    return day_report_text(rows, business_date(datetime.now()) - timedelta(days=1),
                           '🌙 <b>گزارش دیروز</b>')

def handle_message(tg, cfg, msg):
    chat_id = msg['chat']['id']
    uid = msg.get('from', {}).get('id')
    if uid not in cfg['admin_ids']:
        log('پیام از غیرادمین %s نادیده گرفته شد' % uid)
        return
    text = (msg.get('text') or '').strip()
    doc = msg.get('document')

    if doc:  # فایل .dat ارسالی
        fname = doc.get('file_name', '')
        if not fname.lower().endswith('.dat'):
            tg.send_text(chat_id, 'فقط فایل بکاپ با پسوند .dat را پشتیبانی می‌کنم.', KEYBOARD)
            return
        tg.send_text(chat_id, '⏳ فایل دریافت شد، در حال پارس…')
        try:
            data = tg.download_file(doc['file_id'])
            rows, users, serial, exported = parse_dat(data)
            rows = dedup(rows, cfg['dedup_seconds'])
            blob = report_xlsx(rows, users, 'بکاپ دستگاه %s (%s)' % (serial, exported[:10]))
            tg.send_document(chat_id, 'گزارش تردد.xlsx', blob,
                             '✅ %s رکورد از بکاپ دستگاه %s' % (fa(len(rows)), serial))
        except Exception as e:
            log('parse_dat error: %s' % e)
            tg.send_text(chat_id, '❌ خطا در پارس فایل: %s' % e, KEYBOARD)
        return

    if text in ('/start', '/help', 'راهنما'):
        tg.send_text(chat_id, HELP, KEYBOARD)
        return

    if text in ('🔄 ارسال گزارشات به سایت', 'ارسال گزارشات به سایت', 'ارسال به سایت', '/sync'):
        if not cfg.get('site_url') or not cfg.get('site_key'):
            tg.send_text(chat_id, '❌ آدرس یا کلید سایت در تنظیمات ربات وارد نشده است.', KEYBOARD)
            return
        tg.send_text(chat_id, '⏳ در حال دریافت اطلاعات دستگاه و ارسال به سایت…')
        try:
            rows, users = fetch_from_device(cfg)
            push_to_site(cfg, rows, users)
            tg.send_text(
                chat_id,
                '✅ ارسال گزارشات به سایت با موفقیت صورت گرفت.\n'
                '📊 تعداد رکوردهای بررسی‌شده: %s' % fa(len(rows)),
                KEYBOARD,
            )
        except Exception as e:
            log('manual site sync error: %s' % e)
            tg.send_text(chat_id, '❌ ارسال گزارشات به سایت ناموفق بود: %s' % e, KEYBOARD)
        return

    if text in ('📊 گزارش امروز', 'گزارش امروز', '/today', '🌙 گزارش دیروز', 'گزارش دیروز', '/yesterday'):
        wants_yesterday = 'دیروز' in text or text == '/yesterday'
        tg.send_text(chat_id, '⏳ در حال اتصال به دستگاه…')
        try:
            rows, users = fetch_from_device(cfg)
            try_push_to_site(cfg, rows, users)
            rows = dedup(rows, cfg['dedup_seconds'])
            tg.send_text(chat_id, yesterday_text(rows) if wants_yesterday else today_text(rows), KEYBOARD)
        except Exception as e:
            log('device error: %s' % e)
            tg.send_text(chat_id, '❌ دستگاه در دسترس نیست: %s' % e, KEYBOARD)
        return

    if text in ('📅 گزارش کامل', 'گزارش کامل', 'گزارش', '/report'):
        tg.send_text(chat_id, '⏳ در حال اتصال به دستگاه و ساخت گزارش…')
        try:
            rows, users = fetch_from_device(cfg)
            try_push_to_site(cfg, rows, users)
            rows = dedup(rows, cfg['dedup_seconds'])
            blob = report_xlsx(rows, users, 'دریافت مستقیم از دستگاه %s' % cfg['device_ip'])
            first = jdate(rows[0].dt) if rows else '—'
            last = jdate(rows[-1].dt) if rows else '—'
            tg.send_document(chat_id, 'گزارش تردد.xlsx', blob,
                             '✅ %s رکورد | از %s تا %s' % (fa(len(rows)), first, last))
        except Exception as e:
            log('device error: %s' % e)
            tg.send_text(chat_id, '❌ دستگاه در دسترس نیست: %s' % e, KEYBOARD)
        return

    if text in ('👥 اعضا', 'اعضا', '/users'):
        tg.send_text(chat_id, '⏳ در حال اتصال به دستگاه…')
        try:
            rows, users = fetch_from_device(cfg)
            lines = ['👥 <b>اعضای دستگاه</b> (%s نفر)\n' % fa(len(users))]
            for u in users:
                n = sum(1 for r in rows if r.pin == u['pin'])
                lines.append('• %s (شناسه %s)%s — %s تردد' %
                             (u['name'], fa(u['pin']), ' 👑' if u['admin'] else '', fa(n)))
            tg.send_text(chat_id, '\n'.join(lines), KEYBOARD)
        except Exception as e:
            log('device error: %s' % e)
            tg.send_text(chat_id, '❌ دستگاه در دسترس نیست: %s' % e, KEYBOARD)
        return

    if text in ('ℹ️ وضعیت دستگاه', 'وضعیت', '/status'):
        try:
            from zk import ZK
            t0 = time.time()
            zk = ZK(cfg['device_ip'], port=cfg['device_port'], timeout=10,
                    password=cfg['comm_key'], ommit_ping=True)
            conn = zk.connect()
            fw = conn.get_firmware_version()
            sn = conn.get_serialnumber()
            dt = conn.get_time()
            conn.disconnect()
            ms = int((time.time() - t0) * 1000)
            tg.send_text(chat_id, '✅ دستگاه متصل است\n'
                         '🔢 سریال: <code>%s</code>\n🧩 فریمور: %s\n'
                         '🕐 ساعت دستگاه: %s %s\n⚡ زمان پاسخ: %s ms'
                         % (sn, fw, jdate(dt), fa(dt.strftime('%H:%M:%S')), fa(ms)), KEYBOARD)
        except Exception as e:
            tg.send_text(chat_id, '❌ اتصال برقرار نشد: %s' % e, KEYBOARD)
        return

    tg.send_text(chat_id, 'دستور ناشناخته. از دکمه‌های زیر استفاده کنید 👇', KEYBOARD)

def auto_sync(cfg, state):
    """هر چند دقیقه یک‌بار داده‌ها را از دستگاه کشیده و به سایت می‌فرستد."""
    if not cfg.get('site_url') or not cfg.get('site_key'):
        return
    interval = max(1, int(cfg.get('site_sync_minutes', 15))) * 60
    if time.time() - state.get('last_sync', 0) < interval:
        return
    state['last_sync'] = time.time()
    try:
        rows, users = fetch_from_device(cfg)
        push_to_site(cfg, rows, users)
    except Exception as e:
        log('auto sync error: %s' % e)

def main():
    cfg = load_config()
    tg = Tg(cfg['bot_token'])
    state = {'last_sync': 0}
    log('ربات شروع شد — دستگاه %s:%s%s' % (
        cfg['device_ip'], cfg['device_port'],
        ' | sync سایت هر %s دقیقه' % cfg['site_sync_minutes'] if cfg.get('site_url') else ''))
    while True:
        try:
            auto_sync(cfg, state)
            for up in tg.get_updates():
                msg = up.get('message')
                if msg:
                    try:
                        handle_message(tg, cfg, msg)
                    except Exception as e:
                        log('handler error: %s' % e)
        except KeyboardInterrupt:
            log('توقف با دستور کاربر')
            break
        except Exception as e:
            log('loop error: %s' % e)
            time.sleep(10)

if __name__ == '__main__':
    main()
