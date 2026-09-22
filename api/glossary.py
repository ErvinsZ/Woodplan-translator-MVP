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

    # --- Added from a full 15-page sweep of sample_plan.pdf (see
    # classify_translate.py's tokenizer for how word-level entries below
    # combine with part codes/numbers in mixed spans like "D6, 2 pc"). ---

    # Word-level: safe because neither forms a German compound with its
    # neighboring words, unlike e.g. "wood" + "screw" (-> "Holzschraube",
    # one word) which is why "Wood screw" etc. below are whole phrases
    # instead of word-by-word entries.
    "pc": {
        "de": "Stk.",
    },
    "pcs": {
        "de": "Stk.",
    },
    "P": {
        # Recurs identically on nearly every page in the same position;
        # a design glyph/icon rendered as literal text, not an English
        # word, so intentionally left unchanged (same treatment as
        # "WOODSHOPKIDS.ETSY.COM" above).
        "de": "P",
    },

    # Cover page / running title
    "Mobile Flip-Top Miter Saw": {
        "de": "Mobile Kappsäge mit Klapptisch",
    },
    "Station Build Plans.": {
        "de": "- Bauanleitung.",
    },
    "Mobile Miter Saw Flip-Top Station Build Plans.": {
        "de": "Mobile Kappsäge mit Klapptisch - Bauanleitung.",
    },
    "Front view": {
        "de": "Vorderansicht",
    },
    "Left view": {
        "de": "Linke Ansicht",
    },
    "Overall dimensions": {
        "de": "Gesamtabmessungen",
    },
    "Open": {
        "de": "Offen",
    },
    "Shut": {
        "de": "Geschlossen",
    },
    "Detail View": {
        "de": "Detailansicht",
    },
    "Top View open": {
        "de": "Draufsicht offen",
    },
    "Top View closed": {
        "de": "Draufsicht geschlossen",
    },
    "Perspective open": {
        "de": "Perspektive offen",
    },
    "Perspective closed": {
        "de": "Perspektive geschlossen",
    },
    "Perspective, Side View": {
        "de": "Perspektive, Seitenansicht",
    },
    "Perspective, Front View": {
        "de": "Perspektive, Vorderansicht",
    },

    # Page 3 inventory / materials / safety / tools lists
    "Inventory List.": {
        "de": "Inventarliste.",
    },
    "Inventory List": {
        "de": "Inventarliste",
    },
    "Scale 1:2": {
        "de": "Maßstab 1:2",
    },
    "Furniture wheels with brakes,": {
        "de": "Möbelrollen mit Bremse,",
    },
    "4 pc + screws.": {
        "de": "4 Stk. + Schrauben.",
    },
    "Furniture wheels with brakes": {
        "de": "Möbelrollen mit Bremse",
    },
    " Furniture wheels (4 pc)": {
        "de": " Möbelrollen (4 Stk.)",
    },
    "S1, Wood screw, 2\".": {
        "de": "S1, Holzschraube, 2\".",
    },
    "S2, Wood Screw, 3\".": {
        "de": "S2, Holzschraube, 3\".",
    },
    "Material list:": {
        "de": "Materialliste:",
    },
    " Plywood 1\"": {
        "de": " Sperrholz 1\"",
    },
    " Wood 4x2\", 2x2\"": {
        "de": " Holz 4x2\", 2x2\"",
    },
    " Wood screws": {
        "de": " Holzschrauben",
    },
    " Paint or varnish": {
        "de": " Farbe oder Lack",
    },
    " Cramps": {
        "de": " Zwingen",
    },
    "Safety Tips:": {
        "de": "Sicherheitshinweise:",
    },
    # Split across 2 lines in the PDF; translations chosen so the two
    # fragments read as one correct sentence when stacked, same pattern
    # as "Do not share the files.../written permission..." above.
    "Always wear safety goggles and gloves": {
        "de": "Schutzbrille und Handschuhe tragen,",
    },
    "when cutting and sanding wood.": {
        "de": "wenn Holz gesägt oder geschliffen wird.",
    },
    # Split across 3 lines, two sentences with the boundary falling
    # mid-line; German word choice shifted slightly from the literal
    # English breakpoints so no word (e.g. "Werkzeug") is broken across
    # the line boundary. The 2nd/3rd line split point ("Material" moved
    # onto line 2 rather than staying on line 3, unlike the literal
    # English breakpoint) was chosen by checking the rendered width of
    # each candidate split against each line's real bbox from
    # reference/sample_plan.pdf -- the literal-English split put "Material
    # außerhalb der Reichweite von Kindern aufbewahren." (60 chars) alone
    # on line 3's much narrower box (sized for the shorter English
    # "materials out of reach of children."), needing the font shrunk to
    # ~50% to fit at all and visibly overlapping the page's dashed layout
    # guide line to its right. This split balances both lines to need
    # only ~67-72% of the original size, both well inside a single
    # consistent-looking shrink instead of one line at full size and the
    # next drastically smaller.
    "Work in a well-ventilated area when ": {
        "de": "In einem gut belüfteten Bereich arbeiten, wenn ",
    },
    "using paint or stain. Keep tools and ": {
        "de": "Farbe oder Beize verwendet wird. Werkzeug und Material ",
    },
    "materials out of reach of children.": {
        "de": "außerhalb der Reichweite von Kindern aufbewahren.",
    },
    "Tools:": {
        "de": "Werkzeuge:",
    },
    " Table Saw": {
        "de": " Tischkreissäge",
    },
    " Miter Saw": {
        "de": " Kappsäge",
    },
    " Jig Saw": {
        "de": " Stichsäge",
    },
    " Drill/driver": {
        "de": " Bohrschrauber",
    },
    " Clamps": {
        "de": " Zwingen",
    },
    " Safety goggles and gloves": {
        "de": " Schutzbrille und Handschuhe",
    },
    " Tape measure": {
        "de": " Maßband",
    },
    " Square": {
        "de": " Winkel",
    },
    " Sandpaper": {
        "de": " Schleifpapier",
    },
    " Level": {
        "de": " Wasserwaage",
    },
    "Wood or metal rung,": {
        "de": "Holz- oder Metallstange,",
    },
    "diameter 22 mm, 2 pc.": {
        "de": "Durchmesser 22 mm, 2 Stk.",
    },
    "Wood or metal rung": {
        "de": "Holz- oder Metallstange",
    },
    "Wood rung, 2 pc, diameter 7/8\".": {
        "de": "Holzstange, 2 Stk., Durchmesser 7/8\".",
    },

    # Step headings (page-by-page, matches "Step 1. Frame." above)
    "Step 2. ": {
        "de": "Schritt 2. ",
    },
    "Step 3. Floor.": {
        "de": "Schritt 3. Boden.",
    },
    "Step 4. Vertical 2x2\".": {
        "de": "Schritt 4. Vertikal 2x2\".",
    },
    "Step 5. Spacers.": {
        "de": "Schritt 5. Abstandshalter.",
    },
    "Step 6. Diagonals.": {
        "de": "Schritt 6. Diagonalen.",
    },
    "Step 7. Wheels.": {
        "de": "Schritt 7. Räder.",
    },
    "Step 8. Flip-Top Assembly.": {
        "de": "Schritt 8. Klapptisch-Montage.",
    },
    "Step 9. Flip-Top.": {
        "de": "Schritt 9. Klapptisch.",
    },
    "Step 10. Temporary attach the Miter Saw": {
        "de": "Schritt 10. Kappsäge vorübergehend befestigen",
    },
    "Step 11. Table top.": {
        "de": "Schritt 11. Tischplatte.",
    },
    "Step 12. Attach Miter Saw.": {
        "de": "Schritt 12. Kappsäge befestigen.",
    },

    # Per-step cutout list captions and assembly notes
    "S2 Screws on connections ": {
        "de": "S2-Schrauben an Verbindungen ",
    },
    "Details are built from 4x2\".": {
        "de": "Teile bestehen aus 4x2\".",
    },
    "Details are built from 2x2\".": {
        "de": "Teile bestehen aus 2x2\".",
    },
    "Detail is built from 1\" plywood or alternative.": {
        "de": "Teil besteht aus 1\" Sperrholz oder Alternative.",
    },
    "S1 or S2, 2 pc on each corner": {
        "de": "S1 oder S2, 2 Stk. je Ecke",
    },
    "S2, 12 pc (2 pc on each connection)": {
        "de": "S2, 12 Stk. (2 Stk. je Verbindung)",
    },
    "S2 screws, 16+ pc. Min 2 on each detail.": {
        "de": "S2-Schrauben, 16+ Stk. Mind. 2 pro Teil.",
    },
    "S2 screws, 12+ pc. Min 2 on each detail.": {
        "de": "S2-Schrauben, 12+ Stk. Mind. 2 pro Teil.",
    },
    "Second D6 from Step 2": {
        "de": "Zweites D6 aus Schritt 2",
    },
    "Secure with S2, 4 pc ": {
        "de": "Mit S2 sichern, 4 Stk. ",
    },
    "Align and add cramps to fix the flip top in position.": {
        "de": "Zwingen anbringen, um die Klappplatte in Position zu fixieren.",
    },
    "Secure flat and level with clamps when assembling.": {
        "de": "Beim Zusammenbau mit Zwingen flach und eben sichern.",
    },
    # Split across 2 lines; reused with different fill-ins (cramps/wheels,
    # 16/20 pc) but each concrete pair is its own literal glossary entry
    # since the glossary has no wildcard/template matching.
    "Use the screws that are included with the cramps ": {
        "de": "Mitgelieferte Schrauben für die Zwingen verwenden ",
    },
    "or purchase them separately, 20 pc total.": {
        "de": "oder separat kaufen, insgesamt 20 Stk.",
    },
    "Use the screws that are included with the wheels ": {
        "de": "Mitgelieferte Schrauben für die Räder verwenden ",
    },
    "or purchase them separately, 16 pc total.": {
        "de": "oder separat kaufen, insgesamt 16 Stk.",
    },
    "45° cut": {
        "de": "45°-Schnitt",
    },
    "Cramp position": {
        "de": "Zwingenposition",
    },
    # Split across 2 lines; German recast so "attach"/"befestigen" lands
    # at the end of the clause (German infinitive-final word order)
    # rather than mirroring the English line break literally.
    "Use S1 to attach": {
        "de": "Mit S1 befestigen: ",
    },
    "the table top, 28 pc.": {
        "de": "die Tischplatte, 28 Stk.",
    },

    # Final page: finishing touches, thank-you note, attaching the saw
    " Adding Finishing Touches": {
        "de": " Abschließende Arbeiten",
    },
    " Sand the entire surface with 220 grit sandpaper.": {
        "de": " Gesamte Oberfläche mit Schleifpapier Körnung 220 schleifen.",
    },
    " Apply a coat of primer and let it dry.": {
        "de": " Grundierung auftragen und trocknen lassen.",
    },
    " Paint or stain as desired. Let dry completely.": {
        "de": " Nach Wunsch lackieren oder beizen. Vollständig trocknen lassen.",
    },
    " Thank you for your purchase!": {
        "de": " Vielen Dank für Ihren Kauf!",
    },
    " Let us know how was this build": {
        "de": " Lassen Sie uns wissen, wie dieses",
    },
    " project and contact us if we can help.": {
        "de": " Bauprojekt war, und kontaktieren Sie uns, wenn wir helfen können.",
    },
    "Fix the Miter Saw": {
        "de": "Kappsäge ",
    },
    "permanently with": {
        "de": "dauerhaft mit ",
    },
    "or alternative.": {
        "de": "oder alternativ befestigen.",
    },

    # NOT translated on purpose -- flagged rather than guessed. These 4
    # spans are one continuous instruction split across 4 lines:
    #   "Add spacers or underboard if necessary to match the Miter Saw
    #    top flat and level."
    # The referent of "to match the Miter Saw top flat and level" is
    # genuinely ambiguous (what should be flush/level with what -- the
    # underboard, the surrounding surface, or the saw's own table?) and
    # this is functional assembly guidance, not decorative copy, so it
    # is left in English with needs_review left True rather than risk a
    # confidently-wrong translation of a safety-adjacent instruction.
    # "Add spacers or "
    # "underboard if necessary"
    # "to match the Miter Saw"
    # "top flat and level."
}
