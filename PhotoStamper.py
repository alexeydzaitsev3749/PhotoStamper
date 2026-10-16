#!/usr/bin/env python3
"""
PhotoStamper.py — Универсальный инструмент штамповки:
1. print_glyph() — наложение CJK-глифа в левый нижний угол.
2. print_stamp() — наложение даты, координат и адреса в правый нижний угол.
Поддерживает беспотерьную врезку через jpegtran и защиту от спуфинга РЭБ.
"""

import os
import sys
import glob
import json
import re
import shutil
import argparse
import tempfile
import subprocess
import urllib.request
from datetime import datetime
from PIL import Image, ImageOps, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

RU_MONTHS = [
    "", "янв.", "февр.", "мар.", "апр.", "мая", "июн.",
    "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."
]

GEO_CACHE = {}

# --- ПОИСК ШРИФТОВ ---
def get_font(size):
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    for p in candidates:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: continue
    return ImageFont.load_default()

def get_cjk_font(size=80):
    candidates = [
        os.path.join(SCRIPT_DIR, "cjk_font.ttf"),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\msgothic.ttc"
    ]
    for p in candidates:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size, index=0) if p.endswith('.ttc') else ImageFont.truetype(p, size)
            except Exception: continue
    return ImageFont.load_default()

# --- ПРОВЕРКА ИМЕНИ НА ТАЙМСТЕМПНОСТЬ ---
def is_timestamp_named(filename):
    return bool(re.search(r'TimePhoto_\d{8}_\d{6}', filename, re.IGNORECASE))

# --- ПАРСИНГ EXIF (ДАТА И GPS) ---
def parse_exif_datetime(exif_dict):
    if not exif_dict: return None
    raw_dt = exif_dict.get(36867) or exif_dict.get(36868) or exif_dict.get(306)
    if not raw_dt: return None
    try:
        s = str(raw_dt).strip().replace("\x00", "")
        dt = datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
        return f"{dt.day} {RU_MONTHS[dt.month]} {dt.year} г. {dt.strftime('%H:%M:%S')}"
    except Exception:
        return str(raw_dt)

def dms_to_decimal(dms, ref):
    try:
        deg = float(dms[0])
        mn = float(dms[1]) / 60.0
        sec = float(dms[2]) / 3600.0
        val = deg + mn + sec
        return -val if ref in ['S', 'W'] else val
    except Exception:
        return None

def parse_exif_gps(exif):
    if not exif: return None, None
    try:
        gps_ifd = exif.get_ifd(0x8825)
        if not gps_ifd: return None, None
        lat = dms_to_decimal(gps_ifd.get(2), gps_ifd.get(1))
        lon = dms_to_decimal(gps_ifd.get(4), gps_ifd.get(3))
        return lat, lon
    except Exception:
        return None, None

def reverse_geocode(lat, lon):
    if lat is None or lon is None: return []
    cache_key = (round(lat, 3), round(lon, 3))
    if cache_key in GEO_CACHE: return GEO_CACHE[cache_key]

    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&accept-language=ru"
    req = urllib.request.Request(url, headers={"User-Agent": "PhotoStamper/2.0"})
    lines = []
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            addr = data.get("address", {})
            road = addr.get("road") or addr.get("street")
            house = addr.get("house_number")
            if road: lines.append(f"{road}, {house}" if house else road)
            suburb = addr.get("suburb") or addr.get("village") or addr.get("hamlet")
            if suburb and suburb not in lines: lines.append(suburb)
            city = addr.get("city") or addr.get("town") or addr.get("county")
            if city: lines.append(city)
            state = addr.get("state")
            if state: lines.append(state)
    except Exception:
        pass
    GEO_CACHE[cache_key] = lines
    return lines

# --- БЕСПОТЕРЬНАЯ ВРЕЗКА ЧЕРЕЗ JPEGTRAN (СЕТКА MCU 16x16) ---
def find_jpegtran():
    return shutil.which("jpegtran") or (os.path.join(SCRIPT_DIR, "jpegtran.exe") if os.path.exists(os.path.join(SCRIPT_DIR, "jpegtran.exe")) else None)

def apply_jpeg_patch_lossless(base_jpg, patch_img, x_pos, y_pos):
    """Врезает заплатку в JPEG через jpegtran без пересжатия остального кадра."""
    jpegtran_bin = find_jpegtran()
    if not jpegtran_bin:
        return False  # Сигнал для fallback на Pillow

    pw, ph = patch_img.size
    # Выравнивание координат и размеров строго по границам MCU (кратно 16)
    x_mcu = (x_pos // 16) * 16
    y_mcu = (y_pos // 16) * 16
    pw_mcu = ((pw + 15) // 16) * 16
    ph_mcu = ((ph + 15) // 16) * 16

    aligned_patch = Image.new("RGB", (pw_mcu, ph_mcu), "black")
    aligned_patch.paste(patch_img, (0, 0))

    tmp_dir = tempfile.gettempdir()
    tmp_patch = os.path.join(tmp_dir, f"patch_{os.getpid()}_{x_mcu}_{y_mcu}.jpg")
    tmp_out = os.path.join(tmp_dir, f"out_{os.getpid()}.jpg")

    try:
        aligned_patch.save(tmp_patch, "JPEG", quality=95, subsampling="4:2:0")
        cmd = [jpegtran_bin, "-drop", f"+{x_mcu}+{y_mcu}", tmp_patch, "-copy", "all", "-outfile", tmp_out, base_jpg]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            os.replace(tmp_out, base_jpg)
            return True
    except Exception:
        pass
    finally:
        for f in (tmp_patch, tmp_out):
            if os.path.exists(f):
                try: os.remove(f)
                except Exception: pass
    return False

# =========================================================================
# ИНТЕРФЕЙС 1: ПЕЧАТЬ ГЛИФА (ЛЕВЫЙ НИЖНИЙ УГОЛ)
# =========================================================================
def print_glyph(filepath, glyph):
    """Впечатывает CJK-глиф в левый нижний угол (монолитная плашка 112x96)."""
    abs_path = os.path.abspath(filepath)
    with Image.open(abs_path) as im:
        w, h = im.size

    # Размер плашки 112x96 (кратен 16 для MCU)
    box_w, box_h = 112, 96
    patch = Image.new("RGB", (box_w, box_h), "black")
    draw = ImageDraw.Draw(patch)
    font = get_cjk_font(80)

    bbox = font.getbbox(glyph)
    gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    gx = (box_w - gw) // 2 - bbox[0]
    gy = (box_h - gh) // 2 - bbox[1]
    draw.text((gx, gy), glyph, fill="white", font=font)

    x_pos = 0
    y_pos = h - box_h

    # 1. Пробуем беспотерьную врезку через jpegtran
    if apply_jpeg_patch_lossless(abs_path, patch, x_pos, y_pos):
        return True, "Lossless (MCU)"

    # 2. Fallback на Pillow
    with Image.open(abs_path) as im:
        raw_exif = im.getexif()
        im_trans = ImageOps.exif_transpose(im).convert("RGB")
        im_trans.paste(patch, (x_pos, y_pos))
        tmp = f"{abs_path}.tmp_gl.jpg"
        im_trans.save(tmp, "JPEG", quality=95, exif=raw_exif)
        os.replace(tmp, abs_path)
    return True, "Pillow"

# =========================================================================
# ИНТЕРФЕЙС 2: ПЕЧАТЬ ТЕЛЕМЕТРИИ (ПРАВЫЙ НИЖНИЙ УГОЛ)
# =========================================================================
def print_stamp(filepath, coords=None, dt=None, fetch_address=True):
    """
    Впечатывает штамп даты/GPS/адреса в правый нижний угол.
    Поддерживает защиту от РЭБ: если передан coords, берет его вместо EXIF!
    """
    abs_path = os.path.abspath(filepath)
    with Image.open(abs_path) as im:
        raw_exif = im.getexif()
        w, h = im.size

    # Определение даты (переданная вручную или из EXIF)
    dt_str = dt or parse_exif_datetime(raw_exif)

    # Определение координат (защита от спуфинга РЭБ!)
    lat, lon = None, None
    coords_str = ""

    if coords and isinstance(coords, str):
        # Ручные координаты пользователя в обход РЭБ
        coords_str = coords.strip()
        m = re.search(r'(\d+\.\d+)[NnSs]\s*(\d+\.\d+)[EeWw]', coords_str)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
    else:
        # Автоматические координаты из EXIF
        lat, lon = parse_exif_gps(raw_exif)
        if lat is not None and lon is not None:
            coords_str = f"{abs(lat):.7f}{'N' if lat >= 0 else 'S'} {abs(lon):.7f}{'E' if lon >= 0 else 'W'}"

    if not dt_str and not coords_str:
        return False, "нет метаданных даты и координат"

    lines = []
    if dt_str: lines.append(dt_str)
    if coords_str: lines.append(coords_str)

    if fetch_address and lat and lon:
        addr = reverse_geocode(lat, lon)
        lines.extend(addr)

    # Расчет типографики штампа
    font_size = max(18, int(h * 0.018))
    font = get_font(font_size)
    line_sp = int(font_size * 0.35)

    dummy_im = Image.new("RGB", (10, 10))
    d_draw = ImageDraw.Draw(dummy_im)

    widths = [d_draw.textbbox((0, 0), l, font=font)[2] for l in lines]
    heights = [d_draw.textbbox((0, 0), l, font=font)[3] for l in lines]

    max_w = max(widths) if widths else 100
    tot_h = sum(heights) + line_sp * (len(lines) - 1)

    # Размеры плашки (с запасом 15px и кратностью 16)
    patch_w = ((max_w + 30 + 15) // 16) * 16
    patch_h = ((tot_h + 30 + 15) // 16) * 16

    patch = Image.new("RGB", (patch_w, patch_h), "black")
    draw = ImageDraw.Draw(patch)

    curr_y = 15
    for idx, l in enumerate(lines):
        # Рисуем контрастный текст с тенью
        for ox, oy in [(-1,-1), (1,-1), (-1,1), (1,1)]:
            draw.text((15 + ox, curr_y + oy), l, font=font, fill="#222222")
        draw.text((15, curr_y), l, font=font, fill="white")
        curr_y += heights[idx] + line_sp

    x_pos = w - patch_w
    y_pos = h - patch_h

    # 1. Пробуем Lossless врезку через jpegtran
    if apply_jpeg_patch_lossless(abs_path, patch, x_pos, y_pos):
        return True, "Lossless (MCU)"

    # 2. Fallback на Pillow
    with Image.open(abs_path) as im:
        im_trans = ImageOps.exif_transpose(im).convert("RGB")
        im_trans.paste(patch, (x_pos, y_pos))
        tmp = f"{abs_path}.tmp_st.jpg"
        im_trans.save(tmp, "JPEG", quality=95, exif=raw_exif)
        os.replace(tmp, abs_path)
    return True, "Pillow"

# =========================================================================
# КОМАНДНАЯ СТРОКА (CLI) ДЛЯ РУЧНОЙ ПРОВЕРКИ
# =========================================================================
def main():
    parser = argparse.ArgumentParser(description="PhotoStamper: наложение CJK-глифа (-g) и координат (-c).")
    parser.add_argument("targets", nargs="*", help="Файлы (*.jpg)")
    parser.add_argument("-g", "--glyph", default=None, help="Наложить глиф в левый нижний угол (например: -g 丹)")
    parser.add_argument("-c", "--coords", nargs="?", const=True, default=None,
                        help="Наложить координаты в правый угол (из EXIF или вручную: -c '56.051N 37.493E')")
    parser.add_argument("--no-geo", action="store_true", help="Не запрашивать адрес через сеть (защита от РЭБ)")

    args = parser.parse_args()

    actual_targets = args.targets if args.targets else ["."]
    files = []
    for t in actual_targets:
        matched = glob.glob(t) or ([t] if os.path.exists(t) else [])
        for m in matched:
            if os.path.isfile(m) and m.lower().endswith(('.jpg', '.jpeg')):
                files.append(os.path.abspath(m))

    files = sorted(list(set(files)))
    if not files:
        print("[!] Файлы для обработки не найдены.")
        return

    print(f"[*] Отобрано файлов: {len(files)}")
    has_jpegtran = "ДОСТУПЕН (Lossless MCU)" if find_jpegtran() else "НЕ НАЙДЕН (режим Pillow)"
    print(f"[*] Движок врезки   : {has_jpegtran}")

    for f in files:
        fname = os.path.basename(f)

        # 1. Печать глифа (-g)
        if args.glyph:
            ok, mode = print_glyph(f, args.glyph)
            print(f"  [GLYPH] {fname} -> [{args.glyph}] ({mode})")

        # 2. Печать телеметрии (-c)
        if args.coords is not None:
            c_val = None if args.coords is True else str(args.coords)
            ok, mode = print_stamp(f, coords=c_val, fetch_address=not args.no_geo)
            print(f"  [STAMP] {fname} -> Координаты ({mode})")

if __name__ == "__main__":
    main()