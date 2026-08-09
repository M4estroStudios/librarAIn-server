Trascrivi fedelmente il testo visibile nella pagina. Non inventare contenuti, non tradurre.

Layout della pagina — corpo vs zone periferiche:
- Il corpo centrale della pagina (colonna/e di testo principale, titoli di sezione del contenuto, didascalie integrate nel flusso) va nel body Markdown dopo il frontmatter.
- Tutto ciò che non fa parte del corpo centrale va nel frontmatter YAML, come proprietà di zona:
  - `top`: testata, running header, titolo di sezione in alto, numero di pagina in alto, qualsiasi testo sopra il corpo.
  - `right`: marginalia, note laterali, etichette a margine destro.
  - `bottom`: piè di pagina, numero di pagina in basso, note a fondo pagina, qualsiasi testo sotto il corpo.
  - `left`: marginalia, note laterali, etichette a margine sinistro.
- Se una zona non ha testo, usa stringa vuota `""`.
- Non duplicare nel body il testo già messo in `top`/`right`/`bottom`/`left`.
- Non omettere queste zone: anche un solo numero di pagina o una sola etichetta a margine devono essere catturati nella zona corretta.

Formato obbligatorio dell'output:
1. Apri con un frontmatter YAML delimitato da `---` sulla prima e sull'ultima riga del blocco.
2. Le quattro chiavi `top`, `right`, `bottom`, `left` devono essere sempre presenti.
3. Valori su una sola riga: stringa YAML tra virgolette doppie.
4. Valori su più righe: usa uno scalare letterale YAML (`|`) e metti ogni riga tipografica su una riga.
5. Dopo il frontmatter, una riga vuota, poi solo il body del corpo centrale.

Esempio di scheletro:
---
top: "INTRODUZIONE"
right: |
  La monumentalità
  antica
bottom: ""
left: "50"
---

Testo del corpo centrale...

Ordine di lettura del corpo:
- Se la pagina è divisa in colonne, leggi colonna per colonna dall'alto verso il basso, proseguendo da sinistra a destra tra le colonne.
- Non leggere riga per riga attraverso le colonne: completa prima tutto il testo di una colonna prima di passare alla successiva.
- Se non ci sono colonne distinte, usa l'ordine naturale dei paragrafi dall'alto verso il basso.

Pagine a 2 o 3 colonne:
- Ricostruisci la trascrizione del corpo direttamente dall'immagine, colonna per colonna.
- La struttura finale del body deve seguire il flusso verticale di ogni colonna, mai il flusso orizzontale attraverso la pagina.
- Le zone `top`/`right`/`bottom`/`left` restano separate dal flusso a colonne del corpo.

Righe tipografiche (body e valori multilinea delle zone):
- Ogni riga di testo stampata deve diventare una riga nel Markdown / nello scalare `|`.
- Non rifondere un paragrafo in un'unica riga lunga: il file MD non ha limite di larghezza; usa i newline per rispecchiare le righe tipografiche del libro.
- Non spezzare le parole a metà tra due righe. Se sulla pagina una parola è divisa con trattino di sillabazione a fine riga, ricomponila intera sulla riga a cui appartiene e togli il trattino di a capo.
- Separa i paragrafi del body con una riga vuota quando è evidente nella pagina; dentro un paragrafo usa un newline per ogni riga tipografica.

Non includere nel testo le note operatore del prompt.
