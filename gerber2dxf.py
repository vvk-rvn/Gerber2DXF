#!/usr/bin/env python3
"""Gerber2DXF — конвертер Gerber (RS-274X / X2) и Excellon в DXF (мм).

Запуск без аргументов           -> окно программы.
gerber2dxf.py input.zip [-o out] -> консольный режим.

Вход: .zip (в т.ч. вложенные .zip), папка или отдельные файлы.
Каждый набор Gerber (папка) -> своя подпапка вывода:
    <слой>.dxf        — каждый слой отдельно
    ALL_LAYERS.dxf    — все слои в одном файле, DXF-слой = имя файла

Геометрия:
    дорожки  -> LWPOLYLINE по оси, ширина = диаметр апертуры
    площадки -> CIRCLE / замкнутые контуры точной формы (вкл. макро-апертуры)
    полигоны -> замкнутые LWPOLYLINE
    отверстия-> CIRCLE диаметром сверла, пазы -> контур овала
    негативные объекты (%LPC%, exposure off) -> слой <имя>_CLEAR
"""
import argparse
import hashlib
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

try:
    import ezdxf
except ImportError:  # pragma: no cover
    ezdxf = None

VERSION = "1.0"
SKIP_EXT = {".gbrjob", ".json", ".ddw", ".tgz", ".gz", ".tar", ".pdf", ".png", ".jpg",
            ".jpeg", ".bmp", ".step", ".stp", ".kicad_pcb", ".kicad_pro", ".csv", ".xlsx",
            ".xls", ".html", ".dxf", ".exe", ".dll", ".rar", ".7z"}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def layer_color(name):
    """AutoCAD color index by layer name (JLC short names and KiCad names)."""
    n = name.lower()
    jlc = {"tl": 1, "bl": 5, "to": 7, "bo": 8, "ts": 3, "bs": 4, "drl": 2, "ko": 6,
           "vcut": 30, "sk": 40, "gtp": 9, "gbp": 9, "npth": 30, "gko": 6, "gm1": 6}
    if n in jlc:
        return jlc[n]
    rules = [("npth", 30), ("pth", 2), ("drl", 2), ("drill", 2), ("edge", 6), ("outline", 6),
             ("cuts", 6), ("f_cu", 1), ("f.cu", 1), ("top_cu", 1), ("b_cu", 5), ("b.cu", 5),
             ("in", 150), ("mask", 3), ("silk", 7), ("paste", 9), ("fab", 252), ("user", 252)]
    for key, col in rules:
        if key in n:
            if key == "mask" and ("b_" in n or "b." in n or "bot" in n):
                return 4
            if key == "silk" and ("b_" in n or "b." in n or "bot" in n):
                return 8
            return col
    ext = {".gtl": 1, ".gbl": 5, ".gts": 3, ".gbs": 4, ".gto": 7, ".gbo": 8, ".gko": 6,
           ".gm1": 6, ".gtp": 9, ".gbp": 9, ".drl": 2, ".xln": 2}
    return ext.get(Path(n).suffix, 7)


def safe_layer(name):
    name = re.sub(r'[<>/\\":;?*|=\'`,]', "_", name).strip() or "layer"
    return name[:200]


class Shapes:
    """Collects geometry once; written to one or more DXF docs later."""

    def __init__(self, base_layer):
        self.base = base_layer
        self.items = []  # (kind, layer_suffix, data...)
        self.stats = {"tracks": 0, "flashes": 0, "regions": 0, "holes": 0, "slots": 0}

    def circle(self, clear, c, r):
        self.items.append(("c", clear, c, r))

    def poly(self, clear, pts, closed, width=0.0, bulge=False):
        self.items.append(("p", clear, pts, closed, width, bulge))

    def emit(self, doc, layer=None):
        msp = doc.modelspace()
        layer = layer or self.base
        names = {False: layer, True: layer + "_CLEAR"}
        for clear in (False, True):
            if any(it[1] == clear for it in self.items) and names[clear] not in doc.layers:
                doc.layers.add(names[clear], color=9 if clear else layer_color(self.base))
        for it in self.items:
            attrs = {"layer": names[it[1]]}
            if it[0] == "c":
                msp.add_circle(it[2], it[3], dxfattribs=attrs)
            else:
                _, _, pts, closed, width, bulge = it
                if bulge:
                    pl = msp.add_lwpolyline([(x, y, 0, 0, b) for x, y, b in pts], format="xyseb",
                                            close=closed, dxfattribs=attrs)
                else:
                    pl = msp.add_lwpolyline(pts, close=closed, dxfattribs=attrs)
                if width:
                    pl.dxf.const_width = width


def obround(x1, y1, x2, y2, d):
    """Closed slot outline (x, y, bulge) between centres p1 and p2."""
    r = d / 2
    L = math.hypot(x2 - x1, y2 - y1)
    if L < 1e-9:
        return None
    ux, uy = (x2 - x1) / L, (y2 - y1) / L
    nx, ny = -uy * r, ux * r
    return [(x1 + nx, y1 + ny, 0), (x2 + nx, y2 + ny, -1),
            (x2 - nx, y2 - ny, 0), (x1 - nx, y1 - ny, -1)]


_EXPR_OK = re.compile(r"^[0-9.eE+\-*/() ]*$")


def macro_eval(expr, var):
    expr = re.sub(r"\$(\d+)", lambda m: f"({var.get(int(m.group(1)), 0.0)!r})", expr)
    expr = expr.replace("x", "*").replace("X", "*").strip()
    if not expr:
        return 0.0
    if not _EXPR_OK.match(expr):
        raise ValueError(f"bad macro expression: {expr}")
    return float(eval(expr, {"__builtins__": {}}, {}))  # only digits/operators allowed


# ----------------------------------------------------------------------------
# Gerber RS-274X
# ----------------------------------------------------------------------------
TOKEN_RE = re.compile(r"%[^%]*%|[^*%]*\*")
WORD_RE = re.compile(r"([GDXYIJM])([+-]?[\d.]+)")


class Gerber:
    def __init__(self, shapes, log):
        self.s = shapes
        self.log = log
        self.unit = 25.4
        self.int_d, self.dec = 2, 6
        self.trailing = False
        self.incremental = False
        self.apertures = {}
        self.macros = {}
        self.ap = None
        self.x = self.y = 0.0
        self.interp = 1
        self.multi_quadrant = True
        self.clear = False
        self.region = False
        self.path = []
        self.warned = set()

    def warn(self, msg):
        if msg not in self.warned:
            self.warned.add(msg)
            self.log(f"    ! {self.s.base}: {msg}")

    def coord(self, s):
        if "." in s:
            return float(s) * self.unit
        sign = -1 if s.startswith("-") else 1
        d = s.lstrip("+-")
        if self.trailing:
            d = d.ljust(self.int_d + self.dec, "0")
        return sign * int(d) / 10 ** self.dec * self.unit

    # geometry ------------------------------------------------------------
    def flush(self):
        if len(self.path) >= 2:
            if self.region:
                self.s.poly(self.clear, self.path, True, bulge=True)
                self.s.stats["regions"] += 1
            else:
                self.s.poly(self.clear, self.path, False, self.ap_width(), bulge=True)
                self.s.stats["tracks"] += 1
        self.path = []

    def ap_width(self):
        a = self.apertures.get(self.ap)
        if not a:
            return 0.0
        if a[0] in ("C", "P"):
            return a[1]
        if a[0] in ("R", "O"):
            return min(a[1], a[2])
        return 0.0

    def flash(self, x, y):
        a = self.apertures.get(self.ap)
        if not a:
            self.warn(f"flash with undefined aperture D{self.ap}")
            return
        cl = self.clear
        kind = a[0]
        if kind == "C" or (kind == "O" and abs(a[1] - a[2]) < 1e-9):
            self.s.circle(cl, (x, y), a[1] / 2)
        elif kind == "O":
            w, h = a[1] / 2, a[2] / 2
            if w > h:
                pts = obround(x - w + h, y, x + w - h, y, 2 * h)
            else:
                pts = obround(x, y - h + w, x, y + h - w, 2 * w)
            self.s.poly(cl, pts, True, bulge=True)
        elif kind == "R":
            w, h = a[1] / 2, a[2] / 2
            self.s.poly(cl, [(x - w, y - h), (x + w, y - h), (x + w, y + h), (x - w, y + h)], True)
        elif kind == "P":
            r, n, rot = a[1] / 2, int(a[2]), math.radians(a[3])
            self.s.poly(cl, [(x + r * math.cos(rot + 2 * math.pi * k / n),
                              y + r * math.sin(rot + 2 * math.pi * k / n)) for k in range(n)], True)
        elif kind == "M":
            self.flash_macro(a[1], a[2], x, y)
        self.s.stats["flashes"] += 1

    def flash_macro(self, name, args, x, y):
        var = {i + 1: v for i, v in enumerate(args)}
        u = self.unit

        def put(exp, pts_local, rot):
            c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            pts = [(x + (px * c - py * s) * u, y + (px * s + py * c) * u) for px, py in pts_local]
            if len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
                pts.pop()
            if len(pts) >= 2:
                self.s.poly(self.clear != (exp == 0), pts, True)

        for stmt in self.macros.get(name, []):
            stmt = stmt.strip()
            if not stmt or stmt.startswith("0"):
                continue
            m = re.match(r"\$(\d+)\s*=\s*(.+)$", stmt)
            try:
                if m:
                    var[int(m.group(1))] = macro_eval(m.group(2), var)
                    continue
                p = [macro_eval(f, var) for f in stmt.split(",")]
            except (ValueError, SyntaxError, ZeroDivisionError) as e:
                self.warn(f"macro {name}: {e}")
                continue
            code = int(p[0])
            if code == 1:  # circle: exp, diam, cx, cy, [rot]
                rot = math.radians(p[5]) if len(p) > 5 else 0.0
                cx, cy = p[3], p[4]
                cx, cy = cx * math.cos(rot) - cy * math.sin(rot), cx * math.sin(rot) + cy * math.cos(rot)
                self.s.circle(self.clear != (p[1] == 0), (x + cx * u, y + cy * u), p[2] * u / 2)
            elif code == 4:  # outline: exp, n, x0, y0 ... xn, yn, rot
                n = int(p[2])
                co = p[3:3 + 2 * (n + 1)]
                rot = p[3 + 2 * (n + 1)] if len(p) > 3 + 2 * (n + 1) else 0.0
                put(p[1], list(zip(co[0::2], co[1::2])), rot)
            elif code in (2, 20):  # vector line: exp, width, xs, ys, xe, ye, rot
                w, xs, ys, xe, ye = p[2], p[3], p[4], p[5], p[6]
                rot = p[7] if len(p) > 7 else 0.0
                L = math.hypot(xe - xs, ye - ys) or 1e-12
                nx, ny = -(ye - ys) / L * w / 2, (xe - xs) / L * w / 2
                put(p[1], [(xs + nx, ys + ny), (xe + nx, ye + ny), (xe - nx, ye - ny), (xs - nx, ys - ny)], rot)
            elif code == 21:  # center line: exp, w, h, cx, cy, rot
                w, h, cx, cy = p[2] / 2, p[3] / 2, p[4], p[5]
                rot = p[6] if len(p) > 6 else 0.0
                put(p[1], [(cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h)], rot)
            elif code == 22:  # lower-left line (deprecated): exp, w, h, x, y, rot
                w, h, lx, ly = p[2], p[3], p[4], p[5]
                rot = p[6] if len(p) > 6 else 0.0
                put(p[1], [(lx, ly), (lx + w, ly), (lx + w, ly + h), (lx, ly + h)], rot)
            elif code == 5:  # polygon: exp, n, cx, cy, diam, rot
                n, cx, cy, r = int(p[2]), p[3], p[4], p[5] / 2
                rot = p[6] if len(p) > 6 else 0.0
                put(p[1], [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
                           for k in range(n)], rot)
            elif code == 7:  # thermal (approx.: outer + inner circle)
                cx, cy = p[1], p[2]
                self.s.circle(self.clear, (x + cx * u, y + cy * u), p[3] * u / 2)
                self.s.circle(self.clear, (x + cx * u, y + cy * u), p[4] * u / 2)
                self.warn("thermal macro approximated by two circles")
            else:
                self.warn(f"macro primitive {code} not supported")

    def arc(self, x0, y0, x1, y1, i, j):
        cx, cy = x0 + i, y0 + j
        if not self.multi_quadrant:
            best = None
            for sx in (1, -1):
                for sy in (1, -1):
                    c = (x0 + sx * abs(i), y0 + sy * abs(j))
                    err = abs(math.dist(c, (x0, y0)) - math.dist(c, (x1, y1)))
                    if best is None or err < best[0]:
                        best = (err, c)
            cx, cy = best[1]
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        if self.interp == 3:
            sweep = (a1 - a0) % (2 * math.pi)
            if sweep < 1e-9 and self.multi_quadrant:
                sweep = 2 * math.pi
        else:
            sweep = -((a0 - a1) % (2 * math.pi))
            if sweep > -1e-9 and self.multi_quadrant:
                sweep = -2 * math.pi
        if abs(sweep) > math.pi * 1.5:
            r = math.dist((cx, cy), (x0, y0))
            am = a0 + sweep / 2
            b = math.tan(sweep / 8)
            return [(b, (cx + r * math.cos(am), cy + r * math.sin(am))), (b, (x1, y1))]
        return [(math.tan(sweep / 4), (x1, y1))]

    # parser --------------------------------------------------------------
    def run(self, text):
        text = text.replace("\r", "").replace("\n", "")
        for tok in TOKEN_RE.findall(text):
            if tok.startswith("%"):
                self.extended(tok.strip("%"))
            else:
                self.statement(tok.rstrip("*"))
        self.flush()

    def extended(self, body):
        if body.startswith("AM"):
            parts = body.split("*")
            self.macros[parts[0][2:]] = [p for p in parts[1:] if p.strip()]
            return
        for cmd in body.split("*"):
            if not cmd:
                continue
            if cmd.startswith("FS"):
                m = re.search(r"FS([LTD]?)([AI]?)X(\d)(\d)Y(\d)(\d)", cmd)
                if m:
                    self.trailing = m.group(1) == "T"
                    self.incremental = m.group(2) == "I"
                    self.int_d, self.dec = int(m.group(3)), int(m.group(4))
            elif cmd.startswith("MO"):
                self.unit = 25.4 if cmd[2:4] == "IN" else 1.0
            elif cmd.startswith("AD"):
                m = re.match(r"ADD(\d+)([A-Za-z_$.][A-Za-z0-9_$.\-]*),?(.*)", cmd)
                if not m:
                    self.warn(f"bad aperture {cmd}")
                    continue
                code, shape, params = int(m.group(1)), m.group(2), m.group(3)
                raw = [float(v) for v in params.split("X") if v.strip()]
                if shape == "C":
                    self.apertures[code] = ("C", raw[0] * self.unit)
                elif shape in ("R", "O"):
                    self.apertures[code] = (shape, raw[0] * self.unit, raw[1] * self.unit)
                elif shape == "P":
                    self.apertures[code] = ("P", raw[0] * self.unit, raw[1], raw[2] if len(raw) > 2 else 0.0)
                elif shape in self.macros:
                    self.apertures[code] = ("M", shape, raw)
                else:
                    self.warn(f"unknown aperture type {shape}")
            elif cmd.startswith("LP"):
                self.flush()
                self.clear = cmd[2:3] == "C"
            elif cmd.startswith(("SR", "AB", "LM", "LR", "LS", "IP", "IR", "OF", "SF", "MI")):
                if cmd not in ("SR", "IPPOS", "LMN", "LR0", "LS1", "LS1.0"):
                    self.warn(f"command %{cmd}% ignored")

    def statement(self, st):
        st = st.strip()
        if not st or st.startswith("G04") or st.startswith("G4 "):
            return
        coords, op = {}, None
        for letter, val in WORD_RE.findall(st):
            if letter == "G":
                g = int(float(val))
                if g in (1, 2, 3):
                    self.interp = g
                elif g == 36:
                    self.flush()
                    self.region = True
                elif g == 37:
                    self.flush()
                    self.region = False
                elif g == 74:
                    self.multi_quadrant = False
                elif g == 75:
                    self.multi_quadrant = True
                elif g == 91:
                    self.incremental = True
                elif g == 90:
                    self.incremental = False
            elif letter == "D":
                d = int(float(val))
                if d >= 10:
                    if d != self.ap:
                        self.flush()
                    self.ap = d
                else:
                    op = d
            elif letter == "M":
                return
            else:
                coords[letter] = self.coord(val)
        if op is None:
            if "X" in coords or "Y" in coords:
                op = 1
            else:
                return
        if self.incremental:
            nx, ny = self.x + coords.get("X", 0.0), self.y + coords.get("Y", 0.0)
        else:
            nx, ny = coords.get("X", self.x), coords.get("Y", self.y)
        if op == 2:
            self.flush()
        elif op == 3:
            self.flush()
            self.flash(nx, ny)
        elif op == 1:
            if not self.path:
                self.path = [(self.x, self.y, 0.0)]
            if self.interp == 1:
                self.path.append((nx, ny, 0.0))
            else:
                for b, (px, py) in self.arc(self.x, self.y, nx, ny, coords.get("I", 0.0), coords.get("J", 0.0)):
                    lx, ly, _ = self.path[-1]
                    self.path[-1] = (lx, ly, b)
                    self.path.append((px, py, 0.0))
        self.x, self.y = nx, ny


# ----------------------------------------------------------------------------
# Excellon drill
# ----------------------------------------------------------------------------
EXC_WORD = re.compile(r"([GMTXYCSF])([+-]?[\d.]*)")


class Excellon:
    def __init__(self, shapes, log):
        self.s = shapes
        self.log = log
        self.unit = 25.4
        self.int_d, self.dec = 2, 4
        self.lz = True  # leading zeros present -> trailing suppressed
        self.fmt_set = False
        self.tools = {}
        self.tool = None
        self.x = self.y = 0.0
        self.incremental = False
        self.mode = 5  # 5 drill, 0 move, 1 linear route
        self.down = False
        self.route = []

    def num(self, s):
        if "." in s:
            return float(s) * self.unit
        sign = -1 if s.startswith("-") else 1
        d = s.lstrip("+-")
        if self.lz:
            d = d.ljust(self.int_d + self.dec, "0")
        return sign * int(d) / 10 ** self.dec * self.unit

    def dia(self):
        return self.tools.get(self.tool, 0.0)

    def flush_route(self):
        if len(self.route) >= 2:
            self.s.poly(False, self.route, False, self.dia())
            self.s.stats["slots"] += 1
        self.route = []

    def hole(self, x, y):
        self.s.circle(False, (x, y), self.dia() / 2)
        self.s.stats["holes"] += 1

    def slot(self, x1, y1, x2, y2):
        pts = obround(x1, y1, x2, y2, self.dia())
        if pts:
            self.s.poly(False, pts, True, bulge=True)
        else:
            self.s.circle(False, (x1, y1), self.dia() / 2)
        self.s.stats["slots"] += 1

    def xy(self, line):
        xs = re.search(r"X([+-]?[\d.]+)", line)
        ys = re.search(r"Y([+-]?[\d.]+)", line)
        if self.incremental:
            return (self.x + (self.num(xs.group(1)) if xs else 0.0),
                    self.y + (self.num(ys.group(1)) if ys else 0.0))
        return (self.num(xs.group(1)) if xs else self.x, self.num(ys.group(1)) if ys else self.y)

    def run(self, text):
        header = False
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            up = line.upper()
            if line.startswith(";"):
                m = re.search(r"FILE_FORMAT\s*=\s*(\d+):(\d+)", line)
                if m:
                    self.int_d, self.dec = int(m.group(1)), int(m.group(2))
                    self.fmt_set = True
                continue
            if up == "M48":
                header = True
                continue
            if up in ("%", "M95"):
                header = False
                continue
            if up.startswith(("METRIC", "M71")) or up.startswith(("INCH", "M72")):
                metric = up.startswith(("METRIC", "M71"))
                self.unit = 1.0 if metric else 25.4
                if not self.fmt_set:
                    self.int_d, self.dec = (3, 3) if metric else (2, 4)
                if ",TZ" in up:
                    self.lz = False  # trailing zeros present -> leading suppressed
                elif ",LZ" in up:
                    self.lz = True
                m = re.search(r"(0+)\.(0+)", up)
                if m:
                    self.int_d, self.dec = len(m.group(1)), len(m.group(2))
                    self.fmt_set = True
                continue
            m = re.match(r"T(\d+)(.*)", up)
            if m:
                t = int(m.group(1))
                c = re.search(r"C([\d.]+)", m.group(2))
                if c:
                    self.tools[t] = float(c.group(1)) * self.unit
                if not header or not c:
                    self.flush_route()
                    self.tool = t
                continue
            if header:
                continue
            if up.startswith("M30") or up.startswith("M00"):
                break
            if up.startswith("G90"):
                self.incremental = False
                continue
            if up.startswith("G91"):
                self.incremental = True
                continue
            if up.startswith("M15"):
                self.down = True
                self.route = [(self.x, self.y)]
                continue
            if up.startswith(("M16", "M17")):
                self.down = False
                self.flush_route()
                continue
            if "G85" in up:
                a, b = up.split("G85", 1)
                x1, y1 = self.xy(a)
                self.x, self.y = x1, y1
                x2, y2 = self.xy(b)
                self.slot(x1, y1, x2, y2)
                self.x, self.y = x2, y2
                continue
            g = re.match(r"G0?([0-5])", up)
            if g:
                self.mode = {0: 0, 1: 1, 2: 1, 3: 1, 5: 5}.get(int(g.group(1)), self.mode)
            if "X" in up or "Y" in up:
                nx, ny = self.xy(up)
                if self.mode == 5:
                    self.hole(nx, ny)
                elif self.mode == 1 and self.down:
                    if not self.route:
                        self.route = [(self.x, self.y)]
                    self.route.append((nx, ny))
                self.x, self.y = nx, ny
        self.flush_route()


# ----------------------------------------------------------------------------
# input handling
# ----------------------------------------------------------------------------
def classify(path):
    if path.suffix.lower() in SKIP_EXT:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    if b"\x00" in head[:4096]:
        return None
    text = head.decode("latin-1")
    if re.search(r"^\s*M48\s*$", text, re.M) or (re.search(r"^T\d+C[\d.]+", text, re.M) and "%FS" not in text):
        return "excellon"
    if "%FS" in text or "%MO" in text:
        return "gerber"
    return None


def extract_all(src, dest, log):
    """Extract a zip (recursively) into dest. Identical nested zips are skipped."""
    seen = set()

    def unpack(zpath, target):
        h = hashlib.sha1(Path(zpath).read_bytes()).hexdigest()
        if h in seen:
            log(f"  (дубликат архива пропущен: {Path(zpath).name})")
            return
        seen.add(h)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(target)
        for inner in sorted(Path(target).rglob("*.zip")):
            if inner.is_file():
                sub = inner.with_suffix("")
                if sub.exists():
                    sub = inner.parent / (inner.stem + "_zip")
                unpack(inner, sub)
                inner.unlink()

    unpack(src, dest)


def strip_common_prefix(stems):
    if len(stems) < 2:
        return {s: s for s in stems}
    pre = os.path.commonprefix(stems)
    cut = max(pre.rfind("-"), pre.rfind("_") if "-" not in pre else -1)
    pre = pre[:cut + 1] if cut >= 0 else ""
    return {s: (s[len(pre):].strip("-_. ") or s) for s in stems}


def convert_group(files, out_dir, log, per_layer=True, combined=True):
    out_dir.mkdir(parents=True, exist_ok=True)
    stems = [f.stem if f.suffix.lower() not in ("", ) else f.name for f in files]
    short = strip_common_prefix(stems)
    used, results = set(), []
    for f, stem in zip(files, stems):
        name = safe_layer(short[stem])
        if name.lower() in used:
            name = safe_layer(f"{name}{f.suffix.replace('.', '_')}")
        used.add(name.lower())
        kind = classify(f)
        shapes = Shapes(name)
        text = f.read_text(encoding="latin-1")
        try:
            (Gerber if kind == "gerber" else Excellon)(shapes, log).run(text)
        except Exception as e:  # keep going with other layers
            log(f"    ОШИБКА в {f.name}: {e}")
            continue
        st = shapes.stats
        if kind == "gerber":
            log(f"    {name:<22} Gerber   дорожек {st['tracks']:>6}  площадок {st['flashes']:>5}  полигонов {st['regions']:>5}")
        else:
            log(f"    {name:<22} Excellon отверстий {st['holes']:>5}  пазов {st['slots']:>4}")
        results.append(shapes)
        if per_layer:
            doc = ezdxf.new("R2010", setup=True, units=4)
            shapes.emit(doc)
            doc.saveas(out_dir / f"{name}.dxf")
    if combined and results:
        doc = ezdxf.new("R2010", setup=True, units=4)
        for shapes in results:
            shapes.emit(doc)
        doc.saveas(out_dir / "ALL_LAYERS.dxf")
    return len(results)


def convert(inputs, out_root, log=print, per_layer=True, combined=True):
    if ezdxf is None:
        raise RuntimeError("Не установлена библиотека ezdxf: python -m pip install ezdxf")
    total = 0
    for inp in inputs:
        inp = Path(inp)
        log(f"Вход: {inp}")
        tmp = None
        try:
            if inp.is_file() and inp.suffix.lower() == ".zip":
                tmp = Path(tempfile.mkdtemp(prefix="gerber2dxf_"))
                extract_all(inp, tmp, log)
                root = tmp
            elif inp.is_dir():
                root = inp
            elif inp.is_file():
                root = None
            else:
                log("  не найден")
                continue
            files = [inp] if root is None else [p for p in sorted(root.rglob("*")) if p.is_file()]
            groups = {}
            for p in files:
                if classify(p):
                    groups.setdefault(p.parent, []).append(p)
            if not groups:
                log("  Gerber/Excellon файлов не найдено")
                continue
            base = out_root if len(inputs) == 1 else out_root / safe_layer(inp.stem)
            for folder, gfiles in sorted(groups.items()):
                rel = Path(".") if root is None or folder == root else folder.relative_to(root)
                out_dir = base / rel
                log(f"  Набор: {rel.as_posix() if str(rel) != '.' else '(корень)'}  ->  {out_dir}")
                total += convert_group(gfiles, out_dir, log, per_layer, combined)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    log(f"Готово: слоёв конвертировано {total}.")
    return total


def default_out(inp):
    p = Path(inp)
    return p.parent / (p.stem + "_dxf")


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
def run_gui(prefill=None):
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"Gerber → DXF  v{VERSION}")
    root.geometry("820x560")
    root.minsize(640, 420)
    q = queue.Queue()
    in_var, out_var = tk.StringVar(), tk.StringVar()
    per_var, all_var = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
    out_touched = {"v": False}

    def set_input(path):
        in_var.set(path)
        if path and not out_touched["v"]:
            out_var.set(str(default_out(path)))

    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    ttk.Label(frm, text="Вход (zip или папка):").grid(row=0, column=0, sticky="w")
    ttk.Entry(frm, textvariable=in_var).grid(row=0, column=1, sticky="ew", padx=5)
    bf = ttk.Frame(frm)
    bf.grid(row=0, column=2)
    ttk.Button(bf, text="Zip…", width=7, command=lambda: set_input(filedialog.askopenfilename(
        title="Архив Gerber", filetypes=[("Zip", "*.zip"), ("Все файлы", "*.*")]) or in_var.get())).pack(side="left")
    ttk.Button(bf, text="Папка…", width=7, command=lambda: set_input(filedialog.askdirectory(
        title="Папка с Gerber") or in_var.get())).pack(side="left", padx=(3, 0))

    ttk.Label(frm, text="Папка вывода:").grid(row=1, column=0, sticky="w", pady=6)
    ttk.Entry(frm, textvariable=out_var).grid(row=1, column=1, sticky="ew", padx=5)

    def pick_out():
        d = filedialog.askdirectory(title="Куда сохранить DXF")
        if d:
            out_touched["v"] = True
            out_var.set(d)
    ttk.Button(frm, text="Обзор…", width=15, command=pick_out).grid(row=1, column=2)

    opt = ttk.Frame(frm)
    opt.grid(row=2, column=0, columnspan=3, sticky="w")
    ttk.Checkbutton(opt, text="DXF на каждый слой", variable=per_var).pack(side="left")
    ttk.Checkbutton(opt, text="Общий ALL_LAYERS.dxf", variable=all_var).pack(side="left", padx=15)

    act = ttk.Frame(frm)
    act.grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
    go = ttk.Button(act, text="Конвертировать")
    go.pack(side="left")
    open_btn = ttk.Button(act, text="Открыть папку вывода", state="disabled")
    open_btn.pack(side="left", padx=8)
    bar = ttk.Progressbar(act, mode="indeterminate")
    bar.pack(side="left", fill="x", expand=True)

    logbox = tk.Text(frm, height=18, wrap="none", font=("Consolas", 9))
    logbox.grid(row=4, column=0, columnspan=3, sticky="nsew")
    sb = ttk.Scrollbar(frm, command=logbox.yview)
    sb.grid(row=4, column=3, sticky="ns")
    logbox.configure(yscrollcommand=sb.set)
    frm.rowconfigure(4, weight=1)

    def log(msg):
        q.put(("log", msg))

    def worker(inp, out, per, comb):
        try:
            n = convert([inp], Path(out), log, per, comb)
            q.put(("done", n))
        except Exception as e:  # noqa
            q.put(("error", str(e)))

    def start():
        inp, out = in_var.get().strip().strip('"'), out_var.get().strip().strip('"')
        if not inp or not Path(inp).exists():
            messagebox.showwarning("Gerber → DXF", "Выберите zip-архив или папку с Gerber.")
            return
        if not out:
            out = str(default_out(inp))
            out_var.set(out)
        if not (per_var.get() or all_var.get()):
            messagebox.showwarning("Gerber → DXF", "Отметьте хотя бы один вариант вывода.")
            return
        logbox.delete("1.0", "end")
        go.configure(state="disabled")
        open_btn.configure(state="disabled")
        bar.start(12)
        threading.Thread(target=worker, args=(inp, out, per_var.get(), all_var.get()), daemon=True).start()

    def open_out():
        p = out_var.get()
        if sys.platform.startswith("win"):
            os.startfile(p)  # noqa
        else:
            import subprocess
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])

    go.configure(command=start)
    open_btn.configure(command=open_out)

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.insert("end", val + "\n")
                    logbox.see("end")
                else:
                    bar.stop()
                    go.configure(state="normal")
                    if kind == "done" and val:
                        open_btn.configure(state="normal")
                    elif kind == "error":
                        logbox.insert("end", "ОШИБКА: " + val + "\n")
                        messagebox.showerror("Gerber → DXF", val)
        except queue.Empty:
            pass
        root.after(100, poll)

    if prefill:
        set_input(prefill)
    poll()
    root.mainloop()


# ----------------------------------------------------------------------------
def main():
    has_out = any(a in ("-o", "--output") or a.startswith("--output=") for a in sys.argv[1:])
    # no args -> GUI; windowed exe with dropped file (no -o) -> GUI prefilled
    gui_only = len(sys.argv) == 1 or (sys.stdout is None and not has_out)
    if gui_only:
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        run_gui(args[0] if args else None)
        return
    ap = argparse.ArgumentParser(description="Gerber/Excellon -> DXF (мм)")
    ap.add_argument("inputs", nargs="+", help="zip-архив(ы), папки или файлы Gerber")
    ap.add_argument("-o", "--output", help="папка вывода (по умолчанию <вход>_dxf рядом со входом)")
    ap.add_argument("--no-layers", action="store_true", help="не писать отдельные DXF по слоям")
    ap.add_argument("--no-combined", action="store_true", help="не писать ALL_LAYERS.dxf")
    a = ap.parse_args()
    out = Path(a.output) if a.output else default_out(a.inputs[0])
    if sys.stdout is None:  # windowed exe: write the log next to the results
        out.mkdir(parents=True, exist_ok=True)
        logf = open(out / "gerber2dxf_log.txt", "w", encoding="utf-8")

        def log(msg):
            logf.write(msg + "\n")
            logf.flush()
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        log = print
    try:
        n = convert(a.inputs, out, log, not a.no_layers, not a.no_combined)
    except Exception as e:
        log(f"ОШИБКА: {e}")
        n = 0
    sys.exit(0 if n else 1)


if __name__ == "__main__":
    main()
