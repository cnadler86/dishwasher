# Dishwasher Control System

Automate best start of your BSH dishwasher based on the current energy prices.
This allows you to run your dishwasher when the energy prices are low, saving you money and reducing your carbon footprint. To do so, just follow the instruction in here. Then configure your dishwasher in the Home Connect app to manual remote start mode. In order to start the automation, you need to press the remote start button in the dishwasher. The automation does the rest.

## Installation

## Clone the repository

```bash
git clone --recurse-submodules https://github.com/cnadler86/dishwasher.git
```

## Create venv and install requirements in here and in hcpy

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r hcpy/requirements.txt
```

## Get home connect config file

Follow the instruction in [hcpy](https://github.com/hcpy2-0/hcpy?tab=readme-ov-file#authenticate-to-the-cloud-servers) to get the home connect config file

## Change default values in the main file

Adapt the default values in `App.py` in the `Config` class to your needs. The most important ones are:

- `DEFAULT_PROGRAM_ID`: The program ID of the program you want to run by default.
- `DEFAULT_AUTOSELECT_HOUR`: The hour after which the default program will be selected automatically
- `FINISH_TIMES`: List of finish times to consider, if empty, the default time will be used. This is useful if you want to run the dishwasher at a specific time, e.g. at night or in the morning.
- `DEFAULT_FINISH_TIME`: Normally, this is not needed and does not need to be changed, if you use the `finish_times` parameter
- `RETRY_DELAY`: Delay in seconds before retrying connection if it fails

Additional options for load profile publishing:

- `LOAD_PROFILE_DIR`: Directory containing load profile CSV files.
- `LOAD_PROFILE_FILE_TEMPLATE`: File naming pattern. Keep this as `{program_id}.csv` so each file name matches the dishwasher program number.
- `MQTT_ENABLED`: Enable/disable MQTT publishing of load profiles.
- `MQTT_HOST` / `MQTT_PORT`: MQTT broker endpoint (default `localhost:1883`).
- `MQTT_USERNAME` / `MQTT_PASSWORD`: Optional MQTT authentication.
- `MQTT_TOPIC_PREFIX`: Topic root for forecast messages, e.g. `gridpythia/appliance_load/forecast`.
- `MQTT_CLIENT_ID`: Appliance identifier used as topic suffix.
- `MQTT_QOS`: MQTT QoS for load profile messages.
- `MQTT_RETAIN`: Publish retained messages so consumers can read the forecast immediately.

## Load profile CSV format

Load profiles must be CSV with semicolon delimiter and program-number based file names (for example `8196.csv`):

```csv
Time;Energy_wh
00:00;11,170643
00:15;368,298544
...
```

- `Time`: Offset from planned start in `HH:MM`.
- `Energy_wh`: Expected energy demand for that slot (comma decimals are supported).

The controller publishes one retained, timezone-aware forecast payload to:

`<MQTT_TOPIC_PREFIX>/<MQTT_CLIENT_ID>`

Payload is a JSON list with only time/load information (no program id), for example:

```json
[
  {"time": "2026-04-27T22:15:00+02:00", "load_wh": 11.170643},
  {"time": "2026-04-27T22:30:00+02:00", "load_wh": 368.298544}
]
```

Old slots are automatically removed from the retained payload once their timestamp is no longer in the future.

## Install systemd service

```bash
sudo ./install.sh
```

## 3. Logs ansehen

```bash
sudo journalctl -u dishwasher-controller -f
```
