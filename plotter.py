import ctypes
import hashlib
import json
import math
import os
import random
import re
import struct
import tkinter as tk
import zipfile
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ==============================================================================
# ЗАГРУЗЧИК КАСТОМНЫХ ШРИФТОВ (.TTF / .OTF) ЧЕРЕЗ WINDOWS GDI32 (БЕЗ PIP-БИБЛИОТЕК)
# ==============================================================================
GGO_METRICS = 0
GGO_NATIVE = 2
TT_PRIM_LINE = 1
TT_PRIM_QSPLINE = 2
TT_PRIM_CSPLINE = 3
FR_PRIVATE = 0x10


class FIXED(ctypes.Structure):
    _fields_ = [("fract", ctypes.c_uint16), ("value", ctypes.c_int16)]


class MAT2(ctypes.Structure):
    _fields_ = [("eM11", FIXED), ("eM12", FIXED), ("eM21", FIXED), ("eM22", FIXED)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class GLYPHMETRICS(ctypes.Structure):
    _fields_ = [
        ("gmBlackBoxX", ctypes.c_uint),
        ("gmBlackBoxY", ctypes.c_uint),
        ("gmptGlyphOrigin", POINT),
        ("gmCellIncX", ctypes.c_short),
        ("gmCellIncY", ctypes.c_short),
    ]


class WindowsTTFFont:
    """Извлекает векторные контуры символов из любого файла .ttf / .otf в Windows."""
    def __init__(self, font_path):
        self.font_path = os.path.abspath(font_path)
        self.font_name = self._extract_font_family_name(self.font_path)
        self.em_size = 1000
        self.cache = {}

        self.gdi32 = ctypes.windll.gdi32
        self.user32 = ctypes.windll.user32

        added = self.gdi32.AddFontResourceExW(ctypes.c_wchar_p(self.font_path), FR_PRIVATE, 0)
        if added == 0:
            raise RuntimeError(f"Не удалось загрузить файл шрифта:\n{self.font_path}")

        self.hdc = self.user32.GetDC(0)
        self.hfont = self.gdi32.CreateFontW(
            -self.em_size, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0,
            ctypes.c_wchar_p(self.font_name)
        )
        self.old_font = self.gdi32.SelectObject(self.hdc, self.hfont)

        self.mat2 = MAT2()
        self.mat2.eM11.value = 1
        self.mat2.eM22.value = 1

    def close(self):
        try:
            if getattr(self, "hdc", None):
                if getattr(self, "old_font", None):
                    self.gdi32.SelectObject(self.hdc, self.old_font)
                if getattr(self, "hfont", None):
                    self.gdi32.DeleteObject(self.hfont)
                self.user32.ReleaseDC(0, self.hdc)
            self.gdi32.RemoveFontResourceExW(ctypes.c_wchar_p(self.font_path), FR_PRIVATE, 0)
        except Exception:
            pass

    @staticmethod
    def _extract_font_family_name(ttf_path):
        try:
            with open(ttf_path, "rb") as f:
                data = f.read()
            num_tables = struct.unpack(">H", data[4:6])[0]
            name_offset = 0
            for i in range(num_tables):
                rec = data[12 + i * 16: 12 + (i + 1) * 16]
                tag, _, offset, _ = struct.unpack(">4sIII", rec)
                if tag == b"name":
                    name_offset = offset
                    break
            if not name_offset:
                return os.path.splitext(os.path.basename(ttf_path))[0]

            count, str_offset = struct.unpack(">HH", data[name_offset + 2: name_offset + 6])
            storage = name_offset + str_offset
            fallback_name = None

            for i in range(count):
                rec_pos = name_offset + 6 + i * 12
                pid, eid, lid, nid, length, off = struct.unpack(">HHHHHH", data[rec_pos: rec_pos + 12])
                if nid == 1:
                    raw = data[storage + off: storage + off + length]
                    if pid == 3:
                        name = raw.decode("utf-16-be", errors="ignore").strip("\x00")
                        if name:
                            if lid == 0x0409:
                                return name
                            fallback_name = name
                    elif pid == 1 and not fallback_name:
                        name = raw.decode("latin1", errors="ignore").strip("\x00")
                        if name:
                            fallback_name = name
            if fallback_name:
                return fallback_name
        except Exception:
            pass
        return os.path.splitext(os.path.basename(ttf_path))[0]

    @staticmethod
    def _fixed_to_float(raw_bytes, pos):
        return struct.unpack("<i", raw_bytes[pos: pos + 4])[0] / 65536.0

    def get_glyph(self, ch):
        if ch in self.cache:
            return self.cache[ch]

        if ch == " ":
            self.cache[ch] = ([], 5.5)
            return self.cache[ch]

        gm = GLYPHMETRICS()
        buf_size = self.gdi32.GetGlyphOutlineW(
            self.hdc, ord(ch), GGO_NATIVE, ctypes.byref(gm), 0, None, ctypes.byref(self.mat2)
        )
        if buf_size <= 0 or buf_size == 0xFFFFFFFF:
            self.cache[ch] = ([], 5.5)
            return self.cache[ch]

        buf = ctypes.create_string_buffer(buf_size)
        res = self.gdi32.GetGlyphOutlineW(
            self.hdc, ord(ch), GGO_NATIVE, ctypes.byref(gm), buf_size, buf, ctypes.byref(self.mat2)
        )
        if res <= 0 or res == 0xFFFFFFFF:
            self.cache[ch] = ([], 5.5)
            return self.cache[ch]

        raw = buf.raw
        scale_10 = 10.0 / (self.em_size * 0.72)
        strokes = []
        pos = 0
        total_len = len(raw)

        while pos + 16 <= total_len:
            cb, dwType = struct.unpack("<II", raw[pos: pos + 8])
            if cb < 16 or pos + cb > total_len:
                break
            start_x = self._fixed_to_float(raw, pos + 8) * scale_10
            start_y = self._fixed_to_float(raw, pos + 12) * scale_10

            poly = [(start_x, start_y)]
            cur = pos + 16
            end_poly = pos + cb

            while cur + 4 <= end_poly:
                wType, cpfx = struct.unpack("<HH", raw[cur: cur + 4])
                cur += 4
                pts = []
                for _ in range(cpfx):
                    if cur + 8 > end_poly:
                        break
                    px = self._fixed_to_float(raw, cur) * scale_10
                    py = self._fixed_to_float(raw, cur + 4) * scale_10
                    pts.append((px, py))
                    cur += 8

                if not pts:
                    continue

                if wType == TT_PRIM_LINE:
                    poly.extend(pts)
                elif wType == TT_PRIM_QSPLINE:
                    p0 = poly[-1]
                    n = len(pts)
                    for i in range(n - 1):
                        p1 = pts[i]
                        p2 = pts[i + 1] if i == n - 2 else ((p1[0] + pts[i + 1][0]) * 0.5, (p1[1] + pts[i + 1][1]) * 0.5)
                        for step in range(1, 5):
                            t = step / 4.0
                            mt = 1.0 - t
                            bx = mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0]
                            by = mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]
                            poly.append((bx, by))
                        p0 = p2
                    if n == 1:
                        poly.append(pts[0])
                elif wType == TT_PRIM_CSPLINE:
                    poly.extend(pts)

            if len(poly) >= 2:
                if math.hypot(poly[-1][0] - poly[0][0], poly[-1][1] - poly[0][1]) > 0.05:
                    poly.append(poly[0])
                strokes.append(poly)

            pos += cb

        adv = max(3.5, gm.gmCellIncX * scale_10)
        self.cache[ch] = (strokes, adv)
        return self.cache[ch]


# ==============================================================================
# ВСТРОЕННЫЙ ОДНОЛИНЕЙНЫЙ (СКЕЛЕТНЫЙ) ШРИФТ (Кириллица + Латиница + Цифры + Знаки)
# ==============================================================================
FONT_STROKES = {
    ' ': [],
    '0': [[(2, 0), (0, 2), (0, 8), (2, 10), (6, 10), (8, 8), (8, 2), (6, 0), (2, 0)]],
    '1': [[(1, 7), (5, 10), (5, 0)], [(1, 0), (8, 0)]],
    '2': [[(0, 8), (2, 10), (6, 10), (8, 8), (8, 6), (0, 0), (8, 0)]],
    '3': [[(0, 8), (2, 10), (6, 10), (8, 8), (6, 5), (3, 5)], [(6, 5), (8, 3), (8, 2), (6, 0), (2, 0), (0, 2)]],
    '4': [[(6, 0), (6, 10), (0, 3), (8, 3)]],
    '5': [[(8, 10), (1, 10), (0, 5), (5, 6), (8, 4), (8, 2), (5, 0), (1, 0), (0, 2)]],
    '6': [[(7, 9), (5, 10), (2, 10), (0, 7), (0, 2), (2, 0), (6, 0), (8, 2), (8, 4), (6, 6), (2, 6), (0, 4)]],
    '7': [[(0, 10), (8, 10), (2, 0)], [(1, 5), (6, 5)]],
    '8': [[(2, 5), (0, 7), (2, 10), (6, 10), (8, 7), (6, 5), (2, 5), (0, 3), (2, 0), (6, 0), (8, 3), (6, 5)]],
    '9': [[(8, 6), (6, 4), (2, 4), (0, 6), (0, 8), (2, 10), (6, 10), (8, 8), (8, 3), (5, 0), (1, 0)]],

    '.': [[(3, 0), (4, 0), (4, 1), (3, 1), (3, 0)]],
    ',': [[(4, 1), (3, 1), (4, 0), (2, -2)]],
    '!': [[(4, 3), (4, 10)], [(3, 0), (4, 0), (4, 1), (3, 1)]],
    '?': [[(1, 8), (3, 10), (6, 10), (8, 8), (8, 6), (4, 4), (4, 3)], [(4, 0), (4, 1)]],
    '-': [[(1, 5), (7, 5)]],
    '—': [[(0, 5), (9, 5)]],
    '+': [[(1, 5), (7, 5)], [(4, 2), (4, 8)]],
    '=': [[(1, 6), (7, 6)], [(1, 3), (7, 3)]],
    ':': [[(3, 6), (4, 6), (4, 7), (3, 7)], [(3, 0), (4, 0), (4, 1), (3, 1)]],
    ';': [[(3, 6), (4, 6)], [(4, 1), (2, -2)]],
    '(': [[(6, 11), (3, 8), (2, 4), (3, 0), (6, -2)]],
    ')': [[(2, 11), (5, 8), (6, 4), (5, 0), (2, -2)]],
    '[': [[(6, 11), (2, 11), (2, -2), (6, -2)]],
    ']': [[(2, 11), (6, 11), (6, -2), (2, -2)]],
    '/': [[(0, -1), (8, 11)]],
    '"': [[(2, 10), (2, 7)], [(5, 10), (5, 7)]],
    "'": [[(4, 10), (3, 7)]],
    '«': [[(5, 7), (1, 4), (5, 1)], [(8, 7), (4, 4), (8, 1)]],
    '»': [[(1, 7), (5, 4), (1, 1)], [(4, 7), (8, 4), (4, 1)]],
    '%': [[(1, 0), (8, 10)], [(1, 10), (3, 10), (3, 8), (1, 8), (1, 10)], [(6, 2), (8, 2), (8, 0), (6, 0), (6, 2)]],
    '№': [[(0, 0), (0, 10), (6, 0), (6, 10)], [(8, 7), (10, 7), (10, 9), (8, 9), (8, 7)], [(7, 5), (10, 5)]],
    '*': [[(4, 3), (4, 9)], [(1, 4), (7, 8)], [(1, 8), (7, 4)]],
    '^': [[(1, 6), (4, 10), (7, 6)]],
    '_': [[(0, -1), (8, -1)]],
    '>': [[(1, 8), (7, 5), (1, 2)]],
    '<': [[(7, 8), (1, 5), (7, 2)]],

    'А': [[(0, 0), (4, 10), (8, 0)], [(1.5, 4), (6.5, 4)]],
    'Б': [[(0, 0), (0, 10), (7, 10)], [(0, 5), (6, 5), (8, 4), (8, 1), (6, 0), (0, 0)]],
    'В': [[(0, 0), (0, 10), (6, 10), (8, 8), (6, 5), (0, 5)], [(6, 5), (8, 3), (8, 1), (6, 0), (0, 0)]],
    'Г': [[(0, 0), (0, 10), (7, 10)]],
    'Д': [[(1, 0), (3, 10), (7, 10), (7, 0)], [(0, -2), (0, 0), (9, 0), (9, -2)]],
    'Е': [[(7, 10), (0, 10), (0, 0), (7, 0)], [(0, 5), (5, 5)]],
    'Ё': [[(7, 10), (0, 10), (0, 0), (7, 0)], [(0, 5), (5, 5)], [(2, 12), (2.5, 12)], [(5, 12), (5.5, 12)]],
    'Ж': [[(0, 10), (4, 5), (0, 0)], [(4, 10), (4, 0)], [(8, 10), (4, 5), (8, 0)]],
    'З': [[(0, 8), (2, 10), (6, 10), (8, 8), (6, 5), (3, 5)], [(6, 5), (8, 3), (6, 0), (2, 0), (0, 2)]],
    'И': [[(0, 10), (0, 0), (8, 10), (8, 0)]],
    'Й': [[(0, 10), (0, 0), (8, 10), (8, 0)], [(2, 12), (4, 11), (6, 12)]],
    'К': [[(0, 10), (0, 0)], [(8, 10), (0, 5), (8, 0)]],
    'Л': [[(0, 0), (1, 1), (4, 10), (8, 10), (8, 0)]],
    'М': [[(0, 0), (0, 10), (4, 3), (8, 10), (8, 0)]],
    'Н': [[(0, 10), (0, 0)], [(8, 10), (8, 0)], [(0, 5), (8, 5)]],
    'О': [[(2, 0), (0, 2), (0, 8), (2, 10), (6, 10), (8, 8), (8, 2), (6, 0), (2, 0)]],
    'П': [[(0, 0), (0, 10), (8, 10), (8, 0)]],
    'Р': [[(0, 0), (0, 10), (6, 10), (8, 8), (8, 6), (6, 5), (0, 5)]],
    'С': [[(8, 8), (6, 10), (2, 10), (0, 8), (0, 2), (2, 0), (6, 0), (8, 2)]],
    'Т': [[(0, 10), (8, 10)], [(4, 10), (4, 0)]],
    'У': [[(0, 10), (4, 4)], [(8, 10), (2, 0), (0, 0)]],
    'Ф': [[(4, 10), (4, 0)], [(4, 9), (1, 9), (0, 7), (0, 4), (1, 2), (7, 2), (8, 4), (8, 7), (7, 9), (4, 9)]],
    'Х': [[(0, 10), (8, 0)], [(8, 10), (0, 0)]],
    'Ц': [[(0, 10), (0, 0), (8, 0), (8, 10)], [(8, 0), (9, 0), (9, -2)]],
    'Ч': [[(0, 10), (0, 6), (2, 5), (8, 5)], [(8, 10), (8, 0)]],
    'Ш': [[(0, 10), (0, 0), (8, 0), (8, 10)], [(4, 10), (4, 0)]],
    'Щ': [[(0, 10), (0, 0), (8, 0), (8, 10)], [(4, 10), (4, 0)], [(8, 0), (9, 0), (9, -2)]],
    'Ъ': [[(0, 10), (3, 10), (3, 0), (7, 0), (8, 1), (8, 4), (7, 5), (3, 5)]],
    'Ы': [[(0, 10), (0, 0), (4, 0), (5, 1), (5, 4), (4, 5), (0, 5)], [(8, 10), (8, 0)]],
    'Ь': [[(0, 10), (0, 0), (6, 0), (8, 1), (8, 4), (6, 5), (0, 5)]],
    'Э': [[(0, 8), (2, 10), (6, 10), (8, 8), (8, 2), (6, 0), (2, 0), (0, 2)], [(2, 5), (8, 5)]],
    'Ю': [[(0, 10), (0, 0)], [(0, 5), (3, 5)], [(5, 0), (3, 2), (3, 8), (5, 10), (7, 10), (9, 8), (9, 2), (7, 0), (5, 0)]],
    'Я': [[(8, 0), (8, 10), (2, 10), (0, 8), (0, 6), (2, 5), (8, 5)], [(3, 5), (0, 0)]],

    'а': [[(6, 5), (4, 6), (1, 6), (0, 4), (0, 1), (1, 0), (4, 0), (6, 2)], [(6, 6), (6, 0), (7, 0)]],
    'б': [[(7, 9), (2, 8), (0, 5), (0, 1), (2, 0), (6, 0), (7, 2), (7, 4), (5, 5), (1, 5)]],
    'в': [[(0, 0), (0, 9), (3, 10), (5, 9), (4, 6), (0, 5), (5, 5), (7, 3), (7, 1), (5, 0), (0, 0)]],
    'г': [[(0, 0), (0, 6), (6, 6)]],
    'д': [[(6, 5), (4, 6), (1, 6), (0, 4), (0, 1), (2, 0), (6, 1)], [(6, 6), (6, -3), (4, -4), (2, -3), (7, 0)]],
    'е': [[(0, 3), (7, 3), (7, 5), (5, 6), (2, 6), (0, 4), (0, 1), (2, 0), (6, 0)]],
    'ё': [[(0, 3), (7, 3), (7, 5), (5, 6), (2, 6), (0, 4), (0, 1), (2, 0), (6, 0)], [(2, 8), (2.5, 8)], [(5, 8), (5.5, 8)]],
    'ж': [[(0, 6), (4, 3), (0, 0)], [(4, 6), (4, 0)], [(8, 6), (4, 3), (8, 0)]],
    'з': [[(1, 5), (3, 6), (6, 6), (7, 5), (5, 3), (3, 3)], [(5, 3), (7, 2), (6, 0), (2, 0), (0, 1)]],
    'и': [[(0, 6), (0, 1), (1, 0), (3, 0), (6, 3)], [(6, 6), (6, 0), (7, 0)]],
    'й': [[(0, 6), (0, 1), (1, 0), (3, 0), (6, 3)], [(6, 6), (6, 0), (7, 0)], [(2, 8), (4, 7), (6, 8)]],
    'к': [[(0, 6), (0, 0)], [(6, 6), (0, 3), (6, 0)]],
    'л': [[(0, 1), (1, 0), (4, 6), (7, 0), (8, 1)]],
    'м': [[(0, 0), (1, 6), (4, 1), (7, 6), (8, 0)]],
    'н': [[(0, 6), (0, 0)], [(6, 6), (6, 0)], [(0, 3), (6, 3)]],
    'о': [[(2, 0), (0, 1), (0, 5), (2, 6), (5, 6), (7, 5), (7, 1), (5, 0), (2, 0)]],
    'п': [[(0, 0), (0, 6), (6, 6), (6, 0)]],
    'р': [[(0, -4), (0, 6), (5, 6), (7, 5), (7, 1), (5, 0), (0, 0)]],
    'с': [[(6, 5), (5, 6), (2, 6), (0, 4), (0, 2), (2, 0), (5, 0), (6, 1)]],
    'т': [[(0, 6), (0, 0)], [(0, 5), (2, 6), (4, 5), (4, 0)], [(4, 5), (6, 6), (8, 5), (8, 0)]],
    'у': [[(0, 6), (3, 0)], [(7, 6), (2, -3), (0, -4)]],
    'ф': [[(4, 8), (4, -3)], [(4, 5), (1, 6), (0, 4), (0, 2), (1, 0), (7, 0), (8, 2), (8, 4), (7, 6), (4, 5)]],
    'х': [[(0, 6), (7, 0)], [(7, 6), (0, 0)]],
    'ц': [[(0, 6), (0, 0), (6, 0), (6, 6)], [(6, 0), (7, 0), (7, -2)]],
    'ч': [[(0, 6), (0, 4), (2, 3), (6, 3)], [(6, 6), (6, 0)]],
    'ш': [[(0, 6), (0, 0), (8, 0), (8, 6)], [(4, 6), (4, 0)]],
    'щ': [[(0, 6), (0, 0), (8, 0), (8, 6)], [(4, 6), (4, 0)], [(8, 0), (9, 0), (9, -2)]],
    'ъ': [[(0, 6), (2, 6), (2, 0), (6, 0), (7, 1), (7, 3), (6, 4), (2, 4)]],
    'ы': [[(0, 6), (0, 0), (4, 0), (5, 1), (5, 3), (4, 4), (0, 4)], [(8, 6), (8, 0)]],
    'ь': [[(0, 6), (0, 0), (5, 0), (7, 1), (7, 3), (5, 4), (0, 4)]],
    'э': [[(1, 5), (3, 6), (5, 6), (7, 4), (7, 2), (5, 0), (3, 0), (1, 1)], [(2, 3), (7, 3)]],
    'ю': [[(0, 6), (0, 0)], [(0, 3), (3, 3)], [(5, 0), (3, 1), (3, 5), (5, 6), (7, 6), (8, 5), (8, 1), (7, 0), (5, 0)]],
    'я': [[(7, 0), (7, 6), (2, 6), (0, 5), (0, 4), (2, 3), (7, 3)], [(3, 3), (0, 0)]],

    'A': [[(0, 0), (4, 10), (8, 0)], [(1.5, 4), (6.5, 4)]],
    'B': [[(0, 0), (0, 10), (6, 10), (8, 8), (6, 5), (0, 5)], [(6, 5), (8, 3), (8, 1), (6, 0), (0, 0)]],
    'C': [[(8, 8), (6, 10), (2, 10), (0, 8), (0, 2), (2, 0), (6, 0), (8, 2)]],
    'D': [[(0, 0), (0, 10), (5, 10), (8, 7), (8, 3), (5, 0), (0, 0)]],
    'E': [[(7, 10), (0, 10), (0, 0), (7, 0)], [(0, 5), (5, 5)]],
    'F': [[(0, 0), (0, 10), (7, 10)], [(0, 5), (5, 5)]],
    'G': [[(8, 8), (6, 10), (2, 10), (0, 8), (0, 2), (2, 0), (6, 0), (8, 2), (8, 5), (5, 5)]],
    'H': [[(0, 10), (0, 0)], [(8, 10), (8, 0)], [(0, 5), (8, 5)]],
    'I': [[(2, 10), (6, 10)], [(4, 10), (4, 0)], [(2, 0), (6, 0)]],
    'J': [[(7, 10), (7, 2), (5, 0), (2, 0), (0, 2)]],
    'K': [[(0, 10), (0, 0)], [(8, 10), (0, 5), (8, 0)]],
    'L': [[(0, 10), (0, 0), (7, 0)]],
    'M': [[(0, 0), (0, 10), (4, 3), (8, 10), (8, 0)]],
    'N': [[(0, 0), (0, 10), (8, 0), (8, 10)]],
    'O': [[(2, 0), (0, 2), (0, 8), (2, 10), (6, 10), (8, 8), (8, 2), (6, 0), (2, 0)]],
    'P': [[(0, 0), (0, 10), (6, 10), (8, 8), (8, 6), (6, 5), (0, 5)]],
    'Q': [[(2, 0), (0, 2), (0, 8), (2, 10), (6, 10), (8, 8), (8, 2), (6, 0), (2, 0)], [(5, 3), (8, 0)]],
    'R': [[(0, 0), (0, 10), (6, 10), (8, 8), (8, 6), (6, 5), (0, 5)], [(4, 5), (8, 0)]],
    'S': [[(8, 8), (6, 10), (2, 10), (0, 8), (0, 6), (8, 4), (8, 2), (6, 0), (2, 0), (0, 2)]],
    'T': [[(0, 10), (8, 10)], [(4, 10), (4, 0)]],
    'U': [[(0, 10), (0, 2), (2, 0), (6, 0), (8, 2), (8, 10)]],
    'V': [[(0, 10), (4, 0), (8, 10)]],
    'W': [[(0, 10), (2, 0), (4, 6), (6, 0), (8, 10)]],
    'X': [[(0, 10), (8, 0)], [(8, 10), (0, 0)]],
    'Y': [[(0, 10), (4, 5), (4, 0)], [(8, 10), (4, 5)]],
    'Z': [[(0, 10), (8, 10), (0, 0), (8, 0)]],

    'a': [[(6, 5), (4, 6), (1, 6), (0, 4), (0, 1), (1, 0), (4, 0), (6, 2)], [(6, 6), (6, 0), (7, 0)]],
    'b': [[(0, 10), (0, 0), (5, 0), (7, 1), (7, 5), (5, 6), (0, 5)]],
    'c': [[(6, 5), (5, 6), (2, 6), (0, 4), (0, 2), (2, 0), (5, 0), (6, 1)]],
    'd': [[(6, 5), (5, 6), (2, 6), (0, 4), (0, 2), (2, 0), (5, 0), (6, 1)], [(6, 10), (6, 0)]],
    'e': [[(0, 3), (7, 3), (7, 5), (5, 6), (2, 6), (0, 4), (0, 1), (2, 0), (6, 0)]],
    'f': [[(6, 9), (4, 10), (3, 9), (3, 0)], [(1, 6), (5, 6)]],
    'g': [[(6, 5), (4, 6), (1, 6), (0, 4), (0, 2), (1, 0), (4, 0), (6, 1)], [(6, 6), (6, -2), (4, -4), (1, -4)]],
    'h': [[(0, 10), (0, 0)], [(0, 4), (2, 6), (5, 6), (6, 4), (6, 0)]],
    'i': [[(3, 6), (3, 0)], [(3, 8), (3, 9)]],
    'j': [[(4, 6), (4, -2), (2, -4), (0, -3)], [(4, 8), (4, 9)]],
    'k': [[(0, 10), (0, 0)], [(6, 6), (0, 3), (6, 0)]],
    'l': [[(2, 10), (3, 10), (3, 1), (5, 0)]],
    'm': [[(0, 6), (0, 0)], [(0, 5), (2, 6), (4, 5), (4, 0)], [(4, 5), (6, 6), (8, 5), (8, 0)]],
    'n': [[(0, 6), (0, 0)], [(0, 4), (2, 6), (5, 6), (6, 4), (6, 0)]],
    'o': [[(2, 0), (0, 1), (0, 5), (2, 6), (5, 6), (7, 5), (7, 1), (5, 0), (2, 0)]],
    'p': [[(0, -4), (0, 6), (5, 6), (7, 5), (7, 1), (5, 0), (0, 0)]],
    'q': [[(6, 5), (4, 6), (1, 6), (0, 4), (0, 2), (1, 0), (4, 0), (6, 1)], [(6, 6), (6, -4)]],
    'r': [[(0, 6), (0, 0)], [(0, 4), (2, 6), (5, 6)]],
    's': [[(6, 5), (4, 6), (1, 6), (0, 5), (6, 2), (5, 0), (2, 0), (0, 1)]],
    't': [[(3, 9), (3, 1), (5, 0)], [(1, 6), (5, 6)]],
    'u': [[(0, 6), (0, 1), (2, 0), (5, 0), (6, 2)], [(6, 6), (6, 0)]],
    'v': [[(0, 6), (3, 0), (6, 6)]],
    'w': [[(0, 6), (2, 0), (4, 4), (6, 0), (8, 6)]],
    'x': [[(0, 6), (6, 0)], [(6, 6), (0, 0)]],
    'y': [[(0, 6), (3, 0)], [(6, 6), (2, -3), (0, -4)]],
    'z': [[(0, 6), (6, 6), (0, 0), (6, 0)]],
}


def strokes_to_3d_mesh(all_strokes, line_width=0.45, z_bottom=0.0, z_top=0.20):
    """Превращает штрихи плоттера в замкнутую 3D-сетку (vertices, triangles) высотой 1 слой."""
    vertices = []
    triangles = []
    half_w = line_width / 2.0

    for poly in all_strokes:
        for i in range(len(poly) - 1):
            x1, y1 = poly[i]
            x2, y2 = poly[i + 1]
            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy)
            if length < 1e-4:
                continue

            ux = dx / length
            uy = dy / length
            nx = -uy * half_w
            ny = ux * half_w

            sx1 = x1 - ux * (half_w * 0.4)
            sy1 = y1 - uy * (half_w * 0.4)
            sx2 = x2 + ux * (half_w * 0.4)
            sy2 = y2 + uy * (half_w * 0.4)

            base = len(vertices)
            vertices.extend([
                (sx1 + nx, sy1 + ny, z_bottom),
                (sx1 - nx, sy1 - ny, z_bottom),
                (sx2 - nx, sy2 - ny, z_bottom),
                (sx2 + nx, sy2 + ny, z_bottom),
                (sx1 + nx, sy1 + ny, z_top),
                (sx1 - nx, sy1 - ny, z_top),
                (sx2 - nx, sy2 - ny, z_top),
                (sx2 + nx, sy2 + ny, z_top),
            ])

            triangles.extend([
                (base + 0, base + 2, base + 1),
                (base + 0, base + 3, base + 2),
                (base + 4, base + 5, base + 6),
                (base + 4, base + 6, base + 7),
                (base + 0, base + 1, base + 5),
                (base + 0, base + 5, base + 4),
                (base + 1, base + 2, base + 6),
                (base + 1, base + 6, base + 5),
                (base + 2, base + 3, base + 7),
                (base + 2, base + 7, base + 6),
                (base + 3, base + 0, base + 4),
                (base + 3, base + 4, base + 7),
            ])

    if not vertices:
        vertices = [
            (1, 1, 0), (2, 1, 0), (2, 2, 0), (1, 2, 0),
            (1, 1, 0.2), (2, 1, 0.2), (2, 2, 0.2), (1, 2, 0.2)
        ]
        triangles = [
            (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
            (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)
        ]

    return vertices, triangles


class A1MiniPlotterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Bambu Lab A1 mini — Плоттер конспектов (Свой шрифт + Смещение 33.5 мм + Z-Hop + Движение мышкой)")
        self.root.geometry("1300x860")

        self.custom_font = None

        self.vars = {
            "bed_w": tk.DoubleVar(value=180.0),
            "bed_h": tk.DoubleVar(value=180.0),
            "margin_left": tk.DoubleVar(value=15.0),
            "margin_right": tk.DoubleVar(value=15.0),
            "margin_top": tk.DoubleVar(value=15.0),
            "margin_bottom": tk.DoubleVar(value=15.0),
            "paper_mode": tk.StringVar(value="grid5"),
            "font_size": tk.DoubleVar(value=3.6),
            "line_spacing": tk.DoubleVar(value=5.0),
            "char_spacing": tk.DoubleVar(value=0.25),
            "slant_deg": tk.DoubleVar(value=12.0),
            "humanize": tk.DoubleVar(value=0.16),

            # Подъем ручки по умолчанию увеличен до 5.0 мм, чтобы гарантированно отрывать перо от стола!
            "z_down": tk.DoubleVar(value=0.2),
            "z_up": tk.DoubleVar(value=5.0),
            "speed_draw": tk.IntVar(value=2400),
            "speed_travel": tk.IntVar(value=9000),
            "speed_z": tk.IntVar(value=1800),
            "show_grid_preview": tk.BooleanVar(value=True),
            "plot_grid_on_paper": tk.BooleanVar(value=False),
            "pause_at_start": tk.BooleanVar(value=True),

            # Кастомный шрифт
            "use_custom_font": tk.BooleanVar(value=False),

            # Компенсация смещения ручки от сопла (33.5 мм)
            "enable_pen_offset": tk.BooleanVar(value=True),
            "pen_offset_dist": tk.DoubleVar(value=33.5),
            "pen_mount_dir": tk.StringVar(value="Спереди от сопла (Y -33.5)"),
            "pen_offset_x_manual": tk.DoubleVar(value=0.0),
            "pen_offset_y_manual": tk.DoubleVar(value=-33.5),

            # Перемещение и масштабирование самого текста на предпросмотре
            "text_pan_x_mm": tk.DoubleVar(value=0.0),
            "text_pan_y_mm": tk.DoubleVar(value=0.0),
            "text_scale_pct": tk.DoubleVar(value=100.0),
        }

        # Параметры зума/камеры самого холста предпросмотра (ПКМ / Ctrl+Колесо)
        self.view_zoom = 1.0
        self.view_pan_x = 0.0
        self.view_pan_y = 0.0
        self._drag_start_px = None
        self._drag_start_text_xy = None
        self._cam_drag_start = None
        self._last_px_per_mm = 2.5

        self.pages_data = []
        self.current_page_idx = 0

        self._build_ui()
        self.update_preview()

    def get_pen_offset_xy(self):
        if not self.vars["enable_pen_offset"].get():
            return 0.0, 0.0
        try:
            d = self.vars["pen_offset_dist"].get()
        except tk.TclError:
            d = 33.5

        direction = self.vars["pen_mount_dir"].get()
        if direction.startswith("Спереди"):
            return 0.0, -abs(d)
        elif direction.startswith("Слева"):
            return -abs(d), 0.0
        elif direction.startswith("Справа"):
            return abs(d), 0.0
        elif direction.startswith("Сзади"):
            return 0.0, abs(d)
        else:
            try:
                return self.vars["pen_offset_x_manual"].get(), self.vars["pen_offset_y_manual"].get()
            except tk.TclError:
                return 0.0, -33.5

    def get_char_strokes_and_width(self, ch):
        if self.vars["use_custom_font"].get() and self.custom_font is not None:
            strokes, adv = self.custom_font.get_glyph(ch)
            if strokes or ch == " ":
                return strokes, adv

        if ch == ' ':
            return [], 5.5
        strokes = FONT_STROKES.get(ch)
        if strokes is None:
            strokes = FONT_STROKES.get(ch.upper(), [])
        if not strokes:
            return [], 6.5
        max_x = max(pt[0] for stroke in strokes for pt in stroke)
        return strokes, max(max_x + 2.1, 3.8)

    def measure_text_mm(self, text, scale, char_spacing):
        if not text:
            return 0.0
        return sum(self.get_char_strokes_and_width(ch)[1] * scale + char_spacing for ch in text)

    def wrap_text_to_width(self, text, max_width_mm, scale, char_spacing):
        if not text.strip():
            return [""]
        words = text.split(" ")
        lines = []
        current_line = ""
        current_w = 0.0
        space_w = self.get_char_strokes_and_width(' ')[1] * scale + char_spacing

        for word in words:
            word_w = self.measure_text_mm(word, scale, char_spacing)
            if current_line and (current_w + space_w + word_w > max_width_mm):
                lines.append(current_line)
                current_line = word
                current_w = word_w
            else:
                if current_line:
                    current_line += " " + word
                    current_w += space_w + word_w
                else:
                    current_line = word
                    current_w = word_w
        if current_line:
            lines.append(current_line)
        return lines if lines else [""]

    def load_custom_font_dialog(self):
        path = filedialog.askopenfilename(
            title="Выберите файл вашего шрифта (.ttf или .otf)",
            filetypes=[("Шрифты TrueType / OpenType (*.ttf;*.otf)", "*.ttf *.otf"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            if self.custom_font is not None:
                self.custom_font.close()
            self.custom_font = WindowsTTFFont(path)
            self.vars["use_custom_font"].set(True)
            self.vars["slant_deg"].set(0.0)
            self.lbl_font_status.config(
                text=f"Шрифт: {self.custom_font.font_name} ({os.path.basename(path)})",
                foreground="#0a6e2e"
            )
            self.update_preview()
        except Exception as e:
            messagebox.showerror("Ошибка загрузки шрифта", str(e))

    def _build_ui(self):
        main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left_frame = ttk.Frame(main_pane, width=650)
        main_pane.add(left_frame, weight=1)

        right_frame = ttk.Frame(main_pane, width=620)
        main_pane.add(right_frame, weight=1)

        # --- Панель кастомного шрифта ---
        font_bar = ttk.LabelFrame(left_frame, text="Ваш собственный шрифт (.ttf / .otf)", padding=5)
        font_bar.pack(fill=tk.X, pady=(0, 5))

        ttk.Button(
            font_bar, text="📂 Загрузить свой шрифт (.ttf / .otf)",
            command=self.load_custom_font_dialog
        ).pack(side=tk.LEFT, padx=4)

        ttk.Checkbutton(
            font_bar, text="Использовать свой шрифт",
            variable=self.vars["use_custom_font"], command=self.update_preview
        ).pack(side=tk.LEFT, padx=6)

        self.lbl_font_status = ttk.Label(font_bar, text="Активен: Встроенный рукописный", foreground="#555")
        self.lbl_font_status.pack(side=tk.LEFT, padx=6)

        # --- Верхняя панель над текстом ---
        toolbar = ttk.Frame(left_frame)
        toolbar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(toolbar, text="Текст конспекта и таблицы:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="+ Вставить таблицу", command=self.insert_table_template).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="Клетка 5 мм", command=lambda: self.apply_preset("grid5")).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="Через клетку (10 мм)", command=lambda: self.apply_preset("grid10")).pack(side=tk.RIGHT, padx=2)

        self.txt_input = scrolledtext.ScrolledText(left_frame, height=9, font=("Consolas", 10), wrap=tk.WORD)
        self.txt_input.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        sample_text = (
            "Лекция №1. Электрический ток\n"
            "Закон Ома для участка цепи: I = U / R\n\n"
            "| Величина | Обозн. | Ед. изм. |\n"
            "|---|---|---|\n"
            "| Сила тока | I | Ампер (А) |\n"
            "| Напряжение | U | Вольт (В) |\n"
            "| Сопротивление | R | Ом (Ом) |\n\n"
            "Вывод: сила тока прямо пропорциональна напряжению "
            "и обратно пропорциональна сопротивлению проводника."
        )
        self.txt_input.insert(tk.END, sample_text)
        self.txt_input.bind("<KeyRelease>", lambda e: self.update_preview())

        # --- Блок компенсации смещения ручки 33.5 мм ---
        offset_box = ttk.LabelFrame(left_frame, text="Компенсация смещения ручки от сопла (33.5 мм)", padding=5)
        offset_box.pack(fill=tk.X, pady=3)

        ttk.Checkbutton(
            offset_box, text="Компенсировать смещение ручки:",
            variable=self.vars["enable_pen_offset"], command=self.update_preview
        ).grid(row=0, column=0, sticky="w", padx=4)

        ent_dist = ttk.Entry(offset_box, textvariable=self.vars["pen_offset_dist"], width=7)
        ent_dist.grid(row=0, column=1, sticky="w", padx=2)
        ent_dist.bind("<Return>", lambda e: self.update_preview())
        ent_dist.bind("<FocusOut>", lambda e: self.update_preview())
        ttk.Label(offset_box, text="мм | Где ручка:").grid(row=0, column=2, sticky="w", padx=4)

        dir_combo = ttk.Combobox(
            offset_box,
            textvariable=self.vars["pen_mount_dir"],
            values=[
                "Спереди от сопла (Y -33.5)",
                "Слева от сопла (X -33.5)",
                "Справа от сопла (X +33.5)",
                "Сзади от сопла (Y +33.5)",
            ],
            state="readonly",
            width=25
        )
        dir_combo.grid(row=0, column=3, sticky="w", padx=4)
        dir_combo.bind("<<ComboboxSelected>>", lambda e: self.update_preview())

        # --- Настройки ---
        settings_box = ttk.LabelFrame(left_frame, text="Настройки листа, подъема ручки (Z-Hop) и принтера", padding=6)
        settings_box.pack(fill=tk.X, pady=3)

        ttk.Label(settings_box, text="Тип тетрадного листа:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        paper_combo = ttk.Combobox(
            settings_box,
            textvariable=self.vars["paper_mode"],
            values=["grid5", "line8", "blank"],
            state="readonly",
            width=14
        )
        paper_combo.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        paper_combo.bind("<<ComboboxSelected>>", self.on_paper_mode_change)
        ttk.Label(settings_box, text="(grid5 = Клетка 5×5 мм, line8 = Линейка 8 мм)").grid(row=0, column=2, columnspan=2, sticky="w")

        params_layout = [
            ("Высота букв (мм):", "font_size", 1, 0),
            ("Шаг строк (мм, 5=клетка):", "line_spacing", 2, 0),
            ("Наклон почерка (град):", "slant_deg", 3, 0),
            ("Живой почерк (0..0.4):", "humanize", 4, 0),
            ("Межбуквенный интервал:", "char_spacing", 5, 0),
            ("Поле слева (мм):", "margin_left", 6, 0),
            ("Поле справа (мм):", "margin_right", 7, 0),

            ("Z письма (мм, >0):", "z_down", 1, 2),
            ("Z подъема ручки (мм!):", "z_up", 2, 2),
            ("Скорость письма (мм/мин):", "speed_draw", 3, 2),
            ("Скорость холостая (мм/мин):", "speed_travel", 4, 2),
            ("Скорость оси Z (мм/мин):", "speed_z", 5, 2),
            ("Поле сверху (мм):", "margin_top", 6, 2),
            ("Поле снизу (мм):", "margin_bottom", 7, 2),
        ]

        for label_text, var_name, r, c in params_layout:
            ttk.Label(settings_box, text=label_text).grid(row=r, column=c, sticky="w", padx=4, pady=2)
            entry = ttk.Entry(settings_box, textvariable=self.vars[var_name], width=9)
            entry.grid(row=r, column=c + 1, sticky="w", padx=4, pady=2)
            entry.bind("<Return>", lambda e: self.update_preview())
            entry.bind("<FocusOut>", lambda e: self.update_preview())

        chk_frame = ttk.Frame(settings_box)
        chk_frame.grid(row=8, column=0, columnspan=4, sticky="w", pady=(4, 0))

        ttk.Checkbutton(
            chk_frame, text="Показать клетку/линейку на экране",
            variable=self.vars["show_grid_preview"], command=self.update_preview
        ).grid(row=0, column=0, sticky="w", padx=4)

        ttk.Checkbutton(
            chk_frame, text="Начертить саму сетку/клетку ручкой на листе",
            variable=self.vars["plot_grid_on_paper"], command=self.update_preview
        ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Checkbutton(
            chk_frame, text="Пауза (M400 U1) перед стартом для проверки ручки",
            variable=self.vars["pause_at_start"]
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=2)

        # --- Кнопки экспорта ---
        export_box = ttk.LabelFrame(left_frame, text="Экспорт в Bambu Studio и лечение подъема ручки (Z-Hop)", padding=5)
        export_box.pack(fill=tk.X, pady=4)

        row1 = ttk.Frame(export_box)
        row1.pack(fill=tk.X, pady=2)

        ttk.Button(
            row1,
            text="1. Сохранить .3MF с 3D-геометрией",
            command=lambda: self.export_files(mode="3mf")
        ).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)

        ttk.Button(
            row1,
            text="2. Сохранить 3D-модель .STL",
            command=lambda: self.export_files(mode="stl")
        ).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)

        ttk.Button(
            row1,
            text="3. Сохранить готовый .GCODE (Z-Hop уже внутри)",
            command=lambda: self.export_files(mode="gcode")
        ).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)

        row2 = ttk.Frame(export_box)
        row2.pack(fill=tk.X, pady=2)

        ttk.Button(
            row2,
            text="🛠 Вшить подъем ручки (Z-Hop) в нарезанный файл из Bambu Studio (.gcode / .gcode.3mf)",
            command=self.patch_sliced_bambu_file
        ).pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)

        ttk.Button(
            row2,
            text="📋 Start G-code для Bambu Studio",
            command=self.show_bambu_studio_instructions
        ).pack(side=tk.LEFT, padx=2)

        # --- Правая часть: Интерактивный предпросмотр с перемещением и масштабированием ---
        top_bar = ttk.Frame(right_frame)
        top_bar.pack(fill=tk.X)

        ttk.Label(top_bar, text="Предпросмотр (ЛКМ = двигать текст, Колесо = масштаб):", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        self.btn_prev = ttk.Button(top_bar, text="◀", width=3, command=self.prev_page)
        self.btn_prev.pack(side=tk.RIGHT, padx=1)
        self.lbl_page = ttk.Label(top_bar, text="Стр. 1/1")
        self.lbl_page.pack(side=tk.RIGHT, padx=4)
        self.btn_next = ttk.Button(top_bar, text="▶", width=3, command=self.next_page)
        self.btn_next.pack(side=tk.RIGHT, padx=1)

        # Панель интерактивного масштаба и сдвига текста
        transform_bar = ttk.Frame(right_frame)
        transform_bar.pack(fill=tk.X, pady=(4, 2))

        ttk.Label(transform_bar, text="Масштаб текста (%):").pack(side=tk.LEFT)
        scale_slider = ttk.Scale(
            transform_bar, from_=30.0, to=250.0,
            variable=self.vars["text_scale_pct"],
            command=lambda val: self.draw_canvas()
        )
        scale_slider.pack(side=tk.LEFT, padx=4, fill=tk.X, expand=True)

        ent_scale = ttk.Entry(transform_bar, textvariable=self.vars["text_scale_pct"], width=5)
        ent_scale.pack(side=tk.LEFT, padx=2)
        ent_scale.bind("<Return>", lambda e: self.draw_canvas())

        ttk.Label(transform_bar, text="Сдвиг X:").pack(side=tk.LEFT, padx=(6, 1))
        ent_px = ttk.Entry(transform_bar, textvariable=self.vars["text_pan_x_mm"], width=5)
        ent_px.pack(side=tk.LEFT)
        ent_px.bind("<Return>", lambda e: self.draw_canvas())

        ttk.Label(transform_bar, text="Y:").pack(side=tk.LEFT, padx=(4, 1))
        ent_py = ttk.Entry(transform_bar, textvariable=self.vars["text_pan_y_mm"], width=5)
        ent_py.pack(side=tk.LEFT)
        ent_py.bind("<Return>", lambda e: self.draw_canvas())

        ttk.Button(transform_bar, text="Сброс поз.", command=self.reset_text_transform).pack(side=tk.LEFT, padx=4)
        ttk.Button(transform_bar, text="🔍 Вид 100%", command=self.reset_camera_view).pack(side=tk.LEFT, padx=2)

        self.canvas = tk.Canvas(right_frame, bg="#252526", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill=tk.BOTH, expand=True, pady=4)
        self.canvas.bind("<Configure>", lambda e: self.draw_canvas())

        # Привязка мыши для перетаскивания и масштабирования на предпросмотре
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_lmb_down)
        self.canvas.bind("<B1-Motion>", self.on_canvas_lmb_drag)
        self.canvas.bind("<ButtonPress-3>", self.on_canvas_rmb_down)
        self.canvas.bind("<B3-Motion>", self.on_canvas_rmb_drag)
        self.canvas.bind("<MouseWheel>", self.on_canvas_wheel)

        self.lbl_stats = ttk.Label(right_frame, text="", foreground="#333")
        self.lbl_stats.pack(anchor="w")

    # ==========================================================================
    # ИНТЕРАКТИВНОЕ ПЕРЕМЕЩЕНИЕ И МАСШТАБИРОВАНИЕ НА ПРЕДПРОСМОТРЕ
    # ==========================================================================
    def on_canvas_lmb_down(self, event):
        """Зажатие ЛКМ — начало перетаскивания текста по листу."""
        self._drag_start_px = (event.x, event.y)
        self._drag_start_text_xy = (
            self.vars["text_pan_x_mm"].get(),
            self.vars["text_pan_y_mm"].get()
        )

    def on_canvas_lmb_drag(self, event):
        """Перетаскивание ЛКМ — смещает текст в миллиметрах стола."""
        if not self._drag_start_px or self._last_px_per_mm <= 0:
            return
        dx_px = event.x - self._drag_start_px[0]
        dy_px = event.y - self._drag_start_px[1]
        dx_mm = dx_px / self._last_px_per_mm
        dy_mm = -dy_px / self._last_px_per_mm  # Ось Y на столе направлена вверх

        new_x = round(self._drag_start_text_xy[0] + dx_mm, 1)
        new_y = round(self._drag_start_text_xy[1] + dy_mm, 1)
        self.vars["text_pan_x_mm"].set(new_x)
        self.vars["text_pan_y_mm"].set(new_y)
        self.draw_canvas()

    def on_canvas_rmb_down(self, event):
        """Зажатие ПКМ — панорамирование самой камеры просмотра."""
        self._cam_drag_start = (event.x - self.view_pan_x, event.y - self.view_pan_y)

    def on_canvas_rmb_drag(self, event):
        if not self._cam_drag_start:
            return
        self.view_pan_x = event.x - self._cam_drag_start[0]
        self.view_pan_y = event.y - self._cam_drag_start[1]
        self.draw_canvas()

    def on_canvas_wheel(self, event):
        """
        Колесико мыши:
        - Обычное кручение: масштабирует сам текст/таблицы на листе (учитывается при экспорте!)
        - С зажатым Ctrl: приближает/отдаляет камеру предпросмотра (лупа)
        """
        factor = 1.08 if event.delta > 0 else (1.0 / 1.08)
        if event.state & 0x0004:  # Зажат Ctrl -> зум камеры
            self.view_zoom = max(0.5, min(6.0, self.view_zoom * factor))
        else:
            cur_scale = self.vars["text_scale_pct"].get()
            new_scale = round(max(25.0, min(300.0, cur_scale * factor)), 1)
            self.vars["text_scale_pct"].set(new_scale)
        self.draw_canvas()

    def reset_text_transform(self):
        self.vars["text_pan_x_mm"].set(0.0)
        self.vars["text_pan_y_mm"].set(0.0)
        self.vars["text_scale_pct"].set(100.0)
        self.draw_canvas()

    def reset_camera_view(self):
        self.view_zoom = 1.0
        self.view_pan_x = 0.0
        self.view_pan_y = 0.0
        self.draw_canvas()

    def _apply_user_transform_to_strokes(self, strokes_list):
        """
        Применяет пользовательское масштабирование (%) и перемещение мышкой (X, Y мм)
        ко всем штрихам текста и таблиц (относительно центра листа).
        """
        try:
            pan_x = self.vars["text_pan_x_mm"].get()
            pan_y = self.vars["text_pan_y_mm"].get()
            scale_factor = max(0.1, self.vars["text_scale_pct"].get() / 100.0)
            cx = self.vars["bed_w"].get() * 0.5
            cy = self.vars["bed_h"].get() * 0.5
        except tk.TclError:
            return strokes_list

        if abs(pan_x) < 1e-4 and abs(pan_y) < 1e-4 and abs(scale_factor - 1.0) < 1e-4:
            return strokes_list

        transformed = []
        for poly in strokes_list:
            new_poly = []
            for (x, y) in poly:
                tx = cx + (x - cx) * scale_factor + pan_x
                ty = cy + (y - cy) * scale_factor + pan_y
                new_poly.append((tx, ty))
            transformed.append(new_poly)
        return transformed

    def on_paper_mode_change(self, event=None):
        mode = self.vars["paper_mode"].get()
        if mode == "grid5":
            self.vars["line_spacing"].set(5.0)
            self.vars["font_size"].set(3.6)
        elif mode == "line8":
            self.vars["line_spacing"].set(8.0)
            self.vars["font_size"].set(4.5)
        self.update_preview()

    def apply_preset(self, preset):
        if preset == "grid5":
            self.vars["paper_mode"].set("grid5")
            self.vars["line_spacing"].set(5.0)
            self.vars["font_size"].set(3.6)
        elif preset == "grid10":
            self.vars["paper_mode"].set("grid5")
            self.vars["line_spacing"].set(10.0)
            self.vars["font_size"].set(4.2)
        self.update_preview()

    def insert_table_template(self):
        template = (
            "\n| № | Параметр | Значение |\n"
            "|---|---|---|\n"
            "| 1 | Скорость | 40 мм/с |\n"
            "| 2 | Температура | 0 С (холодный) |\n"
        )
        self.txt_input.insert(tk.INSERT, template)
        self.update_preview()

    def _render_text_line_strokes(self, line_str, x_start, y_baseline, scale, char_spacing, slant_tan, humanize, rng):
        strokes_out = []
        x_cursor = x_start
        font_size = scale * 10.0
        line_y_jitter = rng.uniform(-0.12, 0.12) * humanize * font_size

        for ch in line_str:
            strokes, char_w = self.get_char_strokes_and_width(ch)

            char_scale_x = scale * (1.0 + rng.uniform(-0.07, 0.07) * humanize)
            char_scale_y = scale * (1.0 + rng.uniform(-0.07, 0.07) * humanize)
            char_y_offset = line_y_jitter + rng.uniform(-0.10, 0.10) * humanize * font_size

            for stroke in strokes:
                poly = []
                for (px, py) in stroke:
                    jx = px + rng.uniform(-0.18, 0.18) * humanize
                    jy = py + rng.uniform(-0.18, 0.18) * humanize
                    real_x = x_cursor + (jx + jy * slant_tan) * char_scale_x
                    real_y = y_baseline + char_y_offset + jy * char_scale_y
                    poly.append((real_x, real_y))
                if len(poly) >= 2:
                    strokes_out.append(poly)

            x_cursor += char_w * scale + char_spacing
        return strokes_out

    def _build_paper_grid_strokes(self, bed_w, bed_h, m_left, m_right, m_top, m_bottom):
        mode = self.vars["paper_mode"].get()
        grid_strokes = []
        x0, x1 = m_left, bed_w - m_right
        y0, y1 = m_bottom, bed_h - m_top
        if x1 <= x0 or y1 <= y0:
            return grid_strokes

        if mode == "grid5":
            step = 5.0
            y = y1
            toggle = False
            while y >= y0 - 1e-6:
                grid_strokes.append([(x0, y), (x1, y)] if not toggle else [(x1, y), (x0, y)])
                toggle = not toggle
                y -= step
            x = x0
            toggle = False
            while x <= x1 + 1e-6:
                grid_strokes.append([(x, y1), (x, y0)] if not toggle else [(x, y0), (x, y1)])
                toggle = not toggle
                x += step
        elif mode == "line8":
            step = max(2.0, self.vars["line_spacing"].get())
            y = y1 - step
            toggle = False
            while y >= y0 - 1e-6:
                grid_strokes.append([(x0, y), (x1, y)] if not toggle else [(x1, y), (x0, y)])
                toggle = not toggle
                y -= step

        return grid_strokes

    def _get_safe_bounds(self, bed_w, bed_h, m_left, m_right, m_top, m_bottom):
        dx, dy = self.get_pen_offset_xy()
        min_pen_x = max(m_left, 1.0 + dx)
        max_pen_x = min(bed_w - m_right, bed_w - 1.0 + dx)
        min_pen_y = max(m_bottom, 1.0 + dy)
        max_pen_y = min(bed_h - m_top, bed_h - 1.0 + dy)
        return min_pen_x, max_pen_x, min_pen_y, max_pen_y

    def generate_layout(self):
        try:
            bed_w = self.vars["bed_w"].get()
            bed_h = self.vars["bed_h"].get()
            m_left = self.vars["margin_left"].get()
            m_right = self.vars["margin_right"].get()
            m_top = self.vars["margin_top"].get()
            m_bottom = self.vars["margin_bottom"].get()
            font_size = max(1.2, self.vars["font_size"].get())
            line_spacing = max(2.0, self.vars["line_spacing"].get())
            char_spacing = self.vars["char_spacing"].get()
            slant_rad = math.radians(self.vars["slant_deg"].get())
            humanize = max(0.0, self.vars["humanize"].get())
        except tk.TclError:
            return []

        eff_left, eff_right, eff_bottom, eff_top = self._get_safe_bounds(
            bed_w, bed_h, m_left, m_right, m_top, m_bottom
        )

        scale = font_size / 10.0
        slant_tan = math.tan(slant_rad)
        max_width = max(20.0, eff_right - eff_left)

        raw_lines = self.txt_input.get("1.0", tk.END).rstrip("\n").split("\n")

        blocks = []
        i = 0
        while i < len(raw_lines):
            s = raw_lines[i].strip()
            if s.startswith("|") and s.endswith("|") and len(s) > 2:
                table_rows = []
                while i < len(raw_lines):
                    row_s = raw_lines[i].strip()
                    if not (row_s.startswith("|") and row_s.endswith("|")):
                        break
                    cells = [c.strip() for c in row_s.strip("|").split("|")]
                    if all(set(c) <= set("-: ") and len(c) > 0 for c in cells):
                        i += 1
                        continue
                    table_rows.append(cells)
                    i += 1
                if table_rows:
                    blocks.append(("table", table_rows))
            else:
                blocks.append(("text", raw_lines[i]))
                i += 1

        rng = random.Random(42)
        pages = []

        def new_page():
            g_strokes = (
                self._build_paper_grid_strokes(
                    bed_w, bed_h,
                    eff_left, bed_w - eff_right, bed_h - eff_top, eff_bottom
                )
                if self.vars["plot_grid_on_paper"].get() else []
            )
            return {"text_strokes": [], "table_strokes": [], "grid_strokes": g_strokes}

        cur_page = new_page()
        top_y = eff_top
        y_cursor = top_y - line_spacing

        for b_type, b_content in blocks:
            if b_type == "text":
                wrapped = self.wrap_text_to_width(b_content, max_width, scale, char_spacing)
                for w_line in wrapped:
                    if y_cursor < eff_bottom:
                        pages.append(cur_page)
                        cur_page = new_page()
                        y_cursor = top_y - line_spacing

                    if w_line:
                        strokes = self._render_text_line_strokes(
                            w_line, eff_left + 1.0, y_cursor + 0.6,
                            scale, char_spacing, slant_tan, humanize, rng
                        )
                        cur_page["text_strokes"].extend(strokes)
                    y_cursor -= line_spacing

            elif b_type == "table":
                rows = b_content
                num_cols = max(len(r) for r in rows)
                if num_cols == 0:
                    continue
                norm_rows = [r + [""] * (num_cols - len(r)) for r in rows]

                col_padding = 2.0
                natural_widths = []
                for c_idx in range(num_cols):
                    max_cell_w = max(
                        self.measure_text_mm(norm_rows[r_idx][c_idx], scale, char_spacing)
                        for r_idx in range(len(norm_rows))
                    )
                    natural_widths.append(max(15.0, max_cell_w + 2 * col_padding))

                total_nat = sum(natural_widths)
                if total_nat > max_width:
                    col_widths = [w * (max_width / total_nat) for w in natural_widths]
                else:
                    col_widths = [math.ceil(w / 5.0) * 5.0 for w in natural_widths]
                    if sum(col_widths) > max_width:
                        col_widths = [w * (max_width / sum(col_widths)) for w in col_widths]

                table_top_y = y_cursor + line_spacing
                row_y_tops = [table_top_y]
                cur_row_top = table_top_y

                for row_cells in norm_rows:
                    wrapped_cells = []
                    max_lines_in_row = 1
                    for c_idx, cell_text in enumerate(row_cells):
                        avail_w = max(6.0, col_widths[c_idx] - 2 * col_padding)
                        w_lines = self.wrap_text_to_width(cell_text, avail_w, scale, char_spacing)
                        wrapped_cells.append(w_lines)
                        max_lines_in_row = max(max_lines_in_row, len(w_lines))

                    row_height = max_lines_in_row * line_spacing

                    if cur_row_top - row_height < eff_bottom and len(row_y_tops) > 1:
                        self._add_table_grid_strokes(cur_page["table_strokes"], eff_left, col_widths, row_y_tops)
                        pages.append(cur_page)
                        cur_page = new_page()
                        cur_row_top = top_y
                        row_y_tops = [cur_row_top]

                    cell_x = eff_left
                    for c_idx, w_lines in enumerate(wrapped_cells):
                        for l_idx, line_str in enumerate(w_lines):
                            baseline_y = cur_row_top - (l_idx + 1) * line_spacing + 0.6
                            strokes = self._render_text_line_strokes(
                                line_str, cell_x + col_padding, baseline_y,
                                scale, char_spacing, slant_tan, humanize, rng
                            )
                            cur_page["text_strokes"].extend(strokes)
                        cell_x += col_widths[c_idx]

                    cur_row_top -= row_height
                    row_y_tops.append(cur_row_top)

                self._add_table_grid_strokes(cur_page["table_strokes"], eff_left, col_widths, row_y_tops)
                y_cursor = cur_row_top - line_spacing

        if cur_page["text_strokes"] or cur_page["table_strokes"] or cur_page["grid_strokes"] or not pages:
            pages.append(cur_page)

        return pages

    def _add_table_grid_strokes(self, table_strokes_list, x_left, col_widths, row_y_tops):
        if len(row_y_tops) < 2:
            return
        total_w = sum(col_widths)
        x_right = x_left + total_w
        y_top = row_y_tops[0]
        y_bottom = row_y_tops[-1]

        for idx, y in enumerate(row_y_tops):
            table_strokes_list.append([(x_left, y), (x_right, y)] if idx % 2 == 0 else [(x_right, y), (x_left, y)])

        x_positions = [x_left]
        acc_x = x_left
        for w in col_widths:
            acc_x += w
            x_positions.append(acc_x)

        for idx, x in enumerate(x_positions):
            table_strokes_list.append([(x, y_top), (x, y_bottom)] if idx % 2 == 0 else [(x, y_bottom), (x, y_top)])

    def _get_final_nozzle_strokes(self, page_dict):
        """
        Собирает все штрихи страницы, применяет пользовательский сдвиг/масштаб с предпросмотра
        и переводит координаты ручки в координаты сопла (с учетом смещения 33.5 мм).
        """
        t_table = self._apply_user_transform_to_strokes(page_dict["table_strokes"])
        t_text = self._apply_user_transform_to_strokes(page_dict["text_strokes"])
        raw_strokes = page_dict["grid_strokes"] + t_table + t_text

        dx, dy = self.get_pen_offset_xy()
        bed_w = self.vars["bed_w"].get()
        bed_h = self.vars["bed_h"].get()

        shifted = []
        for poly in raw_strokes:
            new_poly = []
            for (x_pen, y_pen) in poly:
                nx = min(bed_w - 0.2, max(0.2, x_pen - dx))
                ny = min(bed_h - 0.2, max(0.2, y_pen - dy))
                new_poly.append((nx, ny))
            if len(new_poly) >= 2:
                shifted.append(new_poly)
        return shifted

    def update_preview(self):
        self.pages_data = self.generate_layout()
        if self.current_page_idx >= len(self.pages_data):
            self.current_page_idx = max(0, len(self.pages_data) - 1)
        self.lbl_page.config(text=f"Стр. {self.current_page_idx + 1}/{len(self.pages_data)}")
        self.draw_canvas()

    def prev_page(self):
        if self.current_page_idx > 0:
            self.current_page_idx -= 1
            self.lbl_page.config(text=f"Стр. {self.current_page_idx + 1}/{len(self.pages_data)}")
            self.draw_canvas()

    def next_page(self):
        if self.current_page_idx + 1 < len(self.pages_data):
            self.current_page_idx += 1
            self.lbl_page.config(text=f"Стр. {self.current_page_idx + 1}/{len(self.pages_data)}")
            self.draw_canvas()

    def draw_canvas(self):
        self.canvas.delete("all")
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 50 or ch < 50:
            return

        bed_w = self.vars["bed_w"].get()
        bed_h = self.vars["bed_h"].get()

        pad = 22
        base_px_per_mm = min((cw - 2 * pad) / bed_w, (ch - 2 * pad) / bed_h)
        px_per_mm = base_px_per_mm * self.view_zoom
        self._last_px_per_mm = px_per_mm

        ox = (cw - bed_w * px_per_mm) / 2 + self.view_pan_x
        oy = (ch - bed_h * px_per_mm) / 2 + self.view_pan_y

        def to_screen(x_mm, y_mm):
            return ox + x_mm * px_per_mm, oy + (bed_h - y_mm) * px_per_mm

        x0, y0 = to_screen(0, bed_h)
        x1, y1 = to_screen(bed_w, 0)
        self.canvas.create_rectangle(x0, y0, x1, y1, fill="#ffffff", outline="#888888", width=2)

        m_left = self.vars["margin_left"].get()
        m_right = self.vars["margin_right"].get()
        m_top = self.vars["margin_top"].get()
        m_bottom = self.vars["margin_bottom"].get()
        eff_left, eff_right, eff_bottom, eff_top = self._get_safe_bounds(
            bed_w, bed_h, m_left, m_right, m_top, m_bottom
        )
        mode = self.vars["paper_mode"].get()

        if self.vars["show_grid_preview"].get():
            if mode == "grid5":
                step = 5.0
                x = eff_left
                while x <= eff_right + 1e-5:
                    sx0, sy0 = to_screen(x, eff_top)
                    sx1, sy1 = to_screen(x, eff_bottom)
                    self.canvas.create_line(sx0, sy0, sx1, sy1, fill="#d4e6f9")
                    x += step
                y = eff_top
                while y >= eff_bottom - 1e-5:
                    sx0, sy0 = to_screen(eff_left, y)
                    sx1, sy1 = to_screen(eff_right, y)
                    self.canvas.create_line(sx0, sy0, sx1, sy1, fill="#d4e6f9")
                    y -= step
            elif mode == "line8":
                step = max(2.0, self.vars["line_spacing"].get())
                y = eff_top - step
                while y >= eff_bottom - 1e-5:
                    sx0, sy0 = to_screen(0, y)
                    sx1, sy1 = to_screen(bed_w, y)
                    self.canvas.create_line(sx0, sy0, sx1, sy1, fill="#d4e6f9")
                    y -= step

            mx0, my0 = to_screen(eff_left, bed_h)
            mx1, my1 = to_screen(eff_left, 0)
            self.canvas.create_line(mx0, my0, mx1, my1, fill="#f29b9b", width=1)

            bx0, by0 = to_screen(eff_left, eff_top)
            bx1, by1 = to_screen(eff_right, eff_bottom)
            self.canvas.create_rectangle(bx0, by0, bx1, by1, outline="#8bc34a", dash=(4, 4))

        if not self.pages_data:
            return

        page = self.pages_data[self.current_page_idx]
        total_dist = 0.0
        total_strokes = 0

        for poly in page["grid_strokes"]:
            coords = []
            for (x_mm, y_mm) in poly:
                coords.extend(to_screen(x_mm, y_mm))
            if len(coords) >= 4:
                self.canvas.create_line(*coords, fill="#90b8e0", width=1)

        transformed_tables = self._apply_user_transform_to_strokes(page["table_strokes"])
        for poly in transformed_tables:
            coords = []
            for idx, (x_mm, y_mm) in enumerate(poly):
                coords.extend(to_screen(x_mm, y_mm))
                if idx > 0:
                    px, py = poly[idx - 1]
                    total_dist += math.hypot(x_mm - px, y_mm - py)
            if len(coords) >= 4:
                self.canvas.create_line(*coords, fill="#0e2463", width=1.8)
                total_strokes += 1

        transformed_text = self._apply_user_transform_to_strokes(page["text_strokes"])
        for poly in transformed_text:
            coords = []
            for idx, (x_mm, y_mm) in enumerate(poly):
                coords.extend(to_screen(x_mm, y_mm))
                if idx > 0:
                    px, py = poly[idx - 1]
                    total_dist += math.hypot(x_mm - px, y_mm - py)
            if len(coords) >= 4:
                self.canvas.create_line(*coords, fill="#1b42b8", width=1.6, capstyle=tk.ROUND, joinstyle=tk.ROUND)
                total_strokes += 1

        dx, dy = self.get_pen_offset_xy()
        z_up = self.vars["z_up"].get()
        est_sec = int((total_dist / max(100, self.vars["speed_draw"].get())) * 60 * 1.45) + 15
        self.lbl_stats.config(
            text=f"Штрихов: {total_strokes} | Подъем Z-Hop: {z_up:.1f} мм | "
                 f"Смещение ручки: X={dx:+.1f}, Y={dy:+.1f} мм | Время: ~{est_sec // 60} мин {est_sec % 60} сек"
        )

    # ==========================================================================
    # ЭКСПОРТ 1: ПОЛНОЦЕННЫЙ .GCODE С ГАРАНТИРОВАННЫМ ВЕРТИКАЛЬНЫМ ПОДЪЕМОМ ПЕРА
    # ==========================================================================
    def build_bambu_gcode(self, page_dict):
        z_down = max(0.10, self.vars["z_down"].get())
        z_up = max(z_down + 1.0, self.vars["z_up"].get())
        f_draw = self.vars["speed_draw"].get()
        f_travel = self.vars["speed_travel"].get()
        f_z = self.vars["speed_z"].get()
        pause_start = self.vars["pause_at_start"].get()

        all_strokes = self._get_final_nozzle_strokes(page_dict)

        draw_dist = 0.0
        for poly in all_strokes:
            for i in range(1, len(poly)):
                draw_dist += math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1])
        est_seconds = max(20, int((draw_dist / max(100, f_draw)) * 60 * 1.45))
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dx, dy = self.get_pen_offset_xy()

        lines = [
            "; HEADER_BLOCK_START",
            f"; generated by BambuStudio 01.09.05.51 on {now_str}",
            "; total layer number: 1",
            "; filament_density: 1.24",
            "; filament_diameter: 1.75",
            f"; max_z_height: {z_up:.2f}",
            f"; model printing time: {est_seconds}s; total estimated time: {est_seconds}s",
            "; total weight: 0.15g",
            "; total length: 0.05m",
            "; HEADER_BLOCK_END",
            "",
            "; CONFIG_BLOCK_START",
            "; printer_model = Bambu Lab A1 mini",
            "; printer_settings_id = Bambu Lab A1 mini 0.4 nozzle",
            "; nozzle_diameter = 0.4",
            "; printable_area = 0x0,180x0,180x180,0x180",
            "; printable_height = 180",
            "; layer_height = 0.2",
            "; initial_layer_print_height = 0.2",
            "; line_width = 0.42",
            "; extruder_colour = #1B42B8",
            "; filament_type = PLA",
            "; filament_settings_id = \"Bambu PLA Basic @BBL A1M\"",
            "; print_settings_id = \"0.20mm Standard @BBL A1M\"",
            "; gcode_flavor = marlin",
            "; use_relative_e_distances = 1",
            "; nozzle_temperature = 0",
            "; nozzle_temperature_initial_layer = 0",
            "; hot_plate_temp = 0",
            "; hot_plate_temp_initial_layer = 0",
            "; textured_plate_temp = 0",
            "; textured_plate_temp_initial_layer = 0",
            "; cool_plate_temp = 0",
            "; cool_plate_temp_initial_layer = 0",
            "; CONFIG_BLOCK_END",
            "",
            "; EXECUTABLE_BLOCK_START",
            f"; Компенсация смещения ручки от сопла: dX={dx:+.2f} мм, dY={dy:+.2f} мм",
            "M73 P0 R1",
            "M412 S0 ; ОТКЛЮЧИТЬ ДАТЧИК ФИЛАМЕНТА",
            "M104 S0 ; Выключить нагрев сопла (0 C)",
            "M140 S0 ; Выключить нагрев стола (0 C)",
            "M106 S0 ; Выключить обдув",
            "G90 ; Абсолютные координаты X/Y/Z",
            "M83 ; Относительные координаты E",
            "G21 ; Миллиметры",
            "G28 ; Авто-дом X, Y, Z",
            f"G1 Z{z_up + 10.0:.2f} F{f_z}",
        ]

        if pause_start:
            lines.extend([
                f"G0 X90.000 Y90.000 Z{z_up + 10.0:.2f} F{f_travel}",
                "M400 U1 ; ПАУЗА: проверьте ручку/лист и нажмите Resume",
            ])

        lines.extend([
            "; CHANGE_LAYER",
            f"; Z_HEIGHT: {z_down:.2f}",
            "; LAYER_HEIGHT: 0.2",
            "G92 E0",
            "T0",
            "; FEATURE: Outer wall",
            "; LINE_WIDTH: 0.42",
            f"G1 Z{z_up:.2f} F{f_z} ; Поднять ручку на высоту Z-Hop",
        ])

        for poly in all_strokes:
            if len(poly) < 2:
                continue
            sx, sy = poly[0]
            # 1. Гарантированный вертикальный подъем перед переездом
            lines.append(f"G1 Z{z_up:.2f} F{f_z}")
            # 2. Переезд строго по воздуху на высоте z_up
            lines.append(f"G0 X{sx:.3f} Y{sy:.3f} Z{z_up:.2f} F{f_travel}")
            # 3. Вертикальное опускание ручки на бумагу в начальной точке штриха
            lines.append(f"G1 Z{z_down:.2f} F{f_z}")

            px, py = sx, sy
            for (nx, ny) in poly[1:]:
                seg_len = math.hypot(nx - px, ny - py)
                if seg_len > 1e-4:
                    e_val = seg_len * 0.033
                    lines.append(f"G1 X{nx:.3f} Y{ny:.3f} Z{z_down:.2f} E{e_val:.5f} F{f_draw}")
                    px, py = nx, ny

            # 4. Вертикальный отрыв ручки вверх сразу в конце штриха!
            lines.append(f"G1 Z{z_up:.2f} F{f_z}")

        lines.extend([
            "M73 P100 R0",
            f"G1 Z{z_up + 15.0:.2f} F{f_z}",
            "G0 X0.000 Y175.000 F6000 ; Подать стол вперед",
            "M400",
            "M104 S0",
            "M140 S0",
            "M84",
            "; EXECUTABLE_BLOCK_END",
        ])
        return "\n".join(lines) + "\n", est_seconds

    # ==========================================================================
    # ПАТЧЕР НАРЕЗАННЫХ ФАЙЛОВ ИЗ BAMBU STUDIO (.GCODE И .GCODE.3MF)
    # Вшивает подъем ручки Z-Hop на каждом холостом переезде G0!
    # ==========================================================================
    def _inject_zhop_into_bambu_gcode_text(self, gcode_text):
        z_down = max(0.10, self.vars["z_down"].get())
        z_up = max(z_down + 1.0, self.vars["z_up"].get())
        f_z = self.vars["speed_z"].get()

        src_lines = gcode_text.splitlines()
        out_lines = [
            "M412 S0 ; Отключить датчик филамента",
            "M104 S0 ; Сопло 0 C",
            "M140 S0 ; Стол 0 C",
        ]

        re_xy = re.compile(r"^(G0|G1)\s+.*?(X[0-9.+-]+).*?(Y[0-9.+-]+)", re.IGNORECASE)
        has_e = re.compile(r"\bE[0-9.+-]+", re.IGNORECASE)
        pen_is_up = True
        in_ printing_area = False

        for line in src_lines:
            stripped = line.strip()
            if stripped.startswith("; CHANGE_LAYER") or stripped.startswith("; FEATURE:"):
                in_printing_area = True

            # Обнуляем любые команды нагрева, которые Bambu Studio вставил сам
            if stripped.startswith(("M104 S", "M109 S", "M140 S", "M190 S")):
                out_lines.append(f"; [Отключен нагрев плоттером] {line}")
                continue

            if in_printing_area and stripped.startswith(("G0 ", "G1 ")):
                m = re_xy.match(stripped)
                if m:
                    is_extruding = bool(has_e.search(stripped)) and not stripped.startswith("G0")
                    if not is_extruding:
                        # Это холостое перемещение (Travel)! Поднимаем ручку перед ним:
                        if not pen_is_up:
                            out_lines.append(f"G1 Z{z_up:.2f} F{f_z} ; Z-Hop подъем ручки")
                            pen_is_up = True
                        out_lines.append(line)
                        continue
                    else:
                        # Это штрих письма! Если ручка была поднята — опускаем вертикально:
                        if pen_is_up:
                            out_lines.append(f"G1 Z{z_down:.2f} F{f_z} ; Z-Hop опускание ручки")
                            pen_is_up = False
                        out_lines.append(line)
                        continue

            out_lines.append(line)

        return "\n".join(out_lines) + "\n"

    def patch_sliced_bambu_file(self):
        path = filedialog.askopenfilename(
            title="Выберите нарезанный в Bambu Studio файл (.gcode.3mf или .gcode)",
            filetypes=[("Нарезанные файлы Bambu Studio (*.gcode.3mf;*.3mf;*.gcode)", "*.3mf *.gcode"), ("Все файлы", "*.*")]
        )
        if not path:
            return

        z_up = self.vars["z_up"].get()
        try:
            if path.lower().endswith(".3mf"):
                out_path = path[:-4] + "_ZHOP_PLOTTER.gcode.3mf"
                with zipfile.ZipFile(path, "r") as zin:
                    namelist = zin.namelist()
                    if "Metadata/plate_1.gcode" not in namelist:
                        messagebox.showerror(
                            "Файл еще не нарезан!",
                            "Этот .3mf файл не содержит нарезанного G-code (Metadata/plate_1.gcode).\n\n"
                            "В Bambu Studio сначала нажмите «Slice plate» (Нарезать), затем в правом верхнем углу "
                            "выберите «Export plate sliced file (.gcode.3mf)» и пропатчите его!"
                        )
                        return
                    raw_gcode = zin.read("Metadata/plate_1.gcode").decode("utf-8", errors="replace")
                    patched_gcode = self._inject_zhop_into_bambu_gcode_text(raw_gcode)
                    patched_bytes = patched_gcode.encode("utf-8")
                    new_md5 = hashlib.md5(patched_bytes).hexdigest()

                    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                        for item in zin.infolist():
                            if item.filename == "Metadata/plate_1.gcode":
                                zout.writestr(item, patched_bytes)
                            elif item.filename == "Metadata/plate_1.gcode.md5":
                                zout.writestr(item, new_md5)
                            else:
                                zout.writestr(item, zin.read(item.filename))
            else:
                out_path = path[:-6] + "_ZHOP_PLOTTER.gcode" if path.lower().endswith(".gcode") else path + "_ZHOP.gcode"
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    raw_gcode = f.read()
                patched_gcode = self._inject_zhop_into_bambu_gcode_text(raw_gcode)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(patched_gcode)

            messagebox.showinfo(
                "Подъем ручки (Z-Hop) успешно вшит!",
                f"Сохранен исправленный файл:\n{out_path}\n\n"
                f"• Перед каждым перемещением добавлен вертикальный подъем ручки на Z = {z_up:.1f} мм.\n"
                "• Полностью отключен нагрев стола и сопла.\n"
                "• Отключен датчик филамента (M412 S0)."
            )
        except Exception as e:
            messagebox.showerror("Ошибка патчинга", str(e))

    # ==========================================================================
    # ЭКСПОРТ 2 И 3: 3MF И STL (С УЧЕТОМ ПЕРЕМЕЩЕНИЯ, МАСШТАБА И СМЕЩЕНИЯ 33.5 ММ)
    # ==========================================================================
    def save_as_3mf_with_geometry(self, filepath, page_dict):
        all_strokes = self._get_final_nozzle_strokes(page_dict)
        vertices, triangles = strokes_to_3d_mesh(all_strokes, line_width=0.44, z_bottom=0.0, z_top=0.20)

        verts_xml = "\n".join(
            f'     <vertex x="{vx:.3f}" y="{vy:.3f}" z="{vz:.3f}"/>'
            for (vx, vy, vz) in vertices
        )
        tris_xml = "\n".join(
            f'     <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>'
            for (v1, v2, v3) in triangles
        )

        content_types_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

        rels_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

        model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel">
 <resources>
  <object id="1" name="Konspekt_Page" type="model">
   <mesh>
    <vertices>
{verts_xml}
    </vertices>
    <triangles>
{tris_xml}
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
 </build>
</model>"""

        with zipfile.ZipFile(filepath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types_xml)
            zf.writestr("_rels/.rels", rels_xml)
            zf.writestr("3D/3dmodel.model", model_xml)

    def save_as_binary_stl(self, filepath, page_dict):
        all_strokes = self._get_final_nozzle_strokes(page_dict)
        vertices, triangles = strokes_to_3d_mesh(all_strokes, line_width=0.44, z_bottom=0.0, z_top=0.20)

        bed_w = self.vars["bed_w"].get()
        bed_h = self.vars["bed_h"].get()
        corner_strokes = [
            [(0.1, 0.1), (0.5, 0.1)],
            [(bed_w - 0.5, 0.1), (bed_w - 0.1, 0.1)],
            [(0.1, bed_h - 0.1), (0.5, bed_h - 0.1)],
            [(bed_w - 0.5, bed_h - 0.1), (bed_w - 0.1, bed_h - 0.1)],
        ]
        c_verts, c_tris = strokes_to_3d_mesh(corner_strokes, line_width=0.44, z_bottom=0.0, z_top=0.20)
        offset = len(vertices)
        vertices.extend(c_verts)
        triangles.extend([(a + offset, b + offset, c + offset) for (a, b, c) in c_tris])

        with open(filepath, "wb") as f:
            header = b"Bambu Lab A1 mini Plotter STL" + b" " * 51
            f.write(header[:80])
            f.write(struct.pack("<I", len(triangles)))
            for (i1, i2, i3) in triangles:
                v1 = vertices[i1]
                v2 = vertices[i2]
                v3 = vertices[i3]
                f.write(struct.pack(
                    "<12fH",
                    0.0, 0.0, 1.0,
                    v1[0], v1[1], v1[2],
                    v2[0], v2[1], v2[2],
                    v3[0], v3[1], v3[2],
                    0
                ))

    def export_files(self, mode="3mf"):
        self.update_preview()
        if not self.pages_data:
            messagebox.showwarning("Пусто", "Введите текст или таблицу для экспорта.")
            return

        if mode == "3mf":
            ext = ".3mf"
            ftypes = [("3MF 3D Model (*.3mf)", "*.3mf")]
        elif mode == "stl":
            ext = ".stl"
            ftypes = [("STL 3D Model (*.stl)", "*.stl")]
        else:
            ext = ".gcode"
            ftypes = [("Bambu Lab G-code (*.gcode)", "*.gcode")]

        if len(self.pages_data) == 1:
            path = filedialog.asksaveasfilename(
                defaultextension=ext,
                filetypes=ftypes,
                initialfile=f"konspekt_page_1{ext}"
            )
            if not path:
                return
            self._save_single_page(path, self.pages_data[0], mode)
            self._show_post_save_msg(path, mode)
        else:
            path = filedialog.asksaveasfilename(
                defaultextension=ext,
                filetypes=ftypes,
                initialfile=f"konspekt{ext}",
                title=f"Сохранить {len(self.pages_data)} страниц"
            )
            if not path:
                return
            base = path[:-len(ext)] if path.lower().endswith(ext) else path
            saved = []
            for idx, page_dict in enumerate(self.pages_data, start=1):
                out_p = f"{base}_page_{idx}{ext}"
                self._save_single_page(out_p, page_dict, mode)
                saved.append(out_p)
            self._show_post_save_msg(saved[0], mode, count=len(saved))

    def _save_single_page(self, path, page_dict, mode):
        if mode == "3mf":
            self.save_as_3mf_with_geometry(path, page_dict)
        elif mode == "stl":
            self.save_as_binary_stl(path, page_dict)
        else:
            gcode_str, _ = self.build_bambu_gcode(page_dict)
            with open(path, "w", encoding="utf-8") as f:
                f.write(gcode_str)

    def _show_post_save_msg(self, path, mode, count=1):
        dx, dy = self.get_pen_offset_xy()
        z_up = self.vars["z_up"].get()
        prefix = f"Сохранено файлов: {count}\nПервый: {path}" if count > 1 else f"Файл сохранен:\n{path}"
        if mode in ("3mf", "stl"):
            messagebox.showinfo(
                "3D-модель сохранена!",
                f"{prefix}\n\n"
                f"• Смещение ручки учтено: dX={dx:+.1f}, dY={dy:+.1f} мм.\n"
                f"• ВАЖНО: чтобы Bambu Studio при нарезке сам поднимал ручку на {z_up:.1f} мм, "
                f"в настройках принтера (вкладка Extruder) поставьте:\n"
                f"  1) Z hop when retract = {z_up:.1f} мм\n"
                f"  2) Z hop type = Normal\n"
                f"  3) Travel distance threshold = 0.1 мм"
            )
        else:
            messagebox.showinfo(
                "Готовый G-code с Z-Hop сохранен!",
                f"{prefix}\n\n"
                f"В этот .gcode уже жестко вшит вертикальный подъем ручки на {z_up:.1f} мм после каждого штриха!"
            )

    def show_bambu_studio_instructions(self):
        z_up = self.vars["z_up"].get()
        start_gcode = (
            "; === START G-CODE ПЛОТТЕРА A1 MINI ===\n"
            "M412 S0 ; Отключить проверку филамента\n"
            "M104 S0 ; Выключить нагрев сопла\n"
            "M140 S0 ; Выключить нагрев стола\n"
            "M106 S0 ; Выключить вентилятор\n"
            "G90\n"
            "G21\n"
            "G28 ; Авто-дом\n"
            f"G0 X90 Y90 Z{z_up + 10:.1f} F6000\n"
            "M400 U1 ; Пауза для установки ручки\n"
            f"G1 Z{z_up:.2f} F1800\n"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(start_gcode)
        messagebox.showinfo(
            "Start G-code скопирован!",
            "Start G-code скопирован в буфер обмена (Ctrl+V).\n\n"
            f"Не забудьте во вкладке «Extruder» в Bambu Studio поставить:\n"
            f"• Z hop when retract = {z_up:.1f} mm\n"
            "• Z hop type = Normal\n"
            "• Travel distance threshold = 0.1 mm"
        )


if __name__ == "__main__":
    root = tk.Tk()
    app = A1MiniPlotterApp(root)
    root.mainloop()