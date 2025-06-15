#!/home/chris/HCApp/.venv/bin/python
import json
from datetime import datetime, timedelta
from hcpy.HCSocket import HCSocket
from hcpy.HCDevice import HCDevice
from transitions import Machine
from pathlib import Path
import time
import logging

# Logging-Konfiguration
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('DishwasherApp')
logger.setLevel(logging.DEBUG)

# File Handler
file_handler = logging.FileHandler('dishwasher.log')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.DEBUG)

# Console Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

DEBUG = False

class DishwasherController:
    state:str

    @staticmethod
    def _get_config_path() -> Path:
        """Returns the path to the devices config file relative to script location"""
        script_dir = Path(__file__).parent
        config_path = script_dir / "hcpy" / "config" / "devices.json"
        
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found at {config_path}")
        
        return config_path

    def __init__(self, config_file:Path|None=None):
        # Lade die Konfiguration
        if not config_file:
            config_file = self._get_config_path()

        with open(config_file, 'r') as f:
            devices = json.load(f)

        # Finde die Spülmaschine in den konfigurierten Geräten
        self.dishwasher = next(
            device for device in devices
            if "dishwasher" in device.get("name", "")
        )

        # Initialisiere die Verbindung
        self.ws = HCSocket(
            self.dishwasher["host"],
            self.dishwasher["key"],
            self.dishwasher.get("iv")
        )

        self.device = HCDevice(self.ws, self.dishwasher, debug=DEBUG)
        self.Machine = Machine(
            model=self,
            states=["idle", "start"],
            transitions=[
                {'trigger': 'start', 'source': 'start', 'dest': None},
                {'trigger': 'start', 'source': 'idle', 'dest': 'start', 'conditions': '_check_conditions_start'},
                {'trigger': 'finish', 'source': 'idle', 'dest': None},
                {'trigger': 'finish', 'source': 'start', 'dest': 'idle', 'conditions': '_is_program_finish'}
            ],
            initial="idle", 
            auto_transitions=False,
        )

    def on_enter_start(self) -> None:
        """Aktion beim Eintritt in den Start-Zustand"""
        logger.info("Starte Spülmaschine...")
        self.start_program()

    def on_enter_idle(self) -> None:
        """Aktion beim Eintritt in den Start-Zustand"""
        logger.info("Programm beendet...")
    
    def _check_conditions_start(self)-> bool:
        """Überprüft die Bedingungen für den Start"""

        with self.device.state_lock:
            # Prüfe ob Tür geschlossen ist und der remote Start erlaubt ist und die spulmachine nicht läuft und an ist
            if self.device.state.get("BSH.Common.Status.DoorState") == "Closed" and \
               self.device.state.get("BSH.Common.Status.RemoteControlStartAllowed") and \
               self.device.state.get("BSH.Common.Status.ActiveProgram") is None and \
               self.device.state.get("BSH.Common.Setting.PowerState") == "On":
                logger.debug("Bedingungen erfüllt: Tür geschlossen und Remote Start erlaubt")
                return True
            else:
                return False

    def _is_program_finish(self) -> bool:
        return self.device.state.get("BSH.Common.Setting.PowerState") == 'Off' or \
                self.device.state.get("BSH.Common.Status.OperationState") == 'Finished'

    def start_program(self, program_id:int=8227, start_in:int|None=None) -> None:
        """Startet das Programm zur angegebenen Zeit"""
        # Bereite Programm-Start vor
        program_data = {
            "program": program_id,  # UID für Programm
            "options": []  # Optionale Parameter wie Startverzögerung etc.
        }
        if not start_in:
            start_in = self._get_time_delta()
        if start_in:
            program_data["options"] = [{"uid": 558, "value": start_in}]

        try:
            with self.device.state_lock:
                self.device.get("/ro/activeProgram", action="POST", data=program_data)
        except Exception as e:
            logger.error(f"Fehler beim Starten: {e}")

    def select_program(self,program_id:int=8227, start_in:int|None=None) -> None:
        program_data = {
            "program": program_id,  # UID für Programm
            "options": []  # Optionale Parameter wie Startverzögerung etc.
        }
        if not start_in:
            start_in = self._get_time_delta()
        if start_in:
            program_data["options"] = [{"uid": 558, "value": start_in}]

        try:
            with self.device.state_lock:
                self.device.get("/ro/selectedProgram", action="POST", data=program_data)
        except Exception as e:
            logger.error(f"Fehler beim Starten: {e}")

    def start_app(self) -> None:
        """Überwacht den Status der Spülmaschine"""
        def on_message(values):
            if values:
                try:
                    with self.device.state_lock:
                        self.device.state.update(values)
                except Exception as e:
                    pass
                if self.state == "idle":
                    self.trigger('start')
                else:
                    self.trigger('finish')
                
        def on_open(ws):
            logger.info("Verbindung hergestellt")

        def on_close(ws, code, message):
            logger.info(f"Verbindung geschlossen: {message}")

        self.device.run_forever(
            on_message=on_message,
            on_open=on_open,
            on_close=on_close
        )
    
    #get time delta to target time, default is tomorrow 2:00 AM
    @staticmethod
    def _get_time_delta(target_time:datetime|None = None) -> int|None:
        """Berechnet die Zeitdifferenz bis zum Zielzeitpunkt"""
        now = datetime.now()
        if not target_time:
            target_time = now.replace(hour=23, minute=59, second=59) 
        delta = (target_time - now + timedelta(hours=1, minutes=45)).seconds
        if delta > 60:
            return int(min(24*60*60, delta))


#!/home/chris/HCApp/.venv/bin/python
if __name__ == "__main__":
    while True:
        try:
            # Initialisiere Controller
            controller = DishwasherController()
            controller.start_app()

        except KeyboardInterrupt:
            logger.info("Programm beendet")
            break
        except Exception as e:
            logger.error(f"Ein Fehler ist aufgetreten: {e}")
            time.sleep(10)  # Warte 5 Sekunden vor dem Neustart