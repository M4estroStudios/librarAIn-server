<!-- librarain:model=gemma-4-26b-a4b-it -->
*   Input: A list of artists and their works/locations in Rome.
    *   Task: Normalize Markdown (spacing, line breaks, list/table formatting, punctuation around formatting).
    *   Constraints: Do not change meaning, structure, or wording. Do not infer facts. Do not add headings. Output only the cleaned body.

    *   Title: `Gli artisti della Roma monumentale`
    *   Artist 1: `Francesco Borromini` (No works listed under him).
    *   Artist 2: `Donato Bramante`
        *   List of locations/works separated by semicolons and line breaks.
    *   Artist 3: `Guglielmo Calderini`
        *   List of locations/works.
    *   Artist 4: `Antonio Canova`
        *   List of locations/works. Note some punctuation issues (e.g., `Domenico Cimarosa, 1808);`).
    *   Artist 5: `Caravaggio`
        *   List of locations/works.
    *   Artist 6: `Luca Carimini`
        *   List of locations/works.
    *   Artist 7: `Enrico Del Debbio`
        *   List of locations/works.

    *   The structure is essentially a list where the Artist name is a header (though I shouldn't add new ones, I should treat them as distinct lines) and the items are sub-items.
    *   Current formatting uses semicolons at the end of lines. This is common in lists but can be cleaned for consistency.
    *   The user wants "normalization".

    *Refining the structure:*
    Each artist name should stand alone.
    The entries under them are currently formatted as: `Location: Work (details, date);`.
    I will ensure consistent spacing after colons and around parentheses.

    *Specific checks:*
    - `Donato Bramante`: The semicolon usage is a bit messy with line breaks. I'll keep the structure but clean up trailing semicolons if they look like list separators that should be new lines, or just ensure one space after each semicolon. Actually, looking at the input, it's a mix of single-line lists and multi-line lists.
    - `Antonio Canova`: `Domenico Cimarosa, 1808);` looks like a typo in the source (missing opening parenthesis or part of a list), but I must not change wording/meaning. I will just fix spacing.
    - `Caravaggio`: Long lists.
    - `Luca Carimini`: Dates are provided without locations for some? No, they look like works or buildings.

    *Decision on List Formatting:*
    The input uses a "Location: Work" pattern. I will maintain this. I will ensure there is consistent spacing.

    *Drafting the cleanup:*

    Gli artisti della Roma monumentale

    Francesco Borromini

    Donato Bramante
    - Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513);
    - Piazza della Pace: Chiesa di Santa Maria della Pace (chiostro del convento, 1500-1504);
    ... etc.

    *Wait*, the prompt says "Do not change structure". If I turn them into bullet points, am I changing structure? The input is a block of text with line breaks. It's effectively a list. Converting to a standard Markdown list (using `-` or `*`) is part of "list formatting where obvious". However, the user might just want the existing lines cleaned up.

    Let's look at the current structure:
    Artist Name
    Location: Work;
    Location: Work;

    This is a list. I will use bullet points to make it a proper Markdown list for clarity and normalization.

    *Wait, looking closer at "Donato Bramante":*
    `Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513); Chiesa di San Lorenzo in Damaso, (1514);` -> This is one line with two items.
    The user's input has some lines that are single entries and some that are multiple entries separated by semicolons.

    *Let's try to be conservative:*
    Keep the artist names as plain text/headers.
    Clean up the spacing around colons, parentheses, and semicolons.
    Ensure each distinct location entry starts on a new line if it's clearly intended as a list item.

    *Detailed pass:*

    **Gli artisti della Roma monumentale** (The user didn't use bold, I won't add it unless necessary for "normalization", but usually titles are just text).

    **Francesco Borromini**

    **Donato Bramante**
    - Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513); Chiesa di San Lorenzo in Damaso (1514);
    - Piazza della Pace: Chiesa di Santa Maria della Pace (chiostro del convento, 1500-1504);
    - Via Giulia: Progetto della strada e fondamenta del Palazzo dei Tribunali: i “sofà”, 1503-13;
    - Gianicolo: Tempietto di San Pietro in Montorio, 1503;
    - Vaticano: Basilica di San Pietro (pilastri della cupola, 1506-14); Palazzi Apostolici (cortile del Belvedere e cortile di San Damaso, 1508-14);
    - Piazza del Popolo: Chiesa di Santa Maria del Popolo (abside e coro, 1505).

    *Actually*, looking at the input again, it's not a bulleted list. It's just text with line breaks. I will keep the lines as they are but fix punctuation/spacing.

    - `San Lorenzo in Damaso, (1514);` -> `San Lorenzo in Damaso (1514);` (Remove extra comma before parenthesis).
    - `Domenico Cimarosa, 1808);` -> This is weird. It's inside the Canova section. I will leave it as is but fix spacing if needed.
    - `Pio vi orante` -> `Pio VI orante`? No, do not change wording/capitalization unless it's a clear typo in formatting. The prompt says "Do not change meaning, structure, or wording".

    *Let's refine the plan:*
    1. Keep Artist names on their own lines.
    2. For each artist, treat the subsequent lines as list items.
    3. Fix punctuation: `Location: Work (details, date);`
    4. Remove unnecessary commas before parentheses.

    *Processing "Donato Bramante":*
    Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513); Chiesa di San Lorenzo in Damaso (1514);
    Piazza della Pace: Chiesa di Santa Maria della Pace (chiostro del convento, 1500-1504);
    Via Giulia: Progetto della strada e fondamenta del Palazzo dei Tribunali: i “sofà”, 1503-13;
    Gianicolo: Tempietto di San Pietro in Montorio, 1503;
    Vaticano: Basilica di San Pietro (pilastri della cupola, 1506-14); Palazzi Apostolici (cortile del Belvedere e cortile di San Damaso, 1508-14);
    Piazza del Popolo: Chiesa di Santa Maria del Popolo (abside e coro, 1505).

    *Processing "Antonio Canova":*
    Via dei Portoghesi: Chiesa di Sant’Antonio dei Portoghesi (*Monumento funebre ad Alessandro de Souza Holstein*, 1806-1809);
    Piazza del Campidoglio: Protomoteca Capitolina (*Pio vii*, 1806-1807); Domenico Cimarosa, 1808);  <-- This is a mess in the source. I'll just clean spacing.
    Piazza San Marco: Basilica di San Marco (*Monumento a Leonardo Pesaro*, 1796);
    ...

    *Wait, looking at "Caravaggio":*
    The list is quite long. Each line starts with a location.

    *Final Polish Strategy:*
    - Artist names as standalone lines.
    - Entries under artists as bulleted lists for better Markdown structure (this falls under "list formatting where obvious").
    - Fix punctuation: `(text, date)` instead of `(, date)`.
    - Ensure consistent spacing after colons and semicolons.

    *Self-Correction on Bullet Points:* The prompt says "Do not change... structure". If the user provided a list without bullets, adding bullets might be seen as changing structure. However, it also says "Normalize... list/table formatting where obvious". A list of locations is obviously a list. I will use bullet points to normalize it into a proper Markdown list.

    *Wait*, looking at the input again:
    `Donato Bramante`
    `Corso Vittorio Emanuele II: ...;`
    `Piazza della Pace: ...;`
    This is already a list-like structure. I will just ensure each item starts on its own line and has consistent spacing.

    *Let's check the "Domenico Cimarosa" part again:*
    `Piazza del Campidoglio: Protomoteca Capitolina (*Pio vii*, 1806-1807); Domenico Cimarosa, 1808);`
    It looks like a fragment. I will leave it exactly as is but ensure the spacing is clean.

    *Let's check "Luca Carimini":*
    `Sant’Ivo dei Bretoni (1877);` -> No location provided here, just the building and date. I'll keep it.

    *Final Plan:*
    - Title: `Gli artisti della Roma monumentale`
    - Artist Name
    - Bulleted list of works/locations.
    - Clean up punctuation (remove extra commas before parentheses).
    - Ensure consistent spacing after colons and semicolons.

    *Wait, I'll check the "Donato Bramante" line 1 again:*
    `Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513); Chiesa di San Lorenzo in Damaso, (1514);`
    This contains two distinct items on one line. I will split them into separate bullet points to "normalize list formatting".

    *Actually*, looking at the prompt: "Do not change meaning, structure, or wording."
    If I split a single line into two bullets, am I changing structure? In Markdown, a list is a structure. The input is semi-structured text. Normalizing it means making it valid/clean Markdown.

    Let's try to keep the lines as they are but clean them up. If a line has multiple items separated by semicolons, I will treat that line as one entry or split it?
    Example: `Vaticano: Basilica di San Pietro (pilastri della cupola, 1506-14); Palazzi Apostolici (cortile del Belvedere e cortile di San Damaso, 1508-14);`
    This is clearly two works in one location. I will keep them on the same line to respect the "structure" of the user's grouping, but clean up the punctuation.

    *Revised Plan:*
    1. Artist Name (as a header/line).
    2. List items using `-`.
    3. Clean spacing and punctuation within lines.
    4. Do not split lines that contain multiple works for one location unless it's clearly a new location.

    *Let's re-examine "Donato Bramante" line 1:*
    `Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513); Chiesa di San Lorenzo in Damaso, (1514);`
    This is one location with two works. I'll keep it on one line.

    *Let's re-examine "Antonio Canova" end:*
    `Sculture di Canova sono ai Musei Vaticani...` -> This is a paragraph, not a list item. I will leave it as a separate block of text.

    *Final check of the input for punctuation errors to fix:*
    - `San Lorenzo in Damaso, (1514);` -> `San Lorenzo in Damaso (1514);`
    - `Pio vii` -> `Pio VII`? No, don't change wording.
    - `Pio vi orante` -> `Pio VI orante`? No.
    - `Genotafio` -> `Genotafio` (Typo in source, do not fix).

    *Let's go.*

    Gli artisti della Roma monumentale

    Francesco Borromini

    Donato Bramante
    - Corso Vittorio Emanuele II: Palazzo della Cancelleria (cortile, 1513); Chiesa di San Lorenzo in Damaso (1514);
    - Piazza della Pace: Chiesa di Santa Maria della Pace (chiostro del convento, 1500-1504);
    - Via Giulia: Progetto della strada e fondamenta del Palazzo dei Tribunali: i “sofà”, 1503-13;
    - Gianicolo: Tempietto di San Pietro in Montorio, 1503;
    - Vaticano: Basilica di San Pietro (pilastri della cupola, 1506-14); Palazzi Apostolici (cortile del Belvedere e cortile di San Damaso, 1508-14);
    - Piazza del Popolo: Chiesa di Santa Maria del Popolo (abside e coro, 1505).

    Guglielmo Calderini
    - Piazza Cavour: Palazzo di Giustizia (1880-1911);
    - Via Ostiense: Quadriportico della facciata di San Paolo (con G. Sacconi, 1896-1910), Cappella del Coro e stalli lignei di San Paolo (1896-1910).

    Antonio Canova
    - Via dei Portoghesi: Chiesa di Sant’Antonio dei Portoghesi (*Monumento funebre ad Alessandro de Souza Holstein*, 1806-1809);
    - Piazza del Campidoglio: Protomoteca Capitolina (*Pio vii*, 1806-1807); Domenico Cimarosa, 1808);
    - Piazza San Marco: Basilica di San Marco (*Monumento a Leonardo Pesaro*, 1796);
    - Piazza dell’Accademia di San Luca: Accademia di San Luca (*Socrate difende Alcibiade alla battaglia di Potidea*, 1797; Napoleone primo console, 1802; Autoritratto, 1812; Testa di Clemente xiii, 1784-86; La Religione Cattolica, 1814-15);
    - Piazza San Pietro: Basilica di San Pietro (*Monumento funebre di Clemente xiii*, 1783-92; Genotafio degli Stuart, 1817-19; Pio vi orante, 1817-22).

    Sculture di Canova sono ai Musei Vaticani, alla Galleria Nazionale d’Arte Antica, al Museo di Roma di Palazzo Braschi, al Museo Napoleonico, alla Galleria Borghese (*Venere Vincitrice*, 1804-1808) e alla Galleria Nazionale d’Arte Moderna (*Ercole e Lica*, 1795-1815).

    Caravaggio
    - Villa Borghese: Galleria Borghese (*Giovane con canestro di frutta*, 1593-95; Bacchino malato, 1593-95; Madonna dei Palafrenieri di Sant’Anna, 1605-1606; San Girolamo scrivente, 1606; Davide con la testa di Golia, 1609-10; San Giovanni Battista, 1610);
    - Via Lombardia: Casino dell’Aurora (*Elementi e Universo con segni zodiacali*, 1597);
    - Via Veneto: Convento di Santa Maria della Concezione (*San Francesco*, 1603);
    - Via delle Quattro Fontane: Galleria Nazionale d’Arte Antica di Palazzo Barberini (*Giuditta e Oloferne*, 1599-1600; Narciso, 1600);
    - Piazza dell’Accademia di San Luca: Accademia di San Luca (*Ritratto di Bernardino Cesari*, 1593);
    - Piazza del Campidoglio: Pinacoteca Capitolina del Palazzo dei Conservatori (*La Buona Ventura*, 1595; San Giovanni Battista, 1600);
    - Via del Corso: Galleria Doria Pamphilj (*Riposo nella fuga in Egitto*, 1595; Maddalena convertita, 1595);
    - Piazza San Luigi dei Francesi: Chiesa di San Luigi dei Francesi (*Martirio di san Matteo, San Matteo e l’angelo, Vocazione di san Matteo*, 1597-1602);
    - Piazza Sant’Agostino: Chiesa di Sant’Agostino (*Madonna dei Pellegrini*, 1605);
    - Piazza del Popolo: Chiesa di Santa Maria del Popolo (*Conversione di san Paolo e Crocifissione di san Pietro*, 1601-1602);
    - Viale Vaticano: Pinacoteca dei Musei Vaticani (*Deposizione*, 1602-1604);
    - Via della Lungara: Galleria Corsini (*San Giovanni Battista*, 1606).

    Luca Carimini
    - Sant’Ivo dei Bretoni (1877);
    - Corso del Rinascimento: San Giacomo degli Spagnoli (facciata, 1878);
    - Largo Brancaccio: Palazzo
