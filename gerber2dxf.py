#!/usr/bin/env python3
"""Gerber2DXF — Gerber (RS-274X / X2) and Excellon to DXF (mm) converter.

Copyright (c) 2026 VLR. Free for any use with attribution, see LICENSE.

Запуск без аргументов           -> окно программы (превью слоёв, EN / UK / RU).
gerber2dxf.py input.zip [-o out] [--lang en|uk|ru] -> консольный режим.

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
import json
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

VERSION = "1.2"
APP = "Gerber2DXF"
DEV_NAME = "VLR"
PATREON_URL = "https://patreon.com/VLR888"
PAYPAL_ID = "vladimir.rovinskiy@gmail.com"
PAYPAL_URL = "https://www.paypal.com/donate/?business=" + PAYPAL_ID.replace("@", "%40") + "&currency_code=USD"
PROJECT_URL = "https://github.com/vvk-rvn/Gerber2DXF"
SKIP_EXT = {".gbrjob", ".json", ".ddw", ".tgz", ".gz", ".tar", ".pdf", ".png", ".jpg",
            ".jpeg", ".bmp", ".step", ".stp", ".kicad_pcb", ".kicad_pro", ".csv", ".xlsx",
            ".xls", ".html", ".dxf", ".exe", ".dll", ".rar", ".7z"}


# ----------------------------------------------------------------------------
# languages & settings
# ----------------------------------------------------------------------------
LANGS = (("en", "English"), ("uk", "Українська"), ("ru", "Русский"))
LANG = "en"
SETTINGS = {}

# key -> (English, Ukrainian, Russian)
STR = {
    "in_label": ("Input (zip or folder):", "Вхід (zip або папка):", "Вход (zip или папка):"),
    "btn_folder": ("Folder…", "Папка…", "Папка…"),
    "btn_refresh": ("Refresh", "Оновити", "Обновить"),
    "out_label": ("Output folder:", "Папка виводу:", "Папка вывода:"),
    "btn_browse": ("Browse…", "Огляд…", "Обзор…"),
    "opt_per": ("DXF per layer", "DXF на кожен шар", "DXF на каждый слой"),
    "opt_all": ("Combined ALL_LAYERS.dxf", "Спільний ALL_LAYERS.dxf", "Общий ALL_LAYERS.dxf"),
    "lang_label": ("Language:", "Мова:", "Язык:"),
    "btn_help": ("Help", "Довідка", "Помощь"),
    "btn_save_checked": ("Save DXF (checked layers)", "Зберегти DXF (позначені шари)",
                         "Сохранить DXF (отмеченные слои)"),
    "btn_convert_all": ("Convert all", "Конвертувати все", "Конвертировать всё"),
    "btn_open_out": ("Open output folder", "Відкрити папку виводу", "Открыть папку вывода"),
    "set_label": ("Set:", "Набір:", "Набор:"),
    "btn_fit": ("Fit", "Вписати", "Вписать"),
    "btn_all_on": ("All layers", "Усі шари", "Все слои"),
    "btn_all_off": ("None", "Жодного", "Ни одного"),
    "layers_title": ("Layers", "Шари", "Слои"),
    "hint_mouse": ("Wheel — zoom · drag — pan · double-click — fit · click a colour — change it",
                   "Коліщатко — масштаб · перетягування — зсув · подвійний клік — вписати · клік по кольору — змінити",
                   "Колесо — масштаб · перетаскивание — сдвиг · двойной клик — вписать · клик по цвету — сменить"),
    "mm": ("mm", "мм", "мм"),
    "placeholder": ("Choose a Gerber zip archive or folder", "Виберіть zip-архів або папку з Gerber",
                    "Выберите zip-архив или папку с Gerber"),
    "st_loading": ("Loading…", "Завантаження…", "Загрузка…"),
    "st_drawing": ("Drawing… {p}%", "Малювання… {p}%", "Отрисовка… {p}%"),
    "st_summary": ("{title}: layers {n}, objects {m}", "{title}: шарів {n}, об'єктів {m}",
                   "{title}: слоёв {n}, объектов {m}"),
    "st_saving": ("Saving layers: {n}…", "Збереження шарів: {n}…", "Сохранение слоёв: {n}…"),
    "st_saved": ("Saved layers: {n}  →  {path}", "Збережено шарів: {n}  →  {path}",
                 "Сохранено слоёв: {n}  →  {path}"),
    "st_failed": ("Error", "Помилка", "Ошибка"),
    "warn_no_layer": ("Check at least one layer.", "Позначте хоча б один шар.", "Отметьте хотя бы один слой."),
    "warn_no_opt": ("Check at least one output option (DXF per layer / ALL_LAYERS.dxf).",
                    "Позначте хоча б один варіант виводу (DXF на кожен шар / ALL_LAYERS.dxf).",
                    "Отметьте хотя бы один вариант вывода (DXF на каждый слой / ALL_LAYERS.dxf)."),
    "warn_no_input": ("Choose a zip archive or folder with Gerber files.", "Виберіть zip-архів або папку з Gerber.",
                      "Выберите zip-архив или папку с Gerber."),
    "err_none_found": ("No Gerber/Excellon files found.", "Файлів Gerber/Excellon не знайдено.",
                       "Файлы Gerber/Excellon не найдены."),
    "error": ("ERROR", "ПОМИЛКА", "ОШИБКА"),
    "dlg_zip": ("Gerber archive", "Архів Gerber", "Архив Gerber"),
    "dlg_all_files": ("All files", "Усі файли", "Все файлы"),
    "dlg_folder": ("Folder with Gerber files", "Папка з Gerber", "Папка с Gerber"),
    "dlg_out": ("Where to save DXF", "Куди зберегти DXF", "Куда сохранить DXF"),
    "log_input": ("Input: {p}", "Вхід: {p}", "Вход: {p}"),
    "log_not_found": ("  not found", "  не знайдено", "  не найден"),
    "log_no_files": ("  no Gerber/Excellon files", "  файлів Gerber/Excellon немає", "  файлов Gerber/Excellon нет"),
    "log_set": ("  Set: {s}", "  Набір: {s}", "  Набор: {s}"),
    "root_dir": ("(root)", "(корінь)", "(корень)"),
    "log_dup": ("  (duplicate archive skipped: {n})", "  (дублікат архіву пропущено: {n})",
                "  (дубликат архива пропущен: {n})"),
    "log_err_in": ("    ERROR in {f}: {e}", "    ПОМИЛКА в {f}: {e}", "    ОШИБКА в {f}: {e}"),
    "log_gerber": ("    {name:<22} Gerber   tracks {t:>6}  pads {f:>5}  regions {r:>5}",
                   "    {name:<22} Gerber   доріжок {t:>6}  площадок {f:>5}  полігонів {r:>5}",
                   "    {name:<22} Gerber   дорожек {t:>6}  площадок {f:>5}  полигонов {r:>5}"),
    "log_exc": ("    {name:<22} Excellon holes {h:>5}  slots {s:>4}",
                "    {name:<22} Excellon отворів {h:>5}  пазів {s:>4}",
                "    {name:<22} Excellon отверстий {h:>5}  пазов {s:>4}"),
    "log_written": ("  Layers written: {n}  ->  {path}", "  Записано шарів: {n}  ->  {path}",
                    "  Записано слоёв: {n}  ->  {path}"),
    "log_done": ("Done: layers converted {n}.", "Готово: шарів конвертовано {n}.", "Готово: слоёв конвертировано {n}."),
    "log_loaded": ("Loaded: sets {g}, layers {n}.", "Завантажено: наборів {g}, шарів {n}.",
                   "Загружено: наборов {g}, слоёв {n}."),
    "no_ezdxf": ("The ezdxf library is not installed: python -m pip install ezdxf",
                 "Не встановлено бібліотеку ezdxf: python -m pip install ezdxf",
                 "Не установлена библиотека ezdxf: python -m pip install ezdxf"),
    "color_title": ("Layer colour — {name}", "Колір шару — {name}", "Цвет слоя — {name}"),
    "color_palette": ("Palette:", "Палітра:", "Палитра:"),
    "color_code": ("Code:", "Код:", "Код:"),
    "color_more": ("More colours…", "Інші кольори…", "Другие цвета…"),
    "color_default": ("Default", "Типовий", "По умолчанию"),
    "color_bad": ("Enter the colour as #RRGGBB, e.g. #FF8000.", "Введіть колір у форматі #RRGGBB, наприклад #FF8000.",
                  "Введите цвет в формате #RRGGBB, например #FF8000."),
    "ok": ("OK", "OK", "OK"),
    "cancel": ("Cancel", "Скасувати", "Отмена"),
    "help_title": ("Help — Gerber → DXF", "Довідка — Gerber → DXF", "Помощь — Gerber → DXF"),
    "btn_close": ("Close", "Закрити", "Закрыть"),
    "btn_copy_paypal": ("Copy PayPal address", "Копіювати адресу PayPal", "Копировать адрес PayPal"),
    "copied": ("Copied: {s}", "Скопійовано: {s}", "Скопировано: {s}"),
    "h_dev": ("Developer", "Розробник", "Разработчик"),
    "h_license": ("License", "Ліцензія", "Лицензия"),
    "h_support": ("Support the project", "Підтримати проєкт", "Поддержать проект"),
    "h_project": ("Project page", "Сторінка проєкту", "Страница проекта"),
    "license_text": (
        "Gerber2DXF © 2026 {dev}. Free for any use, including commercial: you may use, copy, modify and "
        "distribute the program, provided the credit “Gerber2DXF by {dev}” and a link to the project are "
        "kept in the program, its documentation and all copies or derived works. "
        "The program is provided “as is”, without warranty of any kind. Full text: LICENSE.",
        "Gerber2DXF © 2026 {dev}. Вільне використання для будь-яких цілей, зокрема комерційних: програму можна "
        "використовувати, копіювати, змінювати й поширювати за умови, що позначка «Gerber2DXF by {dev}» і "
        "посилання на проєкт зберігаються в програмі, документації та всіх копіях і похідних роботах. "
        "Програма надається «як є», без будь-яких гарантій. Повний текст: LICENSE.",
        "Gerber2DXF © 2026 {dev}. Свободное использование для любых целей, включая коммерческие: программу можно "
        "использовать, копировать, изменять и распространять при условии, что отметка «Gerber2DXF by {dev}» и "
        "ссылка на проект сохраняются в программе, документации и всех копиях и производных работах. "
        "Программа предоставляется «как есть», без каких-либо гарантий. Полный текст: LICENSE."),
    "support_text": ("If the program saves you time, you can support its development:",
                     "Якщо програма заощаджує вам час, можна підтримати її розвиток:",
                     "Если программа экономит вам время, можно поддержать её развитие:"),
    "kind_top_cu": ("Top copper", "Мідь, верхній шар", "Медь, верхний слой"),
    "kind_bot_cu": ("Bottom copper", "Мідь, нижній шар", "Медь, нижний слой"),
    "kind_inner_cu": ("Inner copper", "Мідь, внутрішній шар", "Медь, внутренний слой"),
    "kind_top_mask": ("Top solder mask", "Паяльна маска, верх", "Паяльная маска, верх"),
    "kind_bot_mask": ("Bottom solder mask", "Паяльна маска, низ", "Паяльная маска, низ"),
    "kind_top_silk": ("Top silkscreen", "Шовкографія, верх", "Шелкография, верх"),
    "kind_bot_silk": ("Bottom silkscreen", "Шовкографія, низ", "Шелкография, низ"),
    "kind_top_paste": ("Top solder paste (stencil)", "Паста (трафарет), верх", "Паста (трафарет), верх"),
    "kind_bot_paste": ("Bottom solder paste (stencil)", "Паста (трафарет), низ", "Паста (трафарет), низ"),
    "kind_drill": ("Plated holes (drilling)", "Металізовані отвори (свердління)",
                   "Металлизированные отверстия (сверловка)"),
    "kind_npth": ("Non-plated holes", "Неметалізовані отвори", "Неметаллизированные отверстия"),
    "kind_outline": ("Board outline", "Контур плати", "Контур платы"),
    "kind_vcut": ("V-cut scoring lines", "Лінії V-скрайбування", "Линии V-скрайбирования"),
    "kind_other": ("Other layer", "Інший шар", "Другой слой"),
    "cli_desc": ("Gerber/Excellon -> DXF (mm)", "Gerber/Excellon -> DXF (мм)", "Gerber/Excellon -> DXF (мм)"),
    "cli_inputs": ("zip archive(s), folders or Gerber files", "zip-архів(и), папки або файли Gerber",
                   "zip-архив(ы), папки или файлы Gerber"),
    "cli_out": ("output folder (default: <input>_dxf next to the input)",
                "папка виводу (типово <вхід>_dxf поруч із входом)",
                "папка вывода (по умолчанию <вход>_dxf рядом со входом)"),
    "cli_nolayers": ("do not write a DXF per layer", "не писати окремі DXF по шарах", "не писать отдельные DXF по слоям"),
    "cli_nocombined": ("do not write ALL_LAYERS.dxf", "не писати ALL_LAYERS.dxf", "не писать ALL_LAYERS.dxf"),
    "cli_lang": ("interface language", "мова інтерфейсу", "язык интерфейса"),
}

HELP = {
    "en": """# What it does
Gerber2DXF turns PCB manufacturing files — Gerber (RS-274X / X2) and Excellon drill files — into DXF drawings in millimetres: for CAD, enclosure design, laser cutters, CNC and engraving software.
It understands JLCPCB / JLC CAM archives (tl, bl, to, bo, ts, bs, drl, ko, vcut…) and KiCad output (*.gbr + PTH/NPTH.drl), including nested zip files.
# Quick start
1. Click «Zip…» or «Folder…», or drop a zip onto Gerber2DXF.exe. All layers appear in the preview at once.
2. Untick the layers you do not need. Next to each layer is its KiCad-style name; hover over it for a description.
3. Click a colour square to change the layer colour: 16-colour palette, a #RRGGBB code or the system colour picker. The colour is remembered and is also written to the DXF layer.
4. Mouse wheel — zoom, drag — pan, double-click or «Fit» — show the whole board. The bottom line shows the cursor coordinates in mm.
5. «Save DXF (checked layers)» writes the ticked layers of the current set; «Convert all» writes every layer of every set.
6. «Refresh» re-reads the files after you regenerate the Gerbers.
# Output
Each Gerber set gets its own subfolder: <layer>.dxf for every layer and ALL_LAYERS.dxf with all layers (DXF layer = layer name). Tracks are polylines along the centre line with width, pads and copper pours are closed outlines, holes are circles. Negative objects go to the <layer>_CLEAR layer.
# Command line
Gerber2DXF.exe input.zip -o out_dir [--no-layers] [--no-combined] [--lang en|uk|ru]""",
    "uk": """# Для чого
Gerber2DXF перетворює виробничі файли друкованих плат — Gerber (RS-274X / X2) і файли свердління Excellon — на креслення DXF у міліметрах: для CAD, проєктування корпусів, лазерних верстатів, ЧПК і гравіювання.
Розуміє архіви JLCPCB / JLC CAM (tl, bl, to, bo, ts, bs, drl, ko, vcut…) і файли KiCad (*.gbr + PTH/NPTH.drl), зокрема вкладені zip.
# Коротка інструкція
1. Натисніть «Zip…» або «Папка…» чи перетягніть zip на Gerber2DXF.exe. Усі шари одразу з'являться в превʼю.
2. Зніміть позначки з непотрібних шарів. Поруч із шаром — його назва в стилі KiCad, підказка з описом — при наведенні.
3. Клік по кольоровому квадрату змінює колір шару: палітра з 16 кольорів, код #RRGGBB або системний вибір кольору. Колір запам'ятовується і записується в шар DXF.
4. Коліщатко миші — масштаб, перетягування — зсув, подвійний клік або «Вписати» — уся плата. Унизу — координати курсора в мм.
5. «Зберегти DXF (позначені шари)» записує позначені шари поточного набору; «Конвертувати все» — усі шари всіх наборів.
6. «Оновити» перечитує файли, якщо ви згенерували Gerber заново.
# Результат
Для кожного набору Gerber — своя підпапка: <шар>.dxf для кожного шару та ALL_LAYERS.dxf з усіма шарами (шар DXF = назва шару). Доріжки — полілінії по осі з шириною, площадки й полігони — замкнені контури, отвори — кола. Негативні об'єкти — у шарі <шар>_CLEAR.
# Командний рядок
Gerber2DXF.exe input.zip -o out_dir [--no-layers] [--no-combined] [--lang en|uk|ru]""",
    "ru": """# Для чего
Gerber2DXF превращает производственные файлы печатных плат — Gerber (RS-274X / X2) и файлы сверловки Excellon — в чертежи DXF в миллиметрах: для CAD, проектирования корпусов, лазерных станков, ЧПУ и гравировки.
Понимает архивы JLCPCB / JLC CAM (tl, bl, to, bo, ts, bs, drl, ko, vcut…) и файлы KiCad (*.gbr + PTH/NPTH.drl), в том числе вложенные zip.
# Краткая инструкция
1. Нажмите «Zip…» или «Папка…» либо перетащите zip на Gerber2DXF.exe. Все слои сразу появятся в превью.
2. Снимите галочки с ненужных слоёв. Рядом со слоем — его название в стиле KiCad, подсказка с описанием — при наведении.
3. Клик по цветному квадрату меняет цвет слоя: палитра из 16 цветов, код #RRGGBB или системный выбор цвета. Цвет запоминается и записывается в слой DXF.
4. Колесо мыши — масштаб, перетаскивание — сдвиг, двойной клик или «Вписать» — вся плата. Внизу — координаты курсора в мм.
5. «Сохранить DXF (отмеченные слои)» записывает отмеченные слои текущего набора; «Конвертировать всё» — все слои всех наборов.
6. «Обновить» перечитывает файлы, если вы сгенерировали Gerber заново.
# Результат
Для каждого набора Gerber — своя подпапка: <слой>.dxf для каждого слоя и ALL_LAYERS.dxf со всеми слоями (слой DXF = имя слоя). Дорожки — полилинии по оси с шириной, площадки и полигоны — замкнутые контуры, отверстия — окружности. Негативные объекты — в слое <слой>_CLEAR.
# Командная строка
Gerber2DXF.exe input.zip -o out_dir [--no-layers] [--no-combined] [--lang en|uk|ru]""",
}


def T(key, **kw):
    s = STR[key][{"en": 0, "uk": 1, "ru": 2}[LANG]]
    return s.format(**kw) if kw else s


def set_lang(code):
    global LANG
    LANG = code if code in dict(LANGS) else "en"


def system_lang():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            prim = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return {0x22: "uk", 0x19: "ru"}.get(prim, "en")
        except Exception:  # noqa
            pass
    import locale
    loc = (os.environ.get("LANG") or locale.getlocale()[0] or "").lower()
    return "uk" if loc.startswith(("uk", "ukrainian")) else "ru" if loc.startswith(("ru", "russian")) else "en"


def settings_path():
    base = os.environ.get("APPDATA") if sys.platform.startswith("win") else None
    return (Path(base) if base else Path.home() / ".config") / APP / "settings.json"


def load_settings():
    global SETTINGS
    try:
        SETTINGS = json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:  # noqa  (missing or broken file -> defaults)
        SETTINGS = {}
    if not isinstance(SETTINGS, dict):
        SETTINGS = {}
    if not isinstance(SETTINGS.get("colors"), dict):
        SETTINGS["colors"] = {}
    set_lang(SETTINGS.get("lang") or system_lang())


def save_settings():
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def layer_kind(name):
    """Layer type by name: JLC short names, KiCad names, Protel extensions."""
    n = name.lower()
    jlc = {"tl": "top_cu", "bl": "bot_cu", "to": "top_silk", "bo": "bot_silk", "ts": "top_mask",
           "bs": "bot_mask", "gtp": "top_paste", "gbp": "bot_paste", "drl": "drill", "npth": "npth",
           "ko": "outline", "gko": "outline", "gm1": "outline", "vcut": "vcut"}
    if n in jlc:
        return jlc[n]
    exts = {".gtl": "top_cu", ".gbl": "bot_cu", ".gto": "top_silk", ".gbo": "bot_silk",
            ".gts": "top_mask", ".gbs": "bot_mask", ".gtp": "top_paste", ".gbp": "bot_paste",
            ".gko": "outline", ".gm1": "outline", ".drl": "drill", ".xln": "drill"}
    if Path(n).suffix in exts:
        return exts[Path(n).suffix]
    if "npth" in n:
        return "npth"
    if "pth" in n or "drill" in n or n.endswith("drl"):
        return "drill"
    if "edge" in n or "outline" in n or "cuts" in n:
        return "outline"
    if "vcut" in n or "v-cut" in n:
        return "vcut"
    if re.search(r"(^|[_.\-])in\d+", n):
        return "inner_cu"
    bot = bool(re.search(r"(^|[_.\-])(b|bot|bottom)([_.\-]|$)", n))
    for key, kind in (("cu", "cu"), ("copper", "cu"), ("mask", "mask"), ("silk", "silk"),
                      ("legend", "silk"), ("paste", "paste")):
        if key in n:
            return ("bot_" if bot else "top_") + kind
    return "other"


DXF_COLOR = {"top_cu": 1, "bot_cu": 5, "inner_cu": 40, "top_mask": 3, "bot_mask": 4,
             "top_silk": 7, "bot_silk": 8, "top_paste": 9, "bot_paste": 9, "drill": 2,
             "npth": 30, "outline": 6, "vcut": 30, "other": 252}


def layer_color(name):
    """AutoCAD color index for a layer."""
    return DXF_COLOR[layer_kind(name)]


def safe_layer(name):
    name = re.sub(r'[<>/\\":;?*|=\'`,]', "_", name).strip() or "layer"
    return name[:200]


class Shapes:
    """Collects geometry once; written to one or more DXF docs later."""

    def __init__(self, base_layer):
        self.base = base_layer
        self.ftype = "gerber"  # or "excellon"
        self.items = []  # (kind, clear, data...)
        self.stats = {"tracks": 0, "flashes": 0, "regions": 0, "holes": 0, "slots": 0}

    def bbox(self):
        x0 = y0 = math.inf
        x1 = y1 = -math.inf
        for it in self.items:
            if it[0] == "c":
                (x, y), r = it[2], it[3]
                x0, y0, x1, y1 = min(x0, x - r), min(y0, y - r), max(x1, x + r), max(y1, y + r)
            else:
                for p in it[2]:
                    x, y = p[0], p[1]
                    if x < x0:
                        x0 = x
                    if x > x1:
                        x1 = x
                    if y < y0:
                        y0 = y
                    if y > y1:
                        y1 = y
        return None if x0 == math.inf else (x0, y0, x1, y1)

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
                if clear:
                    doc.layers.add(names[clear], color=9)
                else:
                    rgb = layer_rgb(self.base)
                    doc.layers.add(names[clear], color=layer_color(self.base),
                                   true_color=int(rgb[1:], 16))
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
            log(T("log_dup", n=Path(zpath).name))
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


class Group:
    """One Gerber set (one folder): parsed layers + where its DXF go."""

    def __init__(self, title, out_rel, layers):
        self.title = title
        self.out_rel = out_rel
        self.layers = layers


def parse_group(files, log):
    stems = [f.stem for f in files]
    if len(set(s.lower() for s in stems)) < len(stems):  # e.g. board.GTL, board.GBL
        stems = [f.name for f in files]
    short = strip_common_prefix(stems)
    used, layers = set(), []
    for f, stem in zip(files, stems):
        name = safe_layer(short[stem])
        if name.lower() in used:
            name = safe_layer(f"{name}{f.suffix.replace('.', '_')}")
        used.add(name.lower())
        ftype = classify(f)
        shapes = Shapes(name)
        shapes.ftype = ftype
        text = f.read_text(encoding="latin-1")
        try:
            (Gerber if ftype == "gerber" else Excellon)(shapes, log).run(text)
        except Exception as e:  # keep going with other layers
            log(T("log_err_in", f=f.name, e=e))
            continue
        st = shapes.stats
        if ftype == "gerber":
            log(T("log_gerber", name=name, t=st['tracks'], f=st['flashes'], r=st['regions']))
        else:
            log(T("log_exc", name=name, h=st['holes'], s=st['slots']))
        layers.append(shapes)
    return layers


def write_group(group, out_dir, log, per_layer=True, combined=True, only=None):
    """Write DXF for a parsed group. only = set of layer names to keep (None = all)."""
    if ezdxf is None:
        raise RuntimeError(T("no_ezdxf"))
    layers = [s for s in group.layers if only is None or s.base in only]
    if not layers:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    if per_layer:
        for shapes in layers:
            doc = ezdxf.new("R2010", setup=True, units=4)
            shapes.emit(doc)
            doc.saveas(out_dir / f"{shapes.base}.dxf")
    if combined:
        doc = ezdxf.new("R2010", setup=True, units=4)
        for shapes in layers:
            shapes.emit(doc)
        doc.saveas(out_dir / "ALL_LAYERS.dxf")
    log(T("log_written", n=len(layers), path=out_dir))
    return len(layers)


def convert(inputs, out_root, log=print, per_layer=True, combined=True):
    total = 0
    for g in load(inputs, log):
        total += write_group(g, Path(out_root) / g.out_rel, log, per_layer, combined)
    log(T("log_done", n=total))
    return total


def load(inputs, log=print):
    """Find and parse all Gerber/Excellon sets in the inputs. Returns [Group]."""
    if ezdxf is None:
        raise RuntimeError(T("no_ezdxf"))
    result = []
    for inp in inputs:
        inp = Path(inp)
        log(T("log_input", p=inp))
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
                log(T("log_not_found"))
                continue
            files = [inp] if root is None else [p for p in sorted(root.rglob("*")) if p.is_file()]
            groups = {}
            for p in files:
                if classify(p):
                    groups.setdefault(p.parent, []).append(p)
            if not groups:
                log(T("log_no_files"))
                continue
            base = Path(".") if len(inputs) == 1 else Path(safe_layer(inp.stem))
            for folder, gfiles in sorted(groups.items()):
                rel = Path(".") if root is None or folder == root else folder.relative_to(root)
                rel_txt = rel.as_posix() if str(rel) != "." else T("root_dir")
                log(T("log_set", s=rel_txt))
                layers = parse_group(gfiles, log)
                if layers:
                    title = rel_txt if len(inputs) == 1 else f"{inp.name} / {rel_txt}"
                    result.append(Group(title, base / rel, layers))
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    return result


def default_out(inp):
    p = Path(inp)
    return p.parent / (p.stem + "_dxf")


# ----------------------------------------------------------------------------
# Layer names & colours
# ----------------------------------------------------------------------------
PREVIEW_BG = "#0e1116"
# kind -> (default colour, draw mode, z-order: lower is drawn first)
PREVIEW_STYLE = {
    "bot_silk": ("#b9a2e0", "fill", 0), "bot_paste": ("#8f8fb0", "outline", 1),
    "bot_mask": ("#2fa3a3", "outline", 2), "bot_cu": ("#4a6fe0", "fill", 3),
    "inner_cu": ("#d6a43a", "fill", 4), "top_cu": ("#d9493e", "fill", 5),
    "top_paste": ("#b0b0b0", "outline", 6), "top_mask": ("#39b54a", "outline", 7),
    "top_silk": ("#f2f2f2", "fill", 8), "other": ("#9aa0a6", "fill", 9),
    "vcut": ("#ff6ec7", "fill", 10), "outline": ("#f0e04a", "fill", 11),
    "npth": ("#ff9a2e", "outline", 12), "drill": ("#35d0e0", "outline", 13),
}
PALETTE16 = ("#d9493e", "#ff9a2e", "#f0e04a", "#39b54a", "#2fa3a3", "#35d0e0", "#4a6fe0", "#b9a2e0",
             "#ff6ec7", "#c83cc8", "#d6a43a", "#8b5a2b", "#f2f2f2", "#b0b0b0", "#808080", "#4d4d4d")
KICAD_NAME = {"top_cu": "F_Cu", "bot_cu": "B_Cu", "top_mask": "F_Mask", "bot_mask": "B_Mask",
              "top_silk": "F_Silkscreen", "bot_silk": "B_Silkscreen", "top_paste": "F_Paste",
              "bot_paste": "B_Paste", "drill": "PTH", "npth": "NPTH", "outline": "Edge_Cuts", "vcut": "V_Cut"}


def kicad_name(name):
    """KiCad-style layer name: tl -> F_Cu, ko -> Edge_Cuts, drl -> PTH…"""
    kind = layer_kind(name)
    if kind == "inner_cu":
        m = re.search(r"in(\d+)", name, re.I)
        return f"In{m.group(1)}_Cu" if m else name
    return KICAD_NAME.get(kind, name)


def color_key(name):
    return kicad_name(name).lower()


def parse_hex(s):
    """'#RGB' / 'RRGGBB' / '#RRGGBB' -> '#rrggbb' or None."""
    s = str(s or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(ch * 2 for ch in s)
    return "#" + s.lower() if re.fullmatch(r"[0-9a-fA-F]{6}", s) else None


def layer_rgb(name):
    """Preview / DXF true colour of a layer: the user's choice or the default."""
    return parse_hex(SETTINGS.get("colors", {}).get(color_key(name))) or PREVIEW_STYLE[layer_kind(name)][0]


def flatten(pts, closed):
    """[(x, y, bulge)] -> [(x, y)] with arcs split into short segments."""
    out = []
    n = len(pts)
    for i, (x, y, b) in enumerate(pts):
        out.append((x, y))
        if not b or (i + 1 == n and not closed):
            continue
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        dx, dy = x2 - x, y2 - y
        chord = math.hypot(dx, dy)
        if chord < 1e-12:
            continue
        theta = 4 * math.atan(b)
        off = (chord / 2) * math.cos(theta / 2) / math.sin(theta / 2)
        cx, cy = (x + x2) / 2 - dy / chord * off, (y + y2) / 2 + dx / chord * off
        r = math.hypot(x - cx, y - cy)
        a0 = math.atan2(y - cy, x - cx)
        steps = max(2, int(abs(theta) / (math.pi / 16)))
        for k in range(1, steps):
            a = a0 + theta * k / steps
            out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


# ----------------------------------------------------------------------------
# small Tk helpers
# ----------------------------------------------------------------------------
class UI:
    """Registry of translatable widgets: refresh() re-applies texts after a language change."""

    def __init__(self):
        self.items, self.hooks = [], []

    def reg(self, widget, key, opt="text"):
        self.items.append((widget, key, opt))
        widget.configure(**{opt: T(key)})
        return widget

    def refresh(self):
        alive = []
        for w, key, opt in self.items:
            try:
                w.configure(**{opt: T(key)})
                alive.append((w, key, opt))
            except Exception:  # noqa  (widget destroyed)
                pass
        self.items = alive
        for h in self.hooks:
            h()


class Tip:
    """Tooltip; text_fn is called each time, so it follows the language."""

    def __init__(self, widget, text_fn):
        self.w, self.fn, self.tw = widget, text_fn, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, _e=None):
        import tkinter as tk
        if self.tw:
            return
        x = self.w.winfo_rootx() + 16
        y = self.w.winfo_rooty() + self.w.winfo_height() + 2
        self.tw = tk.Toplevel(self.w)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tw, text=self.fn(), bg="#ffffe0", fg="#000", relief="solid", bd=1,
                 padx=6, pady=2, justify="left").pack()

    def hide(self, _e=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None


def dpi_aware():
    """Crisp rendering on Windows with 125-200 % display scaling."""
    if sys.platform.startswith("win"):
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:  # noqa
                pass


def ui_scale(widget):
    return max(1.0, widget.winfo_fpixels("1i") / 96.0)


def fit_geometry(win, w, h):
    k = ui_scale(win)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = min(int(w * k), int(sw * 0.9)), min(int(h * k), int(sh * 0.78))
    win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, int((sh * 0.92 - h) / 2))}")


def open_path(p):
    if sys.platform.startswith("win"):
        os.startfile(str(p))  # noqa
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])


def open_url(url):
    import webbrowser
    webbrowser.open(url)


def color_dialog(parent, name, current, default):
    """16-colour palette + #RRGGBB code + system picker. Returns '#rrggbb' or None."""
    import tkinter as tk
    from tkinter import colorchooser, messagebox, ttk
    res = {"v": None}
    d = tk.Toplevel(parent)
    d.withdraw()
    d.title(T("color_title", name=name))
    d.transient(parent.winfo_toplevel())
    d.resizable(False, False)
    f = ttk.Frame(d, padding=12)
    f.pack()
    var = tk.StringVar(value=current.upper())

    def ok(_e=None):
        c = parse_hex(var.get())
        if not c:
            messagebox.showwarning("Gerber → DXF", T("color_bad"), parent=d)
            return
        res["v"] = c
        d.destroy()

    ttk.Label(f, text=T("color_palette")).grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 4))
    for k, c in enumerate(PALETTE16):
        b = tk.Label(f, bg=c, width=4, height=2, relief="solid", bd=1, cursor="hand2")
        b.grid(row=1 + k // 8, column=k % 8, padx=2, pady=2)
        b.bind("<Button-1>", lambda e, c=c: var.set(c.upper()))
        b.bind("<Double-Button-1>", lambda e, c=c: (var.set(c.upper()), ok()))

    row = ttk.Frame(f)
    row.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(10, 0))
    ttk.Label(row, text=T("color_code")).pack(side="left")
    ent = ttk.Entry(row, textvariable=var, width=10, font=("Consolas", 10))
    ent.pack(side="left", padx=6)
    sample = tk.Label(row, bg=current, width=5, relief="solid", bd=1)
    sample.pack(side="left", fill="y")

    def more():
        r = colorchooser.askcolor(color=parse_hex(var.get()) or current, parent=d,
                                  title=T("color_title", name=name))
        if r and r[1]:
            var.set(r[1].upper())
    ttk.Button(row, text=T("color_more"), command=more).pack(side="left", padx=(8, 0))

    def update(*_a):
        c = parse_hex(var.get())
        if c:
            sample.configure(bg=c)
    var.trace_add("write", update)

    btns = ttk.Frame(f)
    btns.grid(row=4, column=0, columnspan=8, sticky="ew", pady=(12, 0))
    ttk.Button(btns, text=T("color_default"), command=lambda: var.set(default.upper())).pack(side="left")
    ttk.Button(btns, text=T("cancel"), command=d.destroy).pack(side="right")
    ttk.Button(btns, text=T("ok"), command=ok).pack(side="right", padx=6)
    d.bind("<Return>", ok)
    d.bind("<Escape>", lambda e: d.destroy())

    d.update_idletasks()
    top = parent.winfo_toplevel()  # centre over the main window
    x = top.winfo_rootx() + (top.winfo_width() - d.winfo_reqwidth()) // 2
    y = top.winfo_rooty() + (top.winfo_height() - d.winfo_reqheight()) // 3
    d.geometry(f"+{max(0, x)}+{max(0, y)}")
    d.deiconify()
    ent.focus_set()
    ent.select_range(0, "end")
    d.grab_set()
    parent.wait_window(d)
    return res["v"]


# ----------------------------------------------------------------------------
# Preview panel
# ----------------------------------------------------------------------------
class Preview:
    """All layers of the parsed Gerber sets on one canvas, toggled by checkboxes."""

    CHUNK = 1500  # items drawn between UI updates

    def __init__(self, parent, ui):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.ui = tk, ttk, ui
        self.groups, self.vis, self.boxes = [], [], []
        self.gi, self.gen = 0, 0
        self.s, self.tx, self.ty = 1.0, 0.0, 0.0
        self.widths = {}
        self.drag = None
        self.msg = ("", {})
        self.placeholder = "placeholder"

        top = ttk.Frame(parent, padding=(0, 2, 0, 4))
        top.pack(side="top", fill="x")
        ui.reg(ttk.Label(top), "set_label").pack(side="left")
        self.combo = ttk.Combobox(top, state="readonly", width=46)
        self.combo.pack(side="left", padx=(4, 12))
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.select_group(self.combo.current()))
        ui.reg(ttk.Button(top, command=self.fit), "btn_fit").pack(side="left")
        ui.reg(ttk.Button(top, command=lambda: self.set_all(True)), "btn_all_on").pack(side="left", padx=(12, 0))
        ui.reg(ttk.Button(top, command=lambda: self.set_all(False)), "btn_all_off").pack(side="left", padx=4)

        status = ttk.Frame(parent, padding=(0, 3))
        status.pack(side="bottom", fill="x")
        self.coord = ttk.Label(status, text="", width=32)
        self.coord.pack(side="left")
        self.status = ttk.Label(status, text="")
        self.status.pack(side="left", padx=10)
        ui.reg(ttk.Label(status, foreground="#777"), "hint_mouse").pack(side="right")

        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body, padding=(0, 0, 8, 0))
        side.pack(side="left", fill="y")
        ui.reg(ttk.Label(side, font=("Segoe UI", 10, "bold")), "layers_title").pack(anchor="w", pady=(0, 4))
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
        lc = self.list_canvas = tk.Canvas(side, highlightthickness=0, width=200, bg=bg)
        sb = ttk.Scrollbar(side, orient="vertical", command=lc.yview)
        lc.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        lc.pack(side="left", fill="y", expand=True)
        self.list = ttk.Frame(lc)
        lc.create_window(0, 0, window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: lc.configure(scrollregion=lc.bbox("all"),
                                                             width=self.list.winfo_reqwidth()))
        self.wheel(lc)

        c = self.canvas = tk.Canvas(body, bg=PREVIEW_BG, highlightthickness=0, cursor="crosshair")
        c.pack(side="left", fill="both", expand=True)
        c.bind("<MouseWheel>", lambda e: self.zoom_at(e.x, e.y, 1.25 if e.delta > 0 else 0.8))
        c.bind("<Button-4>", lambda e: self.zoom_at(e.x, e.y, 1.25))  # X11
        c.bind("<Button-5>", lambda e: self.zoom_at(e.x, e.y, 0.8))
        for b in ("1", "2", "3"):
            c.bind(f"<ButtonPress-{b}>", self.drag_start)
            c.bind(f"<B{b}-Motion>", self.drag_move)
        c.bind("<Double-Button-1>", lambda e: self.fit())
        c.bind("<Motion>", self.motion)
        c.bind("<Configure>", lambda e: self.draw_placeholder())
        ui.hooks.append(self.retranslate)

    # --- helpers --------------------------------------------------------
    def wheel(self, w):
        w.bind("<MouseWheel>", lambda e: self.list_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

    def style(self, shapes):
        return PREVIEW_STYLE[layer_kind(shapes.base)]

    def set_status(self, key, **kw):
        self.msg = (key, kw)
        self.status.configure(text=T(key, **kw) if key else "")

    def retranslate(self):
        self.set_status(self.msg[0], **self.msg[1])
        self.coord.configure(text="")
        if self.groups:
            self.build_list()
        self.draw_placeholder()

    def show_message(self, key):
        """Empty preview with a centred hint (no file / loading / nothing found)."""
        self.gen += 1
        self.groups, self.vis, self.boxes = [], [], []
        self.combo.configure(values=[])
        self.combo.set("")
        for child in self.list.winfo_children():
            child.destroy()
        self.placeholder = key
        self.set_status("")
        self.coord.configure(text="")
        self.draw_placeholder()

    def draw_placeholder(self):
        if self.groups or not self.placeholder:
            return
        c = self.canvas
        c.delete("all")
        c.create_text(max(c.winfo_width(), 200) / 2, max(c.winfo_height(), 200) / 2, text=T(self.placeholder),
                      fill="#7d8590", font=("Segoe UI", 13))

    def set_groups(self, groups):
        tk = self.tk
        self.placeholder = None
        self.groups = groups
        self.vis = [[tk.BooleanVar(value=True) for _ in g.layers] for g in groups]
        self.boxes = [[s.bbox() for s in g.layers] for g in groups]
        self.combo.configure(values=[g.title for g in groups])
        self.combo.current(0)
        self.select_group(0)

    def selection(self):
        """(group, set of checked layer names) of the shown set, or None."""
        if not self.groups:
            return None
        g = self.groups[self.gi]
        return g, {s.base for s, v in zip(g.layers, self.vis[self.gi]) if v.get()}

    def visible_bbox(self):
        boxes = [b for b, v in zip(self.boxes[self.gi], self.vis[self.gi]) if b and v.get()]
        boxes = boxes or [b for b in self.boxes[self.gi] if b]
        if not boxes:
            return (0, 0, 10, 10)
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def fit_transform(self):
        self.canvas.update_idletasks()
        W, H = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        x0, y0, x1, y1 = self.visible_bbox()
        s = min((W - 40) / max(x1 - x0, 1e-6), (H - 40) / max(y1 - y0, 1e-6))
        return s, W / 2 - (x0 + x1) / 2 * s, H / 2 + (y0 + y1) / 2 * s

    def update_widths(self):
        for w, tag in self.widths.items():
            self.canvas.itemconfigure(tag, width=max(1.0, w * self.s))

    def width_tag(self, w):
        key = round(w, 5)
        if key not in self.widths:
            self.widths[key] = f"W{len(self.widths)}"
        return self.widths[key]

    # --- view -----------------------------------------------------------
    def zoom_at(self, cx, cy, f):
        if not self.groups:
            return
        self.canvas.scale("all", cx, cy, f, f)
        self.s *= f
        self.tx = (self.tx - cx) * f + cx
        self.ty = (self.ty - cy) * f + cy
        self.update_widths()

    def fit(self):
        if not self.groups:
            return
        s2, tx2, ty2 = self.fit_transform()
        f = s2 / self.s
        self.canvas.scale("all", 0, 0, f, f)
        self.canvas.move("all", tx2 - self.tx * f, ty2 - self.ty * f)
        self.s, self.tx, self.ty = s2, tx2, ty2
        self.update_widths()

    def drag_start(self, e):
        self.drag = (e.x, e.y)

    def drag_move(self, e):
        if self.drag and self.groups:
            dx, dy = e.x - self.drag[0], e.y - self.drag[1]
            self.canvas.move("all", dx, dy)
            self.tx += dx
            self.ty += dy
            self.drag = (e.x, e.y)

    def motion(self, e):
        if self.groups:
            x, y = (e.x - self.tx) / self.s, -(e.y - self.ty) / self.s
            self.coord.configure(text=f"X {x:9.3f}   Y {y:9.3f} {T('mm')}")

    # --- layers ---------------------------------------------------------
    def select_group(self, gi):
        self.gi = gi
        self.build_list()
        self.redraw()

    def build_list(self):
        tk, ttk = self.tk, self.ttk
        for child in self.list.winfo_children():
            child.destroy()
        g = self.groups[self.gi]
        order = sorted(range(len(g.layers)), key=lambda i: -self.style(g.layers[i])[2])
        for r, i in enumerate(order):
            shapes = g.layers[i]
            canon = kicad_name(shapes.base)

            def tip(s=shapes):
                return f"{kicad_name(s.base)} — {T('kind_' + layer_kind(s.base))}\n{s.base}: {len(s.items)}"
            sw = tk.Label(self.list, bg=layer_rgb(shapes.base), width=3, relief="solid", bd=1, cursor="hand2")
            sw.grid(row=r, column=0, padx=(0, 6), pady=2, sticky="ns")
            sw.bind("<Button-1>", lambda e, i=i: self.pick_color(i))
            cb = ttk.Checkbutton(self.list, text=canon, variable=self.vis[self.gi][i],
                                 command=lambda i=i: self.toggle(i))
            cb.grid(row=r, column=1, sticky="w")
            orig = ttk.Label(self.list, text=shapes.base if canon.lower() != shapes.base.lower() else "",
                             foreground="#666")
            orig.grid(row=r, column=2, sticky="w", padx=(8, 0))
            cnt = ttk.Label(self.list, text=f"({len(shapes.items)})", foreground="#888")
            cnt.grid(row=r, column=3, sticky="e", padx=(8, 4))
            for w in (sw, cb, orig, cnt):
                Tip(w, tip)
                self.wheel(w)

    def pick_color(self, i):
        g = self.groups[self.gi]
        shapes = g.layers[i]
        default = PREVIEW_STYLE[layer_kind(shapes.base)][0]
        col = color_dialog(self.canvas, kicad_name(shapes.base), layer_rgb(shapes.base), default)
        if col:
            self.apply_color(i, col)

    def apply_color(self, i, col):
        g = self.groups[self.gi]
        shapes = g.layers[i]
        key = color_key(shapes.base)
        if col == PREVIEW_STYLE[layer_kind(shapes.base)][0]:
            SETTINGS["colors"].pop(key, None)
        else:
            SETTINGS["colors"][key] = col
        save_settings()
        for j, s in enumerate(g.layers):
            if color_key(s.base) == key:
                self.canvas.itemconfigure(f"F{j}", fill=col)
                self.canvas.itemconfigure(f"O{j}", outline=col)
        self.build_list()

    def toggle(self, i):
        state = "normal" if self.vis[self.gi][i].get() else "hidden"
        self.canvas.itemconfigure(f"L{i}", state=state)

    def set_all(self, on):
        if self.groups:
            for i, v in enumerate(self.vis[self.gi]):
                v.set(on)
                self.toggle(i)

    def redraw(self):
        self.gen += 1
        self.canvas.delete("all")
        self.widths = {}
        self.s, self.tx, self.ty = self.fit_transform()
        it = self.draw_iter(self.gen)
        self.canvas.after(1, self.pump, self.gen, it)

    def pump(self, gen, it):
        if gen != self.gen:
            return
        try:
            next(it)
            self.canvas.after(1, self.pump, gen, it)
        except StopIteration:
            pass

    def draw_iter(self, gen):
        g = self.groups[self.gi]
        order = sorted(range(len(g.layers)), key=lambda i: self.style(g.layers[i])[2])
        total = max(1, sum(len(s.items) for s in g.layers))
        done = 0
        for i in order:
            for n in self.draw_layer(i, g.layers[i]):
                done += n
                self.set_status("st_drawing", p=min(100, done * 100 // total))
                yield
        self.set_status("st_summary", title=g.title, n=len(g.layers), m=len(self.canvas.find_all()))

    def draw_layer(self, i, shapes):
        """Positive items carry tag F<i> (colour in 'fill') or O<i> (colour in 'outline')."""
        c = self.canvas
        mode = self.style(shapes)[1]
        color = layer_rgb(shapes.base)
        tag = f"L{i}"
        ftag, otag = (tag, f"F{i}"), (tag, f"O{i}")
        state = "normal" if self.vis[self.gi][i].get() else "hidden"
        count = 0
        for it in shapes.items:
            clear = it[1]
            col = PREVIEW_BG if clear else color
            ft = (tag,) if clear else ftag
            s, tx, ty = self.s, self.tx, self.ty
            if it[0] == "c":
                (x, y), r = it[2], it[3]
                px, py, pr = x * s + tx, -y * s + ty, max(r * s, 0.5)
                if mode == "fill":
                    c.create_oval(px - pr, py - pr, px + pr, py + pr, fill=col, outline="", tags=ft, state=state)
                elif not clear:
                    c.create_oval(px - pr, py - pr, px + pr, py + pr, outline=col, tags=otag, state=state)
            else:
                _, _, pts, closed, width, bulge = it
                flat = flatten(pts, closed) if bulge else pts
                coords = []
                for p in flat:
                    coords.append(p[0] * s + tx)
                    coords.append(-p[1] * s + ty)
                if closed:
                    if len(coords) >= 6:
                        if mode == "fill":
                            c.create_polygon(coords, fill=col, outline="", tags=ft, state=state)
                        elif not clear:
                            c.create_polygon(coords, fill="", outline=col, tags=otag, state=state)
                elif len(coords) >= 4:
                    if mode == "fill" and width > 0:
                        c.create_line(coords, fill=col, width=max(1.0, width * s), capstyle="round",
                                      joinstyle="round", tags=ft + (self.width_tag(width),), state=state)
                    elif not clear:
                        c.create_line(coords, fill=col, width=1, tags=ftag, state=state)
            count += 1
            if count % self.CHUNK == 0:
                yield self.CHUNK
        yield count % self.CHUNK


# ----------------------------------------------------------------------------
# Help window
# ----------------------------------------------------------------------------
def show_help(root, holder):
    import tkinter as tk
    from tkinter import ttk
    if holder.get("win") is not None:
        try:
            holder["win"].destroy()
        except Exception:  # noqa
            pass
    win = holder["win"] = tk.Toplevel(root)
    win.title(T("help_title"))
    fit_geometry(win, 780, 700)

    def close(_e=None):
        holder["win"] = None
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Escape>", close)

    bottom = ttk.Frame(win, padding=(12, 6, 12, 10))
    bottom.pack(side="bottom", fill="x")
    ttk.Button(bottom, text="Patreon", command=lambda: open_url(PATREON_URL)).pack(side="left")
    ttk.Button(bottom, text="PayPal", command=lambda: open_url(PAYPAL_URL)).pack(side="left", padx=6)
    note = ttk.Label(bottom, text="", foreground="#2a7a2a")

    def copy_paypal():
        win.clipboard_clear()
        win.clipboard_append(PAYPAL_ID)
        note.configure(text=T("copied", s=PAYPAL_ID))
    ttk.Button(bottom, text=T("btn_copy_paypal"), command=copy_paypal).pack(side="left")
    note.pack(side="left", padx=10)
    ttk.Button(bottom, text=T("btn_close"), command=close).pack(side="right")

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True)
    txt = tk.Text(frame, wrap="word", padx=16, pady=10, font=("Segoe UI", 10), relief="flat",
                  cursor="arrow", bg="#ffffff")
    sb = ttk.Scrollbar(frame, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    txt.tag_configure("title", font=("Segoe UI", 15, "bold"), spacing3=2)
    txt.tag_configure("sub", foreground="#666", spacing3=6)
    txt.tag_configure("h", font=("Segoe UI", 11, "bold"), spacing1=12, spacing3=4)
    txt.tag_configure("p", spacing3=3)
    txt.tag_configure("code", font=("Consolas", 10), background="#f2f2f2")
    txt.tag_configure("link", foreground="#0b62c4", underline=True)
    nlink = [0]

    def link(text, url):
        tag = f"url{nlink[0]}"
        nlink[0] += 1
        txt.insert("end", text, ("link", tag))
        txt.tag_bind(tag, "<Button-1>", lambda e: open_url(url))
        txt.tag_bind(tag, "<Enter>", lambda e: txt.configure(cursor="hand2"))
        txt.tag_bind(tag, "<Leave>", lambda e: txt.configure(cursor="arrow"))

    txt.insert("end", f"Gerber → DXF  v{VERSION}\n", "title")
    txt.insert("end", f"Gerber2DXF by {DEV_NAME}\n", "sub")
    for line in HELP[LANG].strip().splitlines():
        if line.startswith("# "):
            txt.insert("end", line[2:] + "\n", "h")
        elif line.startswith("Gerber2DXF.exe "):
            txt.insert("end", line + "\n", "code")
        else:
            txt.insert("end", line + "\n", "p")
    txt.insert("end", T("h_dev") + "\n", "h")
    txt.insert("end", f"{DEV_NAME} — ")
    link(PATREON_URL, PATREON_URL)
    txt.insert("end", "\n")
    txt.insert("end", T("h_license") + "\n", "h")
    txt.insert("end", T("license_text", dev=DEV_NAME) + "\n", "p")
    txt.insert("end", T("h_support") + "\n", "h")
    txt.insert("end", T("support_text") + "\n", "p")
    txt.insert("end", "Patreon: ")
    link(PATREON_URL, PATREON_URL)
    txt.insert("end", "\nPayPal: ")
    link(PAYPAL_ID, PAYPAL_URL)
    txt.insert("end", "\n")
    txt.insert("end", T("h_project") + "\n", "h")
    link(PROJECT_URL, PROJECT_URL)
    txt.insert("end", "\n")
    txt.configure(state="disabled")
    return win


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
def run_gui(prefill=None):
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    dpi_aware()
    root = tk.Tk()
    root.title(f"Gerber → DXF  v{VERSION}  —  {DEV_NAME}")
    fit_geometry(root, 1280, 860)
    root.minsize(860, 560)
    ui = UI()
    q = queue.Queue()
    in_var, out_var = tk.StringVar(), tk.StringVar()
    per_var, all_var = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
    st = {"out_touched": False, "groups": None, "src": None, "token": 0}
    help_holder = {"win": None}

    frm = ttk.Frame(root, padding=(10, 8, 10, 2))
    frm.pack(side="top", fill="x")
    frm.columnconfigure(1, weight=1)

    ui.reg(ttk.Label(frm), "in_label").grid(row=0, column=0, sticky="w")
    in_entry = ttk.Entry(frm, textvariable=in_var)
    in_entry.grid(row=0, column=1, sticky="ew", padx=5)
    bf = ttk.Frame(frm)
    bf.grid(row=0, column=2, sticky="w")
    zip_btn = ttk.Button(bf, text="Zip…", width=7)
    zip_btn.pack(side="left")
    dir_btn = ui.reg(ttk.Button(bf, width=8), "btn_folder")
    dir_btn.pack(side="left", padx=(3, 0))
    refresh_btn = ui.reg(ttk.Button(bf), "btn_refresh")
    refresh_btn.pack(side="left", padx=(3, 0))

    ui.reg(ttk.Label(frm), "out_label").grid(row=1, column=0, sticky="w", pady=6)
    ttk.Entry(frm, textvariable=out_var).grid(row=1, column=1, sticky="ew", padx=5)
    browse_btn = ui.reg(ttk.Button(frm), "btn_browse")
    browse_btn.grid(row=1, column=2, sticky="w")

    opt = ttk.Frame(frm)
    opt.grid(row=2, column=0, columnspan=3, sticky="ew")
    ui.reg(ttk.Checkbutton(opt, variable=per_var), "opt_per").pack(side="left")
    ui.reg(ttk.Checkbutton(opt, variable=all_var), "opt_all").pack(side="left", padx=15)
    help_btn = ui.reg(ttk.Button(opt), "btn_help")
    help_btn.pack(side="right")
    lang_combo = ttk.Combobox(opt, state="readonly", width=12, values=[n for _, n in LANGS])
    lang_combo.current([c for c, _ in LANGS].index(LANG))
    lang_combo.pack(side="right", padx=(4, 12))
    ui.reg(ttk.Label(opt), "lang_label").pack(side="right")

    act = ttk.Frame(frm)
    act.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 4))
    save_btn = ui.reg(ttk.Button(act), "btn_save_checked")
    save_btn.pack(side="left")
    conv_btn = ui.reg(ttk.Button(act), "btn_convert_all")
    conv_btn.pack(side="left", padx=(8, 0))
    open_btn = ui.reg(ttk.Button(act, state="disabled"), "btn_open_out")
    open_btn.pack(side="left", padx=8)
    bar = ttk.Progressbar(act, mode="determinate")
    bar.pack(side="left", fill="x", expand=True)

    paned = ttk.PanedWindow(root, orient="vertical")
    paned.pack(fill="both", expand=True, padx=10, pady=(0, 8))
    pv_frame = ttk.Frame(paned)
    log_frame = ttk.Frame(paned)
    paned.add(pv_frame, weight=6)
    paned.add(log_frame, weight=1)
    preview = Preview(pv_frame, ui)
    logbox = tk.Text(log_frame, height=6, wrap="none", font=("Consolas", 9))
    lsb = ttk.Scrollbar(log_frame, command=logbox.yview)
    logbox.configure(yscrollcommand=lsb.set)
    lsb.pack(side="right", fill="y")
    logbox.pack(side="left", fill="both", expand=True)

    # --- plumbing ---------------------------------------------------------
    def log(msg):
        q.put(("log", msg))

    def post(fn):
        q.put(("call", fn))

    def busy(on):
        for b in (save_btn, conv_btn, refresh_btn):
            b.configure(state="disabled" if on else "normal")
        if on:
            bar.configure(mode="indeterminate")
            bar.start(12)
        else:
            bar.stop()
            bar.configure(mode="determinate", value=0)

    def fail(msg):
        busy(False)
        logbox.insert("end", f"{T('error')}: {msg}\n")
        logbox.see("end")
        messagebox.showerror("Gerber → DXF", msg, parent=root)

    def cur_input():
        return in_var.get().strip().strip('"')

    def get_out(inp):
        return Path(out_var.get().strip().strip('"') or default_out(inp))

    # --- loading -----------------------------------------------------------
    def start_load(explicit=True):
        inp = cur_input()
        if not inp or not Path(inp).exists():
            if explicit:
                messagebox.showwarning("Gerber → DXF", T("warn_no_input"), parent=root)
            return
        if not out_var.get().strip():
            out_var.set(str(default_out(inp)))
        st["token"] += 1
        token = st["token"]
        st["groups"], st["src"] = None, None
        logbox.delete("1.0", "end")
        open_btn.configure(state="disabled")
        busy(True)
        preview.show_message("st_loading")

        def work():
            try:
                groups = load([inp], log)
                post(lambda: loaded(token, inp, groups))
            except Exception as e:  # noqa
                post(lambda err=str(e): load_failed(token, err))
        threading.Thread(target=work, daemon=True).start()

    def loaded(token, inp, groups):
        if token != st["token"]:
            return  # a newer file was chosen meanwhile
        busy(False)
        if not groups:
            preview.show_message("err_none_found")
            return
        st["groups"], st["src"] = groups, inp
        log(T("log_loaded", g=len(groups), n=sum(len(g.layers) for g in groups)))
        preview.set_groups(groups)

    def load_failed(token, err):
        if token == st["token"]:
            preview.show_message("placeholder")
            fail(err)

    def set_input(path):
        if not path:
            return
        in_var.set(path)
        if not st["out_touched"]:
            out_var.set(str(default_out(path)))
        start_load(explicit=True)

    def pick_zip():
        set_input(filedialog.askopenfilename(title=T("dlg_zip"), parent=root,
                                             filetypes=[("Zip", "*.zip"), (T("dlg_all_files"), "*.*")]))

    def pick_dir():
        set_input(filedialog.askdirectory(title=T("dlg_folder"), parent=root))

    def pick_out():
        d = filedialog.askdirectory(title=T("dlg_out"), parent=root)
        if d:
            st["out_touched"] = True
            out_var.set(d)

    # --- saving ------------------------------------------------------------
    def saved(n, path):
        busy(False)
        if n:
            open_btn.configure(state="normal")
        preview.set_status("st_saved", n=n, path=path)

    def save_checked():
        sel = preview.selection()
        if not sel:
            messagebox.showwarning("Gerber → DXF", T("warn_no_input"), parent=root)
            return
        g, only = sel
        if not only:
            messagebox.showwarning("Gerber → DXF", T("warn_no_layer"), parent=root)
            return
        per, comb = per_var.get(), all_var.get()
        if not (per or comb):
            messagebox.showwarning("Gerber → DXF", T("warn_no_opt"), parent=root)
            return
        out_dir = get_out(st["src"]) / g.out_rel
        busy(True)
        preview.set_status("st_saving", n=len(only))

        def work():
            try:
                n = write_group(g, out_dir, log, per, comb, only)
                post(lambda: saved(n, out_dir))
            except Exception as e:  # noqa
                post(lambda err=str(e): fail(err))
        threading.Thread(target=work, daemon=True).start()

    def convert_all():
        inp = cur_input()
        if not inp or not Path(inp).exists():
            messagebox.showwarning("Gerber → DXF", T("warn_no_input"), parent=root)
            return
        per, comb = per_var.get(), all_var.get()
        if not (per or comb):
            messagebox.showwarning("Gerber → DXF", T("warn_no_opt"), parent=root)
            return
        groups = st["groups"] if st["src"] == inp else None
        out_root = get_out(inp)
        busy(True)

        def work():
            try:
                gs = groups if groups is not None else load([inp], log)
                total = sum(write_group(g, out_root / g.out_rel, log, per, comb) for g in gs)
                log(T("log_done", n=total))
                post(lambda: saved(total, out_root))
            except Exception as e:  # noqa
                post(lambda err=str(e): fail(err))
        threading.Thread(target=work, daemon=True).start()

    def change_lang(_e=None):
        code = LANGS[lang_combo.current()][0]
        set_lang(code)
        SETTINGS["lang"] = code
        save_settings()
        ui.refresh()
        if help_holder["win"] is not None:
            show_help(root, help_holder)

    zip_btn.configure(command=pick_zip)
    dir_btn.configure(command=pick_dir)
    refresh_btn.configure(command=start_load)
    browse_btn.configure(command=pick_out)
    save_btn.configure(command=save_checked)
    conv_btn.configure(command=convert_all)
    open_btn.configure(command=lambda: open_path(get_out(cur_input())))
    help_btn.configure(command=lambda: show_help(root, help_holder))
    lang_combo.bind("<<ComboboxSelected>>", change_lang)
    in_entry.bind("<Return>", lambda e: start_load())
    root.bind("<F1>", lambda e: show_help(root, help_holder))
    root.bind("<F5>", lambda e: start_load())

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    logbox.insert("end", val + "\n")
                    logbox.see("end")
                else:
                    try:
                        val()
                    except Exception as e:  # noqa
                        fail(str(e))
        except queue.Empty:
            pass
        root.after(100, poll)

    # handles for automated GUI tests
    root.gui = {"preview": preview, "set_input": set_input, "save_checked": save_checked,
                "convert_all": convert_all, "change_lang": change_lang, "lang_combo": lang_combo,
                "help": help_holder, "show_help": lambda: show_help(root, help_holder),
                "state": st, "out_var": out_var, "logbox": logbox}
    preview.draw_placeholder()
    if prefill:
        root.after(200, lambda: set_input(prefill))
    poll()
    return root


# ----------------------------------------------------------------------------
def main():
    load_settings()
    argv = sys.argv[1:]
    pos, skip = [], False
    for k, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in ("-o", "--output", "--lang"):
            skip = True
            if a == "--lang" and k + 1 < len(argv):
                set_lang(argv[k + 1])
            continue
        if a.startswith("--lang="):
            set_lang(a.split("=", 1)[1])
        if not a.startswith("-"):
            pos.append(a)
    has_out = any(a in ("-o", "--output") or a.startswith("--output=") for a in argv)
    # no inputs -> GUI; windowed exe with a dropped file (no -o) -> GUI with the file loaded
    wants_help = any(a in ("-h", "--help") for a in argv)
    if not wants_help and (not pos or (sys.stdout is None and not has_out)):
        run_gui(pos[0] if pos else None).mainloop()
        return
    ap = argparse.ArgumentParser(description=T("cli_desc") + f"  v{VERSION}, Gerber2DXF by {DEV_NAME}")
    ap.add_argument("inputs", nargs="+", help=T("cli_inputs"))
    ap.add_argument("-o", "--output", help=T("cli_out"))
    ap.add_argument("--no-layers", action="store_true", help=T("cli_nolayers"))
    ap.add_argument("--no-combined", action="store_true", help=T("cli_nocombined"))
    ap.add_argument("--lang", choices=[c for c, _ in LANGS], help=T("cli_lang"))
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
        log(f"{T('error')}: {e}")
        n = 0
    sys.exit(0 if n else 1)


if __name__ == "__main__":
    main()
