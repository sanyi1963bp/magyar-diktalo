import customtkinter as ctk
import tkinter as tk
import threading
from collections import deque
import keyboard
import sounddevice as sd
import numpy as np
from scipy.io import wavfile
from faster_whisper import WhisperModel
try:
    from llama_cpp import Llama as LlamaCpp
    LLAMA_CPP_OK = True
except ImportError:
    LLAMA_CPP_OK = False
import pyautogui
import pyperclip
import os
import re
import subprocess
import sys
import json
import ctypes
import time
import tempfile

# --- Windows fókusz-kezelés ---
_user32 = ctypes.windll.user32

def get_foreground_window():
    """Visszaadja az éppen aktív ablak handle-jét."""
    return _user32.GetForegroundWindow()

def restore_focus(hwnd):
    """
    Visszaadja a fókuszt a megadott ablaknak.
    AttachThreadInput trükkel megkerüli a Windows fókusz-lopás védelmet.
    """
    if not hwnd:
        return
    try:
        current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        target_tid  = _user32.GetWindowThreadProcessId(hwnd, None)
        attached = _user32.AttachThreadInput(current_tid, target_tid, True)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        if attached:
            _user32.AttachThreadInput(current_tid, target_tid, False)
    except Exception:
        pass

def paste_text_to_window(hwnd, text):
    """
    Megbízhatóan beilleszt egy szöveget a célablakba.
    Sortöréseket Enter billentyűvel kezeli (webes és natív appokban egyaránt működik).
    A szöveg és az Enterek mindig helyes sorrendben kerülnek ki.
    """
    restore_focus(hwnd)
    time.sleep(0.3)  # Több idő a fókuszváltásra

    # Szöveg felosztása sorokra, majd sorban: szöveg → Enter → szöveg → Enter...
    lines = text.split('\n')
    for i, line in enumerate(lines):
        if line.strip():
            pyperclip.copy(line)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.08)
        if i < len(lines) - 1:  # Ha nem az utolsó sor: Enter
            pyautogui.press('enter')
            time.sleep(0.08)

# --- MEL FILTERBANK JAVÍTÁS ---
def _compute_mel_filters(n_mels: int, n_fft: int = 400, sr: int = 16000) -> np.ndarray:
    """Whisper-kompatibilis mel szűrőmátrix: (n_mels, n_fft//2+1)."""
    def hz_to_mel(f): return 2595.0 * np.log10(1.0 + f / 700.0)
    def mel_to_hz(m): return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    freqs   = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    mel_pts = np.linspace(hz_to_mel(0.0), hz_to_mel(8000.0), n_mels + 2)
    hz_pts  = mel_to_hz(mel_pts)
    filters = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = hz_pts[i], hz_pts[i+1], hz_pts[i+2]
        filters[i]  = np.minimum(np.maximum(0.0, (freqs-lo)/(mid-lo)),
                                 np.maximum(0.0, (hi-freqs)/(hi-mid)))
    filters *= (2.0 / (hz_pts[2:n_mels+2] - hz_pts[:n_mels]))[:, np.newaxis]
    return filters


def fix_mel_bins(model) -> None:
    """
    Ha a modell és a feature extractor mel-száma eltér (pl. large-v3: 128 vs 80),
    közvetlenül kicseréli a mel_filters mátrixot. Osztálypéldányosítás nélkül,
    verziófüggetlen.
    """
    try:
        model_mels    = model.model.n_mels
        extractor_mels = model.feature_extractor.mel_filters.shape[0]
        if model_mels != extractor_mels:
            model.feature_extractor.mel_filters = _compute_mel_filters(model_mels)
    except Exception as e:
        print(f"[fix_mel_bins] {e}")


# --- KONFIGURÁCIÓ ---
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# --- HANGPARANCSOK (LLM nélkül is működnek) ---
# Egyértelmű, biztonságosan cserélhető parancsszavak
# A sorrend fontos: "pontosvessző" előbb jön mint "pont" !
VOICE_COMMANDS = [
    # Írásjelek
    (r'\bkérdőjel\b',                   '?'),
    (r'\bfelkiáltójel\b',               '!'),
    (r'\bpontosvessző\b',               ';'),
    (r'\bkettőspont\b',                 ':'),
    (r'\bvessző\b',                     ','),
    (r'\bkötőjel\b',                    '-'),
    (r'\bgondolatjel\b',                '—'),
    (r'\bmacskakörm[öo]k\b',            '"'),
    (r'\bzárójel\s*nyit\b',             '('),
    (r'\bzárójel\s*zár\b',              ')'),
    # --- Sortörések: minél több természetes változat ---
    # Dupla sortörés (bekezdés)
    (r'\bkét\s*entert?\b',              '\n\n'),
    (r'\bkettő\s*entert?\b',            '\n\n'),
    (r'\b2\s*entert?\b',                '\n\n'),
    (r'\búj\s*bekezdés\b',              '\n\n'),
    (r'\bbekezdés\b',                   '\n\n'),
    # Egyszeres sortörés
    (r'\begy\s*entert?\b',              '\n'),
    (r'\b1\s*entert?\b',                '\n'),
    (r'\bentert\b',                     '\n'),   # "entert nyomok" → \n
    (r'\benternél\b',                   '\n'),
    (r'\bsortörés\b',                   '\n'),
    (r'\búj\s*sor\b',                   '\n'),
    (r'\bkövetkező\s*sor\b',            '\n'),
    # "pont" csak akkor írásjel, ha nem előzi meg szám és nem követi szó
    (r'(?<!\d)\bpont\b(?!\s+\w{3,})',   '.'),
]

def apply_voice_commands(text: str) -> str:
    """Lecseréli a kimondott parancsszavakat a megfelelő jelekre."""
    for pattern, replacement in VOICE_COMMANDS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # "[szó] nagybetű" → az előző szót nagybetűsíti
    text = re.sub(r'(\S+)\s+nagybetű', lambda m: m.group(1).upper(), text, flags=re.IGNORECASE)

    # Szóközök tisztítása írásjelek körül
    text = re.sub(r'\s+([?!.,;:)])', r'\1', text)
    text = re.sub(r'([(])\s+', r'\1', text)
    text = re.sub(r'  +', ' ', text)

    # Szélső szóközök/tabok levágása, sortörések megőrzése
    return text.strip(' \t')

# --- GPU / CPU DETEKTÁLÁS ---
def detect_device():
    """
    Megvizsgálja, hogy elérhető-e CUDA GPU.
    Visszatérési érték: ("cuda", "float16") vagy ("cpu", "int8")
    """
    try:
        import ctranslate2
        generators = ctranslate2.get_supported_compute_types("cuda")
        if generators:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"

DEVICE, DEFAULT_COMPUTE = detect_device()

# --- HELYI MODELL KÖNYVTÁR (konvertált HF modellek ide kerülnek) ---
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voicetex_models")
os.makedirs(MODELS_DIR, exist_ok=True)

# --- MODELLEK LISTÁJA ---
# típus: "standard"  = OpenAI Whisper (azonnal tölthető)
#        "hu_native" = Magyar, natív faster-whisper formátum (azonnal tölthető)
#        "hu_hf"     = Magyar, HuggingFace Transformers formátum (első betöltésnél auto-konverzió)
MODELS = {
    "tiny (gyors, kisebb pontosság)":       ("tiny",                                          "Legkisebb, leggyorsabb. Magyarul gyengébb.", "standard"),
    "base":                                 ("base",                                          "Általános célú alap modell.", "standard"),
    "small":                                ("small",                                         "Jó egyensúly sebesség és pontosság között.", "standard"),
    "medium":                               ("medium",                                        "Jó minőség, kicsit lassabb.", "standard"),
    "large-v3 (legjobb minőség)":           ("large-v3",                                      "Legjobb általános modell. Magyarul is nagyon jó 16GB GPU-n.", "standard"),
    "─────────────────":                    None,
    "HU: faster-whisper-small-hu ✓":       ("domebacsi/faster-whisper-small-hu",             "Magyar CV adaton tanítva. Natív formátum, azonnal betölthető.", "hu_native"),
    "HU: turbo-finetuned (WER 7.5%) ⭐":   ("sarpba/whisper-hu-large-v3-turbo-finetuned",    "Legjobb magyar modell! WER 7.48%. F32 módban tölt (CT2 kvantálás hibás). Auto-konverzió.", "hu_hf_f32"),
    "HU: whisper-large-v3-hu (WER 11.3%)": ("Trendency/whisper-large-v3-hu",                 "Kiváló magyar modell. WER 11.26. Auto-konverzió első betöltésnél.", "hu_hf"),
    "HU: whisper-base-hungarian_v1":        ("sarpba/whisper-base-hungarian_v1",              "1200 óra magyar hangon tanítva. WER 25.58. Auto-konverzió első betöltésnél.", "hu_hf"),
}

VALID_MODELS = {k: v for k, v in MODELS.items() if v is not None}

# --- LLM MOTOR (llama-cpp-python, Ollama nélkül) ---
LLM_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_models")
LLM_REPO  = "bartowski/Phi-3.5-mini-instruct-GGUF"
LLM_FILE  = "Phi-3.5-mini-instruct-Q4_K_M.gguf"

_llm_engine      = None   # betöltött LlamaCpp példány
_llm_engine_lock = threading.Lock()


def _download_llm_if_needed(status_cb) -> str:
    """Letölti a GGUF modellt ha még nincs meg. Visszaadja a helyi útvonalat."""
    os.makedirs(LLM_DIR, exist_ok=True)
    path = os.path.join(LLM_DIR, LLM_FILE)
    if not os.path.exists(path):
        status_cb(f"LLM letöltése: {LLM_FILE}  (~2.4 GB, csak egyszer)...")
        from huggingface_hub import hf_hub_download
        hf_hub_download(repo_id=LLM_REPO, filename=LLM_FILE, local_dir=LLM_DIR)
    return path


def _load_llm_engine(status_cb=lambda _: None):
    """Betölti a Phi-3.5 modellt. Szálbiztos, csak egyszer fut le."""
    global _llm_engine
    with _llm_engine_lock:
        if _llm_engine is not None:
            return _llm_engine
        if not LLAMA_CPP_OK:
            status_cb("llama-cpp-python nincs telepítve – LLM kikapcsolva")
            return None
        try:
            path = _download_llm_if_needed(status_cb)
            status_cb("LLM betöltése GPU-ra...")
            _llm_engine = LlamaCpp(
                model_path  = path,
                n_gpu_layers= -1 if DEVICE == "cuda" else 0,
                n_ctx       = 512,
                verbose     = False,
            )
        except Exception as e:
            status_cb(f"LLM hiba: {e}")
            return None
    return _llm_engine


def llm_correct(text: str) -> str:
    """
    Phi-3.5 alapú magyar szövegjavítás.
    Ha az engine nem elérhető, az eredeti szöveget adja vissza.
    """
    engine = _llm_engine
    if engine is None:
        return text
    prompt = (
        "<|user|>\n"
        "Javítsd ki ezt a magyar szöveget.\n"
        "1. Töröld a töltelékszavakat (hát, ugye, szóval, tehát).\n"
        "2. Javítsd a helyesírást, tedd ki a hiányzó ékezeteket.\n"
        "3. Csak a tiszta, javított szöveget add vissza, semmi mást.\n\n"
        f"Szöveg: {text}"
        "<|end|>\n<|assistant|>\n"
    )
    try:
        result = engine(prompt, max_tokens=300,
                        stop=["<|end|>", "<|user|>"], echo=False)
        corrected = result["choices"][0]["text"].strip()
        return corrected if corrected else text
    except Exception:
        return text


def get_local_model_path(model_id: str) -> str:
    """Visszaadja a helyi konvertált modell útvonalát."""
    safe_name = model_id.replace("/", "__")
    return os.path.join(MODELS_DIR, safe_name)

def is_model_converted(model_id: str) -> bool:
    """Megvizsgálja, hogy a modell már konvertált-e helyileg."""
    path = get_local_model_path(model_id)
    return os.path.exists(os.path.join(path, "model.bin")) or \
           os.path.exists(os.path.join(path, "model.bin.index"))

def convert_hf_to_ctranslate2(model_id: str, status_cb) -> str:
    """
    HuggingFace Transformers Whisper modellt CTranslate2 formátumba konvertál.
    Visszaadja a helyi elérési utat.
    """
    output_path = get_local_model_path(model_id)
    os.makedirs(output_path, exist_ok=True)

    status_cb(f"Konvertálás: {model_id.split('/')[-1]} → CTranslate2 (első betöltés, ~5-15 perc)...")

    # transformers csomag ellenőrzése / telepítése
    try:
        import transformers
    except ImportError:
        status_cb("Telepítés: transformers csomag...")
        subprocess.run([sys.executable, "-m", "pip", "install", "transformers", "accelerate",
                        "--break-system-packages", "-q"], check=True)

    # Konverzió ct2-transformers-converter segítségével
    result = subprocess.run(
        [sys.executable, "-m", "ctranslate2.converters.transformers",
         "--model", model_id,
         "--output_dir", output_path,
         "--quantization", "float16",
         "--force"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        # Fallback: próbálja a ct2-transformers-converter parancsot közvetlenül
        result2 = subprocess.run(
            ["ct2-transformers-converter",
             "--model", model_id,
             "--output_dir", output_path,
             "--quantization", "float16",
             "--force"],
            capture_output=True, text=True
        )
        if result2.returncode != 0:
            raise RuntimeError(f"Konverziós hiba:\n{result.stderr[-300:]}\n{result2.stderr[-300:]}")

    # --- config.json javítás: num_mels beállítása ---
    # A ct2-transformers-converter néha nem örökli a preprocessor_config.json
    # num_mel_bins értékét. Large-v3 alapú modelleknél 128 kell, nem 80.
    _fix_num_mels(output_path, model_id)

    return output_path


def _fix_num_mels(model_path: str, model_id: str):
    """
    Javítja a konvertált modell config.json-jában a num_mels értékét.
    Large-v3 / turbo alapú modelleknél 128, régebbi modelleknél 80.
    """
    needs_128 = any(x in model_id.lower() for x in ["large-v3", "turbo"])
    expected = 128 if needs_128 else 80

    config_path = os.path.join(model_path, "config.json")
    if not os.path.exists(config_path):
        return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        current = cfg.get("num_mels", None)
        if current != expected:
            cfg["num_mels"] = expected
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
    except Exception:
        pass  # ha nem sikerül, a betöltés majd jelez hibát


def get_input_devices():
    """Visszaadja az összes elérhető mikrofon/bemenet eszközt."""
    devices = sd.query_devices()
    input_devs = []
    for i, d in enumerate(devices):
        if d['max_input_channels'] > 0:
            input_devs.append((i, d['name']))
    return input_devs


class VoicetexApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Voicetex AI - Magyar Offline Speech to Text")
        self.geometry("500x820")

        self.recording = False
        self._audio_lock = threading.Lock()
        self.audio_data  = deque()          # Thread-safe hangpuffer
        self.fs = 16000
        self.whisper_model = None
        self.selected_device_id = None      # None = rendszer alapértelmezett
        self._auto_stop_timer = None        # 5 másodperces auto-stop timer
        self.target_hwnd = None             # Célablak handle (ahová beillesztünk)
        self._vu_level   = 0.0              # Aktuális hangszint 0.0–1.0
        self._vu_peak    = 0.0              # Csúcsjelző (lassan csökken)
        self._vu_bars    = []               # Canvas téglalap ID-k (előre létrehozva)
        self._temp_wav   = os.path.join(tempfile.gettempdir(), "voicetex_temp.wav")

        # --- UI Felépítése ---
        self.label = ctk.CTkLabel(self, text="Voicetex AI", font=("Segoe UI", 24, "bold"))
        self.label.pack(pady=(20, 5))

        self.subtitle = ctk.CTkLabel(self, text="Magyar Offline Diktáló", font=("Segoe UI", 12), text_color="gray")
        self.subtitle.pack(pady=(0, 5))

        # GPU / CPU mód jelzése
        if DEVICE == "cuda":
            hw_text = "⚡ GPU mód (CUDA) – gyors feldolgozás"
            hw_color = "#5BC8F5"
        else:
            hw_text = "⚠ CPU mód – NVIDIA GPU nem található, lassabb lesz (~10-30 mp/mondat)"
            hw_color = "orange"
        ctk.CTkLabel(self, text=hw_text, font=("Segoe UI", 10), text_color=hw_color).pack(pady=(0, 10))

        # --- Mikrofon eszközválasztó ---
        self.device_label = ctk.CTkLabel(self, text="Mikrofon / Hangbemeneti eszköz:")
        self.device_label.pack()

        # Eszközök betöltése
        self.input_devices = get_input_devices()
        device_names = [f"[{i}] {name}" for i, name in self.input_devices]
        if not device_names:
            device_names = ["Nincs elérhető eszköz"]

        # Alapértelmezett: az USB 96-ot keressük név szerint
        default_device = device_names[0]
        for i, name in self.input_devices:
            if "usb" in name.lower() and "96" in name.lower():
                default_device = f"[{i}] {name}"
                break

        self.device_var = ctk.StringVar(value=default_device)
        self.device_menu = ctk.CTkOptionMenu(
            self,
            values=device_names,
            variable=self.device_var,
            command=self.on_device_change,
            width=380,
            dynamic_resizing=False
        )
        self.device_menu.pack(pady=(5, 2))

        self.device_info = ctk.CTkLabel(
            self,
            text="",
            font=("Segoe UI", 10),
            text_color="#5BC8F5"
        )
        self.device_info.pack(pady=(0, 10))
        self.on_device_change(default_device)  # info frissítése induláskor

        # --- Whisper modell választó ---
        self.model_label = ctk.CTkLabel(self, text="Whisper Modell:")
        self.model_label.pack()

        self.model_var = ctk.StringVar(value="HU: faster-whisper-small-hu ✓")
        self.model_menu = ctk.CTkOptionMenu(
            self,
            values=list(MODELS.keys()),
            variable=self.model_var,
            command=self.on_model_change,
            width=320
        )
        self.model_menu.pack(pady=(5, 2))

        self.model_info = ctk.CTkLabel(
            self,
            text="🇭🇺 Magyar (faster-whisper): Magyar Common Voice adaton tanítva. Azonnal betölthető.",
            font=("Segoe UI", 10),
            text_color="#5BC8F5",
            wraplength=420
        )
        self.model_info.pack(pady=(0, 10))

        # --- LLM javító ---
        self.llm_label = ctk.CTkLabel(self, text="Szövegjavító (Phi-3.5, offline):")
        self.llm_label.pack()
        self.llm_var = ctk.StringVar(value="nincs")
        self.llm_menu = ctk.CTkOptionMenu(
            self,
            values=["Phi-3.5 (llama-cpp)", "nincs"],
            variable=self.llm_var,
            width=320
        )
        self.llm_menu.pack(pady=(5, 15))

        # --- Állapot ---
        self.status_label = ctk.CTkLabel(self, text="Állapot: Készen áll", text_color="gray", font=("Segoe UI", 13))
        self.status_label.pack(pady=10)

        # --- Betöltés gomb ---
        self.load_button = ctk.CTkButton(
            self,
            text="AI Modellek Betöltése",
            command=self.load_models,
            width=220,
            height=40,
            font=("Segoe UI", 13, "bold")
        )
        self.load_button.pack(pady=10)

        # --- Eszközök frissítése gomb ---
        self.refresh_button = ctk.CTkButton(
            self,
            text="Eszközök frissítése",
            command=self.refresh_devices,
            width=160,
            height=28,
            fg_color="gray30",
            hover_color="gray40",
            font=("Segoe UI", 11)
        )
        self.refresh_button.pack(pady=(0, 10))

        # --- Sortörés gombok ---
        ctk.CTkLabel(self, text="Sortörés küldése az aktív szövegmezőbe:",
                     font=("Segoe UI", 10), text_color="gray").pack()

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=(4, 0))

        self.newline_btn = ctk.CTkButton(
            btn_frame,
            text="↵  Új sor",
            command=self.insert_line_break,
            width=140, height=34,
            fg_color="#2d5a8e", hover_color="#3a6fa8",
            font=("Segoe UI", 12)
        )
        self.newline_btn.pack(side="left", padx=6)

        self.paragraph_btn = ctk.CTkButton(
            btn_frame,
            text="↵↵  Bekezdés",
            command=self.insert_paragraph,
            width=140, height=34,
            fg_color="#2d5a8e", hover_color="#3a6fa8",
            font=("Segoe UI", 12)
        )
        self.paragraph_btn.pack(side="left", padx=6)

        ctk.CTkLabel(self, text="Scroll Lock = új sor  |  Pause/Break = bekezdés",
                     font=("Segoe UI", 9), text_color="gray40").pack(pady=(2, 0))

        # --- Használati útmutató ---
        info_text = "Win + Ctrl → felvétel indul\n5 mp után auto-leáll  |  tovább tartva: folytatja\nElengedéskor: azonnal feldolgoz és beilleszt"
        self.help_label = ctk.CTkLabel(self, text=info_text, font=("Segoe UI", 10), text_color="gray")
        self.help_label.pack(pady=(12, 5))

        # --- VU Meter ---
        ctk.CTkLabel(self, text="Hangszint  –  kattints és tartsd nyomva a felvételhez:",
                     font=("Segoe UI", 10), text_color="gray").pack(pady=(8, 2))

        self._vu_canvas = tk.Canvas(
            self,
            width=400, height=38,
            bg="#1a1a2e",
            highlightthickness=1,
            highlightbackground="#333355",
            cursor="hand2"
        )
        self._vu_canvas.pack(pady=(0, 10))

        # Egér: nyomva tartás = felvétel, elengedés = feldolgozás
        self._vu_canvas.bind("<ButtonPress-1>",   self._on_vu_press)
        self._vu_canvas.bind("<ButtonRelease-1>", self._on_vu_release)

        # VU sávok előre létrehozása (egyszer, induláskor)
        self._init_vu_bars()
        self._draw_vu_meter()

        threading.Thread(target=self.setup_hotkeys, daemon=True).start()
        threading.Thread(target=self._start_monitor_stream, daemon=True).start()

        # Lebegő felvétel-jelző ablak (mindig látható, fókusztól függetlenül)
        self._build_recording_indicator()

        # Auto-betöltés: 300ms késleltetés hogy az UI teljesen felépüljön
        self._set_ui_loading(True)
        self.after(300, lambda: threading.Thread(target=self._auto_load, daemon=True).start())

    # ------------------------------------------------------------------ #
    #  VU METER                                                           #
    # ------------------------------------------------------------------ #

    # VU METER KONSTANSOK
    _VU_N   = 28
    _VU_W   = 400
    _VU_H   = 38
    _VU_GAP = 3

    def _init_vu_bars(self):
        """Egyszer lefut induláskor: létrehozza a téglalapokat, elmenti az ID-ket."""
        c = self._vu_canvas
        N, W, H, gap = self._VU_N, self._VU_W, self._VU_H, self._VU_GAP
        bar_w = (W - (N + 1) * gap) / N
        self._vu_bars = []
        for i in range(N):
            x1 = gap + i * (bar_w + gap)
            x2 = x1 + bar_w
            rid = c.create_rectangle(x1, 5, x2, H - 5, fill="#0a3320", outline="")
            self._vu_bars.append(rid)

    def _draw_vu_meter(self):
        """VU meter frissítése 50ms-enként – csak a színeket változtatja."""
        c     = self._vu_canvas
        N     = self._VU_N
        level = self._vu_level
        peak  = self._vu_peak

        for i, rid in enumerate(self._vu_bars):
            frac = (i + 1) / N
            if frac <= 0.55:
                on_color, off_color = "#00e676", "#0a3320"
            elif frac <= 0.80:
                on_color, off_color = "#ffea00", "#2e2a00"
            else:
                on_color, off_color = "#ff1744", "#2e0007"

            if frac <= level or abs(frac - peak) < 1 / N:
                color = on_color
            else:
                color = off_color
            c.itemconfig(rid, fill=color)

        # Felvétel közben piros szegély
        if self.recording:
            c.configure(highlightbackground="#ff1744", highlightthickness=2)
        else:
            c.configure(highlightbackground="#333355", highlightthickness=1)

        # Szint és csúcs csillapítása
        self._vu_peak  = max(0.0, self._vu_peak  - 0.015)
        self._vu_level = max(0.0, self._vu_level * 0.75)

        self.after(50, self._draw_vu_meter)

    def _on_vu_press(self, event):
        """Egérgomb lenyomása a VU meteren → felvétel indítása."""
        self.target_hwnd = get_foreground_window()
        self.start_recording()

    def _on_vu_release(self, event):
        """Egérgomb elengedése → felvétel leállítása."""
        if self.recording:
            self._cancel_auto_stop()
            self.recording = False
            self.process_audio()

    def _start_monitor_stream(self):
        """
        Folyamatos, csendes figyelő stream – mindig fut (felvételen kívül is),
        csak a hangszintet frissíti, nem menti az adatot.
        Eszközváltáskor újraindul.
        """
        while True:
            try:
                with sd.InputStream(
                    samplerate=self.fs,
                    channels=1,
                    device=self.selected_device_id,
                    callback=self._monitor_callback,
                    blocksize=1024
                ):
                    # Addig fut amíg az eszköz meg nem változik
                    prev_dev = self.selected_device_id
                    while self.selected_device_id == prev_dev:
                        sd.sleep(200)
            except Exception:
                time.sleep(1)   # Hiba esetén 1mp múlva újrapróbálja

    def _monitor_callback(self, indata, frames, t, status):
        """
        Folyamatos figyelő callback – mindig frissíti a VU szintet.
        Ha éppen felvétel is folyik, az adatot is elmenti.
        """
        # RMS számítás → normalizálás 0–1 tartományba
        rms = float(np.sqrt(np.mean(indata ** 2)))
        # Logaritmikus skála: érzékenyebb a kis hangokra
        level = min(1.0, rms * 12.0)

        self._vu_level = max(self._vu_level * 0.4, level)   # gyors attack
        if level > self._vu_peak:
            self._vu_peak = level

        # Felvétel esetén az adat kerüljön a pufferbe (lock alatt)
        if self.recording:
            with self._audio_lock:
                self.audio_data.append(indata.copy())

    # ------------------------------------------------------------------ #
    #  DIMMING + AUTO-LOAD                                               #
    # ------------------------------------------------------------------ #

    def _set_ui_loading(self, loading: bool):
        """Betöltés közben elhalványítja és letiltja az összes vezérlőt."""
        alpha  = 0.55 if loading else 1.0
        state  = "disabled" if loading else "normal"
        self.after(0, lambda: self.attributes('-alpha', alpha))
        for widget in (self.load_button, self.refresh_button,
                       self.model_menu, self.device_menu, self.llm_menu,
                       self.newline_btn, self.paragraph_btn):
            try:
                self.after(0, lambda w=widget, s=state: w.configure(state=s))
            except Exception:
                pass

    def _auto_load(self):
        """Automatikusan betölti a Whisper modellt és előkészíti a Phi-3.5 LLM-et."""
        self.load_models()
        threading.Thread(target=self._prepare_llm, daemon=True).start()
        self._set_ui_loading(False)

    def _prepare_llm(self):
        """
        Háttérben betölti a Phi-3.5 GGUF modellt (llama-cpp-python).
        Ha még nincs letöltve, automatikusan letölti a HuggingFace-ről (~2.4 GB, csak egyszer).
        Betöltés után átállítja az LLM menüt.
        """
        def _status(msg):
            self.after(0, lambda m=msg: self.status_label.configure(
                text=f"Állapot: {m}", text_color="yellow"
            ))

        engine = _load_llm_engine(_status)
        if engine is not None:
            self.after(0, lambda: self.llm_var.set("Phi-3.5 (llama-cpp)"))

    # ------------------------------------------------------------------ #
    #  LEBEGŐ FELVÉTEL-JELZŐ                                             #
    # ------------------------------------------------------------------ #

    def _build_recording_indicator(self):
        """
        Kis mindig-látható ablak a képernyő jobb alsó sarkában.
        Felvétel közben piros villogással jelzi az állapotot –
        akkor is látszik ha a Voicetex nincs fókuszban.
        """
        ind = tk.Toplevel(self)
        ind.overrideredirect(True)          # nincs title bar / keret
        ind.attributes('-topmost', True)    # mindig legfelül
        ind.attributes('-alpha', 0.0)       # kezdetben láthatatlan
        ind.configure(bg="#1a0000")

        w, h = 170, 38
        sw = ind.winfo_screenwidth()
        sh = ind.winfo_screenheight()
        ind.geometry(f"{w}x{h}+{sw - w - 12}+{sh - h - 52}")

        lbl = tk.Label(ind, text="🎙  FELVÉTEL",
                       font=("Segoe UI", 13, "bold"),
                       fg="#ff4444", bg="#1a0000")
        lbl.pack(fill="both", expand=True)

        self._indicator     = ind
        self._indicator_lbl = lbl
        self._indicator_blink_on = False

    def _show_indicator(self, visible: bool):
        """Megjeleníti vagy elrejti a lebegő jelzőt."""
        if not hasattr(self, '_indicator'):
            return
        if visible:
            self._indicator.attributes('-alpha', 0.92)
            self._blink_indicator()
        else:
            self._indicator.attributes('-alpha', 0.0)

    def _blink_indicator(self):
        """Felvétel közben villogó piros jelző."""
        if not self.recording:
            return
        self._indicator_blink_on = not self._indicator_blink_on
        color = "#ff2222" if self._indicator_blink_on else "#661111"
        self._indicator_lbl.configure(fg=color)
        self.after(400, self._blink_indicator)

    def on_device_change(self, selected):
        """Frissíti a kiválasztott eszköz ID-ját és infó feliratát."""
        try:
            # Kinyerjük a szögletes zárójelből az eszköz indexet: "[3] USB 96..." → 3
            idx = int(selected.split("]")[0].replace("[", "").strip())
            self.selected_device_id = idx
            dev = sd.query_devices(idx)
            ch = dev['max_input_channels']
            sr = int(dev['default_samplerate'])
            self.device_info.configure(
                text=f"✓ Kiválasztva: {dev['name']}  |  {ch} csatorna  |  {sr} Hz alapért.",
                text_color="#5BC8F5"
            )
        except Exception:
            self.selected_device_id = None
            self.device_info.configure(text="⚠ Nem sikerült az eszköz azonosítása", text_color="orange")

    def refresh_devices(self):
        """Újra beolvassa az elérhető hangeszközöket."""
        self.input_devices = get_input_devices()
        device_names = [f"[{i}] {name}" for i, name in self.input_devices]
        if not device_names:
            device_names = ["Nincs elérhető eszköz"]
        self.device_menu.configure(values=device_names)
        self.status_label.configure(text="Eszközlista frissítve.", text_color="gray")

    def on_model_change(self, selected):
        if selected in VALID_MODELS:
            _, desc, mtype = VALID_MODELS[selected]
            type_prefix = {
                "standard":   "🌐 Általános:",
                "hu_native":  "🇭🇺 Magyar (natív):",
                "hu_hf":      "🇭🇺 Magyar (auto-konverzió, float16):",
                "hu_hf_f32":  "🇭🇺 Magyar (auto-konverzió, float32 – kvantálási hiba elkerülése):",
            }.get(mtype, "")
            self.model_info.configure(text=f"{type_prefix} {desc}")
        else:
            self.model_info.configure(text="")

    def load_models(self):
        selected = self.model_var.get()
        if selected not in VALID_MODELS:
            return

        model_id, desc, mtype = VALID_MODELS[selected]

        self.status_label.configure(text="Állapot: Betöltés...", text_color="yellow")
        self.load_button.configure(state="disabled")

        def _load():
            try:
                load_path = model_id  # standard modelleknél az ID elég

                if mtype in ("hu_hf", "hu_hf_f32"):
                    # HuggingFace Transformers modell → auto-konverzió ha szükséges
                    if is_model_converted(model_id):
                        load_path = get_local_model_path(model_id)
                        # Config javítás minden betöltésnél (idempotens, gyors)
                        _fix_num_mels(load_path, model_id)
                        self.status_label.configure(
                            text="Állapot: Helyi modell betöltése...", text_color="yellow"
                        )
                    else:
                        load_path = convert_hf_to_ctranslate2(
                            model_id,
                            lambda msg: self.status_label.configure(text=msg, text_color="yellow")
                        )

                # Betöltés faster-whisper-rel
                # GPU módban: float16 (vagy int8_float16 fallback)
                # CPU módban: int8 (gyorsabb és kisebb memóriaigény CPU-n)
                # hu_hf_f32: float32 kötelező GPU-n (float16 kvantálás mondatcsonkítást okoz)
                if DEVICE == "cpu":
                    self.whisper_model = WhisperModel(load_path, device="cpu", compute_type="int8")
                elif mtype == "hu_hf_f32":
                    self.whisper_model = WhisperModel(load_path, device="cuda", compute_type="float32")
                else:
                    try:
                        self.whisper_model = WhisperModel(load_path, device="cuda", compute_type="float16")
                    except Exception:
                        self.whisper_model = WhisperModel(load_path, device="cuda", compute_type="int8_float16")

                # --- MEL BINS JAVÍTÁS ---
                fix_mel_bins(self.whisper_model)

                self.status_label.configure(text="Állapot: AI Online ✓  (Alt + Space)", text_color="green")

            except Exception as e:
                self.status_label.configure(text=f"Hiba: {str(e)[:100]}", text_color="red")

            self.load_button.configure(state="normal")

        threading.Thread(target=_load).start()

    def insert_line_break(self):
        """Egy Enter küldése az aktív szövegmezőbe (új sor)."""
        hwnd = self.target_hwnd or get_foreground_window()
        restore_focus(hwnd)
        time.sleep(0.15)
        pyautogui.press('enter')

    def insert_paragraph(self):
        """Két Enter küldése az aktív szövegmezőbe (bekezdés)."""
        hwnd = self.target_hwnd or get_foreground_window()
        restore_focus(hwnd)
        time.sleep(0.15)
        pyautogui.press('enter')
        time.sleep(0.06)
        pyautogui.press('enter')

    def setup_hotkeys(self):
        """
        Megbízható globális hotkey detektálás keyboard.hook segítségével.
        Az add_hotkey néha kihagyja ha az app nincs fókuszban – a hook mindig tüzel.
        """
        self._win_held  = False
        self._ctrl_held = False

        keyboard.hook(self._on_keyboard_event, suppress=False)
        # Sortörés billentyűk (ezek egyszerű kombinációk, add_hotkey elég)
        keyboard.add_hotkey('scroll lock', self.insert_line_break, suppress=True)
        keyboard.add_hotkey('pause',       self.insert_paragraph,  suppress=True)

    def _on_keyboard_event(self, event):
        """Minden billentyű eseményt figyel; Alt+Space kombinációt kezeli."""
        name = event.name.lower() if event.name else ''
        dn   = event.event_type == keyboard.KEY_DOWN
        up   = event.event_type == keyboard.KEY_UP

        if name == 'left windows':
            self._win_held = dn
            if up and self.recording:
                self._stop_and_process()

        elif name == 'left ctrl':
            self._ctrl_held = dn
            if dn and self._win_held:
                self.start_recording()
            elif up and self.recording:
                self._stop_and_process()

    def _stop_and_process(self):
        """Leállítja a felvételt és elindítja a feldolgozást."""
        self._cancel_auto_stop()
        self.recording = False
        self._show_indicator(False)
        self.process_audio()

    def _on_key_release(self, event):
        """Megtartjuk kompatibilitás miatt."""
        pass

    def _schedule_auto_stop(self):
        """5 másodperces auto-stop timer indítása."""
        self._cancel_auto_stop()
        self._auto_stop_timer = threading.Timer(5.0, self._check_auto_stop)
        self._auto_stop_timer.daemon = True
        self._auto_stop_timer.start()

    def _cancel_auto_stop(self):
        """Érvényteleníti a folyamatban lévő timert."""
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.cancel()
            self._auto_stop_timer = None

    def _check_auto_stop(self):
        """5 mp lejártakor: ha még nyomva tartja → folytatás; ha elengedte → leállás."""
        if not self.recording:
            return
        if keyboard.is_pressed('left windows') and keyboard.is_pressed('left ctrl'):
            # Még nyomja → új 5 másodperces kör
            self._schedule_auto_stop()
        else:
            # Elengedte → leállítás
            self.recording = False
            self.process_audio()

    def start_recording(self):
        if not self.recording and self.whisper_model:
            # Célablak mentése MIELŐTT bármi fókuszt váltana
            if not self.target_hwnd:
                self.target_hwnd = get_foreground_window()
            self.recording = True
            with self._audio_lock:
                self.audio_data = deque()
            self.status_label.configure(
                text="Állapot: 🎙️ HALLGATÓZOM...  (Win+Ctrl tartva: folytatja)",
                text_color="red"
            )
            self.after(0, lambda: self._show_indicator(True))
            # 5 másodperces auto-stop indítása
            self._schedule_auto_stop()
            # Az adat gyűjtését a _monitor_callback végzi (mindig fut)

    def process_audio(self):
        self.status_label.configure(text="Állapot: AI gondolkodik...", text_color="cyan")

        with self._audio_lock:
            if not self.audio_data:
                return
            audio_np = np.concatenate(list(self.audio_data), axis=0)

        wavfile.write(self._temp_wav, self.fs, audio_np)

        def _ai_task():
            try:
                segments, info = self.whisper_model.transcribe(
                    self._temp_wav,
                    beam_size=5,
                    language="hu",
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                    suppress_tokens=[-1]   # Fix: HuggingFace modellek token-ütközési hibájára
                )
                text = " ".join([s.text for s in segments]).strip()
            except RuntimeError as e:
                if "type must be number" in str(e):
                    # Fallback: beam_size=1 és suppress_tokens nélkül
                    try:
                        segments, info = self.whisper_model.transcribe(
                            self._temp_wav,
                            beam_size=1,
                            language="hu",
                            vad_filter=False
                        )
                        text = " ".join([s.text for s in segments]).strip()
                    except Exception as e2:
                        self.status_label.configure(
                            text=f"Átírási hiba: {str(e2)[:70]}",
                            text_color="red"
                        )
                        return
                else:
                    self.status_label.configure(
                        text=f"Hiba: {str(e)[:70]}",
                        text_color="red"
                    )
                    return

            if not text:
                self.status_label.configure(text="Állapot: Nem hallottam semmit", text_color="gray")
                return

            # 1. lépés: szabályalapú hangparancsok alkalmazása (LLM nélkül is működik)
            text = apply_voice_commands(text)

            # 2. lépés: LLM javítás (ha be van kapcsolva)
            if self.llm_var.get() != "nincs":
                text = llm_correct(text)

            paste_text_to_window(self.target_hwnd, text)
            self.target_hwnd = None     # reset, következő felvételnél újra olvassuk

            self.after(0, lambda: self._show_indicator(False))
            self.status_label.configure(text="Állapot: ✅ Beillesztve!", text_color="green")

        threading.Thread(target=_ai_task).start()


if __name__ == "__main__":
    app = VoicetexApp()
    app.mainloop()
