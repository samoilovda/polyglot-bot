"""Генерация графических карточек с цитатами (Pillow, 100% локально, 0 токенов ИИ)."""
import os
import re
from PIL import Image, ImageDraw, ImageFont

# Попытка подключить биди/решейпер для корректного отображения арабской вязи
try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    HAS_BIDI = True
except ImportError:
    HAS_BIDI = False

# Кандидаты шрифтов для разных ОС (macOS, Linux / GitHub Actions)
FONT_CANDIDATES = [
    # Serif / элегантные
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # macOS системные
    "/System/Library/Fonts/Georgia.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
]

ARABIC_FONT_CANDIDATES = [
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
]


def find_font(candidates, size):
    """Ищет первый доступный шрифт из списка кандидатов."""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def get_font_for_culture(culture, size, bold=False):
    """Подбирает подходящий шрифт с учетом культуры (особенно для арабского)."""
    if culture == "ar":
        font = find_font(ARABIC_FONT_CANDIDATES, size)
        if font:
            return font
    return find_font(FONT_CANDIDATES, size)


def wrap_text(text, font, max_width, draw):
    """Разбивает текст на строки, чтобы они укладывались в max_width пикселей."""
    words = text.split()
    if not words:
        return []
    lines = []
    current_line = []

    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_w = bbox[2] - bbox[0]
        if line_w <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    if current_line:
        lines.append(" ".join(current_line))
    return lines


def format_arabic_for_display(text):
    """Форматирует арабский текст для корректного отображения без движка Raqm."""
    if HAS_BIDI:
        try:
            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
        except Exception:
            return text
    return text


def generate_card(quote, culture, lesson=None, output_path="card.png", width=1080, height=1350):
    """Генерирует стильную типографическую карточку (1080x1350) в формате PNG."""
    # 1. Холст с градиентом
    img = Image.new("RGBA", (width, height), (18, 22, 31, 255))
    draw = ImageDraw.Draw(img)

    # Мягкий вертикальный градиент
    top_color = (24, 30, 42)
    bottom_color = (13, 16, 23)
    for y in range(height):
        factor = y / height
        r = int(top_color[0] + factor * (bottom_color[0] - top_color[0]))
        g = int(top_color[1] + factor * (bottom_color[1] - top_color[1]))
        b = int(top_color[2] + factor * (bottom_color[2] - top_color[2]))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # 2. Изящная рамка
    margin = 54
    frame_color = (55, 65, 81, 160)
    draw.rectangle(
        [(margin, margin), (width - margin, height - margin)],
        outline=frame_color,
        width=2,
    )
    # Внутренняя тонкая декоративная линия
    inner_margin = margin + 8
    draw.rectangle(
        [(inner_margin, inner_margin), (width - inner_margin, height - inner_margin)],
        outline=(45, 55, 72, 80),
        width=1,
    )

    # Угловые золотистые акценты
    corner_len = 24
    corner_color = (212, 175, 55, 200)
    corners = [
        (margin, margin, margin + corner_len, margin, margin, margin + corner_len),
        (width - margin, margin, width - margin - corner_len, margin, width - margin, margin + corner_len),
        (margin, height - margin, margin + corner_len, height - margin, margin, height - margin - corner_len),
        (width - margin, height - margin, width - margin - corner_len, height - margin, width - margin, height - margin - corner_len),
    ]
    for x1, y1, x2, y2, x3, y3 in corners:
        draw.line([(x1, y1), (x2, y2)], fill=corner_color, width=3)
        draw.line([(x1, y1), (x3, y3)], fill=corner_color, width=3)

    # 3. Шапка: название культуры и флаг
    header_font = find_font(FONT_CANDIDATES, 26)
    flag_code = quote.get("country", "").upper()
    title_text = f"• POLYGLOT WISDOM •"
    header_bbox = draw.textbbox((0, 0), title_text, font=header_font)
    header_w = header_bbox[2] - header_bbox[0]
    draw.text(((width - header_w) // 2, margin + 40), title_text, font=header_font, fill=(156, 163, 175, 255))

    # 4. Декоративная крупная кавычка на фоне
    quote_mark_font = find_font(FONT_CANDIDATES, 220)
    if culture != "ar":
        draw.text((margin + 40, margin + 110), "“", font=quote_mark_font, fill=(212, 175, 55, 45))

    # 5. Текст цитаты
    raw_text = quote["text"].strip()
    is_ar = culture == "ar"

    # Подбор размера шрифта в зависимости от длины текста
    text_len = len(raw_text)
    if text_len < 80:
        quote_size = 54
        line_spacing = 26
    elif text_len < 160:
        quote_size = 46
        line_spacing = 22
    elif text_len < 240:
        quote_size = 38
        line_spacing = 18
    else:
        quote_size = 32
        line_spacing = 14

    quote_font = get_font_for_culture(culture, quote_size)
    max_text_width = width - (margin * 2) - 140

    if is_ar:
        wrapped_lines = wrap_text(raw_text, quote_font, max_text_width, draw)
        wrapped_lines = [format_arabic_for_display(l) for l in wrapped_lines]
    else:
        wrapped_lines = wrap_text(raw_text, quote_font, max_text_width, draw)

    # Расчет высоты текстового блока
    line_heights = []
    for line in wrapped_lines:
        bbox = draw.textbbox((0, 0), line, font=quote_font)
        line_heights.append(bbox[3] - bbox[1])
    total_quote_height = sum(line_heights) + line_spacing * max(0, len(wrapped_lines) - 1)

    # Вертикальное центрирование цитаты в основной рабочей зоне
    content_top = margin + 180
    content_bottom = height - margin - 280
    available_height = content_bottom - content_top
    start_y = content_top + max(0, (available_height - total_quote_height) // 2)

    # Рендеринг строк цитаты
    current_y = start_y
    for i, line in enumerate(wrapped_lines):
        bbox = draw.textbbox((0, 0), line, font=quote_font)
        line_w = bbox[2] - bbox[0]
        # Центрирование строки
        line_x = (width - line_w) // 2
        draw.text((line_x, current_y), line, font=quote_font, fill=(248, 250, 252, 255))
        current_y += line_heights[i] + line_spacing

    # 6. Автор и источник (в нижней трети карточки)
    author_y = height - margin - 220

    # Разделительная линия
    sep_w = 160
    draw.line(
        [((width - sep_w) // 2, author_y), ((width + sep_w) // 2, author_y)],
        fill=(212, 175, 55, 180),
        width=2,
    )

    author_name = quote["author"]
    if quote.get("author_en") and quote["author_en"] != quote["author"]:
        author_name += f" ({quote['author_en']})"

    if is_ar:
        author_name = format_arabic_for_display(author_name)

    author_font = get_font_for_culture(culture, 34)
    author_bbox = draw.textbbox((0, 0), author_name, font=author_font)
    author_w = author_bbox[2] - author_bbox[0]
    draw.text(
        ((width - author_w) // 2, author_y + 24),
        author_name,
        font=author_font,
        fill=(255, 255, 255, 255),
    )

    # Источник
    source_text = quote.get("source", "").strip()
    if source_text:
        if is_ar:
            source_text = format_arabic_for_display(source_text)
        source_font = get_font_for_culture(culture, 24)
        source_bbox = draw.textbbox((0, 0), source_text, font=source_font)
        source_w = source_bbox[2] - source_bbox[0]
        draw.text(
            ((width - source_w) // 2, author_y + 72),
            source_text,
            font=source_font,
            fill=(148, 163, 184, 255),
        )

    # 7. Футер: фокусное слово с транскрипцией (если передано)
    if lesson and lesson.get("languages") and culture in lesson["languages"]:
        w_info = lesson["languages"][culture]
        word = w_info.get("word", "")
        ipa = w_info.get("ipa", "")
        if word:
            footer_text = f"✨ {word} {ipa}".strip()
            footer_font = get_font_for_culture(culture, 22)
            footer_bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
            footer_w = footer_bbox[2] - footer_bbox[0]
            draw.text(
                ((width - footer_w) // 2, height - margin - 48),
                footer_text,
                font=footer_font,
                fill=(212, 175, 55, 220),
            )

    # Сохранение изображения
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    img.convert("RGB").save(output_path, "PNG", optimize=True)
    print(f"Quote card generated: {output_path}")
    return output_path
