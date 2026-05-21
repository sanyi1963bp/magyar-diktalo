"""
Voicetex AI – Egyszerűsített verzió
  • Egyetlen (elsődleges) hangforrás
  • Legjobb magyar modell: sarpba/whisper-hu-large-v3-turbo-finetuned
  • Induláskor automatikus betöltés
  • Nincs LLM szövegjavító
"""

import customtkinter as ctk
import tkinter as tk
import threading
from collections import deque
import keyboard
import sounddevice as sd
import numpy as np
from scipy.io import wavfile
from faster_whisper import WhisperModel
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

# ------------------------------------------------------------------ #
#  WINDOWS FÓKUSZ-KEZELÉS                                            #
# ------------------------------------------------------------------ #
_user32 = ctypes.windll.user32

def get_foreground_window():
    return _user32.GetForegroundWindow()

def restore_focus(hwnd):
    if not hwnd:
        return
    try:
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        tgt = _user32.GetWindowThreadProcessId(hwnd, None)
        att = _user32.AttachThreadInput(cur, tgt, True)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        if att:
            _user32.AttachThreadInput(cur, tgt, False)
    except Exception:
        pass

def paste_text_to_window(hwnd, text):
    restore_focus(hwnd)
    time.sleep(0.3)
    for i, line in enumerate(text.split('\n')):
        if line.strip():
            pyperclip.copy(line)
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.08)
        if i < text.count('\n'):
            pyautogui.press('enter')
            time.sleep(0.08)

# ------------------------------------------------------------------ #
#  MEL FILTERBANK JAVÍTÁS                                            #
# ------------------------------------------------------------------ #
def _compute_mel_filters(n_mels: int, n_fft: int = 400, sr: int = 16000) -> np.ndarray:
    def hz_to_mel(f): return 2595.0 * np.log10(1.0 + f / 700.0)
    def mel_to_hz(m): return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    freqs   = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    mel_pts = np.linspace(hz_to_mel(0.0), hz_to_mel(8000.0), n_mels + 2)
    hz_pts  = mel_to_hz(mel_pts)
    filters = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = hz_pts[i], hz_pts[i+1], hz_pts[i+2]
        filters[i] = np.minimum(np.maximum(0.0, (freqs-lo)/(mid-lo)),
                                np.maximum(0.0, (hi-freqs)/(hi-mid)))
    filters *= (2.0 / (hz_pts[2:n_mels+2] - hz_pts[:n_mels]))[:, np.newaxis]
    return filters

def fix_mel_bins(model) -> None:
    try:
        m = model.model.n_mels
        e = model.feature_extractor.mel_filters.shape[0]
        if m != e:
            model.feature_extractor.mel_filters = _compute_mel_filters(m)
    except Exception as ex:
        print(f"[fix_mel_bins] {ex}")

# ------------------------------------------------------------------ #
#  GPU / CPU DETEKTÁLÁS                                              #
# ------------------------------------------------------------------ #
def detect_device():
    try:
        import ctranslate2
        if ctranslate2.get_supported_compute_types("cuda"):
            return "cuda"
    except Exception:
        pass
    return "cpu"

DEVICE = detect_device()

# ------------------------------------------------------------------ #
#  MODELL KONFIGURÁCIÓ                                               #
# ------------------------------------------------------------------ #
MODEL_ID   = "sarpba/whisper-hu-large-v3-turbo-finetuned"
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voicetex_models")
os.makedirs(MODELS_DIR, exist_ok=True)

def get_local_model_path() -> str:
    return os.path.join(MODELS_DIR, MODEL_ID.replace("/", "__"))

def is_model_converted() -> bool:
    path = get_local_model_path()
    return os.path.exists(os.path.join(path, "model.bin"))

def fix_config_mels(model_path: str):
    """config.json-ban num_mels = 128 (large-v3/turbo alapú modell)."""
    cfg_path = os.path.join(model_path, "config.json")
    if not os.path.exists(cfg_path):
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("num_mels") != 128:
            cfg["num_mels"] = 128
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
    except Exception:
        pass

def convert_model(status_cb) -> str:
    """HuggingFace → CTranslate2 konverzió. Csak egyszer fut le."""
    out = get_local_model_path()
    os.makedirs(out, exist_ok=True)
    status_cb("Modell letöltése és konvertálása (csak egyszer, ~5–15 perc)...")
    try:
        import transformers  # noqa
    except ImportError:
        status_cb("Telepítés: transformers...")
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "transformers", "accelerate", "-q"], check=True)
    for cmd in [
        [sys.executable, "-m", "ctranslate2.converters.transformers",
         "--model", MODEL_ID, "--output_dir", out, "--quantization", "float32", "--force"],
        ["ct2-transformers-converter",
         "--model", MODEL_ID, "--output_dir", out, "--quantization", "float32", "--force"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            fix_config_mels(out)
            return out
    raise RuntimeError("Konverzió sikertelen. Ellenőrizd a netet és a ct2-transformers-converter telepítését.")

# ------------------------------------------------------------------ #
#  HANGPARANCSOK                                                     #
# ------------------------------------------------------------------ #
VOICE_COMMANDS = [
    (r'\bkérdőjel\b',              '?'),
    (r'\bfelkiáltójel\b',          '!'),
    (r'\bpontosvessző\b',          ';'),
    (r'\bkettőspont\b',            ':'),
    (r'\bvessző\b',                ','),
    (r'\bkötőjel\b',               '-'),
    (r'\bgondolatjel\b',           '—'),
    (r'\bmacskakörm[öo]k\b',       '"'),
    (r'\bzárójel\s*nyit\b',        '('),
    (r'\bzárójel\s*zár\b',         ')'),
    (r'\bkét\s*entert?\b',         '\n\n'),
    (r'\bkettő\s*entert?\b',       '\n\n'),
    (r'\b2\s*entert?\b',           '\n\n'),
    (r'\búj\s*bekezdés\b',         '\n\n'),
    (r'\bbekezdés\b',              '\n\n'),
    (r'\begy\s*entert?\b',         '\n'),
    (r'\b1\s*entert?\b',           '\n'),
    (r'\bentert\b',                '\n'),
    (r'\benternél\b',              '\n'),
    (r'\bsortörés\b',              '\n'),
    (r'\búj\s*sor\b',              '\n'),
    (r'\bkövetkező\s*sor\b',       '\n'),
    (r'(?<!\d)\bpont\b(?!\s+\w{3,})', '.'),
]

def apply_voice_commands(text: str) -> str:
    for pattern, repl in VOICE_COMMANDS:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    text = re.sub(r'(\S+)\s+nagybetű', lambda m: m.group(1).upper(), text, flags=re.IGNORECASE)
    text = re.sub(r'\s+([?!.,;:)])', r'\1', text)
    text = re.sub(r'([(])\s+', r'\1', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip(' \t')

# ------------------------------------------------------------------ #
#  ALKALMAZÁS                                                        #
# ------------------------------------------------------------------ #
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class VoicetexApp(ctk.CTk):
    _VU_N, _VU_W, _VU_H, _VU_GAP = 28, 400, 38, 3

    def __init__(self):
        super().__init__()
        self.title("Voicetex AI")
        self.geometry("460x340")
        self.resizable(False, False)

        self.recording    = False
        self._audio_lock  = threading.Lock()
        self.audio_data   = deque()
        self.fs           = 16000
        self.whisper_model = None
        self._auto_stop_timer = None
        self.target_hwnd  = None
        self._vu_level    = 0.0
        self._vu_peak     = 0.0
        self._vu_bars     = []
        self._temp_wav    = os.path.join(tempfile.gettempdir(), "voicetex_temp.wav")

        self._build_ui()
        self._init_vu_bars()
        self._draw_vu_meter()

        threading.Thread(target=self._setup_hotkeys, daemon=True).start()
        threading.Thread(target=self._start_monitor_stream, daemon=True).start()
        threading.Thread(target=self._auto_load_model, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  UI                                                                 #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        ctk.CTkLabel(self, text="Voicetex AI",
                     font=("Segoe UI", 22, "bold")).pack(pady=(18, 2))
        ctk.CTkLabel(self, text="Magyar Offline Diktáló",
                     font=("Segoe UI", 11), text_color="gray").pack()

        hw = ("⚡ GPU (CUDA)" if DEVICE == "cuda" else "⚠ CPU mód – lassabb")
        hw_col = "#5BC8F5" if DEVICE == "cuda" else "orange"
        ctk.CTkLabel(self, text=hw, font=("Segoe UI", 10),
                     text_color=hw_col).pack(pady=(2, 10))

        self.status_label = ctk.CTkLabel(
            self, text="Állapot: modell betöltése...",
            text_color="yellow", font=("Segoe UI", 13))
        self.status_label.pack(pady=(0, 6))

        # Sortörés gombok
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=(0, 4))
        for txt, cmd in [("↵  Új sor", self.insert_line_break),
                         ("↵↵  Bekezdés", self.insert_paragraph)]:
            ctk.CTkButton(btn_frame, text=txt, command=cmd,
                          width=140, height=32,
                          fg_color="#2d5a8e", hover_color="#3a6fa8",
                          font=("Segoe UI", 12)).pack(side="left", padx=5)

        ctk.CTkLabel(self, text="Scroll Lock = új sor  |  Pause/Break = bekezdés",
                     font=("Segoe UI", 9), text_color="gray40").pack()

        ctk.CTkLabel(self, text="Alt+Space (tartva) = felvétel  |  VU meterre kattintva is",
                     font=("Segoe UI", 9), text_color="gray40").pack(pady=(2, 6))

        # VU meter
        self._vu_canvas = tk.Canvas(
            self, width=400, height=38, bg="#1a1a2e",
            highlightthickness=1, highlightbackground="#333355",
            cursor="hand2")
        self._vu_canvas.pack(pady=(0, 10))
        self._vu_canvas.bind("<ButtonPress-1>",   self._on_vu_press)
        self._vu_canvas.bind("<ButtonRelease-1>", self._on_vu_release)

    # ------------------------------------------------------------------ #
    #  VU METER                                                           #
    # ------------------------------------------------------------------ #
    def _init_vu_bars(self):
        c = self._vu_canvas
        N, W, H, gap = self._VU_N, self._VU_W, self._VU_H, self._VU_GAP
        bar_w = (W - (N + 1) * gap) / N
        for i in range(N):
            x1 = gap + i * (bar_w + gap)
            self._vu_bars.append(
                c.create_rectangle(x1, 5, x1 + bar_w, H - 5,
                                   fill="#0a3320", outline=""))

    def _draw_vu_meter(self):
        N, level, peak = self._VU_N, self._vu_level, self._vu_peak
        for i, rid in enumerate(self._vu_bars):
            frac = (i + 1) / N
            if   frac <= 0.55: on_c, off_c = "#00e676", "#0a3320"
            elif frac <= 0.80: on_c, off_c = "#ffea00", "#2e2a00"
            else:              on_c, off_c = "#ff1744", "#2e0007"
            lit = frac <= level or abs(frac - peak) < 1 / N
            self._vu_canvas.itemconfig(rid, fill=on_c if lit else off_c)

        border = "#ff1744" if self.recording else "#333355"
        width  = 2        if self.recording else 1
        self._vu_canvas.configure(highlightbackground=border,
                                   highlightthickness=width)
        self._vu_peak  = max(0.0, self._vu_peak  - 0.015)
        self._vu_level = max(0.0, self._vu_level * 0.75)
        self.after(50, self._draw_vu_meter)

    def _on_vu_press(self, _):
        self.target_hwnd = get_foreground_window()
        self._start_recording()

    def _on_vu_release(self, _):
        if self.recording:
            self._cancel_auto_stop()
            self.recording = False
            self._process_audio()

    # ------------------------------------------------------------------ #
    #  MIKROFON FIGYELÉS                                                  #
    # ------------------------------------------------------------------ #
    def _start_monitor_stream(self):
        while True:
            try:
                with sd.InputStream(samplerate=self.fs, channels=1,
                                    device=3,
                                    callback=self._monitor_callback,
                                    blocksize=1024):
                    while True:
                        sd.sleep(500)
            except Exception:
                time.sleep(1)

    def _monitor_callback(self, indata, frames, t, status):
        rms   = float(np.sqrt(np.mean(indata ** 2)))
        level = min(1.0, rms * 12.0)
        self._vu_level = max(self._vu_level * 0.4, level)
        if level > self._vu_peak:
            self._vu_peak = level
        if self.recording:
            with self._audio_lock:
                self.audio_data.append(indata.copy())

    # ------------------------------------------------------------------ #
    #  MODELL BETÖLTÉS                                                    #
    # ------------------------------------------------------------------ #
    def _auto_load_model(self):
        try:
            if is_model_converted():
                path = get_local_model_path()
                fix_config_mels(path)
                self._set_status("Helyi modell betöltése...", "yellow")
            else:
                path = convert_model(lambda msg: self._set_status(msg, "yellow"))

            compute = "float32" if DEVICE == "cuda" else "int8"
            self.whisper_model = WhisperModel(path, device=DEVICE,
                                              compute_type=compute)
            fix_mel_bins(self.whisper_model)
            self._set_status("AI Online ✓  (Alt+Space)", "green")
        except Exception as e:
            self._set_status(f"Hiba: {str(e)[:90]}", "red")

    def _set_status(self, text, color):
        self.after(0, lambda: self.status_label.configure(
            text=f"Állapot: {text}", text_color=color))

    # ------------------------------------------------------------------ #
    #  HOTKEYS + SORTÖRÉS                                                 #
    # ------------------------------------------------------------------ #
    def _setup_hotkeys(self):
        keyboard.add_hotkey('alt+space', self._start_recording, suppress=True)
        keyboard.on_release_key('alt',   self._on_key_release)
        keyboard.on_release_key('space', self._on_key_release)
        keyboard.add_hotkey('scroll lock', self.insert_line_break, suppress=True)
        keyboard.add_hotkey('pause',       self.insert_paragraph,  suppress=True)

    def _on_key_release(self, _):
        if self.recording:
            self._cancel_auto_stop()
            self.recording = False
            self._process_audio()

    def insert_line_break(self):
        restore_focus(self.target_hwnd or get_foreground_window())
        time.sleep(0.15)
        pyautogui.press('enter')

    def insert_paragraph(self):
        restore_focus(self.target_hwnd or get_foreground_window())
        time.sleep(0.15)
        pyautogui.press('enter')
        time.sleep(0.06)
        pyautogui.press('enter')

    # ------------------------------------------------------------------ #
    #  FELVÉTEL                                                           #
    # ------------------------------------------------------------------ #
    def _start_recording(self):
        if self.recording or not self.whisper_model:
            return
        if not self.target_hwnd:
            self.target_hwnd = get_foreground_window()
        self.recording = True
        with self._audio_lock:
            self.audio_data = deque()
        self._set_status("🎙️ HALLGATÓZOM...  (5mp / tartva: folytatja)", "red")
        self._schedule_auto_stop()

    def _schedule_auto_stop(self):
        self._cancel_auto_stop()
        t = threading.Timer(5.0, self._check_auto_stop)
        t.daemon = True
        t.start()
        self._auto_stop_timer = t

    def _cancel_auto_stop(self):
        if self._auto_stop_timer:
            self._auto_stop_timer.cancel()
            self._auto_stop_timer = None

    def _check_auto_stop(self):
        if not self.recording:
            return
        if keyboard.is_pressed('alt') and keyboard.is_pressed('space'):
            self._schedule_auto_stop()
        else:
            self.recording = False
            self._process_audio()

    # ------------------------------------------------------------------ #
    #  FELDOLGOZÁS                                                        #
    # ------------------------------------------------------------------ #
    def _process_audio(self):
        self._set_status("AI gondolkodik...", "cyan")
        with self._audio_lock:
            if not self.audio_data:
                self._set_status("Nem hallottam semmit", "gray")
                return
            audio_np = np.concatenate(list(self.audio_data), axis=0)

        wavfile.write(self._temp_wav, self.fs, audio_np)
        hwnd = self.target_hwnd
        self.target_hwnd = None

        def _task():
            try:
                segs, _ = self.whisper_model.transcribe(
                    self._temp_wav, beam_size=5, language="hu",
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                    suppress_tokens=[-1])
                text = " ".join(s.text for s in segs).strip()
            except RuntimeError as e:
                if "type must be number" in str(e):
                    try:
                        segs, _ = self.whisper_model.transcribe(
                            self._temp_wav, beam_size=1,
                            language="hu", vad_filter=False)
                        text = " ".join(s.text for s in segs).strip()
                    except Exception as e2:
                        self._set_status(f"Hiba: {e2}", "red")
                        return
                else:
                    self._set_status(f"Hiba: {e}", "red")
                    return

            if not text:
                self._set_status("Nem hallottam semmit", "gray")
                return

            text = apply_voice_commands(text)
            paste_text_to_window(hwnd, text)
            self._set_status("✅ Beillesztve!", "green")

        threading.Thread(target=_task, daemon=True).start()


if __name__ == "__main__":
    app = VoicetexApp()
    app.mainloop()
