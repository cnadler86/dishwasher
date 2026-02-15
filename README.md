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

## Install systemd service

```bash
sudo ./install.sh
```

## 3. Logs ansehen

```bash
sudo journalctl -u dishwasher-controller -f
```
