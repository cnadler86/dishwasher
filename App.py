#!/home/chris/HCApp/.venv/bin/python
import json
from datetime import datetime, timedelta, time
from hcpy.HCSocket import HCSocket
from hcpy.HCDevice import HCDevice
from transitions import Machine
from pathlib import Path
from time import sleep
from typing import List, Optional, Dict, Any
from setup_logger import setup_logging

DEBUG = False
DEFAULT_PROGRAM_ID = 8227
DEFAULT_FINISH_TIME = time(2, 0)  # 2:00 AM
RETRY_DELAY = 10  # seconds

# Logging-Konfiguration
logger = setup_logging()

class DishwasherController:
    state: str
    device: HCDevice
    ws: HCSocket
    dishwasher: Dict[str, Any]
    finish_times: Optional[List[time]]

    @staticmethod
    def _get_config_path() -> Path:
        """Returns the path to the devices config file relative to script location"""
        script_dir = Path(__file__).parent
        config_path = script_dir / "hcpy" / "config" / "devices.json"
        
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found at {config_path}")
        
        return config_path

    def __init__(self, config_file:Path|None=None, finish_times:List[time]|None=None) -> None:
        # Lade die Konfiguration und sortiere die Zielzeiten
        self.finish_times = sorted(finish_times) if finish_times else None
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

    def _get_next_time(self) -> datetime:
        '''
        Return the next future datetime based on self.finish_times
        '''
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        if not self.finish_times:
            # If no finish times specified, use default (tomorrow 2:00 AM)
            return datetime.combine(tomorrow, DEFAULT_FINISH_TIME)

        # Check today's remaining times first
        for finish_time in self.finish_times:
            target = datetime.combine(today, finish_time)
            if target > now:
                return target
        
        # If no remaining times today, get the first time for tomorrow
        return datetime.combine(tomorrow, self.finish_times[0])


    def on_enter_start(self) -> None:
        """Aktion beim Eintritt in den Start-Zustand"""
        logger.debug("Starte Spülmaschine...")
        self.start_program()

    def on_enter_idle(self) -> None:
        """Aktion beim Eintritt in den Start-Zustand"""
        logger.debug("Programm beendet...")
    
    def _check_conditions_start(self)-> bool:
        """
        Check if all conditions for starting the dishwasher are met.
        
        Returns:
            bool: True if all conditions are met:
                - Door is closed
                - Remote control is allowed
                - No active program running
                - Power is on
        """

        with self.device.state_lock:
            # Prüfe ob Tür geschlossen ist und der remote Start erlaubt ist und die spulmachine nicht läuft und an ist
            if self.device.state.get("BSH.Common.Status.DoorState") == "Closed" and \
               self.device.state.get("BSH.Common.Status.RemoteControlStartAllowed") and \
               self.device.state.get("BSH.Common.Status.ActiveProgram") is None and \
               self.device.state.get("BSH.Common.Setting.PowerState") == "On":
                return True
            else:
                return False

    def _is_program_finish(self) -> bool:
        return self.device.state.get("BSH.Common.Setting.PowerState") == 'Off' or \
                self.device.state.get("BSH.Common.Status.OperationState") == 'Finished'

    def start_program(self, program_id:int=DEFAULT_PROGRAM_ID, start_in:int|None=None) -> None:
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

        logger.debug(f"Starting program {program_id} with delay {start_in}")

        IntensivZone = self.device.state.get("Dishcare.Dishwasher.Option.IntensivZone")
        BrillianceDry = self.device.state.get("Dishcare.Dishwasher.Option.BrillianceDry")
        VarioSpeedPlus = self.device.state.get("Dishcare.Dishwasher.Option.VarioSpeedPlus")

        if IntensivZone:
            program_data["options"].append({"uid": 5126, "value": IntensivZone})
        if BrillianceDry:
            program_data["options"].append({"uid": 5128, "value": BrillianceDry})
        if VarioSpeedPlus:
            program_data["options"].append({"uid": 5127, "value": VarioSpeedPlus})

        logger.debug(f"IntensivZone: {IntensivZone}, BrillianceDry: {BrillianceDry}, VarioSpeedPlus: {VarioSpeedPlus}")
        logger.debug(f"Remaining time: {self.device.state.get("BSH.Common.Option.RemainingProgramTime")}")
        
        try:
            with self.device.state_lock:
                self.device.get("/ro/activeProgram", action="POST", data=[program_data])
        except Exception as e:
            logger.error(f"Failed to start program {program_id}: {e}", exc_info=True)
            raise

    def select_program(self,program_id:int=DEFAULT_PROGRAM_ID, start_in:int|None=None) -> None:
        program_data = {
            "program": program_id,  # UID für Programm
            "options": []  # Optionale Parameter wie Startverzögerung etc.
        }
        if not start_in:
            start_in = self._get_time_delta(self._get_next_time())
        if start_in:
            program_data["options"] = [{"uid": 558, "value": start_in}]

        try:
            with self.device.state_lock:
                self.device.get("/ro/selectedProgram", action="POST", data=program_data)
        except Exception as e:
            logger.error(f"Fehler beim Starten: {e}")



    def start_app(self) -> None:
        """Überwacht den Status der Spülmaschine"""
        def on_message(values: Dict[str, Any]) -> None:
            if values:
                logger.debug(f"Status msg: {values}")
                if values.get("error") and values.get("resource"):
                    return
                try:
                    with self.device.state_lock:
                        self.device.state.update(values)
                except Exception as e:
                    pass
                if self.state == "idle":
                    self.trigger('start')
                else:
                    self.trigger('finish')
                
        def on_open(ws: HCSocket) -> None:
            logger.info("Verbindung hergestellt")

        def on_close(ws: HCSocket, code: int, message: str) -> None:
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
    finish_times=[time(2), time(10), time(16)]
    
    while True:
        try:
            # Initialisiere Controller
            controller = DishwasherController(finish_times=finish_times)
            controller.start_app()
            sleep(1)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Ein Fehler ist aufgetreten: {e}")
            sleep(RETRY_DELAY)