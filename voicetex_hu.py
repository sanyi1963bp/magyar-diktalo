import customtkinter as ctk
import tkinter as tk
import threading
import keyboard
import sounddevice as sd
import numpy as np
from scipy.io import wavfile
from faster_whisper import WhisperModel
import ollama
import pyautogui
import pyperclip
import pystray
from PIL import Image, ImageDraw
from datetime import datetime
import os
import re
import subprocess
import sys
import json
import ctypes
import time

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

# --- TÁLCA IKON RAJZOLÁS ---
def _make_tray_image(color: str = "#4a9eff") -> Image.Image:
    """
    64×64 px tálca ikont készít: sötét háttér + színes kör.
    Szín: kék=készen áll, piros=felvétel, szürke=nincs modell.
    """
    def hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    img  = Image.new("RGB", (64, 64), (28, 28, 46))
    draw = ImageDraw.Draw(img)
    draw.ellipse([6, 6, 58, 58], fill=hex_to_rgb(color))
    # Kis mikrofon szimbólum: fehér téglalap + félkör
    draw.rectangle([26, 18, 38, 38], fill=(255, 255, 255))
    draw.ellipse([22, 28, 42, 46], fill=(255, 255, 255))
    draw.rectangle([26, 44, 38, 52], fill=(255, 255, 255))
    return img


# --- NAPLÓZÁS ---
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "napló")

def save_to_log(text: str, start_dt: datetime):
    """
    Elmenti a felismert szöveget napi naplófájlba.
    Fájlnév: whisper_napló_2026_05_17.txt
    Bejegyzés formátuma: [08:32:15] szöveg...
    Ha a felvétel éjfélen nyúlik át, MINDKÉT nap fájljába beírja a teljes bejegyzést.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    end_dt = datetime.now()

    start_str = start_dt.strftime("%H:%M:%S")
    end_str   = end_dt.strftime("%H:%M:%S")

    # Időbélyeg: ha ugyanaz a nap → [08:32:15], ha átnyúl → [23:58:01 → 00:01:33]
    if start_dt.date() == end_dt.date():
        stamp = f"[{start_str}]"
    else:
        stamp = f"[{start_str} → {end_str}]"

    entry = f"{stamp}\n{text}\n\n"

    # Kezdő nap fájlja
    fname_start = os.path.join(LOG_DIR, f"whisper_napló_{start_dt.strftime('%Y_%m_%d')}.txt")
    with open(fname_start, "a", encoding="utf-8") as f:
        f.write(entry)

    # Ha éjfélen nyúlna át: záró nap fájlja is megkapja a teljes bejegyzést
    if start_dt.date() != end_dt.date():
        fname_end = os.path.join(LOG_DIR, f"whisper_napló_{end_dt.strftime('%Y_%m_%d')}.txt")
        with open(fname_end, "a", encoding="utf-8") as f:
            f.write(entry)


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

    # "nagybetű" → az előtte lévő szót nagybetűsíti, majd törli a parancsszót
    def capitalize_prev(m):
        before = m.string[:m.start()].rstrip()
        after  = m.string[m.end():]
        # Az utolsó szót nagybetűsítjük
        words = before.rsplit(' ', 1)
        if len(words) == 2:
            return words[0] + ' ' + words[1].upper()
        return before.upper()

    # Először megkeressük a "nagybetű" szót és az előtte lévő szót együtt
    text = re.sub(r'(\S+)\s+nagybetű', lambda m: m.group(1).upper(), text, flags=re.IGNORECASE)

    # Felesleges szóközök tisztítása írásjelek előtt
    text = re.sub(r'\s+([?!.,;:)])', r'\1', text)
    text = re.sub(r'([(])\s+', r'\1', text)
    # Dupla szóközök eltávolítása
    text = re.sub(r'  +', ' ', text)

    # Csak szóközöket és tabulátorokat vágunk le a szélekről,
    # a sortöréseket (pl. "bekezdés" a szöveg elején/végén) megőrizzük!
    return text.strip(' \t')

# --- MEL FILTERBANK JAVÍTÁS (standalone, faster-whisper verziófüggetlen) ---
def _compute_mel_filters(n_mels: int, n_fft: int = 400, sr: int = 16000) -> np.ndarray:
    """
    Kiszámolja a Whisper-kompatibilis mel szűrőmátrixot.
    Visszatérési alak: (n_mels, n_fft//2 + 1)
    Ugyanaz a képlet mint az openai/whisper-ben.
    """
    f_min, f_max = 0.0, 8000.0

    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)          # (n_fft//2+1,)
    mel_pts = np.linspace(hz_to_mel(f_min), hz_to_mel(f_max), n_mels + 2)
    hz_pts  = mel_to_hz(mel_pts)

    filters = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = hz_pts[i], hz_pts[i + 1], hz_pts[i + 2]
        up   = np.maximum(0.0, (freqs - lo)  / (mid - lo))
        down = np.maximum(0.0, (hi - freqs)  / (hi - mid))
        filters[i] = np.minimum(up, down)

    # Energia-normalizálás (mint a whisper-ben)
    enorm = 2.0 / (hz_pts[2:n_mels + 2] - hz_pts[:n_mels])
    filters *= enorm[:, np.newaxis]
    return filters


def fix_mel_bins(whisper_model) -> bool:
    """
    Megvizsgálja, hogy a modell és a feature extractor mel-száma egyezik-e.
    Ha nem, kijavítja. Visszaad True-t ha javítás történt, False-t ha nem kellett.
    Sosem dob kivételt – ha nem sikerül, False-t ad.
    """
    try:
        model_mels = whisper_model.model.n_mels
    except Exception:
        return False   # n_mels nem olvasható, hagyjuk

    try:
        extractor_mels = whisper_model.feature_extractor.mel_filters.shape[0]
    except Exception:
        return False

    if model_mels == extractor_mels:
        return False   # Nem kell javítás

    # --- 1. módszer: FeatureExtractor osztály cseréje ---
    for module in ["faster_whisper.feature_extractor", "faster_whisper.audio", "faster_whisper"]:
        try:
            import importlib
            mod = importlib.import_module(module)
            FE  = getattr(mod, "FeatureExtractor")
            # Próbáljuk az összes lehetséges konstruktor-szignatúrát
            for kwargs in [
                {"device": "cuda", "num_mel_bins": model_mels},
                {"num_mel_bins": model_mels},
                {"n_mels": model_mels},
                {},
            ]:
                try:
                    new_fe = FE(**kwargs)
                    # Ha az alap n_mels nem stimmel, patch-eljük a mel_filters mátrixot
                    if new_fe.mel_filters.shape[0] != model_mels:
                        new_fe.mel_filters = _compute_mel_filters(model_mels)
                    whisper_model.feature_extractor = new_fe
                    return True
                except Exception:
                    continue
        except Exception:
            continue

    # --- 2. módszer: Közvetlenül patch-eljük a mel_filters mátrixot ---
    try:
        whisper_model.feature_extractor.mel_filters = _compute_mel_filters(model_mels)
        return True
    except Exception:
        pass

    return False   # Egyik módszer sem működött


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
        self.audio_data = []
        self.fs = 16000
        self.whisper_model = None
        self.selected_device_id = None      # None = rendszer alapértelmezett
        self._auto_stop_timer = None        # 5 másodperces auto-stop timer
        self.target_hwnd = None             # Célablak handle (ahová beillesztünk)
        self._vu_level   = 0.0              # Aktuális hangszint 0.0–1.0
        self._vu_peak    = 0.0              # Csúcsjelző (lassan csökken)
        self._monitor_stream = None         # Folyamatos figyelő stream (nem felvétel)
        self._recording_start_time = None   # Felvétel kezdete (naplózáshoz)
        self._tray_icon  = None             # Tálca ikon

        # X gomb: elrejt a tálcára, nem zár be
        self.protocol("WM_DELETE_WINDOW", self._hide_window)

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
        self.llm_label = ctk.CTkLabel(self, text="Szövegjavító (Llama/Ollama):")
        self.llm_label.pack()
        self.llm_var = ctk.StringVar(value="nincs")
        self.llm_menu = ctk.CTkOptionMenu(
            self,
            values=["llama3", "mistral", "phi3", "nincs"],
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
        info_text = "Alt + Space → felvétel indul\n5 mp után auto-leáll  |  tovább tartva: folytatja\nElengedéskor: azonnal feldolgoz és beilleszt"
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

        # VU animáció indítása
        self._draw_vu_meter()

        threading.Thread(target=self.setup_hotkeys, daemon=True).start()
        threading.Thread(target=self._start_monitor_stream, daemon=True).start()
        threading.Thread(target=self._setup_tray, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  VU METER                                                           #
    # ------------------------------------------------------------------ #

    def _draw_vu_meter(self):
        """VU meter kirajzolása és folyamatos frissítése (50ms-enként)."""
        c = self._vu_canvas
        c.delete("all")

        W, H = 400, 38
        N = 28          # sávok száma
        gap = 3
        bar_w = (W - (N + 1) * gap) / N
        level = self._vu_level          # 0.0–1.0
        peak  = self._vu_peak           # csúcs

        for i in range(N):
            x1 = gap + i * (bar_w + gap)
            x2 = x1 + bar_w
            frac = (i + 1) / N          # hány sáv van bekapcsolva ennél a szintnél

            # Szín logika: zöld → sárga → piros
            if frac <= 0.55:
                on_color  = "#00e676"   # zöld
                off_color = "#0a3320"
            elif frac <= 0.80:
                on_color  = "#ffea00"   # sárga
                off_color = "#2e2a00"
            else:
                on_color  = "#ff1744"   # piros
                off_color = "#2e0007"

            lit = frac <= level
            color = on_color if lit else off_color

            # Csúcsjelző: a csúcs sávot mindig kiemeljük
            if abs(frac - peak) < 1 / N:
                color = on_color

            y1, y2 = 5, H - 5
            c.create_rectangle(x1, y1, x2, y2, fill=color, outline="")

        # Felvétel közben: villogó szegély
        if self.recording:
            c.configure(highlightbackground="#ff1744", highlightthickness=2)
        else:
            c.configure(highlightbackground="#333355", highlightthickness=1)

        # Csúcs lassú csökkenése
        self._vu_peak = max(0.0, self._vu_peak - 0.015)
        # Szint gyors csillapítása (ha nincs új adat)
        self._vu_level = max(0.0, self._vu_level * 0.75)

        # Következő frame 50ms múlva
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

        # Felvétel esetén az adat is kerüljön a pufferbe
        if self.recording:
            self.audio_data.append(indata.copy())

    # ------------------------------------------------------------------ #
    #  TÁLCA (SYSTEM TRAY)                                               #
    # ------------------------------------------------------------------ #

    def _setup_tray(self):
        """Létrehozza és elindítja a tálca ikont."""
        menu = pystray.Menu(
            pystray.MenuItem("Voicetex AI", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Megjelenítés", lambda icon, item: self._show_window()),
            pystray.MenuItem("Elrejtés",     lambda icon, item: self._hide_window()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Kilépés",      lambda icon, item: self._quit_app()),
        )
        self._tray_icon = pystray.Icon(
            name    = "voicetex",
            icon    = _make_tray_image("#555577"),   # szürke = nincs modell
            title   = "Voicetex AI – modell nincs betöltve",
            menu    = menu
        )
        # Dupla kattintás: ablak előhozása
        self._tray_icon.default_action = lambda icon, item: self._show_window()
        self._tray_icon.run_detached()

    def _update_tray(self, color: str, tooltip: str):
        """Frissíti a tálca ikon színét és tooltip szövegét."""
        if self._tray_icon:
            self._tray_icon.icon  = _make_tray_image(color)
            self._tray_icon.title = tooltip

    def _show_window(self):
        """Előhozza az ablakot a tálcáról."""
        self.after(0, self.deiconify)
        self.after(0, self.lift)
        self.after(0, self.focus_force)

    def _hide_window(self):
        """Elrejti az ablakot (tálcán marad)."""
        self.withdraw()

    def _quit_app(self):
        """Teljesen kilép az alkalmazásból."""
        if self._tray_icon:
            self._tray_icon.stop()
        self.after(0, self.destroy)

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
                self._update_tray("#00c853", "Voicetex AI – Készen áll  (Alt+Space)")

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
        # Alt + Space egyszerre → felvétel indítása
        keyboard.add_hotkey('alt+space', self.start_recording, suppress=True)
        # Bármelyik felengedésekor → leállítás (ha éppen felvesz)
        keyboard.on_release_key('alt', self._on_key_release)
        keyboard.on_release_key('space', self._on_key_release)
        # Sortörés billentyűk — globálisan, bármely alkalmazásban
        keyboard.add_hotkey('scroll lock', self.insert_line_break,  suppress=True)
        keyboard.add_hotkey('pause',       self.insert_paragraph,   suppress=True)

    def _on_key_release(self, event):
        """Ha elengedik valamelyik gombot, leállítja a felvételt."""
        if self.recording:
            self._cancel_auto_stop()
            self.recording = False
            self.process_audio()

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
        if keyboard.is_pressed('alt') and keyboard.is_pressed('space'):
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
            self.audio_data = []
            self._recording_start_time = datetime.now()
            self.status_label.configure(
                text="Állapot: 🎙️ HALLGATÓZOM...  (5mp / tartva: folytatja)",
                text_color="red"
            )
            self._update_tray("#ff1744", "Voicetex AI – 🎙️ Felvétel...")
            # 5 másodperces auto-stop indítása
            self._schedule_auto_stop()
            # Az adat gyűjtését a _monitor_callback végzi (mindig fut)

    def stop_recording_if_active(self, event):
        """Megtartjuk kompatibilitás miatt, de most már a _on_key_release kezeli."""
        pass

    def process_audio(self):
        self.status_label.configure(text="Állapot: AI gondolkodik...", text_color="cyan")

        if not self.audio_data:
            return

        audio_np = np.concatenate(self.audio_data, axis=0)
        wavfile.write("temp.wav", self.fs, audio_np)

        def _ai_task():
            try:
                segments, info = self.whisper_model.transcribe(
                    "temp.wav",
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
                            "temp.wav",
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

            # 2. lépés: LLM javítás (ha be van kapcsolva) — a parancsokat is kezeli
            if self.llm_var.get() != "nincs":
                try:
                    response = ollama.generate(
                        model=self.llm_var.get(),
                        prompt=(
                            f"Javítsd ki ezt a magyar szöveget. Feladataid:\n"
                            f"1. Töröld a töltelékszavakat (hát, ugye, szóval, tehát).\n"
                            f"2. Javítsd a helyesírást, tedd ki a hiányzó ékezeteket.\n"
                            f"3. Ha még maradt benne hangparancs szóval kimondva, alakítsd át: "
                            f"'pont' (mondat végén) → '.', 'kérdőjel' → '?', 'felkiáltójel' → '!', "
                            f"'vessző' → ',', 'nagybetű' → az előző szót nagybetűvel, "
                            f"'új sor' → sortörés, 'bekezdés' → bekezdés.\n"
                            f"4. Csak a tiszta, kész szöveget add vissza, semmi mást.\n\n"
                            f"Szöveg: {text}"
                        )
                    )
                    text = response['response'].strip()
                except Exception:
                    pass

            paste_text_to_window(self.target_hwnd, text)
            self.target_hwnd = None     # reset, következő felvételnél újra olvassuk

            # Naplózás
            if self._recording_start_time:
                save_to_log(text, self._recording_start_time)
                self._recording_start_time = None

            self._update_tray("#00c853", "Voicetex AI – Készen áll  (Alt+Space)")
            self.status_label.configure(text="Állapot: ✅ Beillesztve!", text_color="green")

        threading.Thread(target=_ai_task).start()


if __name__ == "__main__":
    app = VoicetexApp()
    app.mainloop()
