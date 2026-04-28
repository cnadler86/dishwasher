"""
Dishwasher Controller - Optimized energy management for Home Connect dishwashers
"""

import csv
import json
import logging
import signal
import sys
from datetime import datetime, time, timedelta
from enum import Enum, auto
from pathlib import Path
from threading import Event, Lock
from time import sleep
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
from awattar.client import AwattarClient
from dateutil import tz
from hcpy.HCDevice import HCDevice
from hcpy.HCSocket import HCSocket


# Configuration
class Config:
    DEBUG: bool = False
    DEFAULT_PROGRAM_ID: int = 8196  # Eco 50°
    DEFAULT_AUTOSELECT_HOUR: Optional[int] = 18
    DEFAULT_FINISH_TIME: time = time(6, 0)
    FINISH_TIMES: Optional[List[time]] = [time(6), time(18, 30)]
    RETRY_DELAY: int = 60
    START_TIME_OFFSET: int = 15  # Minutes
    RECONNECT_DELAY: int = 5  # Seconds before reconnecting
    STATE_CHECK_INTERVAL: int = 30  # Seconds between state evaluations
    LOAD_PROFILE_DIR: Path = Path(__file__).parent / "load_profiles"
    LOAD_PROFILE_FILE_TEMPLATE: str = "{program_id}.csv"
    MQTT_ENABLED: bool = True
    MQTT_HOST: str = "localhost"
    MQTT_PORT: int = 1883
    MQTT_USERNAME: Optional[str] = None
    MQTT_PASSWORD: Optional[str] = None
    MQTT_TOPIC_PREFIX: str = "gridpythia/appliance_load/forecast"
    MQTT_QOS: int = 1
    MQTT_RETAIN: bool = True
    MQTT_CLIENT_ID: str = "dishwasher"


class DishwasherState(Enum):
    """Dishwasher operational states"""

    IDLE = auto()  # Ready to start, waiting for conditions
    SCHEDULED = auto()  # Program scheduled, waiting for start
    RUNNING = auto()  # Program is running
    ERROR = auto()  # Error state
    DISCONNECTED = auto()  # Connection lost


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("DishwasherApp")
    if Config.DEBUG:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG)

    logger.addHandler(console_handler)

    return logger


class DishwasherController:
    """
    Main controller for automated dishwasher operation with energy optimization
    """

    # Re-publish scheduled forecast when start time shifts by more than this threshold
    _RESCHEDULE_THRESHOLD_S: int = 120

    def __init__(
        self,
        config_file: Optional[Path] = None,
        finish_times: Optional[List[time]] = None,
        country: str = "DE",
    ) -> None:
        self.logger = setup_logging()
        self.config = Config()

        # State management
        self._shutdown_event = Event()
        self._last_logged_state: Optional[DishwasherState] = None
        self._awaiting_post_run_reset: bool = False

        # State-driven forecast tracking
        self._last_forecast_dishwasher_state: Optional[DishwasherState] = None
        self._last_forecast_program_id: Optional[int] = None
        self._last_forecast_scheduled_start: Optional[datetime] = None

        # Finish times configuration
        self.finish_times = sorted(finish_times) if finish_times else None

        # Load device configuration
        config_file = config_file or self._get_config_path()
        self.dishwasher_config = self._load_device_config(config_file)

        # Initialize connections
        self.ws: Optional[HCSocket] = None
        self.device: Optional[HCDevice] = None
        self._mqtt_client: Optional[mqtt.Client] = None
        self._mqtt_lock = Lock()
        self._current_forecast: List[Dict[str, Any]] = []

        # Energy optimization
        if country.upper() not in ["DE", "AT"]:
            raise ValueError(f"Unsupported country: {country}")
        self.energy_client = AwattarClient(country.upper())

        # Setup signal handlers for clean shutdown
        self._setup_signal_handlers()

        self.logger.info("DishwasherController initialized")

    @staticmethod
    def _get_config_path() -> Path:
        """Get path to devices config file"""
        script_dir = Path(__file__).parent
        config_path = script_dir / "hcpy" / "config" / "devices.json"

        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found at {config_path}")

        return config_path

    def _load_device_config(self, config_file: Path) -> Dict[str, Any]:
        """Load dishwasher configuration from file"""
        with open(config_file, "r") as f:
            devices = json.load(f)

        dishwasher = next(
            (
                device
                for device in devices
                if "dishwasher" in device.get("name", "").lower()
            ),
            None,
        )

        if not dishwasher:
            raise ValueError("No dishwasher found in config file")

        return dishwasher

    def _setup_signal_handlers(self) -> None:
        """Setup handlers for graceful shutdown"""

        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, initiating shutdown...")
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

    @property
    def state(self) -> DishwasherState:
        """Derive state from device status - no manual state management"""
        if not self.device or not self.ws:
            return DishwasherState.DISCONNECTED

        try:
            with self.device.state_lock:
                device_state = self.device.state

                # Get relevant device status fields
                power_state = device_state.get("BSH.Common.Setting.PowerState")
                active_program = device_state.get("BSH.Common.Root.ActiveProgram")
                operation_state = device_state.get("BSH.Common.Status.OperationState")
                start_in_relative = device_state.get(
                    "BSH.Common.Option.StartInRelative", 0
                )
                program_progress = device_state.get(
                    "BSH.Common.Option.ProgramProgress", 0
                )
                door_state = device_state.get("BSH.Common.Status.DoorState")

                # Determine state based on device status
                # Priority order: RUNNING > SCHEDULED > IDLE

                if operation_state in ["Run", "Pause", "ActionRequired"]:
                    # Program is actively running
                    current_state = DishwasherState.RUNNING
                    self._awaiting_post_run_reset = True

                elif active_program and operation_state == "DelayedStart":
                    # Program scheduled with delayed start
                    current_state = DishwasherState.SCHEDULED

                elif active_program and start_in_relative > 0:
                    # Program scheduled via StartInRelative
                    current_state = DishwasherState.SCHEDULED

                elif active_program and program_progress > 0:
                    # Program has started (progress > 0) but might be paused
                    current_state = DishwasherState.RUNNING
                    self._awaiting_post_run_reset = True

                elif self._awaiting_post_run_reset:
                    # Some devices leave the remote-start flag active after completion.
                    # Stay in RUNNING until user interaction (door open) or power-off occurs.
                    if power_state == "Off" or door_state == "Open":
                        current_state = DishwasherState.IDLE
                        self._awaiting_post_run_reset = False
                        self.logger.info(
                            "Post-run reset detected (door opened or power off). Returning to IDLE."
                        )
                    else:
                        current_state = DishwasherState.RUNNING

                elif (
                    operation_state in ["Ready", "Finished", "Inactive"]
                    and power_state == "On"
                ):
                    # Ready for new program
                    current_state = DishwasherState.IDLE

                elif power_state == "On":
                    # Power on, no active program
                    current_state = DishwasherState.IDLE

                else:
                    # Power off or unknown state - treat as IDLE
                    current_state = DishwasherState.IDLE

                # Log state transitions
                if self._last_logged_state != current_state:
                    if self._last_logged_state is not None:
                        self.logger.info(
                            f"State transition: {self._last_logged_state.name} -> {current_state.name} "
                            f"(ActiveProgram: {active_program}, OpState: {operation_state})"
                        )
                    self._last_logged_state = current_state

                return current_state

        except Exception as e:
            self.logger.error(f"Error deriving state: {e}", exc_info=True)
            return DishwasherState.ERROR

    def _connect(self) -> bool:
        """Establish connection to dishwasher"""
        try:
            self.ws = HCSocket(
                self.dishwasher_config["host"],
                self.dishwasher_config["key"],
                self.dishwasher_config.get("iv"),
            )

            self.device = HCDevice(
                self.ws, self.dishwasher_config, debug=self.config.DEBUG
            )
            self.logger.info("Connected to dishwasher")
            return True

        except Exception as e:
            self.logger.error(f"Connection failed: {e}", exc_info=True)
            return False

    def _get_next_finish_time(self) -> datetime:
        """Calculate next target finish time"""
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        if not self.finish_times:
            return datetime.combine(tomorrow, self.config.DEFAULT_FINISH_TIME)

        # Check remaining times today
        for finish_time in self.finish_times:
            target = datetime.combine(today, finish_time)
            if target > now:
                return target

        # Use first time tomorrow
        return datetime.combine(tomorrow, self.finish_times[0])

    def _get_program_duration(self) -> timedelta:
        """Get estimated program duration"""
        if not self.device:
            return timedelta(hours=3, minutes=20)  # Default fallback

        with self.device.state_lock:
            remaining = self.device.state.get("BSH.Common.Option.RemainingProgramTime")

        if remaining:
            return timedelta(seconds=remaining)

        return timedelta(hours=3, minutes=20)  # Default fallback

    def _get_optimal_start_time(self) -> Optional[datetime]:
        """Calculate optimal start time based on energy prices"""
        finish_time = self._get_next_finish_time()
        program_duration = self._get_program_duration()

        earliest_start = finish_time - program_duration
        earliest_start = earliest_start.astimezone(tz.tzlocal())

        now = datetime.now(tz.tzlocal())

        # If we're already past the earliest start, return None
        if earliest_start < now:
            return None

        try:
            # Request energy prices
            self.energy_client.request(
                datetime.combine(now.date(), time(now.hour, 0)),
                datetime.combine(
                    earliest_start.date(), time(earliest_start.hour + 1, 0)
                ),
            )

            # Find best price slot
            best_slot = self.energy_client.best_slot(1)

            if best_slot:
                optimal_start = best_slot.start_datetime
                # Don't start later than necessary
                optimal_start = min(optimal_start, earliest_start)
            else:
                optimal_start = earliest_start

            # Apply offset for energy-heavy load period
            optimal_start = optimal_start - timedelta(
                minutes=self.config.START_TIME_OFFSET
            )

            # Ensure we don't schedule in the past
            if optimal_start < now:
                optimal_start = now

            return optimal_start

        except Exception as e:
            self.logger.warning(
                f"Energy optimization failed, using earliest start: {e}"
            )
            return earliest_start if earliest_start > now else None

    def _can_start_program(self) -> bool:
        """Check if all conditions for starting are met"""
        if not self.device:
            return False

        with self.device.state_lock:
            state = self.device.state

            door_closed = state.get("BSH.Common.Status.DoorState") == "Closed"
            remote_allowed = state.get("BSH.Common.Status.RemoteControlStartAllowed")
            no_active_program = state.get("BSH.Common.Status.ActiveProgram") is None
            power_on = state.get("BSH.Common.Setting.PowerState") == "On"

            can_start = all([door_closed, remote_allowed, no_active_program, power_on])

            if not can_start:
                self.logger.debug(
                    f"Cannot start - Door: {door_closed}, Remote: {remote_allowed}, "
                    f"NoActive: {no_active_program}, Power: {power_on}"
                )

            return can_start

    def _is_program_finished(self) -> bool:
        """Check if program has finished or was aborted"""
        if not self.device:
            return False

        with self.device.state_lock:
            state = self.device.state

            power_off = state.get("BSH.Common.Setting.PowerState") == "Off"
            operation_state = state.get("BSH.Common.Status.OperationState")
            active_program = state.get("BSH.Common.Root.ActiveProgram")

            # Program is finished if:
            # - Power is off
            # - Operation state is Finished, Aborting, or Inactive
            # - No active program
            finished_states = ["Finished", "Aborting", "Inactive", "Ready"]
            is_finished = (
                power_off
                or operation_state in finished_states
                or active_program is None
            )

            if is_finished:
                self.logger.info(
                    f"Program finished - Power: {power_off}, OpState: {operation_state}, "
                    f"ActiveProgram: {active_program}"
                )

            return is_finished

    def _get_program_options(self) -> List[Dict[str, Any]]:
        """Get current program options"""
        if not self.device:
            return []

        with self.device.state_lock:
            state = self.device.state

            options = []

            if state.get("Dishcare.Dishwasher.Option.IntensivZone"):
                options.append(
                    {
                        "uid": 5126,
                        "value": state["Dishcare.Dishwasher.Option.IntensivZone"],
                    }
                )

            if state.get("Dishcare.Dishwasher.Option.BrillianceDry"):
                options.append(
                    {
                        "uid": 5128,
                        "value": state["Dishcare.Dishwasher.Option.BrillianceDry"],
                    }
                )

            if state.get("Dishcare.Dishwasher.Option.VarioSpeedPlus"):
                options.append(
                    {
                        "uid": 5127,
                        "value": state["Dishcare.Dishwasher.Option.VarioSpeedPlus"],
                    }
                )

            return options

    def _get_selected_or_default_program_id(self) -> int:
        """Determine currently selected program, fallback to configured default."""
        if not self.device:
            return self.config.DEFAULT_PROGRAM_ID

        with self.device.state_lock:
            selected = self.device.state.get("BSH.Common.Root.SelectedProgram")

        if selected:
            return selected

        return self.config.DEFAULT_PROGRAM_ID

    def _get_load_profile_path(self, program_id: int) -> Path:
        """Build profile path from program number and configured file naming."""
        filename = self.config.LOAD_PROFILE_FILE_TEMPLATE.format(program_id=program_id)
        return self.config.LOAD_PROFILE_DIR / filename

    def _load_profile_rows(self, program_id: int) -> List[Dict[str, Any]]:
        """Load CSV profile rows with time offset and energy demand."""
        profile_path = self._get_load_profile_path(program_id)

        if not profile_path.exists():
            self.logger.warning(
                f"No load profile found for program {program_id}: {profile_path}"
            )
            return []

        rows: List[Dict[str, Any]] = []
        with open(profile_path, "r", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file, delimiter=";")
            for row in reader:
                time_raw = (row.get("Time") or "").strip()
                energy_raw = (row.get("Energy_wh") or "").strip()
                if not time_raw or not energy_raw:
                    continue

                try:
                    hours_str, minutes_str = time_raw.split(":")
                    offset = timedelta(hours=int(hours_str), minutes=int(minutes_str))
                    energy_wh = float(energy_raw.replace(",", "."))
                except Exception:
                    self.logger.warning(
                        f"Skipping invalid load profile row in {profile_path}: {row}"
                    )
                    continue

                rows.append({"offset": offset, "energy_wh": energy_wh})

        return rows

    def _ensure_mqtt_connection(self) -> bool:
        """Initialize and connect MQTT client when enabled."""
        if not self.config.MQTT_ENABLED:
            return False

        with self._mqtt_lock:
            if self._mqtt_client is not None:
                return True

            try:
                client = mqtt.Client(client_id=self.config.MQTT_CLIENT_ID)
                if self.config.MQTT_USERNAME:
                    client.username_pw_set(
                        self.config.MQTT_USERNAME, self.config.MQTT_PASSWORD
                    )

                client.connect(
                    self.config.MQTT_HOST, self.config.MQTT_PORT, keepalive=60
                )
                client.loop_start()
                self._mqtt_client = client
                self.logger.info(
                    f"Connected MQTT client to {self.config.MQTT_HOST}:{self.config.MQTT_PORT}"
                )
                return True
            except Exception as e:
                self.logger.error(f"Failed to connect MQTT client: {e}", exc_info=True)
                self._mqtt_client = None
                return False

    def _clear_retained_topic(self, topic: str) -> None:
        """Delete a retained MQTT message by publishing empty payload with retain flag."""
        if not self._mqtt_client:
            return

        info = self._mqtt_client.publish(
            topic,
            payload="",
            qos=self.config.MQTT_QOS,
            retain=self.config.MQTT_RETAIN,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.logger.warning(f"Failed to clear retained topic {topic}, rc={info.rc}")

    def _get_forecast_topic(self) -> str:
        """Return single retained topic for this appliance forecast."""
        return f"{self.config.MQTT_TOPIC_PREFIX}/{self.config.MQTT_CLIENT_ID}"

    def _publish_forecast_payload(self, forecast_rows: List[Dict[str, Any]]) -> None:
        """Publish retained forecast payload to the single appliance topic."""
        if not self._ensure_mqtt_connection():
            return

        topic = self._get_forecast_topic()
        if not forecast_rows:
            self._clear_retained_topic(topic)
            self._current_forecast = []
            self.logger.info(f"Cleared retained forecast topic {topic}")
            return

        payload = json.dumps(
            [
                {
                    "time": row["time"].isoformat(),
                    "load_wh": row["load_wh"],
                }
                for row in forecast_rows
            ]
        )

        info = self._mqtt_client.publish(  # ty:ignore[unresolved-attribute]
            topic,
            payload=payload,
            qos=self.config.MQTT_QOS,
            retain=self.config.MQTT_RETAIN,
        )
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            self._current_forecast = forecast_rows
            self.logger.info(
                f"Published {len(forecast_rows)} forecast slots to {topic}"
            )
        else:
            self.logger.warning(
                f"Failed to publish load forecast to {topic}, rc={info.rc}"
            )

    def _cleanup_expired_profile_topics(self) -> None:
        """Keep retained forecast payload limited to future slots only."""
        if not self._current_forecast:
            return

        now = datetime.now(tz.tzlocal())
        remaining_forecast = [
            row for row in self._current_forecast if row["time"] > now
        ]

        if len(remaining_forecast) != len(self._current_forecast):
            self._publish_forecast_payload(remaining_forecast)

    def _publish_load_profile_forecast(
        self, program_id: int, start_time: datetime
    ) -> None:
        """Publish timezone-aware load forecast as one retained MQTT payload."""
        rows = self._load_profile_rows(program_id)
        if not rows:
            self._publish_forecast_payload([])
            return

        now = datetime.now(tz.tzlocal())
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=tz.tzlocal())
        else:
            start_time = start_time.astimezone(tz.tzlocal())

        forecast_rows: List[Dict[str, Any]] = []

        for row in rows:
            slot_time = start_time + row["offset"]
            if slot_time <= now:
                continue

            forecast_rows.append(
                {
                    "time": slot_time,
                    "load_wh": row["energy_wh"],
                }
            )

        self._publish_forecast_payload(forecast_rows)

    def _derive_scheduled_start(self) -> Optional[datetime]:
        """Compute the scheduled start time from device-reported StartInRelative."""
        if not self.device:
            return None
        with self.device.state_lock:
            start_in_relative = self.device.state.get(
                "BSH.Common.Option.StartInRelative", 0
            )
        if start_in_relative and int(start_in_relative) > 0:
            return datetime.now(tz.tzlocal()) + timedelta(
                seconds=int(start_in_relative)
            )
        return None

    def _update_forecast_from_device_state(self) -> None:
        """State-driven forecast: derive and publish purely from the actual device state.

        Called once per evaluation cycle.  Handles all cases:
        - Program running (manual or automatic start)
        - Program scheduled (manual or automatic, time-shift detection)
        - Program finished, cancelled, or controller idle -> clear forecast
        """
        current_state = self.state
        now = datetime.now(tz.tzlocal())

        if current_state == DishwasherState.RUNNING:
            # Determine active program id
            program_id: Optional[int] = None
            if self.device:
                with self.device.state_lock:
                    program_id = self.device.state.get("BSH.Common.Root.ActiveProgram")
            if program_id is None:
                program_id = self._get_selected_or_default_program_id()

            state_changed = (
                self._last_forecast_dishwasher_state != DishwasherState.RUNNING
            )
            program_changed = self._last_forecast_program_id != program_id

            if state_changed or program_changed or not self._current_forecast:
                reason = (
                    "state change"
                    if state_changed
                    else "program change"
                    if program_changed
                    else "no active forecast"
                )
                self.logger.info(
                    f"Publishing RUNNING forecast for program {program_id} ({reason})"
                )
                self._publish_load_profile_forecast(program_id, now)
                self._last_forecast_dishwasher_state = DishwasherState.RUNNING
                self._last_forecast_program_id = program_id
                self._last_forecast_scheduled_start = None

        elif current_state == DishwasherState.SCHEDULED:
            program_id = self._get_selected_or_default_program_id()
            scheduled_start = self._derive_scheduled_start()

            if scheduled_start is None:
                # StartInRelative not yet reported by device - skip this cycle
                return

            state_changed = (
                self._last_forecast_dishwasher_state != DishwasherState.SCHEDULED
            )
            program_changed = self._last_forecast_program_id != program_id
            start_shifted = (
                self._last_forecast_scheduled_start is not None
                and abs(
                    (
                        scheduled_start - self._last_forecast_scheduled_start
                    ).total_seconds()
                )
                > self._RESCHEDULE_THRESHOLD_S
            )

            if (
                state_changed
                or program_changed
                or start_shifted
                or not self._current_forecast
            ):
                reason = (
                    "state change"
                    if state_changed
                    else "program change"
                    if program_changed
                    else f"start shifted >={self._RESCHEDULE_THRESHOLD_S}s"
                    if start_shifted
                    else "no active forecast"
                )
                self.logger.info(
                    f"Publishing SCHEDULED forecast for program {program_id} "
                    f"starting at {scheduled_start.isoformat()} ({reason})"
                )
                self._publish_load_profile_forecast(program_id, scheduled_start)
                self._last_forecast_dishwasher_state = DishwasherState.SCHEDULED
                self._last_forecast_program_id = program_id
                self._last_forecast_scheduled_start = scheduled_start

        else:
            # IDLE, DISCONNECTED, ERROR → clear forecast
            was_active = self._last_forecast_dishwasher_state in (
                DishwasherState.RUNNING,
                DishwasherState.SCHEDULED,
            )
            if was_active or self._current_forecast:
                self.logger.info(
                    f"Clearing forecast "
                    f"(current={current_state.name}, "
                    f"previous={getattr(self._last_forecast_dishwasher_state, 'name', None)})"
                )
                self._publish_forecast_payload([])
            self._last_forecast_dishwasher_state = current_state
            self._last_forecast_program_id = None
            self._last_forecast_scheduled_start = None

    def _select_program(self, program_id: Optional[int] = None) -> bool:
        """Select a program without starting it"""
        if not self.device:
            return False

        if program_id is None:
            program_id = self.config.DEFAULT_PROGRAM_ID

        program_data = {"program": program_id, "options": self._get_program_options()}

        try:
            with self.device.state_lock:
                self.device.get("/ro/selectedProgram", action="POST", data=program_data)

            self.logger.info(f"Selected program {program_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to select program: {e}", exc_info=True)
            return False

    def _start_program(self, start_in_seconds: Optional[int] = None) -> bool:
        """Start the dishwasher program"""
        if not self.device:
            return False

        with self.device.state_lock:
            program_id = self.device.state.get("BSH.Common.Root.SelectedProgram")

        if not program_id:
            program_id = self.config.DEFAULT_PROGRAM_ID

        program_data = {"program": program_id, "options": self._get_program_options()}

        # Add delayed start if specified
        if start_in_seconds is not None and start_in_seconds > 0:
            # Cap at 24 hours
            start_in_seconds = min(start_in_seconds, 24 * 60 * 60)
            program_data["options"].append({"uid": 558, "value": start_in_seconds})

        try:
            with self.device.state_lock:
                self.device.get("/ro/activeProgram", action="POST", data=program_data)

            self.logger.info(
                f"Started program {program_id}"
                + (f" with {start_in_seconds}s delay" if start_in_seconds else "")
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to start program: {e}", exc_info=True)
            return False

    def _autoselect_default_program(self) -> None:
        """Auto-select default program after configured hour"""
        if self.config.DEFAULT_AUTOSELECT_HOUR is None:
            return

        now = datetime.now(tz.tzlocal())
        if now.hour >= self.config.DEFAULT_AUTOSELECT_HOUR:
            if self._can_start_program():
                self._select_program(self.config.DEFAULT_PROGRAM_ID)

    def _handle_idle_state(self) -> None:
        """Handle actions in IDLE state"""
        if not self._can_start_program():
            return

        # Auto-select program if configured
        self._autoselect_default_program()

        # Check if we should start
        optimal_start = self._get_optimal_start_time()

        if optimal_start is None:
            self.logger.error("No valid start time available")
            return

        now = datetime.now(tz.tzlocal())
        delay_seconds = int((optimal_start - now).total_seconds())

        if delay_seconds <= 0:
            self.logger.info("Starting program immediately")
            self._start_program()
            # _update_forecast_from_device_state will publish once state becomes RUNNING
        else:
            self.logger.info(f"Scheduling program to start at {optimal_start}")
            self._start_program(start_in_seconds=delay_seconds)
            # _update_forecast_from_device_state will publish once state becomes SCHEDULED

    def _handle_scheduled_state(self) -> None:
        """Handle actions in SCHEDULED state"""
        if not self.device:
            return

        with self.device.state_lock:
            operation_state = self.device.state.get("BSH.Common.Status.OperationState")
            active_program = self.device.state.get("BSH.Common.Root.ActiveProgram")

        self.logger.debug(
            f"Scheduled - OperationState: {operation_state}, ActiveProgram: {active_program}"
        )
        # Keep retained payload trimmed to future slots while waiting for start
        self._cleanup_expired_profile_topics()

    def _handle_running_state(self) -> None:
        """Handle actions in RUNNING state"""
        if self._is_program_finished():
            self.logger.debug(
                "Program finished - will transition to IDLE automatically"
            )

        # Keep retained payload trimmed to future slots as program progresses
        self._cleanup_expired_profile_topics()

    def _evaluate_state(self) -> None:
        """Main state evaluation logic"""
        # State-driven forecast: publish, update, or clear based on actual device state.
        # This handles automatic starts, manual starts, manual cancellations,
        # and schedule changes transparently.
        self._update_forecast_from_device_state()

        current_state = self.state

        if current_state == DishwasherState.IDLE:
            self._handle_idle_state()
        elif current_state == DishwasherState.SCHEDULED:
            self._handle_scheduled_state()
        elif current_state == DishwasherState.RUNNING:
            self._handle_running_state()

    def _on_message(self, values: Dict[str, Any]) -> None:
        """Handle incoming websocket messages"""
        if not values:
            return

        # Ignore error messages
        if values.get("error") and values.get("resource"):
            return

        self.logger.debug(f"Received: {values}")

        if self.device:
            try:
                with self.device.state_lock:
                    self.device.state.update(values)

                # Evaluate state after update
                self._evaluate_state()

            except Exception as e:
                self.logger.error(f"Error processing message: {e}", exc_info=True)

    def _on_open(self, ws: HCSocket) -> None:
        """Handle connection opened"""
        self.logger.info("Websocket connection opened")
        # State will be automatically derived from device state

    def _on_close(self, ws: HCSocket, code: int, message: str) -> None:
        """Handle connection closed"""
        self.logger.warning(f"Websocket closed: {message} (code: {code})")
        # State will automatically become DISCONNECTED since device/ws become None

    def run(self) -> None:
        """Main run loop"""
        self.logger.info("Starting dishwasher controller")

        while not self._shutdown_event.is_set():
            try:
                # Ensure connection
                if not self.device or self.state == DishwasherState.DISCONNECTED:
                    if not self._connect():
                        self.logger.error(
                            f"Connection failed, retrying in {self.config.RECONNECT_DELAY}s"
                        )
                        self._shutdown_event.wait(self.config.RECONNECT_DELAY)
                        continue

                # Run websocket loop
                if self.device:
                    self.device.run_forever(
                        on_message=self._on_message,
                        on_open=self._on_open,
                        on_close=self._on_close,
                    )

                # If we exit run_forever, we likely disconnected
                if not self._shutdown_event.is_set():
                    self.logger.warning(
                        f"Disconnected, reconnecting in {self.config.RECONNECT_DELAY}s"
                    )
                    self._shutdown_event.wait(self.config.RECONNECT_DELAY)

            except KeyboardInterrupt:
                self.logger.info("Keyboard interrupt received")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}", exc_info=True)
                self._shutdown_event.wait(self.config.RETRY_DELAY)

        self.logger.info("Shutting down dishwasher controller")

    def shutdown(self) -> None:
        """Graceful shutdown"""
        self.logger.info("Initiating graceful shutdown...")
        self._shutdown_event.set()

        # Cleanup retained topics before disconnecting if possible.
        self._cleanup_expired_profile_topics()

        # Close websocket connection to unblock run_forever()
        if self.device and hasattr(self.device, "ws") and self.device.ws:
            try:
                # Stop the WebSocketApp run_forever loop
                if hasattr(self.device.ws, "ws") and self.device.ws.ws:
                    self.device.ws.ws.keep_running = False
                    self.device.ws.ws.close()
                    self.logger.debug("Websocket closed")
            except Exception as e:
                self.logger.debug(f"Error closing websocket: {e}")

        if self._mqtt_client is not None:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception as e:
                self.logger.debug(f"Error closing MQTT client: {e}")
            finally:
                self._mqtt_client = None

        # Give a moment for cleanup
        sleep(0.5)


def main():
    """Main entry point"""
    controller = DishwasherController(finish_times=Config.FINISH_TIMES, country="DE")

    try:
        controller.run()
    except Exception as e:
        controller.logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        controller.shutdown()


if __name__ == "__main__":
    main()
