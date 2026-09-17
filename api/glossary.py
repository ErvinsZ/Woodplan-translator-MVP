"""Translation-memory glossary — pure data, no logic.

Keyed by original (English) source text, then by target language code, so
adding a new language (e.g. Spanish) is a matter of adding "es" entries
here, never touching classify_translate.py.

Keys preserve the exact whitespace of the PDF text spans they were copied
from (see classify_translate.py's known-bug #3 handling) — some entries
carry meaningful leading/trailing whitespace because the source PDF splits
one line-wrapped sentence across two separate text spans. Do not "clean up"
that whitespace; it is load-bearing for exact-span matching.

Content below is the German (de) glossary validated against
reference/sample_plan.pdf in pipeline_tested.py.
"""

GLOSSARY = {
    "WOODSHOPKIDS.ETSY.COM": {
        "de": "WOODSHOPKIDS.ETSY.COM",
    },
    "Not for commercial use! Copywrite protected.": {
        "de": "Nicht für kommerz. Nutzung! Urheberrechtlich geschützt.",
    },
    "© 2022. Woodshopkids Ltd.": {
        "de": "© 2022. Woodshopkids Ltd.",
    },
    "Do not share the files with anyone without ": {
        "de": "Dateien nicht ohne ",
    },
    "written permission from Woodshopkids Ltd. ": {
        "de": "schriftl. Genehmigung v. Woodshopkids Ltd. weitergeben.",
    },
    "Notes": {
        "de": "Notizen",
    },
    "Mobile Table Saw, Miter Saw,": {
        "de": "Mobile Tischkreissäge, Kappsäge,",
    },
    "and Router Station Build Plans.": {
        "de": "und Frästisch-Bauanleitung.",
    },
    "Step 1. Frame.": {
        "de": "Schritt 1. Rahmen.",
    },
    "Perspective": {
        "de": "Perspektive",
    },
    "Back view": {
        "de": "Rückansicht",
    },
    "Top view": {
        "de": "Draufsicht",
    },
    "Scale 1:10": {
        "de": "Maßstab 1:10",
    },
    'Frame is built from 2x2".': {
        "de": 'Rahmen besteht aus 2x2".',
    },
    " Please predrill before adding 2 wood screws for each detail.": {
        "de": " Bitte vorbohren, dann 2 Schrauben je Teil einsetzen.",
    },
    " Secure with wood screws, ensuring everything": {
        "de": " Mit Holzschrauben sichern, alles",
    },
    " is aligned properly.": {
        "de": " korrekt ausrichten.",
    },
    "Cutout List": {
        "de": "Zuschnitte",
    },
    "screws": {
        "de": "Schrauben",
    },
    "pc.": {
        "de": "Stk.",
    },
}
