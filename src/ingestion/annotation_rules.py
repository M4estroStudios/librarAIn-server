from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from typing import Any

_ASIDE_RULE = (
    "I riquadri tipografici di digressione vanno tra un solo livello di parentesi graffe: "
    "una riga `{` all'inizio e una riga `}` alla fine. Titolo in `**ALL CAPS**` dentro il blocco; "
    "non usare heading (`#`/`##`/`###`) né blockquote (`>`)."
)

OPERATOR_NOTES_CHAR_BUDGET = 8000
LEAK_SIGNATURE_LINES_PER_SECTION = 8
GUIDANCE_MAX_ANNOTATED_IMAGES = 8

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_HASH_PAREN = re.compile(r"[_ ]*[#]+[_ ]*")

_CANONICAL_ALIASES: dict[str, str] = {
    "capitolo": "capitolo",
    "sezione": "sezione",
    "header_pari": "header_pari",
    "testata_pari": "header_pari",
    "header_dispari": "header_dispari",
    "testata_dispari": "header_dispari",
    "box_curiosita": "box_curiosita",
    "doppia_box_curiosita": "box_curiosita_doppia",
    "tripla_box_curiosita": "box_curiosita_tripla",
    "box_curiosita_sandwitch": "box_curiosita_sandwich",
    "box_curiosita_sandwich": "box_curiosita_sandwich",
    "box_curiosita_wimg_connessa": "box_curiosita_wimg",
    "box_curiosita_w_img_connessa": "box_curiosita_wimg",
    "box_curiosita_con_tabella": "box_curiosita_tabella",
    "multipage_box_curiosita_part_1_top": "box_curiosita_multipage_top",
    "multipage_box_curiosita_parte_1_top": "box_curiosita_multipage_top",
    "multipage_box_curiosita_parte_2_bottom": "box_curiosita_multipage_bottom",
    "immagine_part_page_full_width": "immagine_full_width",
    "immagine_partpage_full_width": "immagine_full_width",
    "immagine_part_page_part_width": "immagine_part_width",
    "immagine_partpage_part_width": "immagine_part_width",
    "immagine_full_page": "immagine_full_page",
    "immagine_multi_page_parte_1_sx": "immagine_multipage_sx",
    "immagine_multipage_parte_1_sx": "immagine_multipage_sx",
    "immagine_multi_page_part_2_dx": "immagine_multipage_dx",
    "immagine_multipage_part_2_dx": "immagine_multipage_dx",
    "decorazione_di_fine_capitolo": "decorazione_fine_capitolo",
    "cronologia_monocolonna": "cronologia_monocolonna",
    "cronologia_su_2_colonne": "cronologia_2_colonne",
    "senso_di_marcia_multicolonna": "senso_di_marcia_multicolonna",
    "titolo_appendice": "titolo_appendice",
    "titolo_indice": "titolo_indice",
    "titolo_toc": "titolo_toc",
    "toc_1_colonna": "toc_1_colonna",
    "indice_analitico_a_2_colonne": "indice_analitico_2_colonne",
    "2_righe_di_pagine_cit": "2_righe_di_pagine_cit",
    "2_righe_di_pagine_cit_plus": "2_righe_di_pagine_cit",
    "2_plus_righe_di_pagine_cit": "2_righe_di_pagine_cit",
    "introduzione_lista": "introduzione_lista",
    "grafico_genealigico": "grafico_genealogico",
    "grafico_genealogico": "grafico_genealogico",
}

_RULES: dict[str, str] = {
    "capitolo": (
        "Questa regione è un titolo di capitolo: una sola riga `# Titolo`. "
        "Non usare `#` per box, sezioni o testate."
    ),
    "sezione": (
        "Questa regione è un titolo di sezione (centrato in pagina, spesso con filetti): "
        "una riga `## Titolo`. Non è un box e non va in `{ }`."
    ),
    "header_pari": (
        "Testata di pagina pari (numero a sinistra, titolo libro a destra). "
        "Non è un heading Markdown: omettila o lasciala fuori dal corpo."
    ),
    "header_dispari": (
        "Testata di pagina dispari (titolo capitolo a sinistra, numero a destra). "
        "Non è un heading Markdown: omettila o lasciala fuori dal corpo."
    ),
    "box_curiosita": (
        f"{_ASIDE_RULE} "
        "Un titolo ALL CAPS allineato a sinistra dentro questa regione NON è mai un heading "
        "`#`/`##`/`###`: è il titolo del riquadro in `**TITOLO**` dentro `{` `}`."
    ),
    "box_curiosita_doppia": (
        "Due box curiosità stacked, uno sopra l'altro. Ogni box è un blocco `{` `}` distinto. "
        "I titoli ALL CAPS non sono heading."
    ),
    "box_curiosita_tripla": (
        "Tre box curiosità stacked. Ogni box è un blocco `{` `}` distinto. "
        "I titoli ALL CAPS non sono heading."
    ),
    "box_curiosita_sandwich": (
        "Due box `{` `}` con in mezzo una @Sezione (`##`), centrata, con filetti. "
        "Non trattare la sezione come box e non trattare i box come heading."
    ),
    "box_curiosita_wimg": (
        "L'immagine in cima appartiene al box curiosità sottostante. "
        "Un solo blocco `{` `}` che include didascalia/immagine e testo. "
        "Titolo ALL CAPS = `**TITOLO**`, mai heading."
    ),
    "box_curiosita_tabella": (
        "Box curiosità il cui contenuto è una tabella Markdown, sempre dentro `{` `}`. "
        "Titolo ALL CAPS = `**TITOLO**`, mai heading."
    ),
    "box_curiosita_multipage_top": (
        "Parte superiore di un box curiosità a cavallo di due pagine. "
        "Apri `{` in questa pagina; non chiudere se il box continua sotto."
    ),
    "box_curiosita_multipage_bottom": (
        "Parte inferiore di un box curiosità iniziato nella pagina precedente. "
        "Chiudi `}` in questa pagina; non riaprire un heading."
    ),
    "immagine_full_width": (
        "Immagine a tutta colonna. Didascalia in italics o blockquote `>` immediatamente sotto. "
        "Non usare `>` per i box."
    ),
    "immagine_part_width": (
        "Immagine a larghezza parziale. Didascalia in italics o `>` accanto/sotto, "
        "secondo il lato della pagina. Non è un box."
    ),
    "immagine_full_page": "Immagine a piena pagina. Didascalia sotto in italics o `>`.",
    "immagine_multipage_sx": (
        "Metà sinistra di un'immagine a cavallo di due pagine. Trascrivi la didascalia se presente."
    ),
    "immagine_multipage_dx": (
        "Metà destra di un'immagine a cavallo di due pagine. Trascrivi la didascalia se presente."
    ),
    "decorazione_fine_capitolo": (
        "Ornamento a fine capitolo: nessuna didascalia, nessun testo da inventare. Ometti o nota brevemente."
    ),
    "cronologia_monocolonna": (
        "Cronologia a una colonna. Una voce per riga; non fondere due anni sulla stessa riga."
    ),
    "cronologia_2_colonne": (
        "Cronologia a due colonne. Completa prima la colonna sinistra dall'alto in basso, "
        "poi la destra. Mai due voci (es. `Nome (anni) Nome (anni)`) sulla stessa riga. "
        "Trascrivi gli anni con `a.C.` se in pagina sono a.C."
    ),
    "senso_di_marcia_multicolonna": (
        "Ordine di lettura multicolonna: colonna intera dall'alto in basso, poi la colonna successiva. "
        "Non leggere riga-per-riga attraverso le colonne."
    ),
    "titolo_appendice": "Titolo di appendice: heading `#` o `##` evidente, non un box.",
    "titolo_indice": "Marca l'inizio dell'indice analitico. Non è una voce di indice.",
    "titolo_toc": "Marca l'inizio del sommario (ToC). Non è una voce del sommario.",
    "toc_1_colonna": (
        "Sommario a una colonna: a sinistra il numero di pagina, a destra il titolo di capitolo. "
        "Una voce per riga. Sul primo numero può comparire `p.`."
    ),
    "indice_analitico_2_colonne": (
        "Indice analitico a due colonne. Colonna per colonna dall'alto in basso. "
        "Nomi di persona in regular; nomi di luoghi in italics (`*Luogo*, 12, 15`)."
    ),
    "2_righe_di_pagine_cit": (
        "Se una riga ha solo numeri di pagina (niente lemma a sinistra, stessa colonna), "
        "sono pagine della voce della riga superiore. Conserva la riga di soli numeri."
    ),
    "introduzione_lista": "Testo introduttivo o lista in appendice: corpo normale, non heading di capitolo.",
    "grafico_genealogico": (
        "Trascrivi il contenuto del grafico genealogico in elenco o testo strutturato; non inventare rami."
    ),
}

OVERLAY_USER_INSTRUCTION = (
    "Questa seconda immagine mostra le regioni annotate dall'operatore. "
    "I riquadri colorati e le etichette NON sono contenuto della pagina: non trascriverli mai."
)


def _strip_accents(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )


def normalize_label(name: str) -> str:
    raw = _strip_accents((name or "").strip().lower())
    raw = raw.replace("/", " ")
    raw = raw.replace("+", " plus ")
    raw = _HASH_PAREN.sub(" ", raw)
    raw = raw.replace("(", " ").replace(")", " ").replace(",", " ")
    slug = _NON_ALNUM.sub("_", raw).strip("_")
    if slug in _CANONICAL_ALIASES:
        return _CANONICAL_ALIASES[slug]
    if slug.startswith("box_curiosita") and "sandwich" not in slug and "sandwitch" not in slug:
        if "doppia" in slug:
            return "box_curiosita_doppia"
        if "tripla" in slug:
            return "box_curiosita_tripla"
        if "tabella" in slug:
            return "box_curiosita_tabella"
        if "wimg" in slug or "w_img" in slug or "img" in slug:
            return "box_curiosita_wimg"
        if "top" in slug:
            return "box_curiosita_multipage_top"
        if "bottom" in slug:
            return "box_curiosita_multipage_bottom"
        return "box_curiosita"
    if slug.startswith("immagine") and "multi" in slug:
        if "sx" in slug or "sinistr" in slug:
            return "immagine_multipage_sx"
        if "dx" in slug or "destr" in slug:
            return "immagine_multipage_dx"
    return _CANONICAL_ALIASES.get(slug, slug)


def rule_for_label(canonical: str, *, name: str = "", description: str = "") -> str:
    template = _RULES.get(canonical)
    if not template:
        template = f"Regione annotata: {name or canonical}."
    desc = (description or "").strip()
    if desc:
        return f"{template} Istruzione operatore: {desc}"
    return template


def annotations_by_original_page(
    annotations: list[dict[str, Any]] | None,
) -> dict[int, list[dict[str, Any]]]:
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in annotations or []:
        if not isinstance(item, dict):
            continue
        page = item.get("page")
        if not isinstance(page, int) or page < 1:
            continue
        for el in item.get("elements") or []:
            if isinstance(el, dict):
                by_page[page].append(el)
    return dict(by_page)


def compile_page_annotation_block(elements: list[dict[str, Any]]) -> str:
    if not elements:
        return ""
    lines = [
        "Annotazioni operatore su QUESTA pagina (vincolanti). "
        "Esegui le regole; non stampare etichette, id o coordinate."
    ]
    for index, el in enumerate(elements, start=1):
        name = str(el.get("name") or "").strip()
        canonical = normalize_label(name)
        description = str(el.get("description") or "").strip()
        coords = el.get("coords")
        rule = rule_for_label(canonical, name=name, description=description)
        lines.append(f"{index}. [{canonical}] {name or canonical}")
        if coords:
            lines.append(f"   coords: {coords}")
        lines.append(f"   {rule}")
    return "\n".join(lines)


def element_display_names(elements: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for el in elements:
        name = str(el.get("name") or "").strip()
        if name:
            names.append(name)
        canonical = normalize_label(name)
        if canonical and canonical not in names:
            names.append(canonical)
    return names


def choose_representative_annotated_pages(
    annotations: list[dict[str, Any]],
    *,
    limit: int = GUIDANCE_MAX_ANNOTATED_IMAGES,
) -> list[int]:
    by_page = annotations_by_original_page(annotations)
    if not by_page or limit < 1:
        return []
    page_labels: dict[int, set[str]] = {}
    for page, elements in by_page.items():
        labels = {normalize_label(str(el.get("name") or "")) for el in elements}
        labels.discard("")
        if labels:
            page_labels[page] = labels
    selected: list[int] = []
    covered: set[str] = set()
    remaining = set(page_labels)
    while remaining and len(selected) < limit:
        best_page = min(
            remaining,
            key=lambda page: (-len(page_labels[page] - covered), page),
        )
        if not (page_labels[best_page] - covered) and selected:
            break
        selected.append(best_page)
        covered.update(page_labels[best_page])
        remaining.remove(best_page)
    return sorted(selected)


def compose_operator_notes(
    *,
    raw_notes: str = "",
    guidance: str = "",
    budget: int = OPERATOR_NOTES_CHAR_BUDGET,
) -> str | None:
    sections: list[tuple[str, str]] = []
    raw = (raw_notes or "").strip()
    brief = (guidance or "").strip()
    if raw:
        sections.append(("Note operatore", raw))
    if brief:
        sections.append(("Consiglio AI", brief))
    if not sections:
        return None
    text = _join_sections(sections)
    if len(text) <= budget:
        return text
    if raw and brief:
        reserved = budget - len(_join_sections([("Note operatore", raw)])) - 40
        if reserved < 80:
            return _join_sections([("Note operatore", raw)])[:budget]
        truncated = brief[:reserved].rstrip()
        return _join_sections([("Note operatore", raw), ("Consiglio AI", truncated)])
    return text[:budget]


def compose_page_prompt_notes(
    *,
    page_notes: str | None,
    guidance: str | None,
    budget: int = OPERATOR_NOTES_CHAR_BUDGET,
) -> str | None:
    return compose_operator_notes(
        raw_notes=page_notes or "",
        guidance=guidance or "",
        budget=budget,
    )


def compose_index_prompt_notes(*, index_notes: str | None) -> str | None:
    text = (index_notes or "").strip()
    return text or None


def compose_toc_prompt_notes(*, notes: str | None) -> str | None:
    text = (notes or "").strip()
    return text or None


def notes_cache_hash(*parts: str) -> str:
    cleaned = [part or "" for part in parts]
    if not any(part.strip() for part in cleaned):
        return ""
    payload = "\n\x1e\n".join(cleaned)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def leak_detection_notes(prompt_notes: str | None) -> str | None:
    if not prompt_notes:
        return None
    chunks: list[str] = []
    current: list[str] = []
    for line in prompt_notes.splitlines():
        if line.startswith("## ") and current:
            chunks.append("\n".join(current[:LEAK_SIGNATURE_LINES_PER_SECTION]))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current[:LEAK_SIGNATURE_LINES_PER_SECTION]))
    trimmed = "\n\n".join(chunk for chunk in chunks if chunk.strip())
    return trimmed or None


def guidance_missing_sections(
    guidance: str,
    *,
    notes: str = "",
    index_notes: str = "",
    page_notes: str = "",
) -> list[str]:
    text = (guidance or "").lower()
    missing: list[str] = []
    if (page_notes or "").strip() and not any(
        token in text for token in ("header", "box", "sezione", "capitolo", "curiosit", "testata")
    ):
        missing.append("page")
    if (notes or "").strip() and not any(
        token in text for token in ("toc", "sommario", "titolo_toc", "elenco")
    ):
        missing.append("toc")
    if (index_notes or "").strip() and not any(
        token in text for token in ("indice", "index", "colonn", "luog")
    ):
        missing.append("index")
    return missing


def guidance_looks_truncated(guidance: str) -> bool:
    text = (guidance or "").rstrip()
    if not text:
        return True
    last = text.splitlines()[-1].strip()
    if last.endswith("@") or re.search(r"@[A-Za-z][\w]*$", last) and not last.endswith("."):
        if len(last) < 80:
            return True
    if last.startswith("- @") and not last.endswith((".", ")", "`", "}")):
        return True
    return False


def _join_sections(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"## {title}\n{body.strip()}" for title, body in sections if body.strip())
