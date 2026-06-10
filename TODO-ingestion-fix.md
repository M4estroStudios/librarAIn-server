# TO DO: [INGESTION FIX!]

> Trascritto da appunti manoscritti.  
> Immagini originali: `assets/IMG_2928-*.png`, `assets/IMG_2929-*.png`

---

## Foglio 1 — Fix ingestion e INDEX

- In `BIBLIOTECA.db` non è stato aggiunto il libro.

- In `INDEX.json` (e TOC) non solo l'**HASH** del libro, ma il proprio **nome** [o **SLUG**].

- Il **VISION** deve fare attenzione quando le pagine presentano **2/3 colonne** nel testo; se ciò avviene, **sovrascrivere** il risultato OCR {o usarlo solo come *source of truth* per l'OCR dei numeri delle pagine}.  
  La pagina va trascritta **per colonne** (ordine verticale), **non** per righe (ordine orizzontale):

  **Corretto** (lettura a colonne, zig-zag verticale):

  ```
  col1   col2   col3
   ↓      ↑      ↓
   ↓      ↑      ↓
  ```

  **Sbagliato** (lettura orizzontale su tutta la pagina):

  ```
  → → →
  ← ← ←
  → → →
  ```

- Nel campo di compilazione del libro\* prima dell'ingest, far inserire dall'**USER** una sezione di eventuali **NOTE** sulla formattazione dell'**INDEX** specifica di quel libro, così che poi venga integrata nel **SYSTEM PROMPT** (di tutti e 3 i modelli) [o come **CONTESTO FISSO e PRIORITARIO** dell'LLM].

- \* Comunque, quello che genera l'`INDEX.json`, per mettere una pezza su errori di formattazione, tenga considerazione del fatto che l'**INDEX è ordinato alfabeticamente**. Quindi l'`INDEX.md` eventualmente disordinato può essere **raddrizzato**.

- Nell'`INDEX.json` i **PATH** devono essere ordinati **alfabeticamente**, **non** in ordine di comparsa nell'indice.

- Molte righe dell'INDEX sono mancanti [molte delle *entries*].

---

## Foglio 2 — POH, admin UI, TIME_INDEX

- Dato che di sicuro da diversi libri vengono generate **2+ voci** sullo stesso **POH** {es.: ~~AUGUSTO~~, AUGUSTO, OTTAVIANO, IMPERATORE AUGUSTO}, creare una sezione dell'**admin page** per vedere quali sono i POH con **≥2 libri** in cui è contenuto, così da poterli **MERGIARE** tramite l'UI interface.

- ~~ma~~ Aggiungere anche un campo dedicato a **NOTE** sulla formattazione delle **pagine generiche**  
  [es. NOTE a LATO, a PIEDI, a FRONTE, NOME LIBRO e CAPITOLO, NUMERO di PAGINA {che può interferire perché scambiabili per **DATE**}].

- Aggiungere un ultimo passo alla creazione dell'`INDEX.json`: **RI-LETTURA** dell'`.MD` **COMPLETO** del **LIBRO**, pag per pag, e individuazione di tutti gli **ANNI** e le **DATE SPECIFICHE** presenti nel libro.

- ~~creare~~ Creare **FILE separato** ~~chiamato~~ `TIME_INDEX.json` contenente:
  - **YEARS** / BOOK / PAGES
  - **DATES** / BOOK / PAGES {sia con o senza anno}
