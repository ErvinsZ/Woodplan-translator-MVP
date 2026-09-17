import fitz
import re

SRC = '/mnt/user-data/uploads/Flip_Top_Miter_Saw_Workbench_Table_Step_by_Step_in.pdf'
OUT = '/mnt/user-data/outputs/Step1_Frame_DE_preview.pdf'

GLOSSARY = {
    "WOODSHOPKIDS.ETSY.COM": "WOODSHOPKIDS.ETSY.COM",
    "Not for commercial use! Copywrite protected.": "Nicht für kommerz. Nutzung! Urheberrechtlich geschützt.",
    "© 2022. Woodshopkids Ltd.": "© 2022. Woodshopkids Ltd.",
    "Do not share the files with anyone without ": "Dateien nicht ohne ",
    "written permission from Woodshopkids Ltd. ": "schriftl. Genehmigung v. Woodshopkids Ltd. weitergeben.",
    "Notes": "Notizen",
    "Mobile Table Saw, Miter Saw,": "Mobile Tischkreissäge, Kappsäge,",
    "and Router Station Build Plans.": "und Frästisch-Bauanleitung.",
    "Step 1. Frame.": "Schritt 1. Rahmen.",
    "Perspective": "Perspektive",
    "Back view": "Rückansicht",
    "Top view": "Draufsicht",
    "Scale 1:10": "Maßstab 1:10",
    'Frame is built from 2x2".': 'Rahmen besteht aus 2x2".',
    " Please predrill before adding 2 wood screws for each detail.": " Bitte vorbohren, dann 2 Schrauben je Teil einsetzen.",
    " Secure with wood screws, ensuring everything": " Mit Holzschrauben sichern, alles",
    " is aligned properly.": " korrekt ausrichten.",
    "Cutout List": "Zuschnitte",
    "screws": "Schrauben",
    "pc.": "Stk.",
}

CODE_TOKEN = re.compile(r'^(D\d+|S\d+|\d+[\d\s\'"\/\.°xX×-]*|[0-9]+)$')

def classify_and_translate(text):
    stripped = text.strip()
    leading_ws = text[:len(text) - len(text.lstrip())]
    trailing_ws = text[len(text.rstrip()):]
    if stripped in GLOSSARY:
        return leading_ws + GLOSSARY[stripped] + trailing_ws, True
    if CODE_TOKEN.match(stripped):
        return text, False
    parts = re.split(r'(\s+|,)', stripped)
    out, changed = [], False
    for p in parts:
        key = p.strip()
        if not key:
            out.append(p)
        elif CODE_TOKEN.match(key):
            out.append(p)
        elif key in GLOSSARY:
            out.append(GLOSSARY[key]); changed = True
        else:
            out.append(p)
    return leading_ws + ''.join(out) + trailing_ws, changed

def text_width(text, fontsize):
    return fitz.get_text_length(text, fontname="helv", fontsize=fontsize)

doc = fitz.open(SRC)
page = doc[3]
d = page.get_text('dict')

items = []
for block in d['blocks']:
    if 'lines' not in block: continue
    for line in block['lines']:
        line_dir = line.get('dir', (1, 0))  # direction lives on the LINE, not the span
        for span in line['spans']:
            orig = span['text']
            if not orig.strip(): continue
            span['_line_dir'] = line_dir
            items.append(span)

report = []
for span in items:
    bbox = fitz.Rect(span['bbox'])
    origin = span['origin']  # exact baseline start point (x, y)
    font_size = span['size']
    rot_dir = span.get('_line_dir', (1, 0))  # writing direction vector; (0,-1)/(0,1) = rotated/vertical text
    is_rotated = abs(rot_dir[0]) < 0.5  # roughly vertical

    translated, changed = classify_and_translate(span['text'])

    # measure along the actual writing direction, not raw bbox.width
    available_len = bbox.height if is_rotated else bbox.width
    w = text_width(translated, font_size)
    used_size = font_size
    fits_ok = w <= available_len
    if not fits_ok:
        shrink = font_size
        while shrink > font_size * 0.75:
            shrink -= 0.3
            w2 = text_width(translated, shrink)
            if w2 <= available_len:
                used_size = shrink; w = w2; fits_ok = True
                break

    page.add_redact_annot(bbox, fill=(1, 1, 1))
    items[items.index(span)] = (span, translated, used_size, origin, rot_dir, fits_ok, w, available_len)

    if changed or not fits_ok:
        report.append({
            "original": span['text'], "translated": translated,
            "font": f"{font_size}->{round(used_size,1)}",
            "avail_len": round(available_len,1), "text_len": round(w,1),
            "rotated": is_rotated, "overflow": not fits_ok
        })

page.apply_redactions()

for span, translated, used_size, origin, rot_dir, fits_ok, w, avail in items:
    if not translated.strip():
        continue
    if abs(rot_dir[0]) < 0.5:  # vertical text: rotate 90
        angle = 90 if rot_dir[1] < 0 else -90
        page.insert_text(origin, translated, fontsize=used_size, fontname="helv", rotate=angle)
    else:
        page.insert_text(origin, translated, fontsize=used_size, fontname="helv")

doc.save(OUT)

print(f"{len(report)} spans changed or flagged:\n")
for r in report:
    flag = " <<< OVERFLOW" if r["overflow"] else ""
    rot = " [ROTATED]" if r["rotated"] else ""
    print(f"- {r['original']!r} -> {r['translated']!r} | font {r['font']} | {r['text_len']}/{r['avail_len']}pt{rot}{flag}")
