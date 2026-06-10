from base64 import b64encode
from collections import Counter
from datetime import datetime
from easyocr import Reader
from io import BytesIO
from numpy import array
import io
import fitz
import time
from gc import collect
from hashlib import sha256 as SHA256
from json import dumps
from numpy import array
from numpy import float32
from PIL import Image
from openai import OpenAI
from os import environ
from os import listdir
from os.path import basename
from os.path import join
from pymilvus import Collection
from pymilvus import CollectionSchema
from pymilvus import connections
from pymilvus import DataType
from pymilvus import FieldSchema
from pymilvus import Function
from pymilvus import FunctionType
from pymilvus import utility
from re import IGNORECASE
from re import findall
from re import search
from re import sub
from requests import post
from requests.exceptions import RequestException
from sentence_transformers import SentenceTransformer
from time import sleep
from time import time
from warnings import filterwarnings
import os
from pathlib import Path
import json

filterwarnings("ignore")
environ["USE_FLASH_ATTENTION"] = '1'

OPENAI_BASE_URL = environ.get("OPENAI_BASE_URL", "http://localhost:11111/v1")
OPENAI_API_KEY = environ.get("OPENAI_API_KEY", "dummy")

options = [
    {
        'description': 'Converti documenti PDF in MD',
        'function': lambda: convertPDFToMDInteractive()
    },
    {
        'description': 'Importa documenti MD in Milvus',
        'function': lambda: ingestFileInteractive()
    },
    {
        'description': 'Ricerca ibrida (semantica + testuale)',
        'function': lambda: hybridSearchInteractive()
    },
    {
        'description': 'lista collezioni',
        'function': lambda: printCollections()
    },
    {
        'description': 'Elimina collezione',
        'function': lambda: deleteCollectionInteractive()
    },
    {
        'description': 'Chat',
        'function': lambda: chatInteractive()
    },
    {
        'description': 'Chat (curl)',
        'function': lambda: chatCurlInteractive()
    },
    {
        'description': 'Esci',
        'function': lambda: exit()
    }
]

def checkOpenAIConnection():
    print("📡 verifica connessione a OpenAI")
    client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        timeout=5.0,
        max_retries=3
    )

    try:
        client.models.list()
        return True
    except Exception as e:
        return False

def selectInputPDFFile():
    print("📁 File disponibili in input")
    inputPath = Path("input")
    files = list(inputPath.glob("*.pdf"))
    print("=" * 50)
    for i, file in enumerate(files, start=1):
        print(f"{i}. {file.name}")
    print("=" * 50)
    
    if len(files) == 0:
        print("Nessun file disponibile. Importa un file per iniziare.")
        return None
    
    if len(files) == 1:
        print(f"File selezionato automaticamente: {files[0].name}")
        return files[0]
    
    while True:
        choice = input(f"\nSeleziona un file (1-{len(files)}): ").strip()
        if int(choice) in range(1, len(files) + 1):
            return files[int(choice) - 1].name
        print("Selezione non valida. Riprova.")

def ocrWithEasyOCR(imgBytes, pageNum):
    try:
        ocrStart = time()
        reader = Reader(['it', 'en'], gpu=True, verbose=False)
        pilImage = Image.open(BytesIO(imgBytes)).convert('RGB')
        npImage = array(pilImage)
        result = reader.readtext(npImage, detail=0, paragraph=True)
        text = " ".join(result)
        del pilImage, npImage
        collect()
        ocrTime = time() - ocrStart
        print(f"    ✅ OCR completed in {ocrTime:.1f}s")

        return text
    except Exception as e:
        print(f"    ⚠️ ERROR OCR PAGE {pageNum}: {e}")
        return f"[ERROR OCR PAGE {pageNum}: {e}]"

def refineWithVision(imgBytes, ocrText, pageNum):
    client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        timeout=180.0,
        max_retries=3
    )
    prompt = f"""
    quello che stai per ricevere è un testo ottenuto via OCR.
    il testo ottenuto sarà completamente non strutturato ma sarà quasi completamente corretto eccetto errori di battitura.
    il testo di per se dovrà essere corretto e riscritto secondo la struttura dell'immagine.
    quando sei in dubbio sulla struttura tratta il tutto come un serie di paragrafi separati da uno spazio, anche quando la struttura è insolita.
    prioritizza le struture vertcali rispetto a quelle orizzontali.
    assicurati che il testo sia coerente in italiano.

    <ocr>
    {ocrText}
    </ocr>

    Restituisci solo il testo finale, senza spiegazioni.
    """
    imgBytes = b64encode(imgBytes).decode('utf-8')
    messages = [
        {
            "role": "user",
            "content": [
                {'type': 'text', 'text': prompt},
                {
                    'type': 'image_url', 
                    'image_url': {'url': f'data:image/png;base64,{imgBytes}'}
                }
            ]
        }
    ]
    try:
        visionStart = time()
        visionText = client.chat.completions.create(
            model="mistral-small-3.2-24b-instruct-2506",
            messages=messages,
        )
        visionTime = time() - visionStart
        print(f"    ✅ Vision completed in {visionTime:.1f}s")

        return visionText.choices[0].message.content
        
    except Exception as e:
        print(f"    ⚠️ ERROR Vision PAGE {pageNum}: {e}")
        return f"[ERROR Vision PAGE {pageNum}: {e}]"

def refineWithEditor(visionText, pageNum):
    client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        timeout=180.0,
        max_retries=3
    )
    prompt = f"""
    Correggi eventuali errori (eg: se l’ocr ha trasformato la congiunzione ‘e’ in ‘c’ e il successivo modello vision ha tolto la ‘c’; lasciando un vuoto al posto della ‘e’  o ‘è’).
    Metti tra le [[]] (ai fini di creare hyperlinked .md)con le seguenti regole:
    <rules>
    <crown>Per i Papi e Imperatori voglio che appaiano nella forma (per esempio) [[Papa Leone IV]] e [[Imperatore Adriano]], e se appare solo il nome fare [[Papa Leone IV|Leone IV]] e [[Imperatore Adriano|Adriano]]</crown>
    <dates>
    Le date (sia giorni che anni) (attenta con gli altri numeri)
        (NB. "Settecento"/"settecentesco"—>[[XVIII secolo|Settecento/settecentesco]])
    </dates>
    <names>
    I nomi propri di individui, nella forma [[Nome Cognome]]; se nel testo è presente solo il nome segui questo esempio: Raffaello --> [[Raffaello Sanzio|Raffaello]] (e se non sai il cognome per common knowledge, evincilo dal contesto)
    Per i nomi propri di famiglia, quando usati sciolti dal nome proprio, metterli nella forma (per esempio) Borghese-->[[Famiglia Borghese|Borghese]]
    </names>
    <places>
    Nomi propri completi di Istituzioni
    Luoghi della città (sia punti di interesse che indirizzi, con tutte le parole che iniziano in maiuscolo, comprese 'Via' e 'Viale')
        (NB: Non ha senso hyperlinkare 'Roma'...lasciala senza parentesi)
    </places>
    <orders> Gli Ordini religiosi eg. Francescani--> [[Ordine dei Francescani|Francescani]] </orders>
    <entities> Entità extraromane, come altre città, regioni, stati (se trovi aggettivi segui quest'esempio : tedesca --> [[Germania|tedesca]] </entities>
    </rules>
    <formatting>
    Per i titoli del paragrafo/sottoparagrafo, applicare # per renderlo un’Header (e poi ##,###,… se ci sono nested sections and subsections)

    Nel caso il testo non sia formattato, organizzalo in paragrafi e sottoparagrafi al meglio delle tue possibilità cercando mantenere il testo coerente in italiano.
    Nel caso tra le [[]] ci siano dei link che portano a siti web esterni, rimuovi gli hyperlinks completamente.
    </formatting>

    <text>
    {visionText}
    </text>

    Restituisci solo il testo finale, senza spiegazioni.
    """
    messages = [
        {
            "role": "user",
            "content": prompt
        }
    ]
    try:
        editorStart = time()
        editorText = client.chat.completions.create(
            model="qwen3-4b",
            messages=messages,
        )
        editorTime = time() - editorStart
        print(f"    ✅ Editor completed in {editorTime:.1f}s")
        # editorText = sub(r'\[THINK\].*?\[/THINK\]', '', editorText.choices[0].message.content)

        return editorText.choices[0].message.content 

    except Exception as e:
        print(f"    ⚠️ ERROR Editor PAGE {pageNum}: {e}")
        return f"[ERROR Editor PAGE {pageNum}: {e}]"

def convertPDFToMDInteractive():
    print("📄 converti PDF in MD")
    useOpenAI = checkOpenAIConnection()
    if useOpenAI:
        print("✅ openAI disponibile")
    else:
        print("❌ openAI non disponibile")

    inputFile = selectInputPDFFile()
    if inputFile is None:
        return None

    print(f"✅ File selezionato: {inputFile}")
    inputFilePath = join("input", inputFile)
    outputFilePath = Path(join("output", inputFile.replace(".pdf", "")))
    outputFilePath.mkdir(parents=True, exist_ok=True)

    processing_stats = {
        'pages_processed': 0,
        'pages_successful': 0,
        'pages_failed': [],
        'total_time': 0,
        'page_times': [],
        'retry_attempts': 0
    }

    pdfStart = time()
    allContent = []
    doc = fitz.open(inputFilePath)
    outputFileName =f"{inputFile.replace('.pdf', '')}"
    totalPages = len(doc)

    for i,page in enumerate(doc):
        pageSuccess = False
        visionSuccess = False
        editorSuccess = False
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"  [{timestamp}] Starting page processing {i+1}/{totalPages}")
        processing_stats['pages_processed'] += 1
        pageStart = time()
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat)
        imgBytes = pix.tobytes('png')

        ocrText = ocrWithEasyOCR(imgBytes, i+1)

        if ocrText.startswith("[ERROR OCR"):
            processing_stats['pages_failed'].append(i+1)
            print(f"    ⚠️ Page {i+1} marked as failed (OCR failed)")
            continue
        
        visionText = refineWithVision(imgBytes, ocrText, i+1)
        
        if visionText.startswith("[ERROR Vision"):
            processing_stats['pages_failed'].append(i+1)
            print(f"    ⚠️ Page {i+1} marked as failed (Vision failed) using OCR fallback")
            visionText = ocrText
        else:
            visionSuccess = True

        editorText = refineWithEditor(visionText, i+1)

        if editorText.startswith("[ERROR Editor"):
            processing_stats['pages_failed'].append(i+1)
            print(f"    ⚠️ Page {i+1} marked as failed (Editor failed) using Vision fallback")
            editorText = visionText
        else:
            editorSuccess = visionSuccess
        
        pageSuccess = True if editorSuccess else False
        processing_stats['pages_successful'] += 1 if pageSuccess else 0
        pageTime = time() - pageStart
        processing_stats['page_times'].append(pageTime)
        successPercentage = (processing_stats['pages_successful'] / processing_stats['pages_processed']) * 100
        elapsedTotal = (time() - pdfStart) 
        avgTimePerPage = elapsedTotal / processing_stats['pages_processed']
        remainingPages = totalPages - processing_stats['pages_processed']
        etaSeconds = remainingPages * avgTimePerPage
        etaMinutes = etaSeconds / 60

        if pageSuccess:
            allContent.append(editorText)
        else:
            allContent.append(f"[PAGE {i+1} FAILED]\n\n{editorText}")

        timestamp = datetime.now().strftime("%H:%M:%S")
        elapsedTotalMinutes = elapsedTotal / 60
        pageTimeMinutes = pageTime / 60
        print(f"    [{timestamp}] Completed page {i+1}/{totalPages} in {pageTimeMinutes:.1f} minutes {successPercentage:.1f}% {elapsedTotalMinutes:.1f} minutes elapsed {etaMinutes:.1f} minutes remaining")
        outputFile = join(outputFilePath, f"{outputFileName}p{i+1}.md")

        with open(outputFile, "w", encoding="utf-8") as f:
            f.write(editorText)

        del imgBytes, ocrText, visionText, editorText
        collect()
    
    docContent = "\n\n---\n\n".join(allContent)

    with open(join(outputFilePath, f"{outputFileName}.md"), "w", encoding="utf-8") as f:
        f.write(docContent)

    processing_stats['total_time'] = time() - pdfStart
    totalTimeMinutes = processing_stats['total_time'] / 60
    print(f"✅ PDF converted to MD in {totalTimeMinutes:.1f} minutes")
    print(f"✅ {processing_stats['pages_successful']} pages successful")
    print(f"✅ {processing_stats['pages_failed']} pages failed")
    print(f"✅ {processing_stats['pages_processed']} pages processed")
    print(f"✅ {processing_stats['page_times']} page times")
    print(f"✅ {processing_stats['retry_attempts']} retry attempts")

def connectMilvus(host:str, port:int)->None:
    try:
        connections.connect(host=host, port=port)
        print(f"✅ Connessione a Milvus {host}:{port} riuscita")
    except Exception as e:
        print(f"❌ Errore connessione a Milvus: {e}")
        raise 

def showMainMenu() -> int:
    print("\n=== MINERVA ===")
    for i, option in enumerate(options, start=1):
        print(f"{i}. {option['description']}")

    while True:
        choice = input(f"\nSeleziona un'opzione (1-{len(options)}): ").strip()
        if choice in [str(i) for i in range(1, len(options) + 1)]:
            return int(choice)
        print("Selezione non valida. Riprova.")

def selectFolder() -> str:
    folders = listdir("output")
    print("📁 Collezioni disponibili in output")
    print("=" * 50)
    for i, folder in enumerate(folders, start=1):
        print(f"{i}. {folder}")
    print("=" * 50)

    if len(listdir('output')) == 0:
        print("Nessuna collezione disponibile. Importa un file per iniziare.")
        return None

    if len(listdir('output')) == 1:
        print(f"Collezione selezionata automaticamente: {listdir('output')[0]}")
        return listdir('output')[0]

    while True:
        choice = input(f"\nSeleziona una collezione (1-{len(listdir('output'))}): ").strip()

        if choice in [str(i) for i in range(1, len(listdir('output')) + 1)]:
            return listdir("output")[int(choice) - 1]
        print("Selezione non valida. Riprova.")

def selectFile(folder:str)->str:
    if folder is None:
        return None

    files = [f for f in listdir(join("output", folder)) if not search(r"p\d+(?=\.(md|txt)$)", f, IGNORECASE)]
    print(f"📄 File disponibili in {folder}")
    print("=" * 50)

    for i, file in enumerate(files, start=1):
        print(f"{i}. {file}")

    print("=" * 50)

    if len(files) == 0:
        print("Nessun file disponibile. Importa un file per iniziare.")
        return None

    if len(files) == 1:
        print(f"File selezionato automaticamente: {files[0]}")
        return files[0]
    
    while True:
        choice = input(f"\nSeleziona un file (1-{len(files)}): ").strip()

        if choice in range(1, len(files) + 1):
            return files[int(choice) - 1]
    
        print("Selezione non valida. Riprova.")

def extractTags(text:str)->str:
    tags = []

    for tag in findall(r'\[\[(.*?)\]\]', text):
        tags.append(tag.strip().lower())
    
    return ', '.join(tags)

def generateEmbedding(model:SentenceTransformer,text:str)->list:
    embedding = model.encode(text,batch_size=1,normalize_embeddings=True)

    return embedding.astype(float32)

def generateSimplifiedBM25Embedding(model:SentenceTransformer,text:str)->list:
    tokens = model.tokenizer.tokenize(text)
    ids = model.tokenizer.convert_tokens_to_ids(tokens)
    counts = Counter(ids)
    norm = sum(value**2 for value in counts.values())**0.5

    if norm > 0:
        sparseEmbedding = {i:value/norm for i,value in counts.items()}
    else:
        sparseEmbedding = {}
    
    return sparseEmbedding

def getNewCollection(collectionName:str)->Collection:
    schema = CollectionSchema(fields=[
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=1024),
        FieldSchema(name="sparse", dtype=DataType.SPARSE_FLOAT_VECTOR, enable_match=True, enable_analyzer=True),
        FieldSchema(name="tagsSparse", dtype=DataType.SPARSE_FLOAT_VECTOR, enable_match=True, enable_analyzer=True),
        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=2000, enable_analyzer=True, enable_match=True),
        FieldSchema(name="tags", dtype=DataType.VARCHAR, max_length=2000, enable_analyzer=True, enable_match=True),
        FieldSchema(name="metadata", dtype=DataType.JSON),
    ])
    bm25= Function(name="bm25_content", function_type=FunctionType.BM25, input_field_names=["content"], output_field_names=["sparse"])
    bm25Tags = Function(name="bm25_tags", function_type=FunctionType.BM25, input_field_names=["tags"], output_field_names=["tagsSparse"])
    schema.add_function(bm25)
    schema.add_function(bm25Tags)

    if utility.has_collection(collectionName):
        print(f"⚠️  La collezione '{collectionName}' esiste già. Verifica dello schema...")
        existingColl = Collection(collectionName)
        existingSchema = existingColl.schema
        
        # Verifica se lo schema ha tutti i campi necessari
        expectedFields = {"id", "embedding", "sparse", "tagsSparse", "content", "tags", "metadata"}
        existingFields = {field.name for field in existingSchema.fields}
        
        if expectedFields <= existingFields:
            print(f"✅ Schema esistente è compatibile")
            coll = existingColl
        else:
            missingFields = expectedFields - existingFields
            print(f"❌ Schema esistente manca campi: {missingFields}")
            print(f"🗑️  Eliminazione collezione esistente per ricrearla...")
            utility.drop_collection(collectionName)
            print(f"✅ Collezione eliminata")
            coll = Collection(name=collectionName, schema=schema)
            coll.create_index(field_name='embedding', index_type='FLAT', metric_type='COSINE')
            coll.create_index(field_name='sparse', index_params={"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"})
            coll.create_index(field_name='tagsSparse', index_params={"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"})
            coll.create_index(field_name='tags', index_params={"index_type": "INVERTED", "metric_type": "BM25"})
    
    else:
        coll = Collection(name=collectionName, schema=schema)
        coll.create_index(field_name='embedding', index_type='FLAT', metric_type='COSINE')
        coll.create_index(field_name='sparse', index_params={"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"})
        coll.create_index(field_name='tagsSparse', index_params={"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"})
        coll.create_index(field_name='tags', index_params={"index_type": "INVERTED", "metric_type": "BM25"})
        print(f"✅ Collezione creata con successo")
    
    return coll

def getCollection(collectionName:str)->Collection:
    if utility.has_collection(collectionName):
        return Collection(collectionName)
    else:
        return None

def ingestFileInteractive()->None: 
    print("📄 importa file")
    host = 'localhost'
    port = 19530
    useDefault = input("Usare la connessione predefinita a Milvus? (y/n): ").strip()

    if useDefault in ['y', 'Y', '']:
        connectMilvus(host=host, port=port)
    else:
        host = input("Inserisci l'host di Milvus: ").strip()
        port = int(input("Inserisci la porta di Milvus: ").strip())
        connectMilvus(host=host, port=port)

    folder = selectFolder()
    filePath = selectFile(folder)
    records = []
    chunkChars = 1000
    overlap = 200
    model = SentenceTransformer('jinaai/jina-embeddings-v3', trust_remote_code=True)

    if filePath is None:
        return None

    print(f"Importing file: {filePath}")

    with open(join("output", folder, filePath), "r", encoding="utf-8") as file:
        content = file.read()
        sourceHash = SHA256(content.encode()).hexdigest()
        text = [[page.strip() for page in chapter.split("\n---\n")] for chapter in content.split("\n-----\n")]

    commonMetadata = dict()
    commonMetadata['title'] = filePath
    commonMetadata['author'] = ['none'] # placeholder
    commonMetadata['editor'] = 'none' # placeholder
    commonMetadata['ISBN'] = 'none' # placeholder
    commonMetadata['sourceHash'] = sourceHash

    print(f"✅Imported {sum(len(chapter) for chapter in text)} pages from {filePath}")

    for i, chapter in enumerate(text):
        l = 0
        p = 0

        for j, page in enumerate(chapter, start=p):
            prefix = ''
            suffix = ''
            if j >= 1:
                prefix = text[i][j-1].split('.')[-1] if not text[i][j-1].endswith('.') else ''
            if j < len(chapter) - 1:
                suffix = text[i][j+1].split('.')[0] if not text[i][j].endswith('.') else ''

            page = prefix + page + suffix
            page = page.strip()

            start = 0
            n = len(page)
            k = 0

            while start < n:
                end = min(start + chunkChars, n)
                # end = start + chunkChars if n - start > chunkChars else n
                chunk = page[start:end]

                tags = extractTags(chunk)
                metadata = dict()
                metadata['page'] = j
                metadata['pageFragment'] = k
                metadata['chapter'] = i
                metadata['chapterFragment'] = l
                metadata['revisionDate'] = datetime.now().isoformat()
                metadata.update(commonMetadata)
                hashString = chunk 
                hashString += metadata['title'] 
                hashString += ', '.join(metadata['author']) 
                hashString += metadata['editor'] 
                hashString += metadata['ISBN'] 
                hashString += str(metadata['page']) 
                hashString += str(metadata['chapter']) 
                hashString += str(metadata['pageFragment']) 
                hashString += str(metadata['chapterFragment']) 
                hashString += tags
                hashString += metadata['revisionDate']
                hashString = SHA256(hashString.encode()).hexdigest().upper()
                record = dict()
                record['id'] = hashString
                record['content'] = chunk
                record['tags'] = tags
                record['embedding'] = generateEmbedding(model,chunk).tolist()
                record['metadata'] = metadata

                if end == n:
                    break

                records.append(record)
                start = max(0, end - overlap)
                k += 1
                l += 1
            
            p += 1
    
    defaultName = basename(folder).lower().split(' ')[0]
    name = input(f"Inserisci il nome della collezione (default: {defaultName}): ").strip()
    collectionName = name if name != '' else defaultName
    
    # Chiedi se inserire anche nella collezione bookpages
    useBookpages = input("Inserire i record anche nella collezione 'bookpages'? (y/n): ").strip()
    insertInBookpages = useBookpages.lower() in ['y', 'yes', ''] or useBookpages == ''
    
    # Crea/carica la collezione principale
    collection = getNewCollection(collectionName)
    collection.load()
    
    # Crea/carica la collezione bookpages se richiesto
    bookpagesCollection = None
    if insertInBookpages:
        bookpagesCollection = getNewCollection('bookpages')
        bookpagesCollection.load()
    
    # Controlla duplicati per la collezione principale
    newRecords = []
    duplicateCount = 0
    
    for record in records:
        # Controlla se il record esiste già nella collezione principale
        existingRecord = collection.query(
            expr=f'id == "{record["id"]}"',
            output_fields=["id"],
            limit=1
        )
        
        if not existingRecord:
            newRecords.append(record)
        else:
            duplicateCount += 1
    
    # Inserisci nella collezione principale
    if newRecords:
        collection.insert(newRecords)
        print(f"✅ {len(newRecords)} nuovi record importati in {collectionName}")
    else:
        print(f"✅ Nessun nuovo record da importare in {collectionName}")
    
    if duplicateCount > 0:
        print(f"ℹ️ {duplicateCount} record duplicati saltati per {collectionName}")
    
    # Inserisci anche in bookpages se richiesto
    if insertInBookpages and bookpagesCollection is not None:
        newBookpagesRecords = []
        bookpagesDuplicateCount = 0
        
        for record in records:
            # Controlla se il record esiste già in bookpages
            existingBookpagesRecord = bookpagesCollection.query(
                expr=f'id == "{record["id"]}"',
                output_fields=["id"],
                limit=1
            )
            
            if not existingBookpagesRecord:
                newBookpagesRecords.append(record)
            else:
                bookpagesDuplicateCount += 1
        
        if newBookpagesRecords:
            bookpagesCollection.insert(newBookpagesRecords)
            print(f"✅ {len(newBookpagesRecords)} nuovi record importati in bookpages")
        else:
            print(f"✅ Nessun nuovo record da importare in bookpages")
        
        if bookpagesDuplicateCount > 0:
            print(f"ℹ️ {bookpagesDuplicateCount} record duplicati saltati per bookpages")

def listCollections()->list:
    collections = utility.list_collections()

    return sorted(collections)

def selectCollectionInteractive()->str:
    collections = listCollections()
    print("📄 collezioni disponibili")
    print("=" * 50)

    for i, collection in enumerate(collections, start=1):
        print(f"{i}. {collection}")
    
    print("=" * 50)
    
    if len(collections) == 0:
        print("Nessuna collezione disponibile. Importa un file per iniziare.")
        return None
    
    if len(collections) == 1:
        print(f"Collezione selezionata automaticamente: {collections[0]}")
        return collections[0]
    
    while True:
        choice = int(input(f"\nSeleziona una collezione (1-{len(collections)}): ").strip())
        if choice in [i for i in range(1, len(collections) + 1)]:
            return collections[int(choice) - 1]
        print("Selezione non valida. Riprova.")

def getFilterExpression(filters = None)->str:
    if filters is None:
        return None
    
    conditions = []

    if 'title' in filters:
        if isinstance(filters['title'], str):
            conditions.append(f"metadata[\"title\"] == \"{filters['title']}\"")
        elif isinstance(filters['title'], list):
            for title in filters['title']:
                conditions.append(f"metadata[\"title\"] ==  \"{title}\"")
    
    if 'author' in filters:
        if isinstance(filters['author'], str):
            conditions.append(f"metadata[\"author\"] == \"{filters['author']}\"")
        elif isinstance(filters['author'], list):
            for author in filters['author']:
                conditions.append(f"metadata[\"author\"] ==  \"{author}\"")
    
    if 'editor' in filters:
        if isinstance(filters['editor'], str):
            conditions.append(f"metadata[\"editor\"] == \"{filters['editor']}\"")

    if 'page' in filters:
        if filters['page']['min'] == filters['page']['max']:
            conditions.append(f"metadata[\"page\"] == {filters['page']['min']}")
        else:
            conditions.append(f"metadata[\"page\"] >= {filters['page']['min']} AND metadata[\"page\"] <= {filters['page']['max']}")

    if 'chapter' in filters:
        if filters['chapter']['min'] == filters['chapter']['max']:
            conditions.append(f"metadata['chapter'] == {filters['chapter']['min']}")
        else:
            conditions.append(f"metadata['chapter'] >= {filters['chapter']['min']} AND metadata['chapter'] <= {filters['chapter']['max']}")
    
    if conditions:
        filterExpression = ") AND (".join(conditions)
        filterExpression = f"({filterExpression})"
        return filterExpression
    
    return None

def getSemanticResults(query:str, collectionName:str, topK:int, filters:str = None)->list:
    model = SentenceTransformer('jinaai/jina-embeddings-v3', trust_remote_code=True)
    embeddings = generateEmbedding(model,query)
    collection = getCollection(collectionName)

    if collection is None:
        return None
    
    searchParams = {'metric_type': 'COSINE'}

    results = collection.search(
        data=[embeddings],
        anns_field='embedding',
        limit=topK,
        expr=filters if filters else None,
        output_fields=['id', 'content', 'tags', 'metadata'],
        param=searchParams
    )[0]

    results = [
        {
            'id': result.id,
            'content': result.content,
            'tags': result.tags,
            'metadata': result.metadata,
            'semanticScore': float(result.score),
            'keywordScore': 0.0,
            'tagsScore': 0.0
        } for result in results
    ]

    return results

def getKeywordResults(query:str, collectionName:str, topK:int, filters:str = None)->list:
    collection = getCollection(collectionName)

    if collection is None:
        return None
    
    searchParams = {'metric_type': 'BM25'}

    results = collection.search(
        data=[query],
        anns_field='sparse',
        limit=topK,
        expr=filters if filters else None,
        output_fields=['id', 'content', 'tags', 'metadata'],
        param=searchParams
    )[0]

    results = [
        {
            'id': result.id,
            'content': result.content,
            'tags': result.tags,
            'metadata': result.metadata,
            'semanticScore': 0.0,
            'keywordScore': float(result.score),
            'tagsScore': 0.0
        } for result in results
    ]

    return results

def getTagsScore(query:str,tags: str)->float:
    if tags == '':
        return 0.0
    
    query = query.lower()
    score = 0.0

    if query in tags:
        score += 0.6

    words = query.strip().split()

    if words:
        matches = [1 for word in words if word in tags and len(word) > 2]
        wordScore = 0.4 * len(matches) / len(words)
        score += wordScore

    return min(1.0, score)

def getTagsResults(query:str, collectionName:str, topK:int, filters:str = None)->list:
    collection = getCollection(collectionName)

    if collection is None:
        return None
    
    # Ricerca testuale BM25 sul campo tags dedicato
    searchParams = {'metric_type': 'BM25'}

    results = collection.search(
        data=[query],
        anns_field='tagsSparse',
        limit=topK,
        expr=filters if filters else None,
        output_fields=['id', 'content', 'tags', 'metadata'],
        param=searchParams
    )[0]

    results = [
        {
            'id': result.id,
            'content': result.content,
            'tags': result.tags,
            'metadata': result.metadata,
            'semanticScore': 0.0,
            'keywordScore': 0.0,
            'tagsScore': getTagsScore(query, result.tags)
        } for result in results
    ]

    return results

def getCombinedResults(semanticResults:list, keywordResults:list, tagsResults:list, topK:int, semanticWeight:float, keywordWeight:float, tagsWeight:float)->list:
    combinedScores = {}

    for result in semanticResults:
        id = result['id']
        combinedScores[id] = {
            **result,
            'hybridScore': result['semanticScore'] * semanticWeight
        }

    for result in keywordResults:
        id = result['id']
        
        if id in combinedScores:
            combinedScores[id]['keywordScore'] = result['keywordScore']
            combinedScores[id]['hybridScore'] += result['keywordScore'] * keywordWeight
        else:
            combinedScores[id] = {
                **result,
                'hybridScore': result['keywordScore'] * keywordWeight
            }

    for result in tagsResults:
        id = result['id']
        if id in combinedScores:
            combinedScores[id]['tagsScore'] = result['tagsScore']
            combinedScores[id]['hybridScore'] += result['tagsScore'] * tagsWeight
        else:
            combinedScores[id] = {
                **result,
                'hybridScore': result['tagsScore'] * tagsWeight
            }

    sortedResults = sorted(combinedScores.values(), key=lambda x: x['hybridScore'], reverse=True)
    return sortedResults[:topK]

def hybridSearchInteractive():
    print("🔍 ricerca ibrida")
    host = 'localhost'
    port = 19530
    topK = 10
    model = SentenceTransformer('jinaai/jina-embeddings-v3', trust_remote_code=True)
    ef = 512
    vectorWeight = 0.4
    textWeight = 0.4
    tagsWeight = 0.2

    useDefault = input("Usare la connessione predefinita a Milvus? (y/n): ").strip()

    if useDefault in ['y', 'Y', '']:
        connectMilvus(host=host, port=port)
    else:
        host = input("Inserisci l'host di Milvus: ").strip()
        port = int(input("Inserisci la porta di Milvus: ").strip())
        connectMilvus(host=host, port=port)

    collectionName = selectCollectionInteractive()
    query = input("Inserisci la query: ").strip()
    filters = None # placeholder
    filters = getFilterExpression(filters)
    semanticResults = getSemanticResults(query, collectionName, topK, filters)
    keywordResults = getKeywordResults(query, collectionName, topK, filters)
    tagsResults = getTagsResults(query, collectionName, topK, filters)
    combinedResults = getCombinedResults(semanticResults, keywordResults, tagsResults, topK, vectorWeight, textWeight, tagsWeight)

    print(f"🔍 Risultati ibridi per: {query}")
    print("=" * 50)

    for i, result in enumerate(combinedResults, start=1):
        print(f"{i}. {result['content']}")
        print(f"  - Score: {result['hybridScore']:.4f} (Semantico: {result['semanticScore']:.4f}, Testuale: {result['keywordScore']:.4f}, Tags: {result['tagsScore']:.4f})")
        print(f"  - Page: {result['metadata'].get('page', '')}")
        print(f"  - Tags: {result['tags']}")
        print("=" * 20)

    print(f"\n✅ Ricerca completata: {len(combinedResults)} risultati totali")

def printCollections()->None:
    host = 'localhost'
    port = 19530
    useDefault = input("Usare la connessione predefinita a Milvus? (y/n): ").strip()

    if useDefault in ['y', 'Y', '']:
        connectMilvus(host=host, port=port)
    else:
        host = input("Inserisci l'host di Milvus: ").strip()
        port = int(input("Inserisci la porta di Milvus: ").strip())
        connectMilvus(host=host, port=port)
    
    collections = listCollections()
    print("📚 collezioni disponibili")
    print("=" * 70)
    for i, collectionName in enumerate(collections, start=1):
        try:
            collection = Collection(collectionName)
            collection.load()
            recordCount = collection.num_entities
            print(f"{i}. {collectionName} ({recordCount} record)")
        except Exception as e:
            print(f"{i}. {collectionName} (errore nel conteggio: {e})")
    print("=" * 70)

def deleteCollectionInteractive()->None:
    print("🗑️ elimina collezione")
    collectionName = selectCollectionInteractive()
    confirm = input(f"Sei sicuro di voler eliminare la collezione {collectionName}? (ELIMINA): ").strip()

    if confirm in ['ELIMINA', 'elimina']:
        utility.drop_collection(collectionName)
        print(f"✅ Collezione {collectionName} eliminata")
    else:
        print("🚫 eliminazione annullata")

def chatInteractive():
    print("💬 chat")
    
    client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        timeout=30.0,
        max_retries=3
    )
    
    print("Chat iniziata. Digita 'exit' o 'esci' per terminare.")
    print("=" * 50)
    
    while True:
        userInput = input("\n👤 Tu: ").strip()
        
        if userInput.lower() in ['exit', 'esci', 'quit']:
            print("👋 Chat terminata!")
            break
            
        if not userInput:
            continue
            
        try:
            print("🤖 Minerva: ", end="", flush=True)
            
            stream = client.chat.completions.create(
                model="qwen3-4b",
                messages=[
                    {"role": "user", "content": userInput}
                ],
                stream=True
            )
            
            for chunk in stream:
                if chunk.choices[0].delta.content is not None:
                    content = chunk.choices[0].delta.content
                    # Stampa carattere per carattere per simulare streaming token-by-token
                    for char in content:
                        print(char, end="", flush=True)
                                # sleep(0.0001)  # Piccola pausa per effetto visivo
            
            print()  # Nuova riga alla fine della risposta
            
        except Exception as e:
            print(f"❌ Errore durante la chat: {e}")
            print("Verifica che il server sia in esecuzione su localhost:8000")

def chatCurlInteractive():
    print("💬 chat (curl, JSON completo)")
    print("Chat iniziata. Digita 'exit' o 'esci' per terminare.")
    print("=" * 50)
    
    while True:
        userInput = input("\n👤 Tu: ").strip()
        
        if userInput.lower() in ['exit', 'esci', 'quit']:
            print("👋 Chat terminata!")
            break
        
        if not userInput:
            continue
        try:
            # Prepara il payload JSON con streaming abilitato
            payload = {
                "model": "qwen3-4b",
                "messages": [
                    {"role": "user", "content": userInput}
                ],
                "stream": False
            }
            
            # Headers per la richiesta
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer 2E99758548972A8E8822AD47FA1017FF72F06F3FF6A016851F45C398732BC50C"
            }
            
            print("🤖 Minerva: ", end="", flush=True)
            
            # Invia la richiesta POST con streaming
            response = post(
                "http://localhost:8000/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=30,
                stream=False
            )
            
            # Verifica se la richiesta è andata a buon fine
            response.raise_for_status()
            
            # Ottieni la risposta JSON completa
            responseData = response.json()
            
            # Stampa il JSON completo della risposta
            print(f"\n📋 Risposta JSON completa:")
            print(dumps(responseData, indent=2, ensure_ascii=False))
            print("=" * 70)
            
            # Estrai e mostra anche solo il contenuto della risposta per leggibilità
            if 'choices' in responseData and len(responseData['choices']) > 0:
                content = responseData['choices'][0].get('message', {}).get('content', '')
                if content:
                    print(f"\n💬 Contenuto risposta:")
                    print(content)
                    print("=" * 70)
                
        except RequestException as e:
            print(f"❌ Errore di connessione: {e}")
        except ValueError as e:
            print(f"❌ Errore parsing JSON: {e}")
        except Exception as e:
            print(f"❌ Errore durante la chat: {e}")
            print("Verifica che il server sia in esecuzione su localhost:8000")

def main():
    while True:
        choice = showMainMenu()
        options[choice - 1]['function']()

if __name__ == "__main__":
    main()