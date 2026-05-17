import customtkinter as ctk
import threading
import keyboard
import sounddevice as sd
import numpy as np
from scipy.io import wavfile
from faster_whisper import WhisperModel
import ollama
import pyautogui
import pyperclip
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
        self.selected_device_id = None  # None = rendszer alapértelmezett
        self._auto_stop_timer = None    # 5 másodperces auto-stop timer
        self.target_hwnd = None         # Célablak handle (ahová beillesztünk)

        # --- UI Felépítése ---
        self.label = ctk.CTkLabel(self, text="Voicetex AI", font=("Segoe UI", 24, "bold"))
        self.label.pack(pady=(20, 5))

        self.subtitle = ctk.CTkLabel(self, text="Magyar Offline Diktáló", font=("Segoe UI", 12), text_color="gray")
        self.subtitle.pack(pady=(0, 15))

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

        threading.Thread(target=self.setup_hotkeys, daemon=True).start()

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
                # hu_hf_f32: float32 kötelező (float16 kvantálás mondatcsonkítást okoz ennél a modellnél)
                if mtype == "hu_hf_f32":
                    self.whisper_model = WhisperModel(load_path, device="cuda", compute_type="float32")
                else:
                    try:
                        self.whisper_model = WhisperModel(load_path, device="cuda", compute_type="float16")
                    except Exception:
                        self.whisper_model = WhisperModel(load_path, device="cuda", compute_type="int8_float16")

                # --- MEL BINS JAVÍTÁS ---
                # A faster-whisper néha 80 mel-bines feature extractort hoz létre
                # akkor is, ha a modell 128-at vár (large-v3 alapú modellek).
                # A modell saját n_mels értékét olvassuk ki és ha eltér, lecseréljük
                # a feature extractort a helyes értékkel.
                try:
                    from faster_whisper.feature_extractor import FeatureExtractor as FE
                    model_mels = self.whisper_model.model.n_mels
                    extractor_mels = self.whisper_model.feature_extractor.mel_filters.shape[0]
                    if model_mels != extractor_mels:
                        self.whisper_model.feature_extractor = FE(
                            device="cuda",
                            num_mel_bins=model_mels
                        )
                except Exception:
                    pass  # Ha valami miatt nem sikerül, a transcribe majd jelez

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
            # Elmentjük a célablakot MIELŐTT bármi fókuszt váltana
            self.target_hwnd = get_foreground_window()
            self.recording = True
            self.audio_data = []
            self.status_label.configure(text="Állapot: 🎙️ HALLGATÓZOM...  (5mp / Alt+Space tartva: folytatja)", text_color="red")

            # 5 másodperces auto-stop indítása
            self._schedule_auto_stop()

            def record():
                try:
                    with sd.InputStream(
                        samplerate=self.fs,
                        channels=1,
                        device=self.selected_device_id,
                        callback=self.callback
                    ):
                        while self.recording:
                            sd.sleep(100)
                except Exception as e:
                    self._cancel_auto_stop()
                    self.recording = False
                    self.status_label.configure(
                        text=f"Mikrofon hiba: {str(e)[:60]}",
                        text_color="red"
                    )

            threading.Thread(target=record).start()

    def callback(self, indata, frames, time, status):
        if self.recording:
            self.audio_data.append(indata.copy())

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

            self.status_label.configure(text="Állapot: ✅ Beillesztve!", text_color="green")

        threading.Thread(target=_ai_task).start()


if __name__ == "__main__":
    app = VoicetexApp()
    app.mainloop()
