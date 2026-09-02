Estrai metadati bibliografici REICAT dalle pagine del libro fornite nelle immagini.

Regole:
- Cerca ogni campo REICAT in TUTTE le pagine selezionate: un campo può comparire su pagine diverse (frontespizio, verso del titolo, colophon, retro copertina, ecc.). Non limitarti alla prima pagina o al primo collage.
- Prima di rispondere, scorri mentalmente tutte le pagine indicate e tutte le immagini: unisci le informazioni trovate solo se visibili sulle pagine fornite.
- Usa solo testo visibile nelle immagini. Non inventare e non dedurre da conoscenza esterna.
- Se un campo non compare in nessuna delle pagine selezionate, usa null (stringhe) o [] (liste).
- Distingui titolo, sottotitolo e complementi del titolo secondo REICAT.
- autore, curatore e traduttore sono liste di nomi in forma leggibile sul documento.
- tipo_di_pubblicazione solo se esplicitamente indicato; altrimenti null.
- numero_pagine solo se stampato come estensione bibliografica (es. "pp. 320"), non contare le miniature.
- Normalizza ISBN senza spazi superflui se presente.

Rispondi con un solo oggetto JSON, senza markdown né testo aggiuntivo, con esattamente queste chiavi:
{
  "titolo": null,
  "sottotitolo": null,
  "complementi_del_titolo": null,
  "autore": [],
  "curatore": [],
  "traduttore": [],
  "numero_edizione": null,
  "anno_di_pubblicazione": null,
  "tipo_di_pubblicazione": null,
  "luogo_di_pubblicazione": null,
  "editore": null,
  "numero_pagine": null,
  "titolo_collana": null,
  "numero_nella_collana": null,
  "isbn": null
}
