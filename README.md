# Magyar Diktáló – Offline AI Hangfelismerő

Offline magyar hangfelismerő és diktáló alkalmazás Windows alatt.  
Gyorsbillentyűre felveszi a hangot, Whisper AI-jal szöveggé alakítja, majd beilleszti bármely szövegmezőbe.

---

## Főbb funkciók

- **Offline működés** – nincs szükség internetre a hangfelismeréshez
- **Magyar optimalizált modellek** – natív magyar fine-tuned Whisper modellek támogatása
- **Hold-to-record** – `Ctrl+Alt+Space` lenyomva tartásával rögzít, elengedésnél automatikusan dolgozza fel; 5 másodperc után automatikusan leáll
- **Hangparancsok** – kimondott szóval szúr be írásjeleket és sortöréseket (pl. „pont", „kérdőjel", „új bekezdés", „entert")
- **Billentyűparancsok** – `Scroll Lock` = új sor, `Pause` = új bekezdés
- **Fókuszvisszaállítás** – Windows API-n keresztül visszaadja a fókuszt az eredeti ablaknak beillesztés előtt
- **GPU gyorsítás** – CUDA float16/float32 support
- **LLM javítás** – opcionálisan Ollama (Llama3, Mistral, Phi3) javítja a felismert szöveget

---

## Támogatott Whisper modellek

| Modell | Megjegyzés |
|--------|-----------|
| tiny / base / small / medium / large-v3 | Multilingual OpenAI modellek |
| domebacsi/faster-whisper-small-hu | Natív CTranslate2 magyar modell |
| sarpba/whisper-hu-large-v3-turbo-finetuned | WER ~7.5%, float32 módban fut |
| Trendency/whisper-large-v3-hu | WER ~11.3% |
| sarpba/whisper-base-hungarian_v1 | Kis méretű magyar alap modell |

---

## Telepítés

### 1. Python és függőségek

Python 3.10+ szükséges.

```bash
pip install -r requirements.txt
```

### 2. CUDA (GPU gyorsításhoz)

NVIDIA GPU esetén telepítsd a [CUDA Toolkit](https://developer.nvidia.com/cuda-downloads)-et és a megfelelő PyTorch verziót:

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### 3. CTranslate2 (Whisper futtatáshoz)

```bash
pip install ctranslate2 faster-whisper
```

### 4. Ollama (opcionális LLM javítás)

Töltsd le az [Ollama](https://ollama.ai)-t, majd:

```bash
ollama pull llama3
```

---

## Indítás

```bash
python voicetex_hu.py
```

---

## Használat

1. Válaszd ki a kívánt Whisper modellt a legördülő menüből
2. Kattints az **„AI Modellek Betöltése"** gombra (első alkalommal a modell letöltődik)
3. Kattints a célalkalmazás szövegmezőjébe
4. Tartsd nyomva a `Ctrl+Alt+Space` billentyűkombinációt és beszélj
5. Engedd el – a szöveg automatikusan beillesztődik

### Hangparancsok

| Kimondott szó | Eredmény |
|--------------|---------|
| pont | `.` |
| kérdőjel | `?` |
| felkiáltójel | `!` |
| vessző | `,` |
| kettőspont | `:` |
| pontosvessző | `;` |
| kötőjel | `-` |
| gondolatjel | `—` |
| zárójel nyit | `(` |
| zárójel zár | `)` |
| entert / sortörés / új sor | Enter |
| két entert / új bekezdés | dupla Enter |

---

## Rendszerkövetelmények

- Windows 10/11
- Python 3.10+
- NVIDIA GPU ajánlott (CPU-n is fut, de lassabb)
- Mikrofonos hangbemenet (USB mikrofon is támogatott)

---

## Licenc

MIT License – szabadon felhasználható, módosítható.
