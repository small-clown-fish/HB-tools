import ctypes
import json
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

try:
    from pymodbus.client import ModbusTcpClient
except Exception as exc:
    ModbusTcpClient = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


APP_TITLE = "BMS 0x038B + Heartbeat Writer"

CMD_REGISTER_ADDRESS = 0x038B
CMD_WRITE_VALUE = 2

HEARTBEAT_REGISTER_ADDRESS = 0x0380
HEARTBEAT_INTERVAL_SEC = 1.0

# Windows SetThreadExecutionState flags
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def is_windows() -> bool:
    return hasattr(ctypes, "windll")


def prevent_windows_sleep(enable: bool) -> None:
    """
    Prevent system sleep on Windows while allowing the display to turn off.
    This helps the software keep writing after the screen goes black,
    as long as the PC is not manually put into sleep/hibernate.
    """
    if not is_windows():
        return

    try:
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            )
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


@dataclass
class BmsTarget:
    name: str
    ip: str


class DeviceWriter(threading.Thread):
    def __init__(
        self,
        target: BmsTarget,
        port: int,
        unit_id: int,
        cmd_interval_sec: float,
        heartbeat_start_value: int,
        stop_event: threading.Event,
        log_queue: queue.Queue,
    ):
        super().__init__(daemon=True)
        self.target = target
        self.port = port
        self.unit_id = unit_id
        self.cmd_interval_sec = max(0.2, float(cmd_interval_sec))
        self.heartbeat_value = int(heartbeat_start_value) % 256
        self.stop_event = stop_event
        self.log_queue = log_queue
        self.client = None

        self._next_cmd_ts = 0.0
        self._next_heartbeat_ts = 0.0

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] [{self.target.name}] {msg}")

    def close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def connect_once(self) -> bool:
        self.close_client()
        try:
            self.client = ModbusTcpClient(
                host=self.target.ip,
                port=self.port,
                timeout=1.5,
                retries=0,
            )
            ok = self.client.connect()
            if ok:
                self.log(f"Connected {self.target.ip}:{self.port}")
                now = time.monotonic()
                self._next_cmd_ts = now
                self._next_heartbeat_ts = now
                return True

            self.log(f"Connect failed {self.target.ip}:{self.port}")
            self.close_client()
            return False
        except Exception as exc:
            self.log(f"Connect exception: {exc}")
            self.close_client()
            return False

    def write_register_compat(self, address: int, value: int):
        """
        Compatible with pymodbus 3.x variants.
        New versions usually use device_id.
        Older versions may use slave.
        """
        try:
            return self.client.write_register(
                address=address,
                value=value,
                device_id=self.unit_id,
            )
        except TypeError:
            return self.client.write_register(
                address=address,
                value=value,
                slave=self.unit_id,
            )

    def write_one(self, address: int, value: int, label: str) -> bool:
        if self.client is None:
            return False

        try:
            result = self.write_register_compat(address, value)

            if result is None:
                self.log(f"{label} failed: no response")
                return False

            if hasattr(result, "isError") and result.isError():
                self.log(f"{label} error: {result}")
                return False

            self.log(f"{label} OK: 0x{address:04X} = {value}")
            return True

        except Exception as exc:
            self.log(f"{label} exception: {exc}")
            return False

    def sleep_with_stop(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def run_connected_loop(self) -> bool:
        """
        Return False if connection should be closed/reconnected.
        Return True only if stop_event is set.
        """
        while not self.stop_event.is_set():
            now = time.monotonic()

            did_work = False

            if now >= self._next_heartbeat_ts:
                ok = self.write_one(
                    HEARTBEAT_REGISTER_ADDRESS,
                    self.heartbeat_value,
                    "Heartbeat",
                )
                if not ok:
                    return False

                self.heartbeat_value = (self.heartbeat_value + 1) % 256
                self._next_heartbeat_ts = now + HEARTBEAT_INTERVAL_SEC
                did_work = True

            if now >= self._next_cmd_ts:
                ok = self.write_one(
                    CMD_REGISTER_ADDRESS,
                    CMD_WRITE_VALUE,
                    "Command",
                )
                if not ok:
                    return False

                self._next_cmd_ts = now + self.cmd_interval_sec
                did_work = True

            if not did_work:
                next_due = min(self._next_heartbeat_ts, self._next_cmd_ts)
                sleep_sec = max(0.05, min(0.2, next_due - time.monotonic()))
                if self.sleep_with_stop(sleep_sec):
                    return True

        return True

    def run(self) -> None:
        self.log("Worker started")

        while not self.stop_event.is_set():
            if self.client is None:
                if not self.connect_once():
                    # Cooldown before reconnecting. No tight reconnect loop.
                    if self.sleep_with_stop(max(1.0, self.cmd_interval_sec)):
                        break
                    continue

            should_stop = self.run_connected_loop()
            if should_stop:
                break

            self.close_client()
            # Cooldown before reconnecting. No tight reconnect loop.
            if self.sleep_with_stop(max(1.0, self.cmd_interval_sec)):
                break

        self.close_client()
        self.log("Worker stopped")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("940x650")
        self.minsize(860, 590)

        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.workers = []
        self.running = False

        self._build_ui()
        self._load_default_config_if_exists()
        self.after(200, self._poll_log_queue)

        if IMPORT_ERROR is not None:
            messagebox.showerror(
                "Missing dependency",
                f"pymodbus is not available:\n\n{IMPORT_ERROR}\n\n"
                "Please run: pip install -r requirements.txt"
            )

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        cfg = ttk.LabelFrame(root, text="Settings", padding=10)
        cfg.pack(fill=tk.X)

        ttk.Label(cfg, text="Port").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value="502")
        ttk.Entry(cfg, textvariable=self.port_var, width=10).grid(row=0, column=1, sticky="w", padx=(6, 18))

        ttk.Label(cfg, text="Unit ID / Device ID").grid(row=0, column=2, sticky="w")
        self.unit_var = tk.StringVar(value="1")
        ttk.Entry(cfg, textvariable=self.unit_var, width=10).grid(row=0, column=3, sticky="w", padx=(6, 18))

        ttk.Label(cfg, text="0x038B interval seconds").grid(row=0, column=4, sticky="w")
        self.cmd_interval_var = tk.StringVar(value="1.0")
        ttk.Entry(cfg, textvariable=self.cmd_interval_var, width=10).grid(row=0, column=5, sticky="w", padx=(6, 18))

        ttk.Label(cfg, text="Heartbeat start").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.heartbeat_start_var = tk.StringVar(value="0")
        ttk.Entry(cfg, textvariable=self.heartbeat_start_var, width=10).grid(row=1, column=1, sticky="w", padx=(6, 18), pady=(8, 0))

        ttk.Label(cfg, text="Heartbeat interval").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(cfg, text="1.0 s fixed").grid(row=1, column=3, sticky="w", padx=(6, 18), pady=(8, 0))

        self.prevent_sleep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            cfg,
            text="Prevent Windows sleep while running",
            variable=self.prevent_sleep_var,
        ).grid(row=1, column=4, columnspan=3, sticky="w", pady=(8, 0))

        info = ttk.Label(
            root,
            text="Each BMS: write 0x038B=2 by interval, and write heartbeat 0x0380=0~255 every second. One BMS per line: name,ip  or just ip",
        )
        info.pack(anchor="w", pady=(12, 4))

        self.targets_text = tk.Text(root, height=9, wrap="none")
        self.targets_text.pack(fill=tk.X)
        self.targets_text.insert("1.0", "BMS-1,192.168.1.101\nBMS-2,192.168.1.102\n")

        btns = ttk.Frame(root)
        btns.pack(fill=tk.X, pady=10)

        self.start_btn = ttk.Button(btns, text="Start writing", command=self.start_writing)
        self.start_btn.pack(side=tk.LEFT)

        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_writing, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        ttk.Button(btns, text="Save config", command=self.save_config).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Load config", command=self.load_config).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(btns, textvariable=self.status_var).pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(root, text="Log", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=18, wrap="none", state=tk.DISABLED)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scroll.set)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _log(self, msg: str):
        self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] [APP] {msg}")

    def _append_log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_log_queue(self):
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(200, self._poll_log_queue)

    def parse_targets(self):
        raw = self.targets_text.get("1.0", tk.END).strip()
        targets = []
        for idx, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "," in line:
                name, ip = [x.strip() for x in line.split(",", 1)]
                if not name:
                    name = f"BMS-{idx}"
            else:
                ip = line.strip()
                name = f"BMS-{idx}"

            if not ip:
                continue
            targets.append(BmsTarget(name=name, ip=ip))

        return targets

    def validate_settings(self):
        if IMPORT_ERROR is not None:
            raise RuntimeError("pymodbus is not installed")

        port = int(self.port_var.get().strip())
        unit_id = int(self.unit_var.get().strip())
        cmd_interval_sec = float(self.cmd_interval_var.get().strip())
        heartbeat_start_value = int(self.heartbeat_start_var.get().strip())

        if not (1 <= port <= 65535):
            raise ValueError("Port must be 1~65535")
        if not (0 <= unit_id <= 255):
            raise ValueError("Unit ID / Device ID must be 0~255")
        if cmd_interval_sec < 0.2:
            raise ValueError("0x038B interval must be >= 0.2 seconds")
        if not (0 <= heartbeat_start_value <= 255):
            raise ValueError("Heartbeat start must be 0~255")

        targets = self.parse_targets()
        if not targets:
            raise ValueError("Please enter at least one BMS IP")

        return targets, port, unit_id, cmd_interval_sec, heartbeat_start_value

    def start_writing(self):
        if self.running:
            return

        try:
            targets, port, unit_id, cmd_interval_sec, heartbeat_start_value = self.validate_settings()
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        self.stop_event.clear()
        self.workers = []

        prevent_windows_sleep(self.prevent_sleep_var.get())

        for target in targets:
            worker = DeviceWriter(
                target=target,
                port=port,
                unit_id=unit_id,
                cmd_interval_sec=cmd_interval_sec,
                heartbeat_start_value=heartbeat_start_value,
                stop_event=self.stop_event,
                log_queue=self.log_queue,
            )
            self.workers.append(worker)
            worker.start()

        self.running = True
        self.status_var.set(f"Running: {len(self.workers)} BMS")
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._log(
            f"Started. Command 0x{CMD_REGISTER_ADDRESS:04X}={CMD_WRITE_VALUE}, "
            f"cmd_interval={cmd_interval_sec}s, heartbeat 0x{HEARTBEAT_REGISTER_ADDRESS:04X}=0~255 every 1s"
        )

    def stop_writing(self):
        if not self.running:
            return

        self._log("Stopping...")
        self.stop_event.set()

        for worker in self.workers:
            worker.join(timeout=2.0)

        self.workers = []
        self.running = False
        prevent_windows_sleep(False)

        self.status_var.set("Stopped")
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self._log("Stopped. No reconnect/write task remains active.")

    def config_path(self):
        return Path(__file__).with_name("bms_038b_heartbeat_writer_config.json")

    def collect_config(self):
        targets = []
        for target in self.parse_targets():
            targets.append({"name": target.name, "ip": target.ip})
        return {
            "bms_list": targets,
            "port": int(self.port_var.get().strip()),
            "unit_id": int(self.unit_var.get().strip()),
            "cmd_interval_sec": float(self.cmd_interval_var.get().strip()),
            "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
            "heartbeat_start_value": int(self.heartbeat_start_var.get().strip()),
            "cmd_register_address_hex": f"0x{CMD_REGISTER_ADDRESS:04X}",
            "cmd_write_value": CMD_WRITE_VALUE,
            "heartbeat_register_address_hex": f"0x{HEARTBEAT_REGISTER_ADDRESS:04X}",
            "prevent_sleep": bool(self.prevent_sleep_var.get()),
        }

    def apply_config(self, data):
        self.port_var.set(str(data.get("port", 502)))
        self.unit_var.set(str(data.get("unit_id", 1)))
        self.cmd_interval_var.set(str(data.get("cmd_interval_sec", data.get("interval_sec", 1.0))))
        self.heartbeat_start_var.set(str(data.get("heartbeat_start_value", 0)))
        self.prevent_sleep_var.set(bool(data.get("prevent_sleep", True)))

        lines = []
        for i, item in enumerate(data.get("bms_list", []), 1):
            name = item.get("name") or f"BMS-{i}"
            ip = item.get("ip") or ""
            if ip:
                lines.append(f"{name},{ip}")

        if lines:
            self.targets_text.delete("1.0", tk.END)
            self.targets_text.insert("1.0", "\n".join(lines) + "\n")

    def save_config(self):
        try:
            data = self.collect_config()
            self.config_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            self._log(f"Config saved: {self.config_path()}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def load_config(self):
        try:
            path = self.config_path()
            if not path.exists():
                messagebox.showinfo("No config", f"Config file not found:\n{path}")
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            self.apply_config(data)
            self._log(f"Config loaded: {path}")
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))

    def _load_default_config_if_exists(self):
        path = self.config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.apply_config(data)
                self._log(f"Auto-loaded config: {path}")
            except Exception as exc:
                self._log(f"Auto-load config failed: {exc}")

    def on_close(self):
        if self.running:
            self.stop_writing()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
