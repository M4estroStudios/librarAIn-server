# TOC — La Grande Guida dei Monumenti di Roma

*   Input: A text fragment containing an introduction/preface and bibliographic information (copyright, publisher info, etc.) from a book titled "La grande guida dei monumenti di Roma (volume primo)" by Claudio Rendina.
    *   Task: Normalize the *Table of Contents* (sommario).
    *   Constraint 1: Output only the cleaned body. No preamble/markdown fences.
    *   Constraint 2: Remove non-content artifacts (HTML, metadata, copyright info, etc.).
    *   Constraint 3: Preserve factual content (chapters, numbers, pages).
    *   Constraint 4: Canonical line forms (`Capitolo <n> <page>`, `Cap. <n> — <title> <page>`, `<Title> <page>`).

    *   Paragraph 1: "Dire Roma significa automaticamente..." -> This is an introduction/preface text, not a Table of Contents entry.
    *   Paragraph 2: "Biblioteca de Il Messaggero © Newton Compton Editori" -> Metadata/Copyright.
    *   Paragraph 3: "37. Claudio Rendina, La grande guida dei monumenti di Roma (volume primo)" -> Bibliographic info.
    *   Paragraph 4-8: Publisher details, director, registration info, cover art credits, website.

    *   The user wants me to normalize the *Table of Contents*.
    *   Looking at the provided text... there is **no Table of Contents** in this specific input fragment. It's an introduction and a copyright/imprint page.

    *   If I output nothing, it might seem like I failed. However, the instructions say "Output only the cleaned body for this fragment."
    *   Since there are no chapter titles, numbers, or page references (the core components of a TOC), there is nothing to normalize according to the rules provided.

    *Wait*, let me re-read carefully. Is there any hidden TOC?
    "Dire Roma significa..." -> Intro text.
    "37. Claudio Rendina..." -> Title/Author info.
    The rest is legal/publishing boilerplate.

    If I follow the instructions strictly: "Remove all non-content artifacts... Output only the cleaned body for this fragment."
    In a TOC normalization task, if there are no entries, the output should be empty or contain nothing that isn't an entry.

    However, sometimes users provide text thinking it contains the TOC when it doesn't. Or perhaps they want me to extract anything that *looks* like a heading? No, the instructions specifically define canonical line forms for chapters and pages.

    Let's look at "37. Claudio Rendina...". This is not a chapter in a TOC; it's part of the bibliographic metadata.

    Conclusion: The input contains zero Table of Contents entries.

    *Self-Correction:* Is there any chance the first paragraph is considered content? No, the prompt says "You normalize the table of contents (sommario) extracted from a scanned Italian book." This text is clearly the *preface/copyright page*, not the *sommario*.

    If I output nothing, it's technically correct based on the instructions to remove non-content artifacts. But usually, in these LLM tasks, if there's no TOC, I should check if any part of the text could be interpreted as a section. There isn't.

    Let's double-check:
    "Dire Roma significa..." (Intro)
    "Biblioteca de Il Messaggero..." (Copyright)
    "37. Claudio Rendina..." (Title/Author)
    ...rest is metadata.

    Since there are no entries matching the canonical forms (`Capitolo`, `Cap.`, or `<Title> <page>`), I will return an empty response or nothing, as any text provided would violate the "Remove all non-content artifacts" rule (since everything here is a non-content artifact relative to a Table of Contents).

    *Wait*, looking at the prompt again: "If a line cannot be interpreted, keep the closest faithful text on one line rather than deleting it."
    This applies if there's something that *looks* like an entry but is messy. But here, nothing looks like a TOC entry.

    Let's see if I can find any chapter-like structure. No.

    I will provide no output because the input contains no Table of Contents entries to normalize.

    *Alternative thought:* Is "37. Claudio Rendina..." meant to be an entry? No, it's clearly bibliographic info.

    Actually, looking at similar tasks, if the user provides a text that is *not* a TOC, they might have made a mistake in the input or are testing how I handle non-TOC text. The instructions say "Remove all non-content artifacts...". In the context of a TOC task, everything here is an artifact.

    I'll provide no output. (If I must provide something, I'd be inventing content, which is forbidden).

    *Wait*, let me look at the text one more time.
    "Dire Roma significa..." - This is prose.
    The rest is metadata.

    There is nothing to normalize.

    *Decision:* Since there are no TOC entries in the provided text, I will output nothing.

    (Note: If this were a real-world scenario where a user provides an intro and expects a TOC, they'd realize it's missing. As an AI following strict instructions for "cleaning" a TOC, if there is no TOC, there is no cleaned body.)

    *Wait*, I should check if the prompt implies that the *entire* text is the input to be processed as a potential TOC. If I treat the whole thing as a messy TOC:
    - The first paragraph is prose (not an entry).
    - The rest is metadata (not an entry).

    Result: Empty.

    Let's try to see if "37. Claudio Rendina..." could be interpreted as `Capitolo 37`. No, that's a stretch and violates "do not invent".

    Final check of the prompt: "Output only the cleaned body for this fragment."
    If there is no TOC, there is no body.

    *However*, if I am being evaluated on my ability to follow instructions, and the instruction says "Remove all non-content artifacts... including... duplicate headers", and the input has nothing that fits the canonical forms, the correct output is an empty string.

    Let's assume for a moment the user *thinks* this is part of a TOC or wants me to extract anything that looks like a heading. There are no headings.

    I will provide no response.

    (Self-correction: In some LLM interfaces, providing nothing might be seen as an error. I'll double check if there's *any* line that could be a title. "La grande guida dei monumenti di Roma" is the book title. Not a TOC entry.)

    Actually, looking at the prompt again: "The input is raw Markdown from OCR and prior cleanup; it may contain model artifacts...". This implies the user expects me to find the TOC within the mess. If there is no TOC in the mess, I return nothing.

    *Wait*, let's look at the very first line of the prompt instructions: "You normalize the table of contents (sommario) extracted from a scanned Italian book."
    If the input provided is not a Table of Contents, then there is nothing to do.

    I will output nothing.
