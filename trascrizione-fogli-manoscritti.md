# Trascrizione fogli manoscritti

Documento unico ricavato dai due fogli allegati al progetto librarAIn-server.

---

## Foglio 1 — Workflow Upload e Ricerca

### UPLOAD — IN (giallo) · OUT (verde)

1. *(evidenziato giallo)* **PDF della scansione del volume** viene caricata (`UPLOADATA`).

2. *(evidenziato giallo)* **Compilazione dei campi dello standard REICAT**
   - Inclusi: pagine da rimuovere; TOC IN, TOC OUT; INDEX IN, INDEX OUT.
   - Nota: *(eventualmente automatizzabile)*
   - Pipeline OCR: `{OTTIMIZZABILE}`

3. *(evidenziato verde)* **Aggiungere TOC e INDEX alla collezione degli indici**
   - Nota sopra “collezione degli indici”: capire anche se fare qualcosa di “aggregato” cross-book.

**Raggruppamento (parentesi graffa sul foglio):**

- `.MD` → **Milvus** per RAG normale *(o GraphRAG)*.
- *(evidenziato verde)* Esportare un **nuovo PDF “allineato”** — corrispondenza: `[pp. PDF = pp. pagina del libro]`.

---

### RICERCA

1. La **query** viene inserita — *da un utente o da un POH* (contesto progetto: entità “punto di storia” o simile).

2. **Ricerca nella INDEX COLLECTION** → estrapolazione di **capitoli** e **pagine** da leggere.

**Passi successivi (elenco a–d collegato nel disegno):**

| Passo | Testo |
|-------|--------|
| **a** | Scrittura di un **articolo** in stile Wikipedia che raccoglie tutte le informazioni pertinenti sul POH, complementari e non ridondanti. |
| **b** | Rilettura dell’articolo e individuazione / *piazzamento* dei **link alle fonti**, in stile **Perplexity**. *(nel manoscritto una parola è barrata tra “individuazione” e “piazzamento”)* |
| **c** | Rilettura dell’articolo e creazione di **hyperlink** agli altri POH menzionati. |
| **d** | Rilettura dell’articolo e creazione della **vertical time bar**. |

**Termini ricorrenti nel foglio**

- **REICAT**: standard di catalogazione (contesto bibliotecario italiano).
- **Milvus / RAG / GraphRAG**: indice vettoriale e retrieval-augmented generation.
- **POH**: nel disegno indica sia la fonte della query sia l’oggetto dell’articolo (definizione di dominio da confermare nel PRD).

---

## Foglio 2 — Struttura directory e file

Albero progettuale per ingest PDF, OCR e output Markdown.

```text
db/
├── biblioteca.db
└── checkpoints/
    ├── biblio_YYYY_MM_DD.db
    └── …

PDF/
├── RAW/                         (nota al foglio: “nome”)
│   └── Scansione libro.pdf
├── PROCESSED/
│   └── HASH_LIBRO.pdf
└── …

polyindex/
├── TOC.json
├── INDEX.json       
└── checkpoints/
    ├── YYYY_MM_DD_TOC.json
    ├── YYYY_MM_DD_INDEX.json
    └── …

output/
├── "HASH_LIBRO"/
│   ├── pages/
│   │   ├── P.0001 - NomeLibro.md
│   │   └── …
│   ├── NomeLibro.md           {Σ pages}
│   ├── TOC.md
│   └── INDEX.md
└── …

tmp/
└── HASH_LIBRO/
    ├── stage_OCR/
    │   ├── P.0001 - NOMELIBRO.md
    │   └── …
    ├── stage_VISION/
    ├── stage_EDITOR/
    └── …
```

**Note grafiche sul secondo foglio**

- Nel disegno, **VISION** ed **EDITOR** sono collegate con frecce verso gli stessi file elencati sotto **stage_OCR** (brace comune sul foglio: stesse “pagine” attraversate da più stadi della pipeline).
- `YYYY_MM_DD`, `HASH_LIBRO`, `NomeLibro` sono **placeholder** di convenzione (data, hash del libro, titolo/normalizzazione nome).

---

## Riferimento file immagine

Percorsi assoluti delle due scansioni:

1. `/Users/jonathanlanoce/.cursor/projects/Users-jonathanlanoce-Dev-librarAIn-server/assets/11929C31-3296-4DDB-BF28-44F25369CE09_2-a6840e43-f7cf-4a0f-ae2f-798a96e6130d.png`

2. `/Users/jonathanlanoce/.cursor/projects/Users-jonathanlanoce-Dev-librarAIn-server/assets/E7109224-4F15-4DD6-B3B2-F632CBE0137F_2-ede7621e-961a-42fa-ad45-a7de19ed3773.png`
