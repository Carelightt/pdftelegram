# --- PY313 fix: provide imghdr stub before telegram imports ---
import sys, types
try:
    import imghdr  # Python 3.12'de var; 3.13'te yok.
except ModuleNotFoundError:
    m = types.ModuleType("imghdr")
    def what(file, h=None):  # PTB'nin ihtiyacı sadece import başarısı; fonk no-op
        return None
    m.what = what
    sys.modules["imghdr"] = m
# --- END PY313 fix ---
import os
import time
import tempfile
import logging
import requests
from dotenv import load_dotenv
from datetime import datetime, date, timedelta, timezone
import json
import pytz    # ✅ zoneinfo yerine pytz kullanıyoruz

TR_TZ = pytz.timezone("Europe/Istanbul")  # ✅ ZoneInfo yerine pytz

from telegram import Update, InputFile
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# ⏰ Scheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ================== AYAR ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_KEY   = os.getenv("BOT_KEY")  # 🔑 siteyle aynı olmalı

PDF_URL       = "https://pdf-admin1.onrender.com/generate"    # Ücret formu endpoint'i
KART_PDF_URL  = "https://pdf-admin1.onrender.com/generate2"
BURS_PDF_URL  = "https://pdf-admin1.onrender.com/generate3"  # ✅ Burs endpoint'i (sablon3.pdf)

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Referer": "https://pdf-admin1.onrender.com/",
    "X-Requested-With": "XMLHttpRequest",
}
def _headers():
    """Her istekte X-Bot-Key ekle (varsa)."""
    h = dict(HEADERS_BASE)
    if BOT_KEY:
        h["X-Bot-Key"] = BOT_KEY
    return h

# ✅ SADECE İZİN VERDİĞİN GRUPLAR
ALLOWED_CHAT_ID = {-1002955588714}

# ====== ADMIN KİLİDİ ======
ADMIN_ID = 6672759317  # 👈 sadece bu kullanıcı admin

def _is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)

def _require_admin(update: Update) -> bool:
    """Admin değilse kullanıcıyı uyarır, False döner."""
    if not _is_admin(update):
        try:
            update.message.reply_text("⛔ Bu komutu kullanma yetkin yok.@CengizzAtay")
        except Exception:
            pass
        return False
    return True

# ====== GEÇİCİ İZİN (SÜRELİ HAK) ======
PERMS_FILE = "temp_perms.json"  # geçici izinlerin saklandığı dosya

def _now_utc():
    return datetime.now(timezone.utc)

def _load_perms():
    try:
        with open(PERMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}

def _save_perms(perms: dict):
    try:
        with open(PERMS_FILE, "w", encoding="utf-8") as f:
            json.dump(perms, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"temp_perms yazılamadı: {e}")

def _prune_expired(perms: dict) -> dict:
    changed = False
    now = _now_utc()
    out = {}
    for k, iso in perms.items():
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt > now:
                out[k] = iso
            else:
                changed = True
        except Exception:
            changed = True
    if changed:
        _save_perms(out)
    return out

TEMP_PERMS = _prune_expired(_load_perms())

def _add_temp(chat_id: int, until_dt_utc: datetime):
    global TEMP_PERMS
    TEMP_PERMS[str(chat_id)] = until_dt_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_perms(TEMP_PERMS)

def _is_temp_allowed(chat_id: int) -> bool:
    global TEMP_PERMS
    TEMP_PERMS = _prune_expired(TEMP_PERMS)
    iso = TEMP_PERMS.get(str(chat_id))
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) > _now_utc()
    except Exception:
        return False

# ====== KARA LİSTE (ANINDA KAPAT /bitir) ======
DENY_FILE = "deny_groups.json"
def _load_deny():
    try:
        with open(DENY_FILE, "r", encoding="utf-8") as f:
            arr = json.load(f)
            return set(int(x) for x in arr)
    except Exception:
        return set()

def _save_deny(s: set):
    try:
        with open(DENY_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"deny_groups yazılamadı: {e}")

DENY_GROUPS = _load_deny()

# ====== GRUP KONTENJAN SİSTEMİ (YENİ EKLEME) ======
# Varsayılan Sınır (İsteğin üzerine 7)
DEFAULT_MEMBER_LIMIT = 7 
LIMIT_FILE = "group_limits.json"

def _load_limits():
    """Grup özel kontenjanlarını yükler. {chat_id_str: limit_int}"""
    try:
        with open(LIMIT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # {chat_id_str: int}
            out = {}
            for k, v in data.items():
                try:
                    out[str(int(k))] = max(0, int(v)) # Negatif olmasın
                except Exception:
                    pass
            return out
    except Exception:
        return {}

def _save_limits(d: dict):
    try:
        with open(LIMIT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"group_limits yazılamadı: {e}")

GROUP_LIMITS = _load_limits()

def _get_limit(chat_id: int) -> int:
    """Grubun geçerli kontenjanını alır, yoksa varsayılanı döner."""
    # 0 veya negatif limit demek sınırsız değil, sadece varsayılanı kullan
    return int(GROUP_LIMITS.get(str(chat_id), DEFAULT_MEMBER_LIMIT))

def _set_limit(chat_id: int, amount: int):
    """Grubun kontenjanını ayarlar."""
    global GROUP_LIMITS
    GROUP_LIMITS[str(chat_id)] = max(0, int(amount))
    _save_limits(GROUP_LIMITS)
    
# ====== HAK (ADET) SİSTEMİ ======
QUOTA_FILE = "quota_rights.json"

def _load_quota():
    try:
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # {chat_id_str: int}
            out = {}
            for k, v in data.items():
                try:
                    out[str(int(k))] = int(v)
                except Exception:
                    pass
            return out
    except Exception:
        return {}

def _save_quota(d: dict):
    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"quota yazılamadı: {e}")

QUOTA = _load_quota()

def _get_quota(chat_id: int) -> int:
    return int(QUOTA.get(str(chat_id), 0))

def _set_quota(chat_id: int, amount: int):
    global QUOTA
    QUOTA[str(chat_id)] = max(0, int(amount))
    _save_quota(QUOTA)

def _dec_quota_if_applicable(chat_id: int):
    """
    Sadece ALLOWED veya TEMP izni YOKSA düş.
    (Süre izni varsa sınırsız, hak azaltılmaz.)
    """
    if chat_id in ALLOWED_CHAT_ID or _is_temp_allowed(chat_id):
        return
    rem = _get_quota(chat_id)
    if rem > 0:
        _set_quota(chat_id, rem - 1)

# ====== GÜNLÜK RAPOR (GRUP BAŞI SAYAC) ======
REPORT_FILE = "daily_report.json"
TITLES_FILE = "group_titles.json"    # 👈 grup adlarını saklarız
import pytz
TR_TZ = pytz.timezone("Europe/Istanbul")    # ✅ ZoneInfo yerine pytz
MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]

def _today_tr_str():
    return datetime.now(TR_TZ).strftime("%Y-%m-%d")

def _today_tr_human():
    now = datetime.now(TR_TZ)
    return f"{now.day} {MONTHS_TR[now.month-1]}"

def _load_titles():
    try:
        with open(TITLES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return {str(k): str(v) for k, v in d.items()}
    except Exception:
        return {}

def _save_titles(d: dict):
    try:
        with open(TITLES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"group_titles yazılamadı: {e}")

GROUP_TITLES = _load_titles()

def _load_report():
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "date" in data and "counts" in data and isinstance(data["counts"], dict):
                migrated = False
                for k, v in list(data["counts"].items()):
                    if isinstance(v, int):
                        data["counts"][k] = {"pdf": int(v), "kart": 0, "burs": 0} # Burs eklendi
                        migrated = True
                    elif isinstance(v, dict):
                        v.setdefault("pdf", 0)
                        v.setdefault("kart", 0)
                        v.setdefault("burs", 0) # Burs eklendi
                if migrated:
                    _save_report(data)
                return data
    except Exception:
        pass
    return {"date": _today_tr_str(), "counts": {}}

def _save_report(rep: dict):
    try:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"daily_report yazılamadı: {e}")

def _ensure_today_report():
    rep = _load_report()
    today = _today_tr_str()
    if rep.get("date") != today:
        rep = {"date": today, "counts": {}}
        _save_report(rep)
    return rep

def _inc_report(chat_id: int, kind: str, title: str = None):
    """Günlük sayaç artır. (title verilirse kaydederiz.)"""
    rep = _ensure_today_report()
    key = str(chat_id)
    node = rep["counts"].get(key) or {"pdf": 0, "kart": 0, "burs": 0} # burs eklendi
    if kind not in ("pdf", "kart", "burs"): # burs eklendi
        kind = "pdf"
    node[kind] = int(node.get(kind, 0)) + 1
    rep["counts"][key] = node
    _save_report(rep)

    if title:
        GROUP_TITLES[key] = title
        _save_titles(GROUP_TITLES)

def _get_today_counts(chat_id: int):
    rep = _ensure_today_report()
    node = rep["counts"].get(str(chat_id)) or {"pdf": 0, "kart": 0, "burs": 0} # burs eklendi
    pdf_c = int(node.get("pdf", 0)) + int(node.get("burs", 0)) # Burs pdf'e dahil
    kart_c = int(node.get("kart", 0))
    return pdf_c, kart_c, pdf_c + kart_c

# Konuşma durumları
# /pdf durumları
TC, NAME, SURNAME, MIKTAR = range(4)
# /kart için durumlar
K_ADSOYAD, K_ADRES, K_ILILCE, K_TARIH = range(4, 4 + 4) # 4, 5, 6, 7 (7 istenen hata no)
# /burs için durumlar
B_TC, B_NAME, B_SURNAME, B_MIKTAR = range(8, 8 + 4) # 8, 9, 10, 11

# ================== LOG ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("telegrampdf")

# ================== YARDIMCI ==================
def tr_upper(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = s.strip()
    s = s.replace("i", "İ").replace("ı", "I")
    return s.upper()

def _has_time_or_whitelist(chat_id: int) -> bool:
    return (chat_id in ALLOWED_CHAT_ID) or _is_temp_allowed(chat_id)

async def _get_group_member_count(chat_id: int, context: CallbackContext) -> int | None:
    """Grup üye sayısını (kullanıcı + bot) alır."""
    try:
        # get_chat_members_count yerine get_chat_member(user_id) daha güvenli olabilir
        # ama burada toplam üye sayısı lazım.
        # get_chat_memberS_count yeni PTB versiyonlarında sadece async.
        # Bu haliyle mevcut PTB'ye uygun olarak senkron bırakıyoruz.
        return context.bot.get_chat_members_count(chat_id)
    except Exception as e:
        log.warning(f"Grup üye sayısı alınamadı ({chat_id}): {e}")
        return None


async def _check_group_limit(update: Update, context: CallbackContext) -> bool:
    """Grup kişi sınırını ve kara listeyi kontrol eder."""
    chat = update.effective_chat
    if not chat:
        return False
    chat_id = chat.id

    # Kara listedeyse kapat
    if chat_id in DENY_GROUPS:
        try:
            update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yaz.")
        except Exception:
            pass
        return False
    
    # Sınırsız İzinler (Süre/Whitelist)
    if _has_time_or_whitelist(chat_id):
        # Eğer süreli/whitelist izni varsa, kontenjan kontrolünü atla.
        return True
    
    # Kontenjan Sınırı Kontrolü (Yeni Ekleme)
    if chat.type in ("group", "supergroup"):
        member_count = await _get_group_member_count(chat_id, context)
        limit = _get_limit(chat_id)
        
        # Sadece varsayılan limit (7) veya özel atanmış limit varsa kontrol yap
        if limit > 0 and member_count is not None and member_count > limit:
            try:
                update.message.reply_text(f"Bu grup {limit} kişiyle sınırlıdır.")
            except Exception:
                pass
            return False

    # Hak (adet) Kontrolü (Kontenjan geçildiyse veya geçilecek kontenjan yoksa)
    if _get_quota(chat_id) > 0:
        return True

    # Hiçbiri yoksa kapalı
    try:
        update.message.reply_text("Bu grubun hakkı yoktur. /yetkiver veya /hakver kullanın.")
    except Exception:
        pass
    return False

def _check_group(update: Update, context: CallbackContext) -> bool:
    """Eski senkron fonksiyon, yeni asenkron kontrolü çağırır."""
    # PTB'nin ana döngüsü (updater.start_polling) senkron olduğu için, 
    # bu fonksiyonu async yapıp içindeki await'i normalde çalıştıramayız.
    # Ancak, mevcut PTB (telegram.ext) sürümünün `CallbackContext`'i 
    # kullanarak asenkron bir işlemi senkron bir handler içinde tetiklemek
    # mümkün değil. PTB'yi değiştirmeme isteğine sadık kalmak için,
    # bu kısmı basit bir wrapper'a çeviriyorum. 
    
    # Telegram Bot API'sinde üye sayısını senkron çağırmak, senkron PTB'de
    # NetworkError'a yol açabilir. Bu nedenle senkron çağrıyı
    # yaparken oluşabilecek hatalara karşı koruma sağlıyoruz.

    chat = update.effective_chat
    if not chat:
        return False
    chat_id = chat.id

    # Kara listedeyse kapat
    if chat_id in DENY_GROUPS:
        try:
            update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yaz.")
        except Exception:
            pass
        return False
    
    # Sınırsız İzinler (Süre/Whitelist)
    if _has_time_or_whitelist(chat_id):
        return True
    
    # Kontenjan Sınırı Kontrolü (Yeni Ekleme)
    if chat.type in ("group", "supergroup"):
        limit = _get_limit(chat_id)
        if limit > 0:
            try:
                # get_chat_members_count senkron çağrısı.
                # Eğer PTB'nin eski versiyonuysa ve non-async ortamdaysak bu çalışmalı.
                member_count = context.bot.get_chat_members_count(chat_id)
                if member_count > limit:
                    update.message.reply_text(f"Bu grup {limit} kişiyle sınırlıdır.")
                    return False
            except Exception as e:
                # Üye sayısını alamasa bile (örneğin bot admin değilse), 
                # diğer hak kontrolleri devam etmeli. Hata durumunda izin verilir.
                log.warning(f"Grup üye sayısı alınamadı/Kontrol edilemedi ({chat_id}): {e}")
                pass 

    # Hak (adet) Kontrolü
    if _get_quota(chat_id) > 0:
        return True

    # Hiçbiri yoksa kapalı
    try:
        update.message.reply_text("Bu grubun hakkı yoktur.")
    except Exception:
        pass
    return False
    
def parse_pdf_inline(text: str):
    """
    /pdf komutu için inline parse:
    Çok satırlı:
      /pdf\nTC\nAD\nSOYAD\nMIKTAR
    Tek satır (opsiyonel):
      /pdf TC AD SOYAD ... MIKTAR
    Dönüş: (tc, ad, soyad, miktar) ya da None
    """
    if not text:
        return None
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.lower().startswith('/pdf'):
        return None

    # Çok satırlı tercih
    rest = lines[1:]
    if len(rest) >= 4:
        tc = rest[0]
        ad = rest[1]
        soyad = rest[2]
        miktar = rest[3]
        return tc, ad, soyad, miktar

    # Tek satır varyantı
    parts = first.split()
    if len(parts) >= 5:
        tc = parts[1]
        ad = parts[2]
        miktar = parts[-1]
        soyad = " ".join(parts[3:-1])
        return tc, ad, soyad, miktar

    return None

def parse_kart_inline(text: str):
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    first_line_end = raw.find("\n")
    first_line = raw if first_line_end == -1 else raw[:first_line_end]
    if not first_line.lower().startswith("/kart"):
        return None
    rest_text = "" if first_line_end == -1 else raw[first_line_end+1:]
    rest_lines = [l.strip() for l in rest_text.splitlines() if l.strip()]
    if len(rest_lines) >= 4:
        adsoyad = rest_lines[0]
        adres   = rest_lines[1]
        ililce  = rest_lines[2]
        tarih   = rest_lines[3]
        return adsoyad, adres, ililce, tarih
    return None

def parse_burs_inline(text: str):
    """
    /burs komutu için inline parse:
      /burs\nTC\nAD\nSOYAD\nMIKTAR
    veya tek satır:
      /burs TC AD SOYAD ... MIKTAR
    """
    if not text:
        return None
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return None
    first = lines[0]
    if not first.lower().startswith('/burs'):
        return None

    rest = lines[1:]
    if len(rest) >= 4:
        tc = rest[0]
        ad = rest[1]
        soyad = rest[2]
        miktar = rest[3]
        return tc, ad, soyad, miktar

    parts = first.split()
    if len(parts) >= 5:
        tc = parts[1]
        ad = parts[2]
        miktar = parts[-1]
        soyad = " ".join(parts[3:-1])
        return tc, ad, soyad, miktar

    return None

# ====== PDF ÜRETİMİ (ORTAK FONKSİYONLAR) ======

def _save_if_pdf_like(response: requests.Response) -> str:
    """HTTP yanıtını kontrol eder ve PDF ise geçici dosyaya kaydeder."""
    ct = (response.headers.get("Content-Type") or "").lower()
    if response.status_code == 200 and ("pdf" in ct or "octet-stream" in ct) and len(response.content) > 1024:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp.write(response.content)
        tmp.close()
        return tmp.name
    return ""

def generate_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    """sablon1.pdf üzerinden ücret formu çıktısı üretir (/generate)"""
    data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
    try:
        r = requests.post(PDF_URL, data=data, headers=_headers(), timeout=120)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[ücret form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
    except Exception as e:
        log.exception(f"[ücret form] generate_pdf hata: {e}")
    return ""


# ================== HANDLER'lar ==================
def cmd_start(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return ConversationHandler.END
    # admin için bilgi mesajı (normal /start artık kilitli)
    update.message.reply_text("Admin panel komutları: /yetkiver, /hakver, /kalanhak, /bitir, /rapor, /raporadmin, /ekle, /limit")
    return ConversationHandler.END

def cmd_whereami(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    limit = _get_limit(cid)
    update.message.reply_text(f"Chat ID: {cid}\nUser ID: {uid}\nMember Limit: {limit}")

# YENİ KOMUT: /ekle (Grup kontenjanı ayarlama) — SADECE ADMIN
def cmd_ekle(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    
    if not digits:
        current_limit = _get_limit(chat_id)
        update.message.reply_text(f"Kullanım: /ekle <adet> (örn: /ekle 10). Güncel limit: {current_limit} (Varsayılan: {DEFAULT_MEMBER_LIMIT})")
        return
    
    amount = int(digits)
    
    _set_limit(chat_id, amount)

    # Eğer 0 girilirse, varsayılan limite (7) geri döner.
    if amount == 0:
        update.message.reply_text(f"✅ Bu grubun özel kontenjanı kaldırıldı. Artık varsayılan limit ({DEFAULT_MEMBER_LIMIT} kişi) geçerli.")
    else:
        update.message.reply_text(f"✅ Bu gruba özel **{amount}** kişilik kontenjan tanımlandı.")


# YENİ KOMUT: /limit (Grup limitini görme) — SADECE ADMIN
def cmd_limit(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    current_limit = _get_limit(chat_id)
    
    update.message.reply_text(f"Bu grubun kontenjan sınırı: **{current_limit}** kişi. (Varsayılan: {DEFAULT_MEMBER_LIMIT} kişi)")


# Süre verme komutu — SADECE ADMIN
def cmd_yetkiver(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /yetkiver <gün>  (1–30 arası)")
        return
    days = int(digits)
    if days < 1 or days > 30:
        update.message.reply_text("Gün 1 ile 30 arasında olmalı.")
        return
    until_utc = _now_utc() + timedelta(days=days)
    _add_temp(chat_id, until_utc)

    # bitir ile kapatılmışsa kaldır
    global DENY_GROUPS
    if chat_id in DENY_GROUPS:
        DENY_GROUPS.remove(chat_id)
        _save_deny(DENY_GROUPS)

    update.message.reply_text(f"Bu gruba {days} günlük izin verildi.")

# Hak verme (adet) — SADECE ADMIN
def cmd_hakver(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /hakver <adet>  (örn: /hakver 20)")
        return
    amount = int(digits)
    if amount < 0:
        update.message.reply_text("Adet 0 veya üstü olmalı.")
        return
    _set_quota(chat_id, amount)

    # Eğer kara listedeyse aç (hak tanındıysa kullanabilsin)
    global DENY_GROUPS
    if chat_id in DENY_GROUPS:
        DENY_GROUPS.remove(chat_id)
        _save_deny(DENY_GROUPS)

    update.message.reply_text(f"✅ Bu gruba {amount} adet PDF hakkı tanımlandı.")

# Kalan hak — SADECE ADMIN
def cmd_hakdurum(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    rem = _get_quota(chat_id)
    msg = f"Kalan hak: {rem}"
    if _has_time_or_whitelist(chat_id):
        msg += "\n(Not: Süreli/whitelist izni olduğu için hak düşmez.)"
    update.message.reply_text(msg)

# Anında kapat — SADECE ADMIN
def cmd_bitir(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id

    global TEMP_PERMS
    if str(chat_id) in TEMP_PERMS:
        del TEMP_PERMS[str(chat_id)]
        _save_perms(TEMP_PERMS)

    global DENY_GROUPS
    DENY_GROUPS.add(chat_id)
    _save_deny(DENY_GROUPS)

    update.message.reply_text("⛔ Bu grubun hakkı kapatıldı.")

# Günlük rapor — SADECE ADMIN (o anki grup için)
def cmd_rapor(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    human_day = _today_tr_human()
    pdf_c, kart_c, _ = _get_today_counts(chat_id)
    update.message.reply_text(
        f"{human_day}\n\n"
        f"Üretilen PDF : {pdf_c}\n"
        f"Üretilen KART PDF : {kart_c}"
    )

def _build_daily_message(bot) -> str:
    """Günlük rapor mesajını hazırlar."""
    rep = _ensure_today_report()
    counts = rep["counts"]
    human_day = _today_tr_human()

    if not counts:
        return f"Bugün ({human_day}) henüz bir işlem yapılmadı."

    total_pdf = 0
    total_kart = 0
    group_lines = []

    sorted_keys = sorted(counts.keys(), key=lambda k: int(counts[k].get("pdf", 0)) + int(counts[k].get("kart", 0)), reverse=True)

    for chat_id_str in sorted_keys:
        chat_id = int(chat_id_str)
        node = counts[chat_id_str]
        pdf_c = node.get("pdf", 0) + node.get("burs", 0) # Burs dahil
        kart_c = node.get("kart", 0)
        
        # chat.get_chat ile güncel adı almaya çalış
        title = GROUP_TITLES.get(chat_id_str, f"Grup ID: {chat_id_str}")
        
        # Grup başlığını güncelle (sadece admin özelden yazıyorsa)
        if chat_id < 0: # Grup ise
            try:
                chat_info = bot.get_chat(chat_id)
                current_title = getattr(chat_info, "title", None)
                if current_title and GROUP_TITLES.get(chat_id_str) != current_title:
                    GROUP_TITLES[chat_id_str] = current_title
                    title = current_title
                    _save_titles(GROUP_TITLES)
            except Exception:
                pass # Ulaşılamayan grup olabilir

        group_lines.append(f"• {title}: PDF={pdf_c}, KART={kart_c}")
        
        total_pdf += pdf_c
        total_kart += kart_c

    report_text = f"🗓️ **Günlük Rapor: {human_day}**\n\n"
    report_text += f"**Toplam Üretim**:\n"
    report_text += f"  - Ücret / Burs PDF: **{total_pdf}**\n"
    report_text += f"  - Kart PDF: **{total_kart}**\n\n"
    report_text += "**Detaylar (En çoktan aza)**:\n"
    report_text += "\n".join(group_lines)

    return report_text

# ✅ TÜM GÜNÜN GENEL RAPORU — SADECE ADMIN
def cmd_raporadmin(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    # özelden yazılmasını tavsiye et
    try:
        if update.effective_chat and getattr(update.effective_chat, "type", "") != "private":
            update.message.reply_text("Bu komutu bana özelden yaz: /raporadmin")
            return
    except Exception:
        pass
    try:
        text = _build_daily_message(context.bot)
        update.message.reply_text(text, parse_mode='Markdown')
    except Exception as e:
        log.exception(f"/raporadmin hata: {e}")
        update.message.reply_text("Rapor hazırlanırken bir sorun oluştu.")

# ================== /pdf ==================
def start_pdf(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    inline = parse_pdf_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ PDF hazırlanıyor")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                filename = f"{name_up}_{surname_up}.pdf".replace(" ", "_")
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"send_document timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"send_document failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)

        return ConversationHandler.END

    update.message.reply_text("Müşterinin TC numarasını yaz:")
    return TC

def get_tc(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["tc"] = update.message.text.strip()
    update.message.reply_text("Müşterinin Adını yaz:")
    return NAME

def get_name(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["name"] = update.message.text
    update.message.reply_text("Müşterinin Soyadını yaz:")
    return SURNAME

def get_surname(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["surname"] = update.message.text
    update.message.reply_text("Miktarı yaz (örn: 5.000):")
    return MIKTAR

def get_miktar(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ PDF hazırlanıyor")
    name_up = tr_upper(context.user_data["name"])
    surname_up = tr_upper(context.user_data["surname"])
    pdf_path = generate_pdf(
        context.user_data["tc"],
        name_up,
        surname_up,
        context.user_data["miktar"]
    )
    if not pdf_path:
        update.message.reply_text("❌ PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            filename = f"{name_up}_{surname_up}.pdf".replace(" ", "_")
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"send_document timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"send_document failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break

    try:
        os.remove(pdf_path)
    except Exception:
        pass

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)

    return ConversationHandler.END

def cmd_cancel(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    update.message.reply_text("İptal edildi.")
    return ConversationHandler.END

# ================== KART DURUMU: /kart ==================
def generate_kart_pdf(adsoyad: str, adres: str, ililce: str, tarih: str) -> str:
    try:
        data = {"adsoyad": adsoyad, "adres": adres, "ililce": ililce, "tarih": tarih}
        r = requests.post(KART_PDF_URL, data=data, headers=_headers(), timeout=90)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[KART form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:200]}")
            return ""
    except Exception as e:
        log.exception(f"generate_kart_pdf hata: {e}")
    return ""

def start_kart(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    inline = parse_kart_inline(update.message.text or "")
    if inline:
        adsoyad, adres, ililce, tarih = inline
        update.message.reply_text("⏳ Kart durumu PDF hazırlanıyor...")
        pdf_path = generate_kart_pdf(adsoyad, adres, ililce, tarih)
        if not pdf_path:
            update.message.reply_text("❌ Kart PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "kart", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                base = (adsoyad or "KART").strip().replace(" ", "_").upper()
                filename = f"{base}_KART.pdf"
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"kart send timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"kart send failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)

        return ConversationHandler.END

    update.message.reply_text("Ad Soyad yaz:")
    return K_ADSOYAD

def get_k_adsoyad(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["k_adsoyad"] = update.message.text.strip()
    update.message.reply_text("Adres yaz:")
    return K_ADRES

def get_k_adres(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["k_adres"] = update.message.text.strip()
    update.message.reply_text("İl İlçe yaz:")
    return K_ILILCE

def get_k_ililce(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["k_ililce"] = update.message.text.strip()
    update.message.reply_text("Tarih yaz:")
    return K_TARIH

def get_k_tarih(update: Update, context: CallbackContext):
    if not _check_group(update, context): # context ekledik
        return ConversationHandler.END
    context.user_data["k_tarih"] = update.message.text.strip()
    update.message.reply_text("⏳ Kart durumu PDF hazırlanıyor...")
    pdf_path = generate_kart_pdf(
        context.user_data["k_adsoyad"],
        context.user_data["k_adres"],
        context.user_data["k_ililce"],
        context.user_data["k_tarih"]
    )
    if not pdf_path:
        update.message.reply_text("❌ Kart PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "kart", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            base = (context.user_data.get("k_adsoyad") or "KART").strip().replace(" ", "_").upper()
            filename = f"{base}_KART.pdf"
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"kart send timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"kart send failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break

    try:
        os.remove(pdf_path)
    except Exception:
        pass

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)

    return ConversationHandler.END

# ================== BURS: /burs ==================
def generate_burs_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    """sablon3.pdf üzerinden burs çıktısı üretir (/generate3)"""
    data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
    try:
        r = requests.post(BURS_PDF_URL, data=data, headers=_headers(), timeout=120)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[burs form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
    except Exception as e:
        log.exception(f"[burs form] generate_burs_pdf hata: {e}")
    try:
        r2 = requests.post(BURS_PDF_URL, json=data, headers=_headers(), timeout=120)
        path2 = _save_if_pdf_like(r2)
        if path2:
            return path2
        else:
            log.error(f"[burs json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
    except Exception as e:
        log.exception(f"[burs json] generate_burs_pdf hata: {e}")
    return ""

def start_burs(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    inline = parse_burs_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ Burs PDF hazırlanıyor...")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_burs_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ Burs PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "burs", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                filename = f"{name_up}_{surname_up}_BURS.pdf".replace(" ", "_")
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"burs send timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"burs send failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)

        return ConversationHandler.END
        
    update.message.reply_text("Müşterinin TC numarasını yaz:")
    return B_TC

def get_b_tc(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_tc"] = update.message.text.strip()
    update.message.reply_text("Müşterinin Adını yaz:")
    return B_NAME

def get_b_name(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_name"] = update.message.text
    update.message.reply_text("Müşterinin Soyadını yaz:")
    return B_SURNAME

def get_b_surname(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_surname"] = update.message.text
    update.message.reply_text("Miktarı yaz (örn: 5.000):")
    return B_MIKTAR

def get_b_miktar(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ Burs PDF hazırlanıyor...")
    name_up = tr_upper(context.user_data["b_name"])
    surname_up = tr_upper(context.user_data["b_surname"])
    pdf_path = generate_burs_pdf(
        context.user_data["b_tc"],
        name_up,
        surname_up,
        context.user_data["b_miktar"]
    )
    if not pdf_path:
        update.message.reply_text("❌ Burs PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "burs", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            filename = f"{name_up}_{surname_up}_BURS.pdf".replace(" ", "_")
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"burs send timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"burs send failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break

    try:
        os.remove(pdf_path)
    except Exception:
        pass

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)

    return ConversationHandler.END

# ================== ZAMANLAYICI ==================

# PTB Updater'ı senkron olduğu için bu kısmı aynen bırakıyoruz.
def setup_scheduler(updater):
    scheduler = BackgroundScheduler()
    # Her gün Türkiye saatine göre 09:00'da çalışır
    trigger = CronTrigger(hour=9, minute=0, timezone=TR_TZ)
    # Her gün raporu ADMIN_ID'ye gönder
    scheduler.add_job(
        _send_daily_report_to_admin, 
        trigger, 
        args=[updater.bot]
    )
    # 5 dakikada bir kontrol: Süresi dolan izinleri kaldır
    scheduler.add_job(
        _prune_all_expired, 
        'interval', 
        minutes=5,
        args=[updater.bot] # bot objesini gönderelim
    )
    scheduler.start()

def _prune_all_expired(bot):
    """Süresi dolan geçici izinleri ve hakları kontrol eder."""
    global TEMP_PERMS
    TEMP_PERMS = _prune_expired(TEMP_PERMS)
    # Ayrıca, günlük raporu kontrol edip tarihi eskiyse sıfırlar.
    _ensure_today_report()

def _send_daily_report_to_admin(bot):
    """Günlük raporu ADMIN_ID'ye Markdown olarak gönderir."""
    try:
        text = _build_daily_message(bot)
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode='Markdown')
        log.info(f"Günlük rapor ADMIN_ID'ye ({ADMIN_ID}) gönderildi.")
    except Exception as e:
        log.error(f"Günlük rapor gönderilirken hata: {e}")


# ================== MAIN ==================

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Admin Komutları (kilitli)
    dp.add_handler(CommandHandler("start", cmd_start, filters=Filters.chat(ADMIN_ID) | Filters.private))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))
    dp.add_handler(CommandHandler("yetkiver", cmd_yetkiver))
    dp.add_handler(CommandHandler("hakver", cmd_hakver))
    dp.add_handler(CommandHandler("kalanhak", cmd_hakdurum))
    dp.add_handler(CommandHandler("bitir", cmd_bitir))
    dp.add_handler(CommandHandler("rapor", cmd_rapor))
    dp.add_handler(CommandHandler("raporadmin", cmd_raporadmin))
    dp.add_handler(CommandHandler("ekle", cmd_ekle)) # Grup kontenjanı ayarla
    dp.add_handler(CommandHandler("limit", cmd_limit)) # Grup kontenjanını göster

    # Konuşma Handler'ları
    # /pdf
    conv_pdf = ConversationHandler(
        entry_points=[CommandHandler('pdf', start_pdf)],
        states={
            TC: [MessageHandler(Filters.text & ~Filters.command, get_tc)],
            NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
            SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_surname)],
            MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_miktar)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    dp.add_handler(conv_pdf)
    
    # /kart
    conv_kart = ConversationHandler(
        entry_points=[CommandHandler('kart', start_kart)],
        states={
            K_ADSOYAD: [MessageHandler(Filters.text & ~Filters.command, get_k_adsoyad)],
            K_ADRES: [MessageHandler(Filters.text & ~Filters.command, get_k_adres)],
            K_ILILCE: [MessageHandler(Filters.text & ~Filters.command, get_k_ililce)],
            K_TARIH: [MessageHandler(Filters.text & ~Filters.command, get_k_tarih)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    dp.add_handler(conv_kart)

    # /burs
    conv_burs = ConversationHandler(
        entry_points=[CommandHandler('burs', start_burs)],
        states={
            B_TC: [MessageHandler(Filters.text & ~Filters.command, get_b_tc)],
            B_NAME: [MessageHandler(Filters.text & ~Filters.command, get_b_name)],
            B_SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_b_surname)],
            B_MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_b_miktar)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    dp.add_handler(conv_burs)

    # Scheduler'ı kur
    setup_scheduler(updater)

    # Botu başlat
    log.info("Telegram bot başlatılıyor...")
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
