# --- PY313 fix: provide imghdr stub before telegram imports ---
import sys, types
try:
  import imghdr # Python 3.12'de var; 3.13'te yok.
except ModuleNotFoundError:
  m = types.ModuleType("imghdr")
  def what(file, h=None): # PTB'nin ihtiyacı sadece import başarısı; fonk no-op
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
import pytz # ✅ zoneinfo yerine pytz kullanıyoruz

TR_TZ = pytz.timezone("Europe/Istanbul") # ✅ ZoneInfo yerine pytz

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
BOT_KEY = os.getenv("BOT_KEY") # 🔑 siteyle aynı olmalı

PDF_URL   = "https://pdf-admin1.onrender.com/generate" # Ücret formu endpoint'i
KART_PDF_URL = "https://pdf-admin1.onrender.com/generate2"
BURS_PDF_URL = "https://pdf-admin1.onrender.com/generate3" # ✅ Burs endpoint'i (sablon3.pdf)
DIP_PDF_URL  = "https://pdf-admin1.onrender.com/generate4" # ✅ YENİ: Dip endpoint'i (d.pdf)

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
ADMIN_ID = 6672759317 # 👈 sadece bu kullanıcı admin

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
PERMS_FILE = "temp_perms.json" # geçici izinlerin saklandığı dosya

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

# ====== KONTENJAN (ÜYE SAYISI) SİSTEMİ ======
LIMIT_FILE = "group_limits.json"   # 👈 grup limitlerini saklarız
DEFAULT_LIMIT = 5          # 👈 Varsayılan maksimum üye sayısı (SİZİN İSTEĞİNİZ: 7)

def _load_limits():
  """Grup ID'si başına özel limiti yükler."""
  try:
    with open(LIMIT_FILE, "r", encoding="utf-8") as f:
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

def _save_limits(d: dict):
  """Grup limitlerini kaydeder."""
  try:
    with open(LIMIT_FILE, "w", encoding="utf-8") as f:
      json.dump(d, f, ensure_ascii=False, indent=2)
  except Exception as e:
    log.warning(f"group_limits yazılamadı: {e}")

GROUP_LIMITS = _load_limits()

def _get_max_members(chat_id: int) -> int:
  """Bir grup için tanımlanmış özel limiti veya varsayılan limiti döner."""
  return int(GROUP_LIMITS.get(str(chat_id), DEFAULT_LIMIT))

def _set_max_members(chat_id: int, amount: int):
  """Bir gruba özel limit tanımlar."""
  global GROUP_LIMITS
  GROUP_LIMITS[str(chat_id)] = max(0, int(amount))
  _save_limits(GROUP_LIMITS)

# ====== GÜNLÜK RAPOR (GRUP BAŞI SAYAC) ======
REPORT_FILE = "daily_report.json"
TITLES_FILE = "group_titles.json" # 👈 grup adlarını saklarız
import pytz
TR_TZ = pytz.timezone("Europe/Istanbul") # ✅ ZoneInfo yerine pytz
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
            data["counts"][k] = {"pdf": int(v), "kart": 0}
            migrated = True
          elif isinstance(v, dict):
            v.setdefault("pdf", 0)
            v.setdefault("kart", 0)
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
  node = rep["counts"].get(key) or {"pdf": 0, "kart": 0}
  if kind not in ("pdf", "kart"):
    kind = "pdf"
  node[kind] = int(node.get(kind, 0)) + 1
  rep["counts"][key] = node
  _save_report(rep)

  if title:
    GROUP_TITLES[key] = title
    _save_titles(GROUP_TITLES)

def _get_today_counts(chat_id: int):
  rep = _ensure_today_report()
  node = rep["counts"].get(str(chat_id)) or {"pdf": 0, "kart": 0}
  pdf_c = int(node.get("pdf", 0))
  kart_c = int(node.get("kart", 0))
  return pdf_c, kart_c, pdf_c + kart_c

# Konuşma durumları
TC, NAME, SURNAME, MIKTAR = range(4)
# /kart için durumlar
K_ADSOYAD, K_ADRES, K_ILILCE, K_TARIH = range(4)
# /burs için durumlar
B_TC, B_NAME, B_SURNAME, B_MIKTAR = range(4)
# /dip için durumlar
D_TC, D_NAME, D_SURNAME, D_MIKTAR = range(4)

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

def _check_group(update: Update, context: CallbackContext) -> bool: # 👈 context eklendi
  chat = update.effective_chat
  if not chat:
    return False
  chat_id = chat.id

  # 1. Kara listedeyse kapat
  if chat_id in DENY_GROUPS:
    try:
      update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yaz.")
    except Exception:
      pass
    return False

  # 2. Üye Sayısı Kontrolü (YENİ KONTROL)
  try:
    if chat.type in ("group", "supergroup"):
      # Güncel üye sayısını alıyoruz
      member_count = context.bot.get_chat_member_count(chat_id) # 👈 API CALL
      max_limit = _get_max_members(chat_id)
     
      if member_count > max_limit:
        msg = f"⛔ Bu grup 5 kişiyle sınırlıdır. Şu an: {member_count} kişi var."
        try:
          update.message.reply_text(msg)
        except Exception:
          pass
        return False # Kontenjan aşımı
  except Exception as e:
    log.warning(f"Üye sayısı kontrol edilemedi: {e}")
    # Hata olursa, botun çalışmaya devam etmesi için True dönebiliriz.

  # 3. Süre/whitelist ise serbest
  if _has_time_or_whitelist(chat_id):
    return True

  # 4. Değilse hak (adet) kontrolü
  if _get_quota(chat_id) > 0:
    return True

  # 5. Hiçbiri yoksa kapalı
  try:
    update.message.reply_text("Bu grubun hakkı yoktur. /yetkiver veya /hakver kullanın.")
  except Exception:
    pass
  return False

# ================== DEĞİŞİKLİK 1 (parse_pdf_inline) ==================
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

  # === DEĞİŞİKLİK BURADA ===
  # /pdf'in başındaki görünmez karakterleri (\u200B vb.) veya HTML tag'lerini (<code>) temizle
  # ve mavi link (bot komutu) olup olmadığını umursama.
 
  clean_first = first.lstrip().lstrip('\u200B').strip()
 
  # <code>/pdf</code> gibi HTML formatını da temizle
  if clean_first.lower().startswith("<code>") and clean_first.lower().endswith("</code>"):
    clean_first = clean_first[6:-7].strip()
   
  # Sadece /pdf olarak gelirse (<code>/pdf</code> olmadan)
  # Bazen text'in içinde <code>/pdf</code> olabilir, bazen de entity olarak gelir
  # En iyisi metni normalize etmek
  clean_first = clean_first.replace("<code>", "").replace("</code>", "")

  # Temizlenmiş satır /pdf ile başlamıyorsa dikkate alma
  if not clean_first.lower().startswith('/pdf'):
    return None
  # === DEĞİŞİKLİK SONU ===

  # Çok satırlı tercih
  rest = lines[1:]
  if len(rest) >= 4:
    tc = rest[0]
    ad = rest[1]
    soyad = rest[2]
    miktar = rest[3]
    return tc, ad, soyad, miktar

  # Tek satır varyantı
  parts = clean_first.split() # <-- 'first' yerine 'clean_first' kullan
  if len(parts) >= 5:
    tc = parts[1]
    ad = parts[2]
    miktar = parts[-1]
    soyad = " ".join(parts[3:-1])
    return tc, ad, soyad, miktar

  return None
# ================== DEĞİŞİKLİK 1 BİTTİ ==================


# ================== DEĞİŞİKLİK 2 (parse_kart_inline) ==================
def parse_kart_inline(text: str):
  if not text:
    return None
  raw = text.strip()
  if not raw:
    return None
  first_line_end = raw.find("\n")
  first_line = raw if first_line_end == -1 else raw[:first_line_end]

  # === DEĞİŞİKLİK BURADA ===
  clean_first_line = first_line.lstrip().lstrip('\u200B').strip()
  if clean_first_line.lower().startswith("<code>") and clean_first_line.lower().endswith("</code>"):
    clean_first_line = clean_first_line[6:-7].strip()
  clean_first_line = clean_first_line.replace("<code>", "").replace("</code>", "")
 
  if not clean_first_line.lower().startswith("/kart"):
    return None
  # === DEĞİŞİKLİK SONU ===
 
  rest_text = "" if first_line_end == -1 else raw[first_line_end+1:]
  rest_lines = [l.strip() for l in rest_text.splitlines() if l.strip()]
  if len(rest_lines) >= 4:
    adsoyad = rest_lines[0]
    adres = rest_lines[1]
    ililce = rest_lines[2]
    tarih = rest_lines[3]
    return adsoyad, adres, ililce, tarih
  return None
# ================== DEĞİŞİKLİK 2 BİTTİ ==================


# ================== DEĞİŞİKLİK 3 (parse_burs_inline) ==================
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

  # === DEĞİŞİKLİK BURADA ===
  clean_first = first.lstrip().lstrip('\u200B').strip()
  if clean_first.lower().startswith("<code>") and clean_first.lower().endswith("</code>"):
    clean_first = clean_first[6:-7].strip()
  clean_first = clean_first.replace("<code>", "").replace("</code>", "")

  if not clean_first.lower().startswith('/burs'):
    return None
  # === DEĞİŞİKLİK SONU ===

  rest = lines[1:]
  if len(rest) >= 4:
    tc = rest[0]
    ad = rest[1]
    soyad = rest[2]
    miktar = rest[3]
    return tc, ad, soyad, miktar

  parts = clean_first.split() # <-- 'first' yerine 'clean_first' kullan
  if len(parts) >= 5:
    tc = parts[1]
    ad = parts[2]
    miktar = parts[-1]
    soyad = " ".join(parts[3:-1])
    return tc, ad, soyad, miktar

  return None
# ================== DEĞİŞİKLİK 3 BİTTİ ==================


# ================== YENİ (parse_dip_inline) ==================
def parse_dip_inline(text: str):
  """
  /dip komutu için inline parse:
   /dip\nTC\nAD\nSOYAD\nMIKTAR
  veya tek satır:
   /dip TC AD SOYAD ... MIKTAR
  """
  if not text:
    return None
  lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
  if not lines:
    return None
 
  first = lines[0]
  clean_first = first.lstrip().lstrip('\u200B').strip()
  if clean_first.lower().startswith("<code>") and clean_first.lower().endswith("</code>"):
    clean_first = clean_first[6:-7].strip()
  clean_first = clean_first.replace("<code>", "").replace("</code>", "")

  if not clean_first.lower().startswith('/dip'):
    return None

  rest = lines[1:]
  if len(rest) >= 4:
    tc = rest[0]
    ad = rest[1]
    soyad = rest[2]
    miktar = rest[3]
    return tc, ad, soyad, miktar

  parts = clean_first.split()
  if len(parts) >= 5:
    tc = parts[1]
    ad = parts[2]
    miktar = parts[-1]
    soyad = " ".join(parts[3:-1])
    return tc, ad, soyad, miktar

  return None
# ================== YENİ BİTTİ ==================


# ================== HANDLER'lar ==================
def cmd_start(update: Update, context: CallbackContext):
  if not _require_admin(update):
    return ConversationHandler.END
  # admin için bilgi mesajı (normal /start artık kilitli)
  update.message.reply_text("Admin panel komutları: /yetkiver, /hakver, /kalanhak, /bitir, /rapor")
  return ConversationHandler.END

def cmd_whereami(update: Update, context: CallbackContext):
  if not _require_admin(update):
    return
  cid = update.effective_chat.id if update.effective_chat else None
  uid = update.effective_user.id if update.effective_user else None
  update.message.reply_text(f"Chat ID: {cid}\nUser ID: {uid}")

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
    update.message.reply_text("Kullanım: /yetkiver <gün> (1–30 arası)")
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
    update.message.reply_text("Kullanım: /hakver <adet> (örn: /hakver 20)")
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
   _send_temp_pdf(update, pdf_path, name_up, surname_up, "_DIP")
  
  if sent_ok:
    _dec_quota_if_applicable(update.effective_chat.id)

  return ConversationHandler.END
# ================== /dip BİTİŞ ==================


# ================== GÜNLÜK DM RAPORU ==================
def _build_daily_message(bot: "telegram.Bot") -> str:
  rep = _ensure_today_report()
  counts = rep.get("counts", {})
  if not counts:
    return (
      "ÜRETİLEN TOPLAM PDF : 0\n"
      "ÜRETİLEN BURS ve PDF : 0\n"
      "ÜRETİLEN KART PDF : 0\n\n"
      "Bugün üretim yok."
    )

  total_pdf = 0
  total_kart = 0
  lines = []
  for chat_id_str, node in counts.items():
    pdf_c = int(node.get("pdf", 0))
    kart_c = int(node.get("kart", 0))
    total_pdf += pdf_c
    total_kart += kart_c

    title = GROUP_TITLES.get(chat_id_str)
    if not title:
      # son çare: chat başlığını çekmeye çalış (fail olursa ID yaz)
      try:
        ch = bot.get_chat(int(chat_id_str))
        title = getattr(ch, "title", None) or f"Grup {chat_id_str}"
      except Exception:
        title = f"Grup {chat_id_str}"

    lines.append(f"- {title} ({chat_id_str}) → PDF: {pdf_c} | KART: {kart_c}")

  msg = (
    f"ÜRETİLEN TOPLAM PDF : {total_pdf}\n"
    f"ÜRETİLEN BURS ve PDF : {total_pdf}\n"
    f"ÜRETİLEN KART PDF : {total_kart}\n\n"
    + "\n".join(lines)
  )
  return msg

def send_daily_dm(bot: "telegram.Bot"):
  try:
    text = _build_daily_message(bot)
    bot.send_message(chat_id=ADMIN_ID, text=text)
  except Exception as e:
    log.exception(f"Günlük DM raporu gönderilemedi: {e}")

# ================== PDF OLUŞTURMA (Genel) ==================
def _save_if_pdf_like(resp) -> str:
  try:
    ct = (resp.headers.get("Content-Type") or "").lower()
    cd = (resp.headers.get("Content-Disposition") or "").lower()
    content = resp.content or b""
    looks_pdf = (b"%PDF" in content[:10]) or ("application/pdf" in ct) or ("filename=" in cd)
    if resp.status_code == 200 and looks_pdf and content:
      tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
      tmp.write(content)
      tmp.close()
      return tmp.name
    return ""
  except Exception as e:
    log.exception(f"_save_if_pdf_like hata: {e}")
    return ""

# (Bu fonksiyonu _send_temp_pdf'e refactor ettim, 
# generate_pdf, generate_burs_pdf, generate_dip_pdf artık 
# _generate_pdf_base fonksiyonunu kullanıyor)
def _send_temp_pdf(update: Update, pdf_path: str, name_up: str, surname_up: str, suffix: str = "") -> bool:
  """
  Geçici PDF dosyasını göndermeyi dener, 3 deneme yapar.
  Başarı durumunda True döner, ardından dosyayı siler.
 """
  sent_ok = False
  for attempt in range(1, 4):
    try:
      filename = f"{name_up}_{surname_up}{suffix}.pdf".replace(" ", "_")
      with open(pdf_path, "rb") as f:
        update.message.reply_document(
          document=InputFile(f, filename=filename),
          timeout=180
        )
      sent_ok = True
      break
    except (NetworkError, TimedOut) as e:
      log.warning(f"send_document{suffix} timeout/network (attempt {attempt}): {e}")
      if attempt == 3:
        update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
      else:
        time.sleep(2 * attempt)
    except Exception as e:
      log.exception(f"send_document{suffix} failed: {e}")
      update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
      break
 
  try:
    os.remove(pdf_path)
  except Exception:
    pass
   
  return sent_ok

def _generate_pdf_base(url: str, tc: str, name: str, surname: str, miktar: str, log_ctx: str) -> str:
  """PDF, Burs ve Dip için ortak PDF oluşturma mantığı"""
  data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
  try:
    r = requests.post(url, data=data, headers=_headers(), timeout=120)
    path = _save_if_pdf_like(r)
    if path:
      return path
    else:
      log.error(f"[{log_ctx} form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
  except Exception as e:
    log.exception(f"[{log_ctx} form] _generate_pdf_base hata: {e}")
  try:
    r2 = requests.post(url, json=data, headers=_headers(), timeout=120)
    path2 = _save_if_pdf_like(r2)
    if path2:
      return path2
    else:
      log.error(f"[{log_ctx} json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
  except Exception as e:
    log.exception(f"[{log_ctx} json] _generate_pdf_base hata: {e}")
  return ""

def generate_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  return _generate_pdf_base(PDF_URL, tc, name, surname, miktar, "pdf")

def generate_burs_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  return _generate_pdf_base(BURS_PDF_URL, tc, name, surname, miktar, "burs")
 
def generate_dip_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  return _generate_pdf_base(DIP_PDF_URL, tc, name, surname, miktar, "dip")

# ================== ERROR HANDLER ==================
def on_error(update: object, context: CallbackContext):
  log.exception("Unhandled error", exc_info=context.error)

# ================== MAIN ==================
def main():
  if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN .env'de yok!")

  request_kwargs = {
    "con_pool_size": 8,
    "connect_timeout": 30,
    "read_timeout": 180
  }

  updater = Updater(BOT_TOKEN, use_context=True, request_kwargs=request_kwargs)

  try:
    updater.bot.delete_webhook(drop_pending_updates=True)
  except Exception as e:
    log.warning(f"delete_webhook uyarı: {e}")

  dp = updater.dispatcher
  dp.add_error_handler(on_error)

  conv = ConversationHandler(
    entry_points=[CommandHandler("pdf", start_pdf)],
    states={
      TC: [MessageHandler(Filters.text & ~Filters.command, get_tc)],
      NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
      SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_surname)],
      MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_miktar)],
    },
    fallbacks=[CommandHandler("cancel", cmd_cancel)],
    conversation_timeout=180,
    allow_reentry=True
  )

  conv_kart = ConversationHandler(
    entry_points=[CommandHandler("kart", start_kart)],
    states={
      K_ADSOYAD: [MessageHandler(Filters.text & ~Filters.command, get_k_adsoyad)],
      K_ADRES: [MessageHandler(Filters.text & ~Filters.command, get_k_adres)],
      K_ILILCE: [MessageHandler(Filters.text & ~Filters.command, get_k_ililce)],
      K_TARIH: [MessageHandler(Filters.text & ~Filters.command, get_k_tarih)],
_send_temp_pdf(update, pdf_path, name_up, surname_up, "_DIP")
  
  if sent_ok:
    _dec_quota_if_applicable(update.effective_chat.id)

  return ConversationHandler.END
# ================== /dip BİTİŞ ==================


# ================== GÜNLÜK DM RAPORU ==================
def _build_daily_message(bot: "telegram.Bot") -> str:
  rep = _ensure_today_report()
  counts = rep.get("counts", {})
  if not counts:
    return (
      "ÜRETİLEN TOPLAM PDF : 0\n"
      "ÜRETİLEN BURS ve PDF : 0\n"
      "ÜRETİLEN KART PDF : 0\n\n"
      "Bugün üretim yok."
    )

  total_pdf = 0
  total_kart = 0
  lines = []
  for chat_id_str, node in counts.items():
    pdf_c = int(node.get("pdf", 0))
    kart_c = int(node.get("kart", 0))
    total_pdf += pdf_c
    total_kart += kart_c

    title = GROUP_TITLES.get(chat_id_str)
    if not title:
      # son çare: chat başlığını çekmeye çalış (fail olursa ID yaz)
      try:
        ch = bot.get_chat(int(chat_id_str))
        title = getattr(ch, "title", None) or f"Grup {chat_id_str}"
      except Exception:
        title = f"Grup {chat_id_str}"

    lines.append(f"- {title} ({chat_id_str}) → PDF: {pdf_c} | KART: {kart_c}")

  msg = (
    f"ÜRETİLEN TOPLAM PDF : {total_pdf}\n"
    f"ÜRETİLEN BURS ve PDF : {total_pdf}\n"
    f"ÜRETİLEN KART PDF : {total_kart}\n\n"
    + "\n".join(lines)
  )
  return msg

def send_daily_dm(bot: "telegram.Bot"):
  try:
    text = _build_daily_message(bot)
    bot.send_message(chat_id=ADMIN_ID, text=text)
  except Exception as e:
    log.exception(f"Günlük DM raporu gönderilemedi: {e}")

# ================== PDF OLUŞTURMA (Genel) ==================
def _save_if_pdf_like(resp) -> str:
  try:
    ct = (resp.headers.get("Content-Type") or "").lower()
    cd = (resp.headers.get("Content-Disposition") or "").lower()
    content = resp.content or b""
    looks_pdf = (b"%PDF" in content[:10]) or ("application/pdf" in ct) or ("filename=" in cd)
    if resp.status_code == 200 and looks_pdf and content:
      tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
      tmp.write(content)
      tmp.close()
      return tmp.name
    return ""
  except Exception as e:
    log.exception(f"_save_if_pdf_like hata: {e}")
    return ""

# (Bu fonksiyonu _send_temp_pdf'e refactor ettim, 
# generate_pdf, generate_burs_pdf, generate_dip_pdf artık 
# _generate_pdf_base fonksiyonunu kullanıyor)
def _send_temp_pdf(update: Update, pdf_path: str, name_up: str, surname_up: str, suffix: str = "") -> bool:
  """
  Geçici PDF dosyasını göndermeyi dener, 3 deneme yapar.
  Başarı durumunda True döner, ardından dosyayı siler.
 """
  sent_ok = False
  for attempt in range(1, 4):
    try:
      filename = f"{name_up}_{surname_up}{suffix}.pdf".replace(" ", "_")
      with open(pdf_path, "rb") as f:
        update.message.reply_document(
          document=InputFile(f, filename=filename),
          timeout=180
        )
      sent_ok = True
      break
    except (NetworkError, TimedOut) as e:
      log.warning(f"send_document{suffix} timeout/network (attempt {attempt}): {e}")
      if attempt == 3:
        update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
      else:
        time.sleep(2 * attempt)
    except Exception as e:
      log.exception(f"send_document{suffix} failed: {e}")
      update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
      break
 
  try:
    os.remove(pdf_path)
  except Exception:
    pass
   
  return sent_ok

def _generate_pdf_base(url: str, tc: str, name: str, surname: str, miktar: str, log_ctx: str) -> str:
  """PDF, Burs ve Dip için ortak PDF oluşturma mantığı"""
  data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
  try:
    r = requests.post(url, data=data, headers=_headers(), timeout=120)
    path = _save_if_pdf_like(r)
    if path:
      return path
    else:
      log.error(f"[{log_ctx} form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
  except Exception as e:
    log.exception(f"[{log_ctx} form] _generate_pdf_base hata: {e}")
  try:
    r2 = requests.post(url, json=data, headers=_headers(), timeout=120)
    path2 = _save_if_pdf_like(r2)
    if path2:
      return path2
    else:
      log.error(f"[{log_ctx} json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
  except Exception as e:
    log.exception(f"[{log_ctx} json] _generate_pdf_base hata: {e}")
  return ""

def generate_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  return _generate_pdf_base(PDF_URL, tc, name, surname, miktar, "pdf")

def generate_burs_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  return _generate_pdf_base(BURS_PDF_URL, tc, name, surname, miktar, "burs")
 
def generate_dip_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  return _generate_pdf_base(DIP_PDF_URL, tc, name, surname, miktar, "dip")

# ================== ERROR HANDLER ==================
def on_error(update: object, context: CallbackContext):
  log.exception("Unhandled error", exc_info=context.error)

# ================== MAIN ==================
def main():
  if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN .env'de yok!")

  request_kwargs = {
    "con_pool_size": 8,
    "connect_timeout": 30,
    "read_timeout": 180
  }

  updater = Updater(BOT_TOKEN, use_context=True, request_kwargs=request_kwargs)

  try:
    updater.bot.delete_webhook(drop_pending_updates=True)
  except Exception as e:
    log.warning(f"delete_webhook uyarı: {e}")

 Miktar ("(örn: 5.000)"):
  return D_MIKTAR

def get_d_miktar(update: Update, context: CallbackContext):
  if not _check_group(update, context):
    return ConversationHandler.END
  context.user_data["d_miktar"] = update.message.text.strip()
  update.message.reply_text("⏳ DİP PDF hazırlanıyor")
  name_up = tr_upper(context.user_data["d_name"])
  surname_up = tr_upper(context.user_data["d_surname"])
  pdf_path = generate_dip_pdf(
    context.user_data["d_tc"],
    name_up,
    surname_up,
    context.user_data["d_miktar"]
  )
  if not pdf_path:
    update.message.reply_text("❌ DİP PDF oluşturulamadı.")
    return ConversationHandler.END

  try:
    _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
  except Exception:
    pass

  sent_ok = False
  for attempt in range(1, 4):
    try:
      filename = f"{name_up}_{surname_up}_DIP.pdf".replace(" ", "_")
      with open(pdf_path, "rb") as f:
        update.message.reply_document(
          document=InputFile(f, filename=filename),
          timeout=180
        )
      sent_ok = True
      break
    except (NetworkError, TimedOut) as e:
      log.warning(f"dip send timeout/network (attempt {attempt}): {e}")
      if attempt == 3:
        update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
      else:
        time.sleep(2 * attempt)
    except Exception as e:
      log.exception(f"dip send failed: {e}")
      update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
      break

  try:
    os.remove(pdf_path)
  except Exception:
    pass

  if sent_ok:
    _dec_quota_if_applicable(update.effective_chat.id)

  return ConversationHandler.END
# ================== /dip BİTİŞ ==================


# ================== GÜNLÜK DM RAPORU ==================
def _build_daily_message(bot: "telegram.Bot") -> str:
  rep = _ensure_today_report()
  counts = rep.get("counts", {})
  if not counts:
    return (
      "ÜRETİLEN TOPLAM PDF : 0\n"
      "ÜRETİLEN BURS ve PDF : 0\n"
      "ÜRETİLEN KART PDF : 0\n\n"
      "Bugün üretim yok."
    )

  total_pdf = 0
  total_kart = 0
  lines = []
  for chat_id_str, node in counts.items():
    pdf_c = int(node.get("pdf", 0))
    kart_c = int(node.get("kart", 0))
    total_pdf += pdf_c
    total_kart += kart_c

    title = GROUP_TITLES.get(chat_id_str)
    if not title:
      # son çare: chat başlığını çekmeye çalış (fail olursa ID yaz)
      try:
        ch = bot.get_chat(int(chat_id_str))
        title = getattr(ch, "title", None) or f"Grup {chat_id_str}"
      except Exception:
        title = f"Grup {chat_id_str}"

    lines.append(f"- {title} ({chat_id_str}) → PDF: {pdf_c} | KART: {kart_c}")

  msg = (
    f"ÜRETİLEN TOPLAM PDF : {total_pdf}\n"
    f"ÜRETİLEN BURS ve PDF : {total_pdf}\n"
    f"ÜRETİLEN KART PDF : {total_kart}\n\n"
    + "\n".join(lines)
  )
  return msg

def send_daily_dm(bot: "telegram.Bot"):
  try:
    text = _build_daily_message(bot)
    bot.send_message(chat_id=ADMIN_ID, text=text)
  except Exception as e:
    log.exception(f"Günlük DM raporu gönderilemedi: {e}")

# ================== PDF OLUŞTURMA ==================
def _save_if_pdf_like(resp) -> str:
  try:
    ct = (resp.headers.get("Content-Type") or "").lower()
    cd = (resp.headers.get("Content-Disposition") or "").lower()
    content = resp.content or b""
    looks_pdf = (b"%PDF" in content[:10]) or ("application/pdf" in ct) or ("filename=" in cd)
    if resp.status_code == 200 and looks_pdf and content:
      tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
      tmp.write(content)
      tmp.close()
      return tmp.name
    return ""
  except Exception as e:
    log.exception(f"_save_if_pdf_like hata: {e}")
    return ""

def generate_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
  data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
  try:
    r = requests.post(PDF_URL, data=data, headers=_headers(), timeout=120)
    path = _save_if_pdf_like(r)
    if path:
      return path
    else:
      log.error(f"[form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
  except Exception as e:
    log.exception(f"[form] generate_pdf hata: {e}")
  try:
    r2 = requests.post(PDF_URL, json=data, headers=_headers(), timeout=120)
    path2 = _save_if_pdf_like(r2)
    if path2:
      return path2
    else:
      log.error(f"[json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
  except Exception as e:
    log.exception(f"[json] generate_pdf hata: {e}")
  return ""

# ================== ERROR HANDLER ==================
def on_error(update: object, context: CallbackContext):
  log.exception("Unhandled error", exc_info=context.error)

# ================== MAIN ==================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env'de yok!")

    request_kwargs = {
        "con_pool_size": 8,
        "connect_timeout": 30,
        "read_timeout": 180
    }

    updater = Updater(BOT_TOKEN, use_context=True, request_kwargs=request_kwargs)

    try:
        updater.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook uyarı: {e}")

    dp = updater.dispatcher
    dp.add_error_handler(on_error)

    conv = ConversationHandler(
        entry_points=[CommandHandler("pdf", start_pdf)],
        states={
            TC: [MessageHandler(Filters.text & ~Filters.command, get_tc)],
            NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
            SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_surname)],
            MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    conv_kart = ConversationHandler(
        entry_points=[CommandHandler("kart", start_kart)],
        states={
            K_ADSOYAD: [MessageHandler(Filters.text & ~Filters.command, get_k_adsoyad)],
            K_ADRES: [MessageHandler(Filters.text & ~Filters.command, get_k_adres)],
            K_ILILCE: [MessageHandler(Filters.text & ~Filters.command, get_k_ililce)],
            K_TARIH: [MessageHandler(Filters.text & ~Filters.command, get_k_tarih)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    # ✅ /burs handler
    conv_burs = ConversationHandler(
        entry_points=[CommandHandler("burs", start_burs)],
        states={
            B_TC: [MessageHandler(Filters.text & ~Filters.command, get_b_tc)],
            B_NAME: [MessageHandler(Filters.text & ~Filters.command, get_b_name)],
            B_SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_b_surname)],
            B_MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_b_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )
    
    # ✅ YENİ /dip handler
    conv_dip = ConversationHandler(
        entry_points=[CommandHandler("dip", start_dip)],
        states={
            D_TC: [MessageHandler(Filters.text & ~Filters.command, get_d_tc)],
            D_NAME: [MessageHandler(Filters.text & ~Filters.command, get_d_name)],
            D_SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_d_surname)],
            D_MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_d_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    # Admin-only komutlar
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))
    dp.add_handler(CommandHandler("yetkiver", cmd_yetkiver, pass_args=True))
    dp.add_handler(CommandHandler("hakver", cmd_hakver))
    dp.add_handler(CommandHandler("kalanhak", cmd_hakdurum))
    dp.add_handler(CommandHandler("bitir", cmd_bitir))
    dp.add_handler(CommandHandler("rapor", cmd_rapor))
    dp.add_handler(CommandHandler("raporadmin", cmd_raporadmin))
    dp.add_handler(CommandHandler("kontenjan", cmd_kontenjan))
    dp.add_handler(CommandHandler("ekle", cmd_kontenjan))
    
    # Normal akışlar
    dp.add_handler(conv)
    dp.add_handler(conv_kart)
    dp.add_handler(conv_burs)
    dp.add_handler(conv_dip) # ✅ YENİ eklendi

    # ⏰ Günlük 23:55'te ADMIN_ID'ye DM rapor
    scheduler = BackgroundScheduler(timezone=TR_TZ)
    scheduler.add_job(
        send_daily_dm,
        CronTrigger(hour=23, minute=55, timezone=TR_TZ),
        args=[updater.bot],
        id="daily_dm_2355",
        replace_existing=True,
    )
    scheduler.start()

    log.info("Bot açılıyor...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
