# Magyar Diktáló – Offline AI Hangfelismerő

Offline magyar hangfelismerő és diktáló alkalmazás Windows alatt.  
`Alt+Space` lenyomva tartásával rögzít, Whisper AI-jal szöveggé alakítja, majd beilleszti bármely nyitott szövegmezőbe – internet nélkül, teljesen helyileg.

---

## Főbb funkciók

- **Offline működés** – a hangfelismerés teljesen helyi, nincs szükség internetre
- **Hold-to-record** – `Alt+Space` lenyomva tartásával felvesz; elengedéskor azonnal feldolgoz; 5 másodperc után automatikusan leáll (ha tovább tartod, folytatja)
- **Magyar optimalizált modellek** – több fine-tuned magyar Whisper modell beépítve, automatikus letöltéssel és konverzióval
- **Hangparancsok** – kimondott szóval szúr be írásjeleket és sortöréseket (pl. „pont", „kérdőjel", „új bekezdés", „entert")
- **Billentyűparancsok** – `Scroll Lock` = új sor, `Pause/Break` = új bekezdés
- **UI gombok** – az ablakban lévő „Új sor" és „Bekezdés" gombokkal is beilleszthető sortörés
- **Mikrofon kiválasztó** – automatikusan felismeri az USB mikrofont, de bármely bemeneti eszköz kiválasztható
- **Fókuszvisszaállítás** – Windows API-n keresztül visszaadja a fókuszt az eredeti ablaknak, így bármely alkalmazásba beilleszthető a szöveg
- **GPU gyorsítás** – CUDA float16/float32 támogatás (CPU-n is fut, de lassabb)
- **LLM javítás** – opcionálisan Ollama (Llama3, Mistral, Phi3) javítja a felismert szöveget

---

## Támogatott Whisper modellek

| Megjelenített név | HuggingFace modell | Típus | Megjegyzés |
|-------------------|--------------------|-------|-----------|
| tiny | openai/whisper-tiny | standard | Leggyorsabb, gyengébb minőség |
| base | openai/whisper-base | standard | Általános alap |
| small | openai/whisper-small | standard | Jó egyensúly |
| medium | openai/whisper-medium | standard | Jó minőség |
| large-v3 | openai/whisper-large-v3 | standard | Legjobb általános, 16GB GPU ajánlott |
| HU: faster-whisper-small-hu ✓ | domebacsi/faster-whisper-small-hu | natív CT2 | Magyar CV adaton tanítva, azonnal betölthető |
| HU: turbo-finetuned ⭐ | sarpba/whisper-hu-large-v3-turbo-finetuned | HF → CT2 float32 | WER ~7.5%, legjobb magyar modell |
| HU: whisper-large-v3-hu | Trendency/whisper-large-v3-hu | HF → CT2 | WER ~11.3% |
| HU: whisper-base-hungarian_v1 | sarpba/whisper-base-hungarian_v1 | HF → CT2 | WER ~25.6%, kis méret |

A `HF → CT2` jelölésű modellek első betöltéskor automatikusan konvertálódnak CTranslate2 formátumra (ez egyszer ~5–15 percet vehet igénybe). A konvertált modellek a `voicetex_models/` mappában tárolódnak.

---

## Hangparancsok

Kimondhatók a szöveg bármelyik részén – a felismerés után automatikusan cserélődnek a megfelelő jelekre:

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
| macskakörm**ök** | `"` |
| entert / sortörés / új sor / következő sor | Enter |
| egy entert / 1 entert | Enter |
| két entert / kettő entert / új bekezdés / bekezdés | dupla Enter |
| [szó] nagybetű | az előző szót nagybetűsíti |

---

## Billentyűparancsok

| Billentyű | Funkció |
|-----------|---------|
| `Alt + Space` (lenyomva tartva) | Felvétel indítása / folytatása |
| `Alt + Space` (elengedve) | Felvétel leállítása + feldolgozás |
| `Scroll Lock` | Új sor beillesztése az aktív szövegmezőbe |
| `Pause / Break` | Új bekezdés (dupla Enter) beillesztése |

---

## Hardverkövetelmények

Az alkalmazás **NVIDIA GPU nélkül is működik**, de a feldolgozási sebesség erősen függ a hardvertől.

### GPU mód (ajánlott) – NVIDIA CUDA

| Modell méret | Ajánlott VRAM | Feldolgozási idő* |
|-------------|--------------|-------------------|
| tiny / base | 2 GB | < 1 mp |
| small | 2–4 GB | 1–2 mp |
| medium | 4–8 GB | 2–4 mp |
| large-v3, turbo | 8–16 GB | 3–6 mp |

*Egy ~5 másodperces mondathoz. A program automatikusan észleli a GPU-t és float16 módban tölt.

### CPU mód – GPU nélkül is működik

Ha nincs NVIDIA videokártya (vagy nincs CUDA driver), az alkalmazás automatikusan CPU módra vált és int8 kvantált modellt tölt. Ez **lassabb**, de teljesen működőképes:

| Modell méret | RAM szükséglet | Feldolgozási idő* |
|-------------|---------------|-------------------|
| tiny / base | 2–4 GB | 5–15 mp |
| small | 4–6 GB | 15–30 mp |
| medium | 6–10 GB | 30–60 mp |
| large-v3 | 10–16 GB | 1–3 perc |

*CPU-n a `tiny` vagy `base` modell a legjobb választás a gyorsaság miatt.  
**CPU-n a magyar `HU: faster-whisper-small-hu` modell is jól működik és jobb minőséget ad mint a `tiny`.**

### Összefoglalás

| | Működik? | Sebesség |
|---|---------|---------|
| NVIDIA GPU (8–16 GB VRAM) | ✅ | Gyors (1–5 mp) |
| NVIDIA GPU (2–4 GB VRAM) | ✅ | Jó (kis modellek) |
| AMD GPU / Intel GPU | ✅ CPU módban | Lassabb |
| Laptop, beépített grafika | ✅ CPU módban | Lassabb |
| GPU nélkül | ✅ CPU módban | Lassabb |

---

## Telepítés

### 1. Követelmények

- Windows 10 / 11
- Python 3.10+
- NVIDIA GPU ajánlott, de nem kötelező

### 2. Függőségek telepítése

```bash
pip install -r requirements.txt
```

### 3. PyTorch CUDA (GPU gyorsításhoz)

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
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

1. Válaszd ki a mikrofont a legördülő menüből (az USB mikrofon automatikusan kiválasztódik)
2. Válaszd ki a kívánt Whisper modellt
3. Kattints az **„AI Modellek Betöltése"** gombra (első alkalommal a modell letöltődik)
4. Kattints a célalkalmazás szövegmezőjébe (pl. Word, Notepad, böngésző)
5. Tartsd nyomva az `Alt+Space` billentyűkombinációt és beszélj
6. Engedd el – a szöveg automatikusan beillesztődik

---

## Licenc

MIT License – szabadon felhasználható és módosítható.
