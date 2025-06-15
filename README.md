git clone https://github.com/cnadler86/dishwasher.git
cd dishwasher
git submodule update --init --recursive

# Create venv and install requirements in here and in hcpy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r hcpy/requirements.txt

# Install systemd service
chmod +x setup_service.py
chmod +x App.py
python setup_service.py
sudo cp dishwasher.service /etc/systemd/system/

sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable dishwasher.service
sudo systemctl start dishwasher.service
