import json
from datetime import datetime, timedelta, time
from hcpy.HCSocket import HCSocket
from hcpy.HCDevice import HCDevice
from transitions import Machine
from pathlib import Path
from time import sleep
from typing import List, Optional, Dict, Any
from setup_logger import setup_logging
from awattar.client  import AwattarClient
from dateutil import tz
from threading import Timer

### Configuration
DEBUG:bool = False
DEFAULT_PROGRAM_ID:int = 8196 # Eco 50°. MaxEfficiency is 8227. Used only for auto-selection
DEFAULT_AUTOSELECT_HOUR:Optional[int] = 18  # After this hour, the default program will always be selected/used automatically (optional)
DEFAULT_FINISH_TIME:time = time(6, 00)  # Normally, this is not needed and does not need to be changed, if you use the FINISH_TIMES parameter
FINISH_TIMES:Optional[List[time]] = [time(6), time(18,30)] # Optional list of finish times to consider; if empty, the default time will be used
RETRY_DELAY:int = 60  # Delay in seconds before retrying connection if it fails (normally not needed, but can be useful for debugging)
START_TIME_OFFSET:int = 15  # Minutes to shift the start time earlier for better energy optimization (in order to catch the energy heavy load period)

# Logging configuration
logger = setup_logging()

class DishwasherController:
    state: str
    device: HCDevice
    ws: HCSocket
    dishwasher: Dict[str, Any]
    finish_times: Optional[List[time]]

    @staticmethod
    def _get_config_path() -> Path:
        """Returns the path to the devices config file relative to the script location"""
        script_dir = Path(__file__).parent
        config_path = script_dir / "hcpy" / "config" / "devices.json"
        
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found at {config_path}")
        
        return config_path

    def __init__(self, config_file:Path|None=None, finish_times:List[time]|None=None, country:str='DE') -> None:
        # Load the configuration and sort the target times
        self.finish_times = sorted(finish_times) if finish_times else None
        if not config_file:
            config_file = self._get_config_path()

        with open(config_file, 'r') as f:
            devices = json.load(f)

        # Find the dishwasher in the configured devices
        self.dishwasher = next(
            device for device in devices
            if "dishwasher" in device.get("name", "")
        )

        # Initialize the connection
        self.ws = HCSocket(
            self.dishwasher["host"],
            self.dishwasher["key"],
            self.dishwasher.get("iv")
        )

        if country.upper() not in ['DE', 'AT']:
            raise ValueError(f"Unsupported country: {country}. Supported countries are: DE, AT.")
        self.client = AwattarClient(country.upper())

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
            # If no finish times are specified, use the default (tomorrow 2:00 AM)
            return datetime.combine(tomorrow, DEFAULT_FINISH_TIME)

        # Check today's remaining times first
        for finish_time in self.finish_times:
            target = datetime.combine(today, finish_time)
            if target > now:
                return target
        
        # If there are no remaining times today, get the first time for tomorrow
        return datetime.combine(tomorrow, self.finish_times[0])


    def on_enter_start(self) -> None:
        """Action when entering the start state"""
        logger.debug("Starting dishwasher...")
        self._autoselect_default_program(hour=DEFAULT_AUTOSELECT_HOUR) # Needed in order to correctly fetch the program duration
        t = Timer(2, self.start_program)
        t.start()
        # self.start_program()

    def on_enter_idle(self) -> None:
        """Action when entering the idle state"""
        logger.debug("Program finished...")
    
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
            # Check if the door is closed, remote start is allowed, the dishwasher is not running, and power is on
            if self.device.state.get("BSH.Common.Status.DoorState") == "Closed" and \
               self.device.state.get("BSH.Common.Status.RemoteControlStartAllowed") and \
               self.device.state.get("BSH.Common.Status.ActiveProgram") is None and \
               self.device.state.get("BSH.Common.Setting.PowerState") == "On":
                return True
            else:
                return False

    def _is_program_finish(self) -> bool:
        return self.device.state.get("BSH.Common.Setting.PowerState") == 'Off' or \
                self.device.state.get("BSH.Common.Status.OperationState") in ['Aborting']

    def _get_options(self) -> List[Optional[Dict[str, Any]]]:
        IntensivZone = self.device.state.get("Dishcare.Dishwasher.Option.IntensivZone")
        BrillianceDry = self.device.state.get("Dishcare.Dishwasher.Option.BrillianceDry")
        VarioSpeedPlus = self.device.state.get("Dishcare.Dishwasher.Option.VarioSpeedPlus")
        options = []
        if IntensivZone:
            options.append({"uid": 5126, "value": IntensivZone})
        if BrillianceDry:
            options.append({"uid": 5128, "value": BrillianceDry})
        if VarioSpeedPlus:
            options.append({"uid": 5127, "value": VarioSpeedPlus})
        return options

    def start_program(self, program_id:Optional[int]=None, start_in:int|None=None) -> None:
        """Starts the program at the specified time"""
        # Prepare program start
        if program_id is None:
            program_id = self.device.state.get("BSH.Common.Root.SelectedProgram")

        program_data = {
            "program": program_id if program_id else DEFAULT_PROGRAM_ID,  # UID for program
            "options": self._get_options()
        }
        if not start_in:
            start_in = self._get_time_delta()
        if start_in:
            program_data["options"].append({"uid": 558, "value": start_in})

        logger.debug(f"Starting program {program_data['program']} with options {program_data['options']}")

        try:
            with self.device.state_lock:
                self.device.get("/ro/activeProgram", action="POST", data=program_data)
        except Exception as e:
            logger.error(f"Failed to start program {program_id}: {e}", exc_info=True)
            raise

    def select_program(self,program_id:int=DEFAULT_PROGRAM_ID) -> None:
        program_data = {
            "program": program_id,  # UID for program
            "options": self._get_options()
        }

        logger.debug(f"Selecting program {program_data['program']} with options {program_data['options']}")
        try:
            with self.device.state_lock:
                self.device.get("/ro/selectedProgram", action="POST", data=program_data)
        except Exception as e:
            logger.error(f"Error while starting: {e}")

    def _get_program_duration(self) -> timedelta:
        RemainingProgramTime = self.device.state.get("BSH.Common.Option.RemainingProgramTime")
        if RemainingProgramTime:
            return timedelta(seconds=RemainingProgramTime)
        else:
            return timedelta(seconds=12000)

    def _get_best_start_time(self) -> datetime | None:
        next_start_time = (self._get_next_time() - self._get_program_duration()).astimezone(tz.tzlocal())
        now = datetime.now(tz.tzlocal())
        if next_start_time < now:
            return None
        self.client.request(datetime.combine(now.date(), time(now.hour,0)), 
                            datetime.combine(next_start_time.date(), time(next_start_time.hour+1,0)))
        best_spot = self.client.best_slot(1)
        if best_spot:
            next_start_time = best_spot.start_datetime if best_spot.start_datetime < next_start_time else next_start_time
        
        # Shift start time earlier by START_TIME_OFFSET
        next_start_time = next_start_time - timedelta(minutes=START_TIME_OFFSET)
        
        if next_start_time < now:
            return None
        return next_start_time

    def _evaluate_state(self) -> None:
        if self.state == "idle":
            self.trigger('start') # type: ignore
        else:
            self.trigger('finish') # type: ignore

    def start_app(self) -> None:
        """Monitors the status of the dishwasher"""
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
                self._evaluate_state()
                
        def on_open(ws: HCSocket) -> None:
            logger.info("Connection established")

        def on_close(ws: HCSocket, code: int, message: str) -> None:
            logger.info(f"Connection closed: {message}")

        self.device.run_forever(
            on_message=on_message,
            on_open=on_open,
            on_close=on_close
        )
    
    # Calculate time delta to target time
    def _get_time_delta(self, start_time:datetime|None = None) -> int:
        """Calculates the time difference to the target time"""
        if start_time is None:
            start_time = self._get_best_start_time()
        
        if start_time is not None:
            delta = (start_time - datetime.now(tz.tzlocal())).total_seconds()
            return int(min(24*60*60,delta)) if delta > 0 else 0
        else:
            return 0

    def _autoselect_default_program(self, hour:Optional[int]=None) -> None:
        if hour is not None and (datetime.now(tz.tzlocal()).hour >= hour) and self.device.state.get("BSH.Common.Status.RemoteControlStartAllowed"):
            self.select_program(program_id=DEFAULT_PROGRAM_ID)

if __name__ == "__main__":
    while True:
        try:
            # Initialize controller
            controller = DishwasherController(finish_times=FINISH_TIMES)
            controller.start_app()
            sleep(RETRY_DELAY)
        except KeyboardInterrupt:
            break
        except Exception as e:
            sleep(RETRY_DELAY)