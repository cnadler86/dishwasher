#!/bin/bash
#
# Dishwasher Controller - Automatic Installation Script
# Usage: sudo ./install.sh
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Service name
SERVICE_NAME="dishwasher-controller"
SERVICE_FILE="${SERVICE_NAME}.service"

echo "======================================================================="
echo "  Dishwasher Controller - Installation Script"
echo "======================================================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Error: This script must be run with sudo${NC}"
    echo "Usage: sudo ./install.sh"
    exit 1
fi

# Get the actual user (not root)
if [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
else
    echo -e "${RED}Error: Could not determine the actual user${NC}"
    echo "Please run with sudo, not as root directly"
    exit 1
fi

# Get user's primary group
USER_GROUP=$(id -gn "$REAL_USER")

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Find Python binary (prefer venv)
PYTHON_BIN=""

# Check for venv in common locations
if [ -f "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"
    echo -e "${GREEN}✓${NC} Found Python in venv: $PYTHON_BIN"
elif [ -f "$SCRIPT_DIR/../venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/../venv/bin/python3"
    echo -e "${GREEN}✓${NC} Found Python in parent venv: $PYTHON_BIN"
elif [ -f "$HOME/venv/bin/python3" ]; then
    PYTHON_BIN="$HOME/venv/bin/python3"
    echo -e "${GREEN}✓${NC} Found Python in home venv: $PYTHON_BIN"
else
    # Use system Python
    PYTHON_BIN=$(which python3)
    echo -e "${YELLOW}⚠${NC} Using system Python: $PYTHON_BIN"
    echo -e "${YELLOW}⚠${NC} Consider using a virtual environment (venv)"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}Error: Python3 not found${NC}"
    exit 1
fi

# Verify Python installation
if ! $PYTHON_BIN --version &> /dev/null; then
    echo -e "${RED}Error: Python binary is not executable: $PYTHON_BIN${NC}"
    exit 1
fi

PYTHON_VERSION=$($PYTHON_BIN --version)
echo -e "${GREEN}✓${NC} Python version: $PYTHON_VERSION"

# Create logs directory if it doesn't exist
LOGS_DIR="$SCRIPT_DIR/logs"
if [ ! -d "$LOGS_DIR" ]; then
    mkdir -p "$LOGS_DIR"
    chown "$REAL_USER:$USER_GROUP" "$LOGS_DIR"
    echo -e "${GREEN}✓${NC} Created logs directory: $LOGS_DIR"
else
    echo -e "${GREEN}✓${NC} Logs directory exists: $LOGS_DIR"
fi

# Check if service file template exists
if [ ! -f "$SCRIPT_DIR/$SERVICE_FILE" ]; then
    echo -e "${RED}Error: Service file template not found: $SCRIPT_DIR/$SERVICE_FILE${NC}"
    exit 1
fi

echo ""
echo "======================================================================="
echo "  Configuration Summary"
echo "======================================================================="
echo "User:           $REAL_USER"
echo "Group:          $USER_GROUP"
echo "Working Dir:    $SCRIPT_DIR"
echo "Python Binary:  $PYTHON_BIN"
echo "Logs Dir:       $LOGS_DIR"
echo "Service Name:   $SERVICE_NAME"
echo ""

# Ask for confirmation
read -p "Continue with installation? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled"
    exit 0
fi

echo ""
echo "======================================================================="
echo "  Installing Service"
echo "======================================================================="

# Create temporary service file with substitutions
TMP_SERVICE="/tmp/${SERVICE_FILE}.tmp"
sed -e "s|{{USER}}|$REAL_USER|g" \
    -e "s|{{GROUP}}|$USER_GROUP|g" \
    -e "s|{{WORKING_DIR}}|$SCRIPT_DIR|g" \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    "$SCRIPT_DIR/$SERVICE_FILE" > "$TMP_SERVICE"

echo -e "${GREEN}✓${NC} Generated service file from template"

# Check if service is already installed and running
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo -e "${YELLOW}⚠${NC} Service is currently running, stopping it..."
    systemctl stop "$SERVICE_NAME"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo -e "${YELLOW}⚠${NC} Service is enabled, disabling it temporarily..."
    systemctl disable "$SERVICE_NAME"
fi

# Install service file
cp "$TMP_SERVICE" "/etc/systemd/system/$SERVICE_FILE"
rm "$TMP_SERVICE"
echo -e "${GREEN}✓${NC} Installed service file to /etc/systemd/system/$SERVICE_FILE"

# Reload systemd
systemctl daemon-reload
echo -e "${GREEN}✓${NC} Reloaded systemd daemon"

# Enable service
systemctl enable "$SERVICE_NAME"
echo -e "${GREEN}✓${NC} Enabled $SERVICE_NAME service"

# Start service
systemctl start "$SERVICE_NAME"
echo -e "${GREEN}✓${NC} Started $SERVICE_NAME service"

# Wait a moment for service to start
sleep 2

# Check service status
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "======================================================================="
    echo -e "  ${GREEN}Installation Successful!${NC}"
    echo "======================================================================="
    echo ""
    echo "Service is running. Useful commands:"
    echo ""
    echo "  View status:    sudo systemctl status $SERVICE_NAME"
    echo "  View logs:      sudo journalctl -u $SERVICE_NAME -f"
    echo "  Stop service:   sudo systemctl stop $SERVICE_NAME"
    echo "  Start service:  sudo systemctl start $SERVICE_NAME"
    echo "  Restart:        sudo systemctl restart $SERVICE_NAME"
    echo "  Disable:        sudo systemctl disable $SERVICE_NAME"
    echo ""
    echo "Logs are also stored in: $LOGS_DIR"
    echo ""
    
    # Show last few log lines
    echo "Recent log output:"
    echo "-------------------------------------------------------------------"
    journalctl -u "$SERVICE_NAME" -n 10 --no-pager
    echo "-------------------------------------------------------------------"
    echo ""
else
    echo ""
    echo "======================================================================="
    echo -e "  ${RED}Installation completed but service failed to start${NC}"
    echo "======================================================================="
    echo ""
    echo "Check the logs for errors:"
    echo "  sudo journalctl -u $SERVICE_NAME -n 50"
    echo ""
    echo "Common issues:"
    echo "  - Missing dependencies (run: pip install -r requirements.txt)"
    echo "  - Invalid configuration in hcpy/config/devices.json"
    echo "  - Python path issues"
    echo ""
    exit 1
fi
